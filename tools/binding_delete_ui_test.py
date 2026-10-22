"""GUI test of deleting ONE binding, on the SIMULATED phone: the controls panel's Remove button, the Delete key / selected marker, and
the marker's right-click menu. A version-2 profile is used in which two bindings share a first input (LMB), a third has an
additional trigger that overlaps it (LMB), and every binding has several actions or triggers. Whatever is deleted, nothing else
may go, also when the list changed after the row or marker was selected, and a delete that cannot be resolved is refused.

The profile store is a temporary folder (the window's store is replaced), so your own profiles are never touched; your own
settings are put back at the end. No touch reaches any device."""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox  # noqa: E402

from app.logging_setup import setup_logging  # noqa: E402
from fake_phone import attach_fake_phone  # noqa: E402
from app.mapping import layout_edit as le  # noqa: E402
from app.mapping.profile import Action, Binding, Profile, ProfileStore, Stick  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

RESULTS: list[bool] = []
NAME = "Shared"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


def shared_profile() -> Profile:
    p = Profile(NAME, orientation="any", id="aaaabbbbcccc")
    p.bindings = [
        Binding("LMB", 0.80, 0.70, "hold", "Fire", id="a", also=[(0.10, 0.40)], extra_actions=[Action("tap", 0.50, 0.20, delay_ms=60)]),
        Binding("LMB", 0.30, 0.30, "tap", "Peek", id="b", extra_triggers=["Z"],
                extra_actions=[Action("swipe", 0.20, 0.50, x2=0.60, y2=0.30, delay_ms=120), Action("hold", 0.40, 0.40)]),
        Binding("R", 0.70, 0.90, "tap", "Reload", id="c", extra_triggers=["LMB", "Shift+R"], on_failure="abort",
                extra_actions=[Action("repeat", 0.60, 0.60, rate_hz=12)]),
        Binding("Q", 0.20, 0.80, "sequence", "Menu", id="d", steps=[(0.1, 0.1, 0), (0.2, 0.2, 300)], extra_triggers=["E", "F"]),
    ]
    p.set_stick(Stick(0.62, 0.55, radius=0.05))
    p.share_trigger("LMB", ["a", "b", "c"])                          # Fire, Peek and Reload run together on LMB (explicit sharing)
    return p


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
    controls, overlay, video = window.controls, window.overlay, window.video
    folder = tempfile.TemporaryDirectory(prefix="mko-delete-test-")
    window.store = ProfileStore(Path(folder.name))                  # NOT the user's profile folder

    def fresh() -> Profile:
        """Save the shared profile into the temporary store and open it in the window; edit mode on."""
        window._set_edit_mode(False)
        window.store.save(shared_profile())
        window._refresh_profile_list(NAME)
        window._set_edit_mode(True)
        pump(0.05)
        return window.profile

    def ids() -> list[str]:
        return [b.id for b in window.profile.bindings]

    def on_disk() -> Profile:
        return window.store.load(NAME)

    def target_size() -> tuple[float, float]:
        t = video.target_rect()
        return t.width(), t.height()

    def marker_hit(x: float, y: float):
        tw, th = target_size()
        return le.hit_test(window.profile, x, y, tw, th)

    def click_remove(row: int) -> None:
        controls.table.selectRow(row)
        pump(0.02)
        QTest.mouseClick(controls.remove_btn, Qt.MouseButton.LeftButton)
        pump(0.05)

    def untouched_bindings_equal(survivors: dict[str, dict]) -> bool:
        return all(b.to_dict_v2() == survivors[b.id] for b in window.profile.bindings if b.id in survivors)

    p = fresh()
    original = {b.id: b.to_dict_v2() for b in p.bindings}
    check("the version-2 profile with shared bindings is open: 4 controls, 4 rows", ids() == ["a", "b", "c", "d"]
          and controls.table.rowCount() == 4 and p.required_version == 2 and window.profile.id == "aaaabbbbcccc")

    print("1. Remove button of the controls panel")
    click_remove(1)                                                   # Peek: shares LMB with Fire, and Reload lists LMB as a trigger
    check("only the selected control (Peek) was removed; the other control on LMB and the one with LMB as a trigger stay",
          ids() == ["a", "c", "d"] and controls.table.rowCount() == 3, str(ids()))
    check("every surviving control is exactly as it was (triggers, actions, delays, extra spots, failure policy)",
          untouched_bindings_equal(original))
    saved = on_disk()
    check("the sharing group on LMB lost only Peek and still has Fire and Reload",
          window.profile.shared_triggers == {"LMB": ["a", "c"]} and on_disk().shared_triggers == {"LMB": ["a", "c"]},
          str(window.profile.shared_triggers))
    check("saved to disk with the same survivors, order, profile id and version",
          [b.id for b in saved.bindings] == ["a", "c", "d"] and saved.id == "aaaabbbbcccc" and saved.schema_version == 2
          and all(b.to_dict_v2() == original[b.id] for b in saved.bindings))

    print("2. The panel's rows are stale (the list moved after they were drawn)")
    p = fresh()
    controls.table.selectRow(2)                                       # the row that shows Reload
    pump(0.02)
    window.profile.bindings.insert(0, Binding("N", 0.9, 0.1, "tap", "New", id="n"))     # behind the panel's back: no redraw
    QTest.mouseClick(controls.remove_btn, Qt.MouseButton.LeftButton)
    pump(0.05)
    check("the control the row showed (Reload) was removed, not the one that is now at that position (Peek)",
          ids() == ["n", "a", "b", "d"], str(ids()))
    p = fresh()
    controls.table.selectRow(3)                                       # Menu
    pump(0.02)
    window.profile.remove_by_id("d")                                  # it disappears behind the panel's back
    before = [b.to_dict_v2() for b in window.profile.bindings]
    controls.set_hint("")
    QTest.mouseClick(controls.remove_btn, Qt.MouseButton.LeftButton)
    pump(0.05)
    check("a row whose control is already gone is refused, with a message, and nothing else is deleted",
          [b.to_dict_v2() for b in window.profile.bindings] == before and "nothing was deleted" in controls.hint.text(),
          controls.hint.text())

    print("3. Delete key on the selected marker (identity survives list changes)")
    p = fresh()
    overlay.select(marker_hit(0.30, 0.30))                            # the Peek marker
    pump(0.02)
    check("selecting the marker remembers which control it is", window._selection.binding_id == "b" and controls.selected_row() == 1)
    window.profile.bindings.insert(0, Binding("N", 0.9, 0.1, "tap", "New", id="n"))
    window.profile.remove_by_id("a")                                  # positions changed twice
    window._delete_selected()
    pump(0.05)
    check("Delete removes Peek, not whatever is now at its old position", ids() == ["n", "c", "d"], str(ids()))
    p = fresh()
    overlay.select(le.Hit("tap", 1))                                  # a hit made WITHOUT an id (like selecting a control just added)
    pump(0.02)
    check("the window pins such a selection to its control", window._selection.binding_id == "b")
    window.profile.bindings.insert(0, Binding("N", 0.9, 0.1, "tap", "New", id="n"))
    window._delete_selected()
    pump(0.05)
    check("and Delete removes that control after the list moved", ids() == ["n", "a", "c", "d"], str(ids()))
    p = fresh()
    overlay.select(marker_hit(0.70, 0.90))                            # Reload
    pump(0.02)
    window.profile.remove_by_id("c")
    before = [b.to_dict_v2() for b in window.profile.bindings]
    controls.set_hint("")
    window._delete_selected()
    pump(0.05)
    check("a selected marker whose control is gone: Delete is refused and removes nothing",
          [b.to_dict_v2() for b in window.profile.bindings] == before and "nothing was deleted" in controls.hint.text())
    p = fresh()
    overlay.select(marker_hit(0.80, 0.70))                            # Fire, the first of the two controls on LMB
    pump(0.02)
    window._delete_selected()
    pump(0.05)
    check("deleting one of two controls on the same key leaves the other", ids() == ["b", "c", "d"]
          and window.profile.find("LMB").id == "b" and on_disk().find("LMB").id == "b")
    check("and the sharing group, now left with Peek and Reload, is intact on disk", on_disk().shared_triggers == {"LMB": ["b", "c"]})

    print("4. Right-click menu on a marker")
    triggered: list[str] = []
    import app.ui.main_window as main_window_module
    real_menu = main_window_module.QMenu

    def run_menu(pick, shake, hit=None):
        """Open the marker menu; `shake` changes the list while it is open; `pick` is the entry to choose (None = dismiss)."""
        class FakeMenu(real_menu):
            def exec(self, *_a, **_k):
                shake()
                for action in self.actions():
                    if action.text() == pick:
                        triggered.append(action.text())
                        action.trigger()
                        return action
                return None
        main_window_module.QMenu = FakeMenu
        try:
            window._show_context_menu(hit if hit is not None else marker_hit(0.30, 0.30), QPoint(0, 0))
        finally:
            main_window_module.QMenu = real_menu
        pump(0.05)

    p = fresh()
    run_menu("Delete", lambda: (window.profile.bindings.insert(0, Binding("N", 0.9, 0.1, "tap", id="n")), window.profile.remove_by_id("a")))
    check("the menu deletes the control it was opened on even though the list changed while it was open",
          ids() == ["n", "c", "d"] and triggered == ["Delete"], str(ids()))
    p = fresh()
    run_menu("Delete", lambda: window.profile.bindings.insert(0, Binding("N", 0.9, 0.1, "tap", id="n")),
             hit=le.Hit("tap", 1))                                    # an unpinned hit: the menu pins it when it opens
    check("the same when the menu is opened on a hit that carries no id (Peek goes, not the control that took its position)",
          ids() == ["n", "a", "c", "d"], str(ids()))
    p = fresh()
    run_menu(None, lambda: None)
    check("a dismissed menu changes nothing", ids() == ["a", "b", "c", "d"] and untouched_bindings_equal(original))
    p = fresh()
    controls.set_hint("")
    run_menu("Delete", lambda: window.profile.remove_by_id("b"))
    check("the menu of a control that disappeared meanwhile is refused and deletes nothing else", ids() == ["a", "c", "d"]
          and "nothing was deleted" in controls.hint.text(), str(ids()))

    print("5. Extra spots and the rest of the editor still behave")
    p = fresh()
    window._delete_hit(le.Hit("tap", 0, "also0"))
    check("deleting an extra spot keeps its control", ids() == ["a", "b", "c", "d"] and window.profile.find_by_id("a").also == [])
    window.profile.bindings.insert(0, Binding("N", 0.9, 0.1, "tap", id="n", also=[(0.5, 0.5)]))
    window._delete_hit(le.pin(window.profile, le.Hit("tap", 1, "also0")))        # (pinned to 'a', which has none left)
    check("a hit on an extra spot that is gone is refused", ids() == ["n", "a", "b", "c", "d"] and window.profile.find_by_id("n").also == [(0.5, 0.5)])
    window._delete_hit(le.Hit("stick", 0, "center"))
    check("the stick is deleted by the same route", window.profile.sticks == [])

    window._set_edit_mode(False)
    for key, value in snapshot.items():
        window.settings.setValue(key, value)
    for key in set(window.settings.allKeys()) - set(snapshot):
        window.settings.remove(key)
    window.settings.sync()
    folder.cleanup()
    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed (temporary profile store; no touch reached a phone)", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
