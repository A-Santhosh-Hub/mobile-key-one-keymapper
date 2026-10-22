"""The control channel under a stalled phone: bounded, ordered, MOVEs coalesced, UPs never lost, failures visible.

Two kinds of peer:
- `Stub` is a socket whose sendall() blocks exactly when the test says so (a phone that stopped reading). Windows loopback
  sockets swallow megabytes without ever blocking, so a real socket cannot make a stall deterministic.
- `Phone` is a real socket pair, used for ordinary delivery, graceful close and disconnects."""
import socket
import struct
import threading
import time

import pytest

from app.stream import protocol
from app.stream.control_channel import ControlChannel, release_message, touch_header

D, U, M = protocol.TOUCH_DOWN, protocol.TOUCH_UP, protocol.TOUCH_MOVE
W, H = 1920, 864


def msg(action, pid, x=100, y=200, pressure=1.0):
    return protocol.encode_touch(action, pid, x, y, W, H, pressure=pressure)


def decode(data):
    events, i = [], 0
    while i < len(data):
        kind = data[i]
        if kind == protocol.TYPE_INJECT_TOUCH_EVENT:
            _, action, pid, x, y, _w, _h, pressure, _ab, _b = struct.unpack_from(">BBqiiHHHII", data, i)
            events.append(("touch", action, pid, x, y, pressure))
            i += 32
        elif kind == protocol.TYPE_INJECT_KEYCODE:
            _, action, code, _r, _m = struct.unpack_from(">BBiii", data, i)
            events.append(("key", action, code))
            i += 14
        else:
            raise AssertionError(f"unexpected byte 0x{kind:02x} at {i}")
    return events


class Stub:
    """A socket that can be told to stop accepting data."""

    def __init__(self):
        self.sent = bytearray()
        self.gate = threading.Event()
        self.gate.set()                              # open: sendall succeeds at once
        self.inside = threading.Event()              # set while sendall is blocked waiting for the gate
        self.closed = threading.Event()
        self._timeout = None
        self.stalled_calls = []

    def settimeout(self, value):
        self._timeout = value

    def gettimeout(self):
        return self._timeout

    def sendall(self, data):
        if not self.gate.is_set():
            self.inside.set()
        if not self.gate.wait(self._timeout):
            raise TimeoutError("timed out")
        if self.closed.is_set():
            raise OSError("closed")
        self.sent += data

    def recv(self, count):
        if self.closed.wait(self._timeout):
            return b""
        raise TimeoutError("timed out")

    # -- test controls
    def stall(self, channel):
        """Make the writer sit inside sendall() with one message in its hands."""
        self.gate.clear()
        self.inside.clear()
        assert channel.send(msg(protocol.TOUCH_MOVE, 9, 1, 1))           # a decoy message for the writer to get stuck on
        assert self.inside.wait(2), "the writer never reached the socket"

    def resume(self, channel, settle=2.0):
        self.gate.set()
        end = time.time() + settle
        while time.time() < end and channel.pending_count():
            time.sleep(0.01)
        time.sleep(0.15)

    def events(self):
        return decode(bytes(self.sent))


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.closed.set()
    s.gate.set()


def make_channel(stub, **kw):
    return ControlChannel(stub, on_stalled=stub.stalled_calls.append, **kw)


class Phone:
    def __init__(self):
        self.pc, self.peer = socket.socketpair()
        self.stalled_calls = []

    def channel(self, **kw):
        return ControlChannel(self.pc, on_stalled=self.stalled_calls.append, **kw)

    def read_events(self, idle=0.4):
        self.peer.settimeout(idle)
        data = b""
        try:
            while True:
                chunk = self.peer.recv(65536)
                if not chunk:
                    break
                data += chunk
        except (TimeoutError, OSError):
            pass
        return decode(data)

    def close(self):
        for sock in (self.pc, self.peer):
            try:
                sock.close()
            except OSError:
                pass


@pytest.fixture
def phone():
    p = Phone()
    yield p
    p.close()


DECOY = ("touch", M, 9, 1, 1, 0xFFFF)               # the message the stalled writer is holding


def without_decoy(events):
    assert events[0] == DECOY
    return events[1:]


# ===================================================================== ordinary use (real sockets)
def test_messages_arrive_in_order_unchanged(phone):
    ch = phone.channel()
    ch.send(msg(D, 0, 10, 20))
    ch.key(protocol.KEYCODE_BACK)
    ch.send(msg(M, 0, 11, 21))
    ch.send(msg(U, 0, 11, 21, pressure=0.0))
    assert phone.read_events() == [("touch", D, 0, 10, 20, 0xFFFF), ("key", 0, 4), ("key", 1, 4),
                                   ("touch", M, 0, 11, 21, 0xFFFF), ("touch", U, 0, 11, 21, 0)]


def test_moves_of_a_finger_that_is_not_down_still_get_through(phone):
    ch = phone.channel()
    ch.send(msg(M, 5, 9, 9))
    assert phone.read_events() == [("touch", M, 5, 9, 9, 0xFFFF)]


def test_a_phone_that_simply_disconnects_is_not_reported_as_a_stall(phone):
    ch = phone.channel()
    phone.peer.close()
    for _ in range(50):
        ch.send(msg(M, 0))
        time.sleep(0.01)
        if ch.closed:
            break
    time.sleep(0.2)
    assert ch.closed and phone.stalled_calls == []


def test_a_graceful_close_sends_what_is_queued_and_lifts_every_finger_that_is_down(phone):
    ch = phone.channel()
    ch.send(msg(D, 0, 10, 10))
    ch.send(msg(D, 1, 50, 60))
    ch.send(msg(M, 1, 55, 66))
    ch.send(msg(D, 2, 5, 5))
    ch.send(msg(U, 2, 5, 5, pressure=0.0))
    ch.close(graceful=True)
    assert ch.wait_drained(3)
    events = phone.read_events()
    ups = [e for e in events if e[1] == U]
    by_pointer = {e[2]: e for e in ups}
    assert set(by_pointer) == {0, 1, 2} and len(ups) == 3                      # finger 2 was already up: no second UP
    assert by_pointer[0][3:5] == (10, 10) and by_pointer[1][3:5] == (55, 66)   # lifted where it was last seen
    assert by_pointer[0][5] == 0 and by_pointer[1][5] == 0                     # zero pressure
    assert ch.send(msg(D, 0)) is False                                         # nothing new after closing


# ===================================================================== never blocks, MOVEs coalesce (stub)
def test_send_never_blocks_the_caller_when_the_phone_has_stopped_reading(stub):
    ch = make_channel(stub, stall_seconds=30)
    stub.stall(ch)
    started = time.perf_counter()
    ch.send(msg(D, 0))
    for i in range(5000):
        ch.send(msg(M, 0, i, i))
    ch.send(msg(U, 0))
    assert time.perf_counter() - started < 0.5


def test_key_messages_are_never_coalesced(stub):
    ch = make_channel(stub, stall_seconds=30)
    stub.stall(ch)
    for _ in range(3):
        ch.key(protocol.KEYCODE_BACK)
    stub.resume(ch)
    assert without_decoy(stub.events()).count(("key", 0, 4)) == 3


def test_a_flood_of_moves_of_one_finger_becomes_one_move_with_the_latest_position(stub):
    ch = make_channel(stub, stall_seconds=30)
    stub.stall(ch)
    ch.send(msg(D, 0, 0, 0))
    for i in range(1, 1000):
        ch.send(msg(M, 0, i, i))
    ch.send(msg(U, 0, 999, 999, pressure=0.0))
    assert ch.pending_count() == 3                                      # DOWN, one MOVE, UP
    assert ch.coalesced_moves == 998
    stub.resume(ch)
    assert without_decoy(stub.events()) == [("touch", D, 0, 0, 0, 0xFFFF), ("touch", M, 0, 999, 999, 0xFFFF),
                                            ("touch", U, 0, 999, 999, 0)]


def test_a_move_is_never_merged_across_a_down_or_an_up_of_the_same_finger(stub):
    ch = make_channel(stub, stall_seconds=30)
    stub.stall(ch)
    for m in (msg(D, 0, 1, 1), msg(M, 0, 2, 2), msg(U, 0, 2, 2), msg(D, 0, 3, 3), msg(M, 0, 4, 4), msg(M, 0, 5, 5),
              msg(U, 0, 5, 5)):
        ch.send(m)
    stub.resume(ch)
    assert [(e[1], e[3]) for e in without_decoy(stub.events())] == [(D, 1), (M, 2), (U, 2), (D, 3), (M, 5), (U, 5)]


def test_a_move_queued_before_a_down_is_not_overwritten_by_a_move_after_it(stub):
    """MOVE, DOWN, MOVE of one finger (no UP in between): the second MOVE must stay behind the DOWN."""
    ch = make_channel(stub, stall_seconds=30)
    stub.stall(ch)
    ch.send(msg(M, 0, 1, 1))
    ch.send(msg(D, 0, 5, 5))
    ch.send(msg(M, 0, 6, 6))
    stub.resume(ch)
    assert [(e[1], e[3]) for e in without_decoy(stub.events())] == [(M, 1), (D, 5), (M, 6)]


def test_each_finger_keeps_its_own_latest_move(stub):
    ch = make_channel(stub, stall_seconds=30)
    stub.stall(ch)
    for m in (msg(D, 0, 0, 0), msg(D, 1, 0, 0), msg(M, 0, 1, 1), msg(M, 1, 7, 7), msg(M, 0, 2, 2), msg(M, 1, 8, 8)):
        ch.send(m)
    stub.resume(ch)
    events = without_decoy(stub.events())
    assert {e[2]: e[3] for e in events if e[1] == M} == {0: 2, 1: 8} and [e[1] for e in events].count(M) == 2


def test_the_coalesced_move_keeps_its_place_in_the_order(stub):
    """MOVE(0) queued before DOWN(1): the update stays in the first MOVE's slot, never jumps over the DOWN of 1."""
    ch = make_channel(stub, stall_seconds=30)
    stub.stall(ch)
    ch.send(msg(D, 0, 0, 0))
    ch.send(msg(M, 0, 1, 1))
    ch.send(msg(D, 1, 5, 5))
    ch.send(msg(M, 0, 2, 2))
    stub.resume(ch)
    got = [(e[1], e[2], e[3]) for e in without_decoy(stub.events())]
    assert got == [(D, 0, 0), (M, 0, 2), (D, 1, 5)]
    assert got.index((D, 0, 0)) < got.index((M, 0, 2))                  # the finger is down before it moves


# ===================================================================== releases are never lost
def test_no_down_or_up_is_ever_dropped_under_load(stub):
    ch = make_channel(stub, stall_seconds=30, max_pending=100_000)
    stub.stall(ch)
    expected = []
    for i in range(300):
        pid = i % 10
        ch.send(msg(D, pid, i, i))
        for j in range(20):
            ch.send(msg(M, pid, i + j, i))
        ch.send(msg(U, pid, i, i, pressure=0.0))
        expected += [D, U]
    stub.resume(ch)
    events = without_decoy(stub.events())
    assert [e[1] for e in events if e[1] != M] == expected
    down = set()
    for e in events:                                                       # every finger that went down came up, in order
        if e[1] == D:
            assert e[2] not in down
            down.add(e[2])
        elif e[1] == U:
            down.remove(e[2])
    assert not down


def test_pressed_pointers_follow_the_messages(stub):
    ch = make_channel(stub)
    ch.send(msg(D, 0))
    ch.send(msg(D, 3))
    assert ch.pressed_pointers() == {0, 3}
    ch.send(msg(U, 0))
    assert ch.pressed_pointers() == {3}


# ===================================================================== stalled phone: bounded and visible (stub)
def test_a_phone_that_stops_reading_ends_the_session_after_the_timeout(stub):
    ch = make_channel(stub, stall_seconds=0.4)
    stub.stall(ch)
    deadline = time.time() + 5
    while not stub.stalled_calls and time.time() < deadline:
        time.sleep(0.05)
    assert len(stub.stalled_calls) == 1 and "did not accept" in stub.stalled_calls[0]
    assert ch.closed and ch.failure and ch.pending_count() == 0
    assert ch.send(msg(D, 0)) is False                                     # callers can see that nothing will be sent


def test_too_many_waiting_state_changes_are_reported_not_silently_dropped(stub):
    ch = make_channel(stub, stall_seconds=30, max_pending=20)
    stub.stall(ch)
    results = [ch.send(msg(D, i % 10)) for i in range(25)]
    assert results[:20] == [True] * 20 and False in results[20:]
    assert len(stub.stalled_calls) == 1 and "not keeping up" in stub.stalled_calls[0]
    assert ch.closed and ch.pending_count() == 0


def test_moves_alone_can_never_overflow_the_queue(stub):
    ch = make_channel(stub, stall_seconds=30, max_pending=20)
    stub.stall(ch)
    ch.send(msg(D, 0))
    assert all(ch.send(msg(M, 0, i, i)) for i in range(50_000))
    assert not stub.stalled_calls and ch.pending_count() <= 3


def test_the_queue_stays_bounded_for_any_mix_of_moves_over_many_fingers(stub):
    ch = make_channel(stub, stall_seconds=30, max_pending=1000)
    stub.stall(ch)
    for pid in range(10):
        ch.send(msg(D, pid))
    for i in range(20_000):
        ch.send(msg(M, i % 10, i, i))
    assert ch.pending_count() <= 10 + 10                                   # 10 DOWNs + at most one MOVE per finger


def test_a_graceful_close_does_not_hang_when_the_phone_is_not_reading(stub):
    ch = make_channel(stub, stall_seconds=30)
    stub.stall(ch)
    ch.send(msg(D, 0))
    ch.close(graceful=True)
    started = time.perf_counter()
    assert ch.wait_drained(0.3) is False                                   # not delivered, and we found out in time
    assert time.perf_counter() - started < 1.0
    ch.close()                                                             # a hard close always finishes
    assert ch.wait_drained(1)


def test_closing_twice_is_harmless(stub):
    ch = make_channel(stub)
    ch.close(graceful=True)
    ch.close(graceful=True)
    ch.close()
    ch.close()
    assert ch.wait_drained(2)


def test_a_failing_stall_callback_does_not_kill_the_channel(stub):
    def boom(reason):
        raise RuntimeError("bad callback")
    ch = ControlChannel(stub, on_stalled=boom, stall_seconds=30, max_pending=2)
    stub.stall(ch)
    for _ in range(5):
        ch.send(msg(D, 1))
    assert ch.closed                                                       # failed visibly, callback error swallowed


# ===================================================================== helpers
def test_release_message_is_an_up_with_zero_pressure_at_the_same_place():
    down = msg(M, 4, 321, 123)
    up = release_message(down)
    assert touch_header(up) == (U, 4) and up[2:22] == down[2:22] and up[22:24] == b"\x00\x00" and up[24:] == down[24:]
    assert len(up) == 32


def test_touch_header_ignores_other_messages():
    assert touch_header(protocol.encode_keycode(0, 4)) is None
    assert touch_header(b"") is None
    assert touch_header(msg(M, 7)) == (M, 7)
