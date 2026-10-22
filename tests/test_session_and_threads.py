"""Stopping must never block the caller (the GUI thread), must finish the teardown, must deliver queued releases, and must
turn a stalled phone into a clear error. Uses fakes for adb and sockets; no phone is involved."""
import socket
import threading
import time

import pytest
from PySide6.QtCore import QCoreApplication

from app.device.foreground import ForegroundWatcher
from app.device.tracker import DeviceTracker
from app.stream import protocol, worker as worker_module
from app.stream.control_channel import ControlChannel
from app.stream.session import ScrcpyError, ScrcpySession, StreamConfig
from app.stream.worker import MirrorWorker
from test_control_channel import Stub, decode, msg

D, U = protocol.TOUCH_DOWN, protocol.TOUCH_UP


@pytest.fixture(scope="module", autouse=True)
def qt_core():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


class FakeAdb:
    def __init__(self, remove_delay=0.0):
        self.remove_delay = remove_delay
        self.removed = []
        self.pushes = 0

    def forward_remove(self, serial, port):
        time.sleep(self.remove_delay)
        self.removed.append(port)

    def run(self, *args, **kwargs):
        time.sleep(kwargs.get("block", 0))
        raise AssertionError("unexpected adb call")


def session_with_channel(adb):
    """A session as it is while playing: a control channel on a real socket pair, a forwarded port, no server process."""
    session = ScrcpySession(adb, "SERIAL", StreamConfig())
    pc, peer = socket.socketpair()
    video_a, video_b = socket.socketpair()
    session._control_sock = pc
    session.video_sock = video_a
    session._port = 5555
    session.control = ControlChannel(pc)
    return session, peer, video_b


def read_all(peer):
    peer.settimeout(0.5)
    data = b""
    try:
        while True:
            chunk = peer.recv(65536)
            if not chunk:
                break
            data += chunk
    except (TimeoutError, OSError):
        pass
    return decode(data)


# ===================================================================== session.stop
def test_a_graceful_stop_delivers_queued_releases_and_lifts_fingers_that_are_still_down():
    adb = FakeAdb()
    session, peer, _video = session_with_channel(adb)
    session.control.send(msg(D, 0, 10, 10))
    session.control.send(msg(D, 1, 20, 20))
    session.control.send(msg(U, 1, 20, 20, pressure=0.0))
    session.stop(grace=1.0)
    events = read_all(peer)
    assert [(e[1], e[2]) for e in events] == [(D, 0), (D, 1), (U, 1), (U, 0)]       # finger 0 was still down: lifted
    assert adb.removed == [5555] and session._port == 0 and session.video_sock is None and session._control_sock is None


def test_stop_is_idempotent_and_a_second_caller_waits_for_the_first_to_finish():
    adb = FakeAdb(remove_delay=0.4)
    session, _peer, _video = session_with_channel(adb)
    finished = {}

    def stopper(name):
        session.stop()
        finished[name] = time.perf_counter()

    started = time.perf_counter()
    threads = [threading.Thread(target=stopper, args=(n,)) for n in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert adb.removed == [5555]                                                       # torn down exactly once
    assert all(finished[n] - started >= 0.35 for n in "ab")                            # both return only when it is finished


def test_stop_before_a_connection_exists_is_harmless():
    session = ScrcpySession(FakeAdb(), "SERIAL", StreamConfig())
    session.stop()
    session.stop(grace=0.2)
    assert session._stopped


def test_a_stop_during_start_makes_start_give_up_and_clean_up(monkeypatch):
    release = threading.Event()
    calls = []

    class SlowAdb(FakeAdb):
        def push(self, serial, local, remote):
            calls.append("push")
            release.wait(5)

        def forward(self, *a, **k):
            calls.append("forward")
            return 7777

        def popen_shell(self, *a, **k):
            calls.append("shell")
            raise AssertionError("must not start the server after a stop")

    adb = SlowAdb()
    session = ScrcpySession(adb, "SERIAL", StreamConfig())
    errors = []
    thread = threading.Thread(target=lambda: _run_start(session, errors))
    thread.start()
    time.sleep(0.2)
    session.stop()                                                                      # while push() is still running
    release.set()
    thread.join(5)
    assert len(errors) == 1 and isinstance(errors[0], ScrcpyError) and "Stopped" in str(errors[0])
    assert "shell" not in calls and session._port == 0


def _run_start(session, errors):
    try:
        session.start()
    except Exception as exc:                                                            # noqa: BLE001
        errors.append(exc)


# ===================================================================== a stalled phone ends the session visibly
def test_a_stalled_control_channel_stops_the_session_with_a_clear_reason():
    adb = FakeAdb()
    session = ScrcpySession(adb, "SERIAL", StreamConfig())
    stub = Stub()
    session._port = 4242
    session.control = ControlChannel(stub, stall_seconds=0.3, on_stalled=session._control_stalled)
    stub.stall(session.control)
    deadline = time.time() + 5
    while not session._stopped and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    assert session._stopped and "did not accept" in session.failure_reason
    assert adb.removed == [4242]
    stub.closed.set()
    stub.gate.set()


class FakeFailingSession:
    failure_reason = "The phone did not accept control messages for 3 seconds"

    def __init__(self, *a, **k):
        self.control = None
        self.stopped = 0

    def start(self):
        raise ScrcpyError("connection closed by the device")

    def stop(self, grace=0.0):
        self.stopped += 1


def test_the_worker_reports_the_real_reason_not_just_a_closed_connection(monkeypatch):
    monkeypatch.setattr(worker_module, "ScrcpySession", FakeFailingSession)
    worker = MirrorWorker(FakeAdb(), "SERIAL", StreamConfig())
    messages = []
    worker.failed.connect(messages.append)
    worker.run()
    assert messages == [FakeFailingSession.failure_reason]


# ===================================================================== stopping the worker never blocks the caller
def test_worker_stop_returns_at_once_and_the_teardown_finishes_in_the_background():
    adb = FakeAdb(remove_delay=1.0)                                                     # a slow `adb forward --remove`
    session, peer, _video = session_with_channel(adb)
    worker = MirrorWorker(adb, "SERIAL", StreamConfig())
    worker._session = session
    session.control.send(msg(D, 0, 5, 5))
    started = time.perf_counter()
    worker.stop()
    assert time.perf_counter() - started < 0.15                                         # the GUI thread is not held up
    deadline = time.time() + 5
    while session._port and time.time() < deadline:
        time.sleep(0.05)
    assert adb.removed == [5555] and session._port == 0
    assert (U, 0) in [(e[1], e[2]) for e in read_all(peer)]                             # the release still got out first


# ===================================================================== background threads
class BlockingAdb:
    """run() blocks like an adb call to a phone that stopped answering."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.inside = threading.Event()

    def run(self, *args, **kwargs):
        self.inside.set()
        time.sleep(self.seconds)

        class Result:
            stdout = ""
        return Result()


def test_foreground_watcher_request_stop_returns_at_once_even_inside_an_adb_call():
    adb = BlockingAdb(1.0)
    watcher = ForegroundWatcher(adb, "SERIAL", interval=0.05)
    watcher.start()
    assert adb.inside.wait(2)
    started = time.perf_counter()
    watcher.request_stop()
    assert time.perf_counter() - started < 0.05
    assert watcher.wait(4000)                                                           # and it does end once the call returns


def test_device_tracker_request_stop_unblocks_a_read_and_returns_at_once(monkeypatch):
    server, client = socket.socketpair()

    def serve():                                                                        # answers the handshake, then says nothing
        server.recv(64)
        server.sendall(b"OKAY")
    threading.Thread(target=serve, daemon=True).start()

    class QuietAdb:
        def start_server(self):
            pass

    monkeypatch.setattr("app.device.tracker.socket.create_connection", lambda *a, **k: client)
    tracker = DeviceTracker(QuietAdb())
    tracker.start()
    time.sleep(0.4)                                                                     # now blocked reading the device list
    started = time.perf_counter()
    tracker.request_stop()
    assert time.perf_counter() - started < 0.05
    assert tracker.wait(3000)
    server.close()
