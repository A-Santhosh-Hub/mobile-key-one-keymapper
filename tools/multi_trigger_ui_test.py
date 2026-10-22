"""GUI test of multi-trigger bindings on the SIMULATED phone: real Qt key and mouse events go through the router and the engine,
the overlay's real pixels show which marker is lit, and every touch message is intercepted (nothing reaches any device).

One control (marker "E") has the triggers E, F, the middle mouse button and Shift+G. The profile lives only in memory: no profile
file is written, and your own settings are put back at the end."""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QRegion  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

from app.logging_setup import setup_logging  # noqa: E402
from fake_phone import attach_fake_phone  # noqa: E402
from app.mapping.profile import Binding, Profile  # noqa: E402
from app.stream import protocol  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.overlay import RADIUS  # noqa: E402

RESULTS: list[bool] = []
KEY = Qt.Key


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


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
    sent: list[bytes] = []
    window.worker.control.send = lambda data: (sent.append(data), True)[1]
    window.router.window = type("A", (), {"isActiveWindow": staticmethod(lambda: True)})()
    overlay, engine, video = window.overlay, window.engine, window.video
    window._set_marker_opacity(100, toast=False)

    profile = Profile("zz-multi-trigger-in-memory")
    profile.add(Binding("E", 0.5, 0.5, "hold", "Interact", extra_triggers=["F", "MMB", "Shift+G"]))
    profile.add(Binding("R", 0.2, 0.8, "tap", "Reload"))
    window.profile = profile
    engine.set_profile(profile)
    window._set_mapping(True)
    pump(0.3)
    sent.clear()

    def touches() -> list[tuple]:
        out = []
        for m in sent:
            if m[0] == protocol.TYPE_INJECT_TOUCH_EVENT:
                _, action, pid, x, y, _w, _h, _p, _ab, _b = struct.unpack(">BBqiiHHHII", m)
                out.append((action, pid, x, y))
        return out

    def layer() -> QImage:
        image = QImage(overlay.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        overlay.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
        return image

    def marker_colour(binding: Binding) -> QColor:
        """The marker's fill, sampled inside its ring next to the letter, from the overlay's real pixels."""
        target = video.target_rect()
        cx = target.x() + binding.x * target.width()
        cy = target.y() + binding.y * target.height()
        return layer().pixelColor(int(cx - RADIUS + 5), int(cy))

    def lit(binding: Binding) -> bool:
        c, ok = marker_colour(binding), QColor(theme.OK)
        return abs(c.red() - ok.red()) < 14 and abs(c.green() - ok.green()) < 14 and abs(c.blue() - ok.blue()) < 14

    e, r = profile.find("E"), profile.find("R")
    check("mapping is on with the multi-trigger control and a plain one", engine.mapping_active and e.triggers == ("E", "F", "MMB", "Shift+G"))
    check("both markers are drawn and idle (not green)", not lit(e) and not lit(r))

    print("1. A second key starts the control; the first key joins it")
    QTest.keyPress(window, KEY.Key_F)
    pump(0.1)
    t = touches()
    check("F presses the control's spot once", len(t) == 1 and t[0][0] == protocol.TOUCH_DOWN, str(t))
    check("the overlay lights the marker of the control (named after its first key E), not a marker for F",
          overlay.pressed == {"E"} and lit(e) and not lit(r), f"{overlay.pressed}")
    QTest.keyPress(window, KEY.Key_E)
    pump(0.1)
    check("E joins: no second finger", len(touches()) == 1 and engine.injector.active_count == 1)
    QTest.keyRelease(window, KEY.Key_F)
    pump(0.1)
    check("releasing F keeps the finger down and the marker lit (E still holds it)",
          len(touches()) == 1 and engine.injector.active_count == 1 and lit(e))
    QTest.keyRelease(window, KEY.Key_E)
    pump(0.1)
    t = touches()
    check("releasing E lifts the finger exactly once and the marker goes dark",
          [x[0] for x in t] == [protocol.TOUCH_DOWN, protocol.TOUCH_UP] and not lit(e) and overlay.pressed == set(), str(t))

    print("2. The middle mouse button and the combination Shift+G are triggers too")
    sent.clear()
    QTest.mousePress(video, Qt.MouseButton.MiddleButton)
    pump(0.15)
    check("the middle mouse button presses the control's spot (and is not Android's Home key)",
          len(touches()) == 1 and engine.injector.active_count == 1 and lit(e)
          and not any(m[0] == protocol.TYPE_INJECT_KEYCODE for m in sent))
    QTest.keyPress(window, KEY.Key_Shift)
    QTest.keyPress(window, KEY.Key_G)
    pump(0.1)
    check("Shift+G joins the same run", len(touches()) == 1 and engine.injector.active_count == 1)
    QTest.mouseRelease(video, Qt.MouseButton.MiddleButton)
    pump(0.1)
    check("releasing the mouse button leaves it held by Shift+G", engine.injector.active_count == 1 and lit(e))
    QTest.keyRelease(window, KEY.Key_Shift)
    pump(0.1)
    check("releasing Shift (a key the combination needs) lets go: one UP in total",
          engine.injector.active_count == 0 and [x[0] for x in touches()] == [protocol.TOUCH_DOWN, protocol.TOUCH_UP] and not lit(e))
    QTest.keyRelease(window, KEY.Key_G)
    pump(0.05)

    print("3. The plain control still works and is unaffected")
    sent.clear()
    QTest.keyPress(window, KEY.Key_R)
    pump(0.15)
    QTest.keyRelease(window, KEY.Key_R)
    pump(0.1)
    t = touches()
    check("R still taps the Reload spot", [x[0] for x in t] == [protocol.TOUCH_DOWN, protocol.TOUCH_UP], str(t))

    print("4. Safety: Esc lifts everything and clears the highlight")
    sent.clear()
    QTest.keyPress(window, KEY.Key_F)
    QTest.keyPress(window, KEY.Key_E)
    pump(0.1)
    check("two triggers down, one finger", engine.injector.active_count == 1 and lit(e))
    QTest.keyClick(window, KEY.Key_Escape)
    pump(0.2)
    check("Esc: mapping off, no finger left down, marker dark", not engine.mapping_active and engine.injector.active_count == 0
          and not lit(e) and touches()[-1][0] == protocol.TOUCH_UP, str(touches()))
    QTest.keyRelease(window, KEY.Key_F)
    QTest.keyRelease(window, KEY.Key_E)
    pump(0.1)

    for key, value in snapshot.items():
        window.settings.setValue(key, value)
    for key in set(window.settings.allKeys()) - set(snapshot):
        window.settings.remove(key)
    window.settings.sync()
    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed (no touch reached a phone)", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
