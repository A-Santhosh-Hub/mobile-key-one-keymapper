"""GUI test of the reliability fixes, on a simulated phone (nothing reaches a real device):
rotation / resolution change during play, disconnect and reconnect, releases queued before a disconnect, stopping and
closing without blocking the window. Your own settings and profiles are put back at the end."""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.device.adb import DeviceInfo  # noqa: E402
from app.device.foreground import ForegroundWatcher  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.mapping import templates  # noqa: E402
from app.process import hard_exit  # noqa: E402
from app.mapping.profile import Binding  # noqa: E402
from app.stream import protocol  # noqa: E402
from app.ui import main_window as mw  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from fake_phone import FAKE_SIZE, attach_fake_phone  # noqa: E402

RESULTS: list[bool] = []
KEY = Qt.Key
LAND, PORT = FAKE_SIZE, (FAKE_SIZE[1], FAKE_SIZE[0])


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


class BlockingAdb:
    """An adb whose run() hangs and CANNOT be cancelled (a call stuck inside the system)."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.inside = False

    def run(self, *args, **kwargs):
        self.inside = True
        time.sleep(self.seconds)

        class Result:
            stdout = ""
        return Result()


class CancellableAdb:
    """An adb call that is a real process, reported through process_hook, so it can be killed like the real one."""

    def __init__(self):
        self.inside = False

    def run(self, *args, process_hook=None, **kwargs):
        import subprocess
        self.inside = True
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        if process_hook is not None:
            process_hook(process)
        process.wait()

        class Result:
            stdout = ""
        return Result()


def main() -> int:
    setup_logging()
    app = QApplication([])
    app.setStyleSheet(theme.STYLE_SHEET)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    window = MainWindow()
    snapshot = {k: window.settings.value(k) for k in window.settings.allKeys()}
    window.show()
    window.raise_()
    window.activateWindow()

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.004)

    attach_fake_phone(window)
    pump(0.4)
    events: list[tuple] = []                                     # ("msg", bytes) / ("stop",) in the order they happen

    def record(data: bytes) -> bool:
        events.append(("msg", data))
        return True

    window.worker.control.send = record
    real_stop = window.worker.stop
    window.worker.stop = lambda: (events.append(("stop",)), real_stop())
    window.router.window = type("A", (), {"isActiveWindow": staticmethod(lambda: True)})()
    window.isActiveWindow = lambda: True
    engine, overlay = window.engine, window.overlay

    def touches(since=0):
        out = []
        for e in events[since:]:
            if e[0] == "msg" and e[1][0] == protocol.TYPE_INJECT_TOUCH_EVENT:
                _, action, pid, x, y, w, h, _p, _ab, _b = struct.unpack(">BBqiiHHHII", e[1])
                out.append((action, pid, x, y, w, h))
        return out

    name = "zz-reliability"
    window.store.save(templates.build(templates.BATTLE_ROYALE, name))
    window._refresh_profile_list(name)
    profile = window.profile
    profile.add(Binding("B", 0.80, 0.50, "hold", "Test hold button"))     # a button that stays down while the key is held
    window._profile_changed()
    D, U = protocol.TOUCH_DOWN, protocol.TOUCH_UP

    def start_play():
        window.worker.video_size = LAND
        window._last_video_size = LAND
        window._set_mapping(True)
        pump(0.2)

    def rotate_to(size):
        window.worker.video_size = size                          # the worker thread updates this first ...
        window._on_video_size(*size)                             # ... then the GUI is told
        pump(0.1)

    try:
        print("1. Rotation while fingers are down")
        start_play()
        check("playing on a landscape profile", engine.mapping_active and profile.orientation == "landscape")
        QTest.keyPress(window, KEY.Key_W)
        QTest.keyPress(window, KEY.Key_B)
        pump(0.2)
        before = len(events)
        check("two fingers are down (stick and a held button)", engine.injector.active_count >= 2, str(engine.injector.active_count))
        rotate_to(PORT)
        new = touches(before)
        check("every finger was lifted at the NEW size", len([t for t in new if t[0] == U]) >= 2
              and all(t[4:] == PORT for t in new), str(new))
        check("nothing is pressed any more", engine.injector.active_count == 0)
        check("a landscape profile on a portrait phone pauses key mapping", not engine.mapping_active
              and window._rotation_paused and not engine.look_captured)
        check("the player is told why", "turned to portrait" in window.controls.hint.text(), window.controls.hint.text())
        QTest.keyRelease(window, KEY.Key_W)
        QTest.keyRelease(window, KEY.Key_B)
        after = len(events)
        QTest.keyClick(window, KEY.Key_R)
        pump(0.2)
        check("while paused, keys send nothing to the phone", touches(after) == [])
        rotate_to(LAND)
        check("turning back resumes key mapping by itself", engine.mapping_active and not window._rotation_paused)
        mark = len(events)
        QTest.keyClick(window, KEY.Key_R)
        pump(0.3)
        check("and the keys work at the landscape size again", [t[0] for t in touches(mark)] == [D, U]
              and all(t[4:] == LAND for t in touches(mark)), str(touches(mark)))
        window._set_mapping(False)

        print("2. A manual stop cancels the automatic resume")
        start_play()
        rotate_to(PORT)
        check("paused by the rotation", window._rotation_paused)
        window._set_mapping(False)                               # the player pressed Stop
        check("the pending resume is cancelled", not window._rotation_paused)
        rotate_to(LAND)
        check("turning back does NOT start mapping the player stopped", not engine.mapping_active)

        print("3. Never resumes while editing, or in the background")
        start_play()
        rotate_to(PORT)
        window.controls.edit_btn.setChecked(True)
        pump(0.1)
        window._rotation_paused = True                           # editing switches mapping off; keep the pause to test it
        rotate_to(LAND)
        check("no automatic start while the layout is being edited", not engine.mapping_active and window._rotation_paused)
        window.controls.edit_btn.setChecked(False)
        pump(0.1)
        window.isActiveWindow = lambda: False
        window._try_resume_after_rotation()
        check("no automatic start (and no mouse capture) while the window is in the background",
              not engine.mapping_active and not engine.look_captured)
        window.isActiveWindow = lambda: True
        window._try_resume_after_rotation()
        check("it starts when the window is active again", engine.mapping_active and not window._rotation_paused)
        window._set_mapping(False)

        print("4. Same orientation, different resolution; profiles for any orientation")
        start_play()
        QTest.keyPress(window, KEY.Key_B)
        pump(0.1)
        before = len(events)
        rotate_to((1600, 720))
        check("a resolution change lifts the finger at the new size but keeps playing",
              engine.mapping_active and touches(before)[-1][0] == U and touches(before)[-1][4:] == (1600, 720))
        QTest.keyRelease(window, KEY.Key_B)
        window._set_mapping(False)
        profile.orientation = "any"
        start_play()
        rotate_to(PORT)
        check("a profile that works in any orientation keeps playing after a rotation", engine.mapping_active
              and not window._rotation_paused)
        window._set_mapping(False)
        profile.orientation = "landscape"

        print("5. The first size of a session is not a change")
        window._set_mapping(False)
        window.worker.video_size = LAND
        window._last_video_size = (0, 0)
        start_play()
        window._last_video_size = (0, 0)
        QTest.keyPress(window, KEY.Key_B)
        pump(0.1)
        before = len(events)
        window._on_video_size(*LAND)
        check("it lifts nothing", touches(before) == [] and engine.injector.active_count >= 1)
        QTest.keyRelease(window, KEY.Key_B)
        window._set_mapping(False)

        print("6. Disconnect while fingers are down, then reconnect")
        start_play()
        QTest.keyPress(window, KEY.Key_B)
        QTest.keyPress(window, KEY.Key_W)
        pump(0.2)
        down = engine.injector.active_count
        events.clear()
        window._toggle_connection()                              # the player presses Disconnect
        kinds = [e[0] if e[0] == "stop" else "msg" for e in events]
        ups = [i for i, e in enumerate(events) if e[0] == "msg" and e[1][0] == protocol.TYPE_INJECT_TOUCH_EVENT
               and e[1][1] == U]
        stop_at = kinds.index("stop") if "stop" in kinds else -1
        check("the releases are queued BEFORE the connection is asked to close", down >= 2 and len(ups) >= 2
              and stop_at > max(ups), f"{len(ups)} UPs, stop at {stop_at}, order {kinds}")
        check("nothing is pressed any more", engine.injector.active_count == 0)
        QTest.keyRelease(window, KEY.Key_B)
        QTest.keyRelease(window, KEY.Key_W)

        worker = window.worker
        started: list[str] = []
        window.devices = [DeviceInfo("FAKE-PHONE", "device", "fake")]
        window._wanted_serial = "FAKE-PHONE"
        window.reconnect_check.setChecked(True)
        window._start_worker = lambda device: started.append(device.serial)
        window._last_video_size = LAND
        window._on_worker_finished(worker)                       # the connection ended (cable pulled)
        check("after the connection ends, nothing is pressed and play is off", engine.injector.active_count == 0
              and not engine.mapping_active and window.worker is None)
        check("the remembered screen size is forgotten, so the next size is a fresh start", window._last_video_size == (0, 0))
        check("a reconnect is scheduled", window.retry_timer.isActive())
        window._maybe_reconnect()
        check("and the app reconnects to the same phone", started == ["FAKE-PHONE"], str(started))
        window.retry_timer.stop()
        window._wanted_serial = None

        print("7. Stopping and closing never freeze the window")
        attach_fake_phone(window)
        pump(0.2)
        slow = BlockingAdb(1.2)
        watcher = ForegroundWatcher(slow, "SERIAL", interval=0.05, parent=window)
        watcher.packageChanged.connect(lambda p: None)
        window.foreground = watcher
        watcher.start()
        end = time.time() + 2
        while not slow.inside and time.time() < end:
            pump(0.02)
        started_at = time.perf_counter()
        window._stop_foreground_watch()
        elapsed = time.perf_counter() - started_at
        check("stopping the foreground watcher returns at once, even inside an adb call", elapsed < 0.1, f"{elapsed * 1000:.0f} ms")
        check("it is kept alive until it has really ended", watcher in window._retiring and watcher.isRunning())
        pump(2.0)
        check("and is released afterwards", watcher not in window._retiring and not watcher.isRunning())

        def close_with(adb, wait_ms):
            """Close a fresh window while its foreground thread is inside `adb.run`; returns (seconds, ticks, window, thread)."""
            win = MainWindow()
            win.show()
            pump(0.3)
            mw.SHUTDOWN_WAIT_MS = wait_ms
            thread = ForegroundWatcher(adb, "SERIAL", interval=0.05, parent=win)
            win.foreground = thread
            thread.start()
            end_at = time.time() + 2
            while not adb.inside and time.time() < end_at:
                pump(0.02)
            ticks = []
            pulse = QTimer()
            pulse.timeout.connect(lambda: ticks.append(time.perf_counter()))
            pulse.start(20)
            t0 = time.perf_counter()
            win.close()
            seconds = time.perf_counter() - t0
            pulse.stop()
            pump(0.3)
            return seconds, len(ticks), win, thread

        seconds, ticks, win, thread = close_with(BlockingAdb(1.0), 6000)
        check("a call that cannot be cancelled: closing waits for it with the event loop still running", ticks >= 15
              and seconds < 4.0, f"{ticks} event-loop ticks during {seconds:.2f} s")
        check("and every background thread has ended afterwards", not thread.isRunning() and not win.tracker.isRunning()
              and win.stuck_threads == [])

        seconds, ticks, win, thread = close_with(CancellableAdb(), 6000)
        check("a call that CAN be cancelled is cancelled, so closing is quick", seconds < 1.5, f"{seconds:.2f} s")
        check("and nothing is left running", not thread.isRunning() and not win.tracker.isRunning() and win.stuck_threads == [])

        seconds, ticks, win, thread = close_with(BlockingAdb(6.0), 300)
        check("a thread that never ends cannot hold the window open for long", seconds < 3.0, f"{seconds:.2f} s")
        check("it is reported, never terminated (terminating a Python thread can crash the process)",
              thread in win.stuck_threads and thread.isRunning())
    finally:
        mw.SHUTDOWN_WAIT_MS = 6000
        try:
            window.store.delete(name)
        except Exception:
            pass
        for key in window.settings.allKeys():
            if key not in snapshot:
                window.settings.remove(key)
        for key, value in snapshot.items():
            window.settings.setValue(key, value)
        window.settings.sync()
    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    hard_exit(code)         # a deliberately hung thread is still running: leave it behind, exactly as the app does
