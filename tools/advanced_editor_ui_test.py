"""GUI test of the advanced mapping editor (Phase 2, step 8) on the SIMULATED phone: triggers, additional actions, shared triggers,
the identity-based controls table and overlay, the checks panel, and version-1 / version-2 profile handling.

Real Qt events drive the window; the options dialog is driven through its own buttons and hooks (its modal loop is replaced so a test can
script it). The profile store is a temporary folder (the window's store is replaced), so your own profiles are never touched, and your own
settings are put back at the end. Every touch the app would send is intercepted; no phone is used or needed."""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QRegion  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

from app.logging_setup import setup_logging  # noqa: E402
from fake_phone import attach_fake_phone  # noqa: E402
from app.mapping import binding_edit as be  # noqa: E402
from app.mapping import layout_edit as le  # noqa: E402
from app.mapping.profile import Action, Binding, Profile, ProfileStore, Stick  # noqa: E402
from app.stream import protocol  # noqa: E402
from app.ui import controls_panel as controls_module  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.binding_options import BindingOptionsDialog  # noqa: E402
from app.ui.key_capture import KeyCaptureDialog  # noqa: E402
from app.ui.key_conflict import KeyConflictDialog  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.overlay import RADIUS  # noqa: E402

RESULTS: list[bool] = []
KEY = Qt.Key
NAME = "Adv"
LEGACY = "Legacy"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


def advanced_profile() -> Profile:
    p = Profile(NAME, orientation="any", id="advadvadvadv")
    p.bindings = [
        Binding("F", 0.80, 0.70, "hold", "Fire", id="fire"),
        Binding("F", 0.30, 0.30, "tap", "Interact", id="interact"),                 # the same key, NOT authorized
        Binding("R", 0.70, 0.90, "tap", "Reload", id="reload"),
        Binding("Q", 0.20, 0.80, "tap", "Lean", id="lean", extra_triggers=["E"], on_failure="abort", also=[(0.12, 0.62)],
                extra_actions=[Action("tap", 0.35, 0.80, delay_ms=80, uid="s1", also=[(0.45, 0.70)]),
                               Action("swipe", 0.50, 0.55, x2=0.62, y2=0.55, delay_ms=200, uid="s2")]),
    ]
    p.set_stick(Stick(0.15, 0.70, radius=0.05))
    return p


def legacy_profile() -> Profile:
    p = Profile(LEGACY, orientation="any", id="leglegleglegl")
    p.bindings = [Binding("G", 0.1, 0.2, "tap", "Grenade"), Binding("T", 0.5, 0.5, "hold", "Tab"), Binding("M", 0.9, 0.1, "tap", "Map")]
    return p


class Scripted(BindingOptionsDialog):
    """The real dialog with its modal loop replaced by a script, so a test can press its buttons and then choose OK or Cancel."""
    script = None
    last = None

    def exec(self):
        Scripted.last = self
        outcome = Scripted.script(self) if Scripted.script else "cancel"
        if outcome == "ok":
            self.accept()
        else:
            self.reject()
        return self.result()


def main() -> int:
    setup_logging()
    app = QApplication([])
    app.setStyleSheet(theme.STYLE_SHEET)
    asked_confirm: list = []
    confirm_answer = {"value": True}

    def fake_question(*args, **kwargs):
        asked_confirm.append(args[2] if len(args) > 2 else "")
        return QMessageBox.StandardButton.Yes if confirm_answer["value"] else QMessageBox.StandardButton.No
    QMessageBox.question = staticmethod(fake_question)
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
    controls, overlay, video, engine = window.controls, window.overlay, window.video, window.engine
    window._set_marker_opacity(100, toast=False)
    folder = tempfile.TemporaryDirectory(prefix="mko-adv-test-")
    window.store = ProfileStore(Path(folder.name))                  # NOT the user's profile folder
    controls_module.BindingOptionsDialog = Scripted
    saved_hotkeys = dict(window.hotkeys)

    def fresh(name: str = NAME, builder=advanced_profile, edit: bool = True) -> Profile:
        window._set_edit_mode(False)
        window.store.save(builder())
        window._refresh_profile_list(name)
        if edit:
            window._set_edit_mode(True)
        pump(0.05)
        return window.profile

    def ids() -> list[str]:
        return [b.id for b in window.profile.bindings]

    def on_disk(name: str = NAME) -> Profile:
        return window.store.load(name)

    def file_bytes(name: str = NAME) -> bytes:
        return window.store.path(name).read_bytes()

    def pos(nx: float, ny: float) -> QPoint:
        r = video.target_rect()
        return QPoint(round(r.x() + nx * r.width()), round(r.y() + ny * r.height()))

    def drag(a: QPoint, b: QPoint) -> None:
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=a)
        for i in range(1, 9):
            QTest.mouseMove(overlay, QPoint(a.x() + (b.x() - a.x()) * i // 8, a.y() + (b.y() - a.y()) * i // 8))
            pump(0.01)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=b)
        pump(0.1)

    def layer() -> QImage:
        image = QImage(overlay.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        overlay.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
        return image

    def near(c: QColor, hexcolor: str, tol: int = 18) -> bool:
        t = QColor(hexcolor)
        return abs(c.red() - t.red()) < tol and abs(c.green() - t.green()) < tol and abs(c.blue() - t.blue()) < tol and c.alpha() > 100

    def action_pixel(x: float, y: float, dx: int = -14, dy: int = 0) -> QColor:
        p = pos(x, y)
        return layer().pixelColor(p.x() + dx, p.y() + dy)

    def open_options(binding_id: str, script) -> BindingOptionsDialog:
        Scripted.script = script
        controls.open_options(binding_id)
        pump(0.05)
        return Scripted.last

    def touches() -> list[tuple]:
        out = []
        for m in sent:
            if m[0] == protocol.TYPE_INJECT_TOUCH_EVENT:
                _, action, pid, x, y, _w, _h, _p, _ab, _b = struct.unpack(">BBqiiHHHII", m)
                out.append((action, pid, x, y))
        return out

    # ================================================================================================
    print("1. The controls table shows keys, actions and sharing; selection is by id")
    p = fresh()
    check("version-2 profile with an unauthorized duplicate key is open", ids() == ["fire", "interact", "reload", "lean"]
          and p.required_version == 2 and not p.shared_triggers)
    row = controls.row_of("lean")
    keys_item = controls.table.item(row, controls_module.COL_KEYS)
    more_item = controls.table.item(row, controls_module.COL_INFO)
    check("a control with another key shows 'Q +1' and its tooltip lists every key", keys_item.text() == "Q +1" and "Q, E" in keys_item.toolTip(),
          keys_item.text())
    check("the same row says how many actions it has", "3 actions" in more_item.text(), more_item.text())
    check("the two controls on F are marked as something to check", all("to check" in controls.table.item(controls.row_of(i), controls_module.COL_INFO).text()
                                                                   for i in ("fire", "interact")))
    check("a plain control has no remarks", controls.table.item(controls.row_of("reload"), controls_module.COL_INFO).text() == "")
    controls.select_binding("lean")
    pump(0.05)
    check("selecting a row selects THAT control by id and lights it on the picture", controls.selected_id() == "lean" and overlay.selected_id == "lean")
    window.profile.bindings.insert(0, Binding("N", 0.9, 0.1, "tap", "New", id="n"))
    controls.set_profile(window.profile)                              # the list changed and the table is rebuilt
    pump(0.05)
    check("after the list changed the same control stays selected (by id, not by position)", controls.selected_id() == "lean"
          and controls.table.currentRow() == controls.row_of("lean") == 4)
    window.profile.remove_by_id("n")
    controls.set_profile(window.profile)

    # ================================================================================================
    print("2. Checks panel: intentional sharing vs an unintended duplicate")
    diag = controls.diagnostics
    texts = diag.texts()
    check("the repeated F is listed as NOT shared (a warning), naming both controls", any("NOT shared" in t and "Fire" in t and "Interact" in t for t in texts),
          str(texts))
    check("the sidebar button shows the number of things to check", "⚠" in controls.check_btn.text(), controls.check_btn.text())
    check("nothing is called 'Shared on purpose' while the profile has no sharing", not any("Shared on purpose" in t for t in texts), str(texts))
    unshared = next(i for i in diag.issues if i.code == "duplicate_trigger_not_shared")
    check("the unshared duplicate carries the 'NOT shared' tag", any(t.startswith("\u26a0 Same key, NOT shared") and "sharing it was not set up" in t for t in texts), str(texts))
    check("the issue names the controls by id", set(unshared.binding_ids) == {"fire", "interact"})
    diag.select_issue_row(next(r for r, t in enumerate(diag.texts()) if "NOT shared" in t))
    chosen: list[str] = []
    diag.bindingChosen.connect(chosen.append)
    diag.go_buttons[1].click()
    pump(0.05)
    check("'Go to' selects the affected control in the table and on the picture", chosen == ["interact"] and controls.selected_id() == "interact"
          and overlay.selection is not None and overlay.selection.binding_id == "interact", str(chosen))

    # ================================================================================================
    print("3. Options dialog: keys (add, refuse, cancel, remove, make first)")
    p = fresh()
    before = json.dumps(p.to_dict(), sort_keys=True)

    def script_cancel(d):
        d.capture_key = lambda: "G"
        d.add_key()
        check("the dialog lists the keys with the first one marked", [d.trigger_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(d.trigger_list.count())]
              == ["Q", "E", "G"] and d.trigger_list.item(0).text().startswith("★"))
        check("adding a key did not add an action", len(d.wbinding.extra_actions) == 2)
        check("the real profile is untouched while the dialog is open", p.find_by_id("lean").triggers == ("Q", "E"))
        return "cancel"
    open_options("lean", script_cancel)
    check("Cancel throws the edits away completely", json.dumps(p.to_dict(), sort_keys=True) == before and p.find_by_id("lean").triggers == ("Q", "E"))

    def script_refuse(d):
        results = []
        for key in ("W", "F9", "E"):
            d.capture_key = lambda k=key: k
            results.append(d.add_key())
        check("a stick key, a window hotkey and a key it already has are all refused with a reason", all(r is not None and not r.ok for r in results),
              str([r.message for r in results]))
        check("...and the dialog's working copy is unchanged", d.wbinding.triggers == ("Q", "E"))
        return "cancel"
    open_options("lean", script_refuse)

    def script_keys(d):
        d.capture_key = lambda: "Shift+G"
        r = d.add_key()
        check("a key combination can be added", r.ok and "Shift+G" in d.wbinding.triggers)
        d.trigger_list.setCurrentRow(1)
        check("the last remaining key cannot be removed from a one-key control; this one has three so it can", d.remove_key_btn.isEnabled())
        d.remove_key()
        check("removing a key keeps the others", d.wbinding.triggers == ("Q", "Shift+G"))
        d.trigger_list.setCurrentRow(1)
        d.make_first()
        check("'Make first' puts the chosen key first without losing any", d.wbinding.triggers == ("Shift+G", "Q") and d.wbinding.input == "Shift+G")
        d.trigger_list.setCurrentRow(0)
        d.remove_key()
        check("removing the FIRST key promotes the next one", d.wbinding.triggers == ("Q",) and d.wbinding.input == "Q")
        check("with one key left the Remove button is disabled", not d.remove_key_btn.isEnabled())
        d.capture_key = lambda: "Z"
        d.add_key()
        return "ok"
    open_options("lean", script_keys)
    lean = p.find_by_id("lean")
    check("OK adopts the whole edit: Q first, then Z", lean.triggers == ("Q", "Z") and len(lean.extra_actions) == 2 and lean.on_failure == "abort")
    check("it was saved to disk", on_disk().find_by_id("lean").triggers == ("Q", "Z"))
    check("every other control is exactly as it was", [b.to_dict_v2() for b in p.bindings if b.id != "lean"]
          == [b.to_dict_v2() for b in advanced_profile().bindings if b.id != "lean"])

    # ================================================================================================
    print("4. Options dialog: actions (add, configure, duplicate, remove, failure policy)")
    p = fresh()

    def script_actions(d):
        check("the action list shows all three actions, the first one 'at once'", d.action_list.count() == 3 and "at once" in d.action_list.item(0).text()
              and "after 80 ms" in d.action_list.item(1).text(), str([d.action_list.item(i).text() for i in range(3)]))
        d.select_action(p.find_by_id("lean").extra_actions[1].uid)
        check("a swipe shows its end point and duration, not repeat or zoom settings",
              d.swipe_box.isVisible() or not d.isVisible() and d.action_editor.selected_mode() == "swipe")
        check("an additional action has a delay, the first one does not", d.action_editor.delay_spin.isEnabled())
        d.select_action(be.PRIMARY)
        check("the first action starts at once: its delay row is hidden", not d.action_editor.form.isRowVisible(d.action_editor.delay_spin))
        r = d.add_action()
        check("Add an action creates a new additional action with a delay", r.ok and len(d.wbinding.extra_actions) == 3 and d.selected_ref() == r.ref)
        d.action_editor.mode_combo.setCurrentIndex(d.action_editor.mode_combo.findData("repeat"))
        d.action_editor.rate_spin.setValue(12)
        d.action_editor.delay_spin.setValue(450)
        check("the mode and delay are applied to that action (and shown in the list)", "Repeat" in d.action_list.item(3).text() and "450" in d.action_list.item(3).text(),
              d.action_list.item(3).text())
        check("repeat shows its speed setting", d.rate_spin.value() == 12)
        d.duplicate_action()
        check("Duplicate copies the selected action", len(d.wbinding.extra_actions) == 4 and d.wbinding.extra_actions[3].mode == "repeat"
              and d.wbinding.extra_actions[3].uid != d.wbinding.extra_actions[2].uid)
        d.remove_action()
        check("Remove takes away only the selected action", len(d.wbinding.extra_actions) == 3)
        d.failure_combo.setCurrentIndex(d.failure_combo.findData("continue"))
        return "ok"
    open_options("lean", script_actions)
    lean = p.find_by_id("lean")
    check("OK: the actions, their modes, delays and the failure policy are adopted",
          [a.mode for a in lean.extra_actions] == ["tap", "swipe", "repeat"] and lean.extra_actions[2].delay_ms == 450 and lean.extra_actions[2].rate_hz == 12
          and lean.on_failure == "continue")
    saved = on_disk().find_by_id("lean")
    check("everything survives the file: modes, delays, repeat speed, extra spots, policy",
          [(a.mode, a.delay_ms) for a in saved.extra_actions] == [("tap", 80), ("swipe", 200), ("repeat", 450)] and saved.extra_actions[0].also == [(0.45, 0.7)]
          and saved.also == [(0.12, 0.62)] and saved.on_failure == "continue")

    def script_last_action(d):
        d.action_editor.mode_combo.setCurrentIndex(d.action_editor.mode_combo.findData("tap"))
        return "cancel"
    open_options("reload", script_last_action)

    def script_only_action(d):
        check("a control with one action cannot lose it (Remove is disabled) and the result is a refusal anyway", not d.remove_action_btn.isEnabled()
              and not d.remove_action().ok and len(d.wbinding.extra_actions) == 0)
        return "cancel"
    open_options("reload", script_only_action)

    def script_remove_first(d):
        d.select_action(be.PRIMARY)
        d.remove_action()
        return "ok"
    open_options("lean", script_remove_first)
    lean = p.find_by_id("lean")
    check("removing the FIRST action promotes the next one, which now starts at once", lean.mode == "tap" and (lean.x, lean.y) == (0.35, 0.8)
          and lean.extra_actions[0].mode == "swipe" and [a.delay_ms for a in lean.extra_actions] == [200, 450])

    # ================================================================================================
    print("5. Options dialog: a key another control uses (replace / share / cancel) and sharing metadata")
    p = fresh()
    choices: list = []

    def make_script(choice, key="F", target_ok=True):
        def script(d):
            d.capture_key = lambda: key
            d.choose_conflict = lambda info, run: (choices.append((info.trigger, info.users, info.choices, {c: run(c).ok for c in info.choices})), choice)[1]
            r = d.add_key()
            return "ok" if target_ok else "cancel"
        return script
    open_options("reload", make_script("cancel", target_ok=False))
    check("the conflict question offers replace and share (and says who uses the key)", choices and choices[0][0] == "F" and choices[0][1] == ("fire", "interact")
          and set(choices[0][2]) >= {"replace", "share"} and all(choices[0][3].values()), str(choices))
    check("Cancel in the question leaves everything untouched", p.find_by_id("reload").triggers == ("R",) and not p.shared_triggers)
    choices.clear()
    open_options("reload", make_script("share"))
    check("Share authorizes ALL three controls to run together on F, and only then", p.shared_triggers == {"F": ["fire", "interact", "reload"]}, str(p.shared_triggers))
    d_file = json.loads(file_bytes())
    check("the authorization is written to the profile file", d_file["shared_triggers"] == [{"trigger": "F", "bindings": ["fire", "interact", "reload"]}])
    check("the file keeps its binding ids and stays version 2", d_file["version"] == 2 and [b["id"] for b in d_file["bindings"]] == ["fire", "interact", "reload", "lean"])
    check("reloaded from disk: the same sharing, and the engine's resolver runs all of them",
          on_disk().shared_triggers == p.shared_triggers and [b.id for b in on_disk().runners()["F"]] == ["fire", "interact", "reload"])
    infos = {i.trigger: i for i in be.ProfileEditor(p).sharing_info("fire")}
    check("the editor can tell who takes part in the group", [m[0] for m in infos["F"].members] == ["fire", "interact", "reload"] and infos["F"].state == "shared")
    row_text = controls.table.item(controls.row_of("fire"), controls_module.COL_INFO).text()
    check("the table row says it shares a key, and the checks panel calls it 'Shared on purpose'", "shares a key with" in row_text
          and any("Shared on purpose" in t for t in controls.diagnostics.texts()) and not any("NOT shared" in t for t in controls.diagnostics.texts()),
          row_text + " | " + str(controls.diagnostics.texts()))
    check("the table marks shared controls", controls.table.item(controls.row_of("reload"), controls_module.COL_KEYS).text().startswith("⇄"))

    # ================================================================================================
    print("6. Running: unauthorized duplicates keep the winner, a shared key runs all of them")
    p = fresh(edit=False)
    window._set_mapping(True)
    pump(0.2)
    sent.clear()
    QTest.keyPress(window, KEY.Key_F)
    pump(0.15)
    downs = [t for t in touches() if t[0] == protocol.TOUCH_DOWN]
    QTest.keyRelease(window, KEY.Key_F)
    pump(0.1)
    check("unauthorized duplicates: pressing F touches ONE spot (the later control, Interact)", len(downs) == 1, str(downs))
    window._set_mapping(False)
    window._set_edit_mode(True)
    editor = be.ProfileEditor(p)
    editor.add_trigger("reload", "F", be.SHARE)
    window._on_layout_edited()
    window._set_edit_mode(False)
    window._set_mapping(True)
    pump(0.2)
    sent.clear()
    QTest.keyPress(window, KEY.Key_F)
    pump(0.15)
    downs = [t for t in touches() if t[0] == protocol.TOUCH_DOWN]
    target = video.target_rect()
    check("after the explicit sharing, F starts all three controls, each with its own finger", len(downs) == 3 and len({d[1] for d in downs}) == 3, str(downs))

    def lit(x: float, y: float) -> bool:
        return near(action_pixel(x, y, -RADIUS + 5, 0), theme.OK)
    check("the F markers stay lit while F is held, although Interact's quick tap is over (gameplay highlight of a shared key)",
          lit(0.80, 0.70) and lit(0.30, 0.30) and overlay.pressed == {"F"},
          f"fire={lit(0.80, 0.70)} interact={lit(0.30, 0.30)} pressed={overlay.pressed}")
    check("Reload (a tap named 'R') is dark again once its tap is over", not lit(0.70, 0.90))
    check("the markers of other controls are not lit", not lit(0.20, 0.80))
    QTest.keyRelease(window, KEY.Key_F)
    pump(0.15)
    check("releasing F lifts every finger and lights nothing", engine.injector.active_count == 0 and not lit(0.80, 0.70))
    sent.clear()
    QTest.keyPress(window, KEY.Key_Q)
    pump(0.4)
    QTest.keyRelease(window, KEY.Key_Q)
    pump(0.1)
    check("the multi-action control runs all of its actions (3 here)", len([t for t in touches() if t[0] == protocol.TOUCH_DOWN]) >= 3,
          str([t for t in touches() if t[0] == protocol.TOUCH_DOWN]))
    window._set_mapping(False)

    # ================================================================================================
    print("7. Markers on the picture: triggers, actions and extra spots are different things")
    p = fresh()
    lean = p.find_by_id("lean")
    pixel_action = action_pixel(0.35, 0.80, -14, -14)                       # the corner of the numbered square
    pixel_also = action_pixel(0.12, 0.62, -14, -14)                         # the same offset around an extra spot
    check("an additional action is a numbered SQUARE, an extra spot is a CIRCLE", pixel_action.alpha() > 0 and pixel_also.alpha() == 0,
          f"{pixel_action.alpha()} / {pixel_also.alpha()}")
    check("the trigger badge '+1' is drawn on the control that has another key", action_pixel(0.20, 0.80, int(RADIUS * 0.45) + 4, RADIUS - 3).alpha() > 0)
    s1 = lean.extra_actions[0]
    s2 = lean.extra_actions[1]
    hit = le.hit_test(p, 0.35, 0.80, *(lambda r: (r.width(), r.height()))(video.target_rect()))
    check("clicking on the square selects THAT action by identity", hit == le.Hit("tap", 3, f"act:{s1.uid}") and hit.binding_id == "lean", str(hit))
    overlay.select(hit)
    pump(0.05)
    check("the selected action has its own colour (not the control's, not the other action's, not the 'held' green)",
          near(action_pixel(0.35, 0.80, -14, 0), theme.SELECT) and not near(action_pixel(0.50, 0.55, -14, 0), theme.SELECT)
          and not near(action_pixel(0.20, 0.80, -RADIUS + 5, 0), theme.SELECT) and not near(action_pixel(0.35, 0.80, -14, 0), theme.OK))
    check("the control the action belongs to is highlighted as the selected control (the table follows the selection)", near(action_pixel(0.20, 0.80, -RADIUS + 5, 0), theme.WARN))
    check("the table follows: the owning control's row is selected", controls.selected_id() == "lean")
    window.profile.bindings.insert(0, Binding("N", 0.9, 0.1, "tap", "New", id="n"))         # every position shifts; nothing is redrawn by the table
    overlay.set_state(window.profile.bindings, False, window.profile.sticks, window.profile.looks)
    pump(0.05)
    check("the selection on the picture stays on the same action after the list shifted (matched by id, not by position)",
          near(action_pixel(0.35, 0.80, -14, 0), theme.SELECT) and not near(action_pixel(0.70, 0.90, -RADIUS + 5, 0), theme.WARN)
          and not near(action_pixel(0.70, 0.90, -RADIUS + 5, 0), theme.SELECT))
    window.profile.remove_by_id("n")
    overlay.set_state(window.profile.bindings, False, window.profile.sticks, window.profile.looks)
    keys_before = (lean.x, lean.y)
    drag(pos(0.35, 0.80), pos(0.40, 0.72))
    check("dragging the square moves only that action (and its own extra spot)", abs(s1.x - 0.40) < 0.01 and abs(s1.y - 0.72) < 0.01
          and (lean.x, lean.y) == keys_before and abs(s1.also[0][0] - 0.50) < 0.02, f"{(s1.x, s1.y)} {s1.also}")
    check("the other action and the other controls did not move", (lean.extra_actions[1].x, lean.extra_actions[1].y) == (0.50, 0.55)
          and (p.find_by_id("reload").x, p.find_by_id("reload").y) == (0.70, 0.90))
    check("the drag was saved, with the action's identity-independent data intact", abs(on_disk().find_by_id("lean").extra_actions[0].x - 0.40) < 0.01
          and on_disk().find_by_id("lean").extra_actions[0].delay_ms == 80)
    drag(pos(0.50, 0.62), pos(0.55, 0.52))                                   # the action's extra spot (it moved along with the action)
    check("dragging an action's extra spot moves just that spot", abs(s1.also[0][0] - 0.55) < 0.02 and abs(s1.also[0][1] - 0.52) < 0.02
          and abs(s1.x - 0.40) < 0.01)
    overlay.select(le.hit_test(p, s1.x, s1.y, *(lambda r: (r.width(), r.height()))(video.target_rect())))
    uid_before = s1.uid
    check("(the selection is the action, not the control)", window._selection.part == f"act:{uid_before}")
    window.profile.bindings.insert(0, Binding("N", 0.9, 0.1, "tap", "New", id="n"))     # positions shift after the selection was made
    window._delete_selected()
    pump(0.05)
    check("Delete removes only the selected ACTION, found by id + uid after the list shifted", ids() == ["n", "fire", "interact", "reload", "lean"]
          and [a.uid for a in p.find_by_id("lean").extra_actions] == [s2.uid], str([a.uid for a in p.find_by_id("lean").extra_actions]))
    window.profile.remove_by_id("n")
    controls.set_profile(window.profile)
    overlay.select(le.hit_test(p, 0.20, 0.80, *(lambda r: (r.width(), r.height()))(video.target_rect())))
    check("selecting the control itself selects the control, not an action", overlay.selection == le.Hit("tap", 3) and window._selection.binding_id == "lean")

    # ================================================================================================
    print("8. Rebinding, swapping and new controls ask before they replace or share")
    p = fresh()
    choice = {"value": "cancel"}
    asked: list = []
    window._ask_key_choice = lambda info, run, can_also=False, **kw: (asked.append((info.trigger, info.choices, can_also, {c: run(c).ok for c in info.choices})), choice["value"])[1]
    before = json.dumps(p.to_dict(), sort_keys=True)
    window._on_rebind(le.Hit("tap", 2), "F")
    check("rebinding Reload to F (used by Fire and Interact) asks, offering replace and share", asked and asked[0][0] == "F" and {"replace", "share"} <= set(asked[0][1]), str(asked))
    check("answering Cancel changes nothing at all", json.dumps(p.to_dict(), sort_keys=True) == before and window._add_state is None)
    choice["value"] = "share"
    window._on_rebind(le.Hit("tap", 2), "F")
    check("answering Share puts Reload on F and authorizes the group", p.find_by_id("reload").input == "F" and p.shared_triggers == {"F": ["fire", "interact", "reload"]})
    check("the old key R is free and nobody else changed", on_disk().find("R") is None and [b.input for b in on_disk().bindings] == ["F", "F", "F", "Q"])
    p = fresh()
    window._on_rebind(le.Hit("tap", 2), "Z")
    check("rebinding to a free key needs no question and shares nothing", p.find_by_id("reload").input == "Z" and not p.shared_triggers)
    p = fresh()
    choice["value"] = "replace"
    window._on_rebind(le.Hit("tap", 2), "E")                                 # E is only an ADDITIONAL key of Lean (the old code never saw it)
    check("a key that is only another control's additional key is seen as taken", asked[-1][0] == "E" and p.find_by_id("lean").triggers == ("Q",)
          and p.find_by_id("reload").input == "E", str(asked[-1]))
    p = fresh()
    window.profile.bindings.insert(0, Binding("N", 0.9, 0.1, "tap", "New", id="n"))
    window._on_rebind(le.Hit("tap", 2, "", "reload"), "Y")                    # pinned to Reload; the stale position 2 is now Interact
    check("a rebind uses the control the hit was pinned to, not the one now at the stale position", [b.input for b in window.profile.bindings] == ["N", "F", "F", "Y", "Q"],
          str([b.input for b in window.profile.bindings]))
    window.profile.remove_by_id("n")

    p = fresh()
    window.profile.bindings[:] = [b for b in window.profile.bindings if b.id != "interact"]
    window.store.save(window.profile)
    controls.set_profile(window.profile)
    choice["value"] = "share"
    controls.addControl.emit("custom")
    QTest.mouseClick(video, Qt.MouseButton.LeftButton, pos=pos(0.55, 0.30))
    QTest.keyClick(window, KEY.Key_F)
    pump(0.1)
    new = window.profile.bindings[-1]
    check("a NEW control on a used key asks too (replace / add spot / share) and Share keeps both", asked[-1][0] == "F" and asked[-1][2] is True
          and new.input == "F" and window.profile.shared_triggers == {"F": ["fire", new.id]} and len(window.profile.bindings) == 4, str(window.profile.shared_triggers))
    check("the new control is selected by id", overlay.selection is not None and overlay.selection.binding_id == new.id)

    # ================================================================================================
    print("9. A change that alters a sharing group asks first; refusing it changes nothing")
    p = fresh()
    be.ProfileEditor(p).share("F", ["fire", "interact"])
    window.store.save(p)
    asked_confirm.clear()
    confirm_answer["value"] = False
    snapshot_before = json.dumps(p.to_dict(), sort_keys=True)
    window._remove_binding("interact")
    check("deleting a member of a sharing group warns first", asked_confirm and "Sharing of F ends" in asked_confirm[0], str(asked_confirm))
    check("answering No deletes nothing and keeps the group", json.dumps(p.to_dict(), sort_keys=True) == snapshot_before and "interact" in ids())
    confirm_answer["value"] = True
    window._remove_binding("interact")
    check("answering Yes deletes it and the sharing group is cleaned up everywhere", ids() == ["fire", "reload", "lean"] and p.shared_triggers == {}
          and "shared_triggers" not in json.loads(file_bytes()))
    p = fresh()
    be.ProfileEditor(p).share("F", ["fire", "interact"])
    asked_confirm.clear()
    window._on_rebind(le.Hit("tap", 1, "", "interact"), "H")
    check("moving a member to another key warns, drops it from the group and does NOT share the new key", asked_confirm and p.shared_triggers == {}
          and not p.is_shared("H") and p.find_by_id("interact").input == "H")
    p = fresh()
    be.ProfileEditor(p).share("F", ["fire", "interact"])
    overlay.select(le.Hit("tap", 0, "", "fire"))
    asked_confirm.clear()
    window._delete_selected()
    check("deleting the selected marker of a member warns too and updates the group", asked_confirm and p.shared_triggers == {} and ids() == ["interact", "reload", "lean"])

    # ================================================================================================
    print("10. Table edits are by id even if the list changed behind the panel's back")
    p = fresh()
    row = controls.row_of("reload")
    window.profile.bindings.insert(0, Binding("N", 0.9, 0.1, "tap", "New", id="n"))      # the table was NOT redrawn
    controls.table.item(row, controls_module.COL_LABEL).setText("Reload!")
    check("a label edit goes to the control the row shows, not the one now at that position", window.profile.find_by_id("reload").label == "Reload!"
          and window.profile.find_by_id("n").label == "New" and window.profile.bindings[row].id != "reload" or window.profile.find_by_id("reload").label == "Reload!")
    mode_box = controls.table.cellWidget(row, controls_module.COL_MODE)
    mode_box.setCurrentIndex(mode_box.findData("hold"))
    check("a mode change goes to the same control", window.profile.find_by_id("reload").mode == "hold" and window.profile.find_by_id("n").mode == "tap")
    spin = controls.table.cellWidget(row, controls_module.COL_X)
    spin.setValue(33.0)
    check("a coordinate edit goes to the same control", abs(window.profile.find_by_id("reload").x - 0.33) < 1e-6 and window.profile.find_by_id("n").x == 0.9)
    window.profile.remove_by_id("reload")                                    # the row's control disappears behind the panel's back
    before = [b.to_dict_v2() for b in window.profile.bindings]
    controls.table.item(row, controls_module.COL_LABEL).setText("ghost")
    spin.setValue(10.0)
    check("an edit of a row whose control is gone changes nothing", [b.to_dict_v2() for b in window.profile.bindings] == before)
    window._toggle_mode(le.Hit("tap", 0, "", "gone"))
    check("a mode toggle on a control that is gone is refused", [b.to_dict_v2() for b in window.profile.bindings] == before and "nothing was deleted" in controls.hint.text())

    # ================================================================================================
    print("11. Version-1 profiles are edited without being converted")
    p = fresh(LEGACY, legacy_profile)
    raw = file_bytes(LEGACY)
    data = json.loads(raw)
    check("the version-1 profile is open and has no stored binding ids", data["version"] == 1 and all("id" not in b for b in data["bindings"]) and window.profile.schema_version == 1)
    open_options(p.bindings[0].id, lambda d: "cancel")
    check("opening the options dialog and cancelling does not touch the file", file_bytes(LEGACY) == raw)
    controls.table.item(0, controls_module.COL_LABEL).setText("Frag")
    data = json.loads(file_bytes(LEGACY))
    check("renaming a control keeps version 1 and does not add ids", data["version"] == 1 and data["bindings"][0]["label"] == "Frag"
          and all("id" not in b for b in data["bindings"]) and not (Path(folder.name) / "backup-before-v2").exists())
    window._on_rebind(le.Hit("tap", 1), "Y")
    data = json.loads(file_bytes(LEGACY))
    check("changing a key to a free one still writes version 1", data["version"] == 1 and data["bindings"][1]["input"] == "Y" and all("id" not in b for b in data["bindings"]))
    check("opening the Options dialog with OK but no change rewrites nothing", (lambda before: (open_options(window.profile.bindings[2].id, lambda d: "ok"), file_bytes(LEGACY) == before)[1])(file_bytes(LEGACY)))

    def script_extra_key(d):
        d.capture_key = lambda: "Shift+M"
        d.add_key()
        return "ok"
    open_options(window.profile.bindings[2].id, script_extra_key)
    data = json.loads(file_bytes(LEGACY))
    backups = list((Path(folder.name) / "backup-before-v2").glob("*.json"))
    check("a feature that needs version 2 (a second key) converts the file, after copying the old one", data["version"] == 2 and data["bindings"][2]["triggers"] == ["M", "Shift+M"]
          and len(backups) == 1 and json.loads(backups[0].read_text(encoding="utf-8"))["version"] == 1, str(backups))
    check("the profile id and the other controls survived the conversion", data.get("id") == "leglegleglegl" and [b["triggers"][0] for b in data["bindings"]] == ["G", "Y", "M"])

    # ================================================================================================
    print("12. Version-2 round trip keeps every field, unknown ones included")
    p = fresh()
    path = window.store.path(NAME)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["future_top"] = {"keep": 1}
    raw["bindings"][3]["future_binding"] = [1, 2]
    raw["bindings"][3]["actions"][1]["future_action"] = "x"
    raw["bindings"][3]["actions"][0]["future_primary"] = True
    be.ProfileEditor(p).share("F", ["fire", "interact"])
    window.store.save(p)
    raw2 = json.loads(path.read_text(encoding="utf-8"))
    raw2["shared_triggers"][0]["future_sharing"] = 5
    raw2["future_top"] = {"keep": 1}
    raw2["bindings"][3]["future_binding"] = [1, 2]
    raw2["bindings"][3]["actions"][1]["future_action"] = "x"
    raw2["bindings"][3]["actions"][0]["future_primary"] = True
    path.write_text(json.dumps(raw2), encoding="utf-8")
    window._refresh_profile_list(NAME)
    p = window.profile
    window._set_edit_mode(True)

    def script_roundtrip(d):
        d.capture_key = lambda: "Z"
        d.add_key()
        d.select_action(be.PRIMARY)
        d.label_edit.setText("Lean!")
        return "ok"
    open_options("lean", script_roundtrip)
    out = json.loads(path.read_text(encoding="utf-8"))
    lean_out = out["bindings"][3]
    check("unknown profile, binding, action and sharing fields are all preserved", out.get("future_top") == {"keep": 1} and lean_out.get("future_binding") == [1, 2]
          and lean_out["actions"][1].get("future_action") == "x" and lean_out["actions"][0].get("future_primary") is True
          and out["shared_triggers"][0].get("future_sharing") == 5, str(lean_out))
    check("the edit is there too", lean_out["label"] == "Lean!" and lean_out["triggers"] == ["Q", "E", "Z"])
    check("ids, order, delays, failure policy and the permanent profile id are preserved", [b["id"] for b in out["bindings"]] == ["fire", "interact", "reload", "lean"]
          and [a.get("delay_ms") for a in lean_out["actions"]] == [None, 80, 200] and lean_out["on_failure"] == "abort" and out["id"] == "advadvadvadv")
    again = on_disk()
    check("loading what was saved gives the same profile", again.to_dict() == p.to_dict() and again.shared_triggers == p.shared_triggers)

    # ================================================================================================
    print("13. Refused and cancelled operations corrupt nothing")
    p = fresh()
    raw = file_bytes()
    before = json.dumps(p.to_dict(), sort_keys=True)
    window._on_rebind(le.Hit("tap", 2), "W")
    window._on_rebind(le.Hit("tap", 2), "F9")
    window.hotkeys["opacity_up"]
    check("a stick key and a window hotkey cannot become a control key", json.dumps(p.to_dict(), sort_keys=True) == before)
    window._remove_binding("nonexistent")
    window._remove_binding("")
    check("deleting an unknown or empty id removes nothing", json.dumps(p.to_dict(), sort_keys=True) == before)
    check("and the file on disk is untouched by all of it", file_bytes() == raw)
    r = be.ProfileEditor(p, window._reserved()).add_trigger("lean", "Esc")
    check("Esc (always reserved) cannot be a trigger", not r.ok and json.dumps(p.to_dict(), sort_keys=True) == before, r.message)

    # ================================================================================================
    print("14. The real key-capture and conflict dialogs")
    cap = KeyCaptureDialog()
    QTest.keyClick(cap, KEY.Key_G)
    check("pressing a key names it", cap.name == "G")
    cap = KeyCaptureDialog()
    QTest.keyClick(cap, KEY.Key_E, Qt.KeyboardModifier.ShiftModifier)
    check("Shift held while pressing a key gives a combination", cap.name == "Shift+E", str(cap.name))
    cap = KeyCaptureDialog()
    QTest.keyClick(cap, KEY.Key_Control)
    check("a modifier pressed and released alone is that modifier", cap.name == "Ctrl", str(cap.name))
    cap = KeyCaptureDialog()
    QTest.mouseClick(cap, Qt.MouseButton.MiddleButton)
    check("a mouse button is named", cap.name == "MMB", str(cap.name))
    cap = KeyCaptureDialog()
    cap.typed.setText("Shift+Ctrl+W")
    cap._use_typed()
    check("a typed name is made canonical", cap.name == "Ctrl+Shift+W", str(cap.name))
    cap = KeyCaptureDialog()
    cap.typed.setText("Ctrl+Shift")
    cap._use_typed()
    check("a nonsense name is refused with a message", cap.name is None and cap.error.text() != "")
    conflict = KeyConflictDialog(None, "F", ["'Fire'"], [("replace", "Replace", "x"), ("share", "Share", "y")])
    conflict.buttons["share"].click()
    check("the conflict dialog returns the clicked choice", conflict.choice == "share")
    conflict = KeyConflictDialog(None, "F", ["'Fire'"], [("replace", "Replace", "x")])
    conflict.buttons["cancel"].click()
    check("Cancel is always there", conflict.choice == "cancel")

    # ================================================================================================
    print("15. The overlay keeps its gameplay behaviour (opacity, highlight) with the new markers")
    p = fresh(edit=False)
    window._set_mapping(True)
    pump(0.2)
    QTest.keyPress(window, KEY.Key_Q)
    pump(0.1)
    check("while a control with several actions is held, its markers are all lit", near(action_pixel(0.20, 0.80, -RADIUS + 5, 0), theme.OK)
          and near(action_pixel(0.35, 0.80, -14, 0), theme.OK))
    QTest.keyRelease(window, KEY.Key_Q)
    pump(0.1)
    window._set_marker_opacity(0, toast=False)
    pump(0.05)
    check("at 0 % opacity no marker is drawn, action squares included", action_pixel(0.35, 0.80, -14, 0).alpha() == 0 and action_pixel(0.20, 0.80, -RADIUS + 5, 0).alpha() == 0)
    window._set_marker_opacity(100, toast=False)
    window._set_mapping(False)
    pump(0.1)
    check("mapping stops cleanly: no finger left down", engine.injector.active_count == 0)

    # ================================================================================================
    print("16. Every setting of an action can be edited, and only the ones that apply are shown")
    p = fresh()
    odd = advanced_profile()
    odd.find_by_id("lean").x = 0.234567
    odd.find_by_id("lean").extra_actions[0].y = 0.345678
    odd.find_by_id("lean").extra_actions[1].x2 = 0.612345
    window._set_edit_mode(False)
    window.store.save(odd)
    window._refresh_profile_list(NAME)
    window._set_edit_mode(True)
    p = window.profile
    odd_before = [(b.x, b.y, [(a.x, a.y, a.x2) for a in b.extra_actions]) for b in p.bindings]
    for ref in (None, "s1", "s2"):
        open_options("lean", lambda d: (d.select_action(p.find_by_id("lean").extra_actions[1].uid) if ref == "s2" else None, "ok")[1])
    check("opening the options of a control and pressing OK does not nudge any coordinate by the rounding of a spin box",
          [(b.x, b.y, [(a.x, a.y, a.x2) for a in b.extra_actions]) for b in p.bindings] == odd_before, str(p.find_by_id("lean").x))
    p = fresh()

    def script_fields(d):
        s2_uid = p.find_by_id("lean").extra_actions[1].uid
        d.select_action(s2_uid)                                       # the swipe
        ed = d.action_editor
        ed.x_spin.setValue(40.0)
        ed.y_spin.setValue(30.0)
        ed.end_x.setValue(75.0)
        ed.end_y.setValue(65.0)
        ed.swipe_ms.setValue(500)
        ed.delay_spin.setValue(1200)
        check("a swipe offers its end point and duration, and hides repeat, zoom and multi-tap",
              not ed.swipe_box.isHidden() and ed.repeat_box.isHidden() and ed.pinch_box.isHidden() and ed.seq_box.isHidden())
        ed.mode_combo.setCurrentIndex(ed.mode_combo.findData("pinch-out"))
        ed.span_spin.setValue(20.0)
        ed.pinch_ms.setValue(300)
        check("a zoom offers finger distance and duration instead", not ed.pinch_box.isHidden() and ed.swipe_box.isHidden())
        ed.mode_combo.setCurrentIndex(ed.mode_combo.findData("sequence"))
        check("a multi-tap offers its table of points and hides the single spot", not ed.seq_box.isHidden() and not ed.form.isRowVisible(ed.where_row))
        ed._add_step_row(0.6, 0.6, 250)
        ed.steps_table.cellWidget(0, 0).setValue(41.0)
        d.add_action()                                                # also flushes the multi-tap into the working copy
        action = d.wbinding.extra_actions[1]
        check("the multi-tap's points were written, the first one is the action's position",
              action.mode == "sequence" and len(action.steps) == 2 and abs(action.steps[0][0] - 0.41) < 1e-6 and (action.x, action.y) == (action.steps[0][0], action.steps[0][1])
              and action.steps[1][2] == 250, str(action.steps))
        d.select_action(s2_uid)
        check("the delay, spot and mode of that action are shown again when it is selected", d.action_editor.delay_spin.value() == 1200
              and d.action_editor.selected_mode() == "sequence")
        return "ok"
    open_options("lean", script_fields)
    s2 = p.find_by_id("lean").extra_actions[1]
    check("OK adopts them: position, delay and mode (the swipe became a multi-tap)", s2.mode == "sequence" and s2.delay_ms == 1200 and len(s2.steps) == 2)
    p = fresh()

    def script_swipe(d):
        s2_uid = p.find_by_id("lean").extra_actions[1].uid
        d.select_action(s2_uid)
        ed = d.action_editor
        ed.x_spin.setValue(40.0)
        ed.end_x.setValue(75.0)
        ed.end_y.setValue(65.0)
        ed.swipe_ms.setValue(500)
        ed.delay_spin.setValue(1200)
        return "ok"
    open_options("lean", script_swipe)
    s2 = p.find_by_id("lean").extra_actions[1]
    check("a swipe's start, end, duration and delay are all written", abs(s2.x - 0.40) < 1e-6 and s2.y == 0.55 and abs(s2.x2 - 0.75) < 1e-6
          and abs(s2.y2 - 0.65) < 1e-6 and s2.duration_ms == 500 and s2.delay_ms == 1200)
    check("a coordinate nobody touched is not nudged by rounding", p.find_by_id("lean").x == 0.2 and p.find_by_id("lean").extra_actions[0].x == 0.35)

    # ================================================================================================
    print("17. Double-click and right-click on an additional action")
    p = fresh()
    s1 = p.find_by_id("lean").extra_actions[0]
    Scripted.script = None
    opened: list = []
    real_open = controls.open_options
    controls.open_options = lambda binding, ref=None: opened.append((binding, ref))
    window._rebind_hit(le.hit_test(p, 0.35, 0.80, *(lambda r: (r.width(), r.height()))(video.target_rect())))
    check("double-clicking an additional action opens ITS options (it does not rebind the control's key)", opened == [("lean", s1.uid)]
          and window._add_state is None and p.find_by_id("lean").input == "Q", str(opened))
    controls.open_options = real_open
    seen: list[str] = []
    import app.ui.main_window as main_window_module
    real_menu = main_window_module.QMenu

    class SpyMenu(real_menu):
        def exec(self, *_a, **_k):
            seen.extend(a.text() for a in self.actions())
            return None
    main_window_module.QMenu = SpyMenu
    try:
        window._show_context_menu(le.hit_test(p, 0.35, 0.80, *(lambda r: (r.width(), r.height()))(video.target_rect())), QPoint(0, 0))
        on_action = list(seen)
        seen.clear()
        window._show_context_menu(le.hit_test(p, 0.20, 0.80, *(lambda r: (r.width(), r.height()))(video.target_rect())), QPoint(0, 0))
        on_control = list(seen)
    finally:
        main_window_module.QMenu = real_menu
    check("the menu of an action offers 'Delete this action' and 'Delete the whole control'; the control's menu just 'Delete'",
          "Delete this action" in on_action and "Delete the whole control" in on_action and "Delete" in on_control and "Delete this action" not in on_control,
          f"{on_action} | {on_control}")
    check("both menus offer to add a key and an action", all(any(t.startswith(prefix) for t in menu) for menu in (on_action, on_control)
                                                             for prefix in ("Add another key", "Add an action")))
    window._add_action_to("reload")
    check("'Add an action' gives the control a new action and selects it", len(p.find_by_id("reload").extra_actions) == 1
          and window._selection is not None and window._selection.part == f"act:{p.find_by_id('reload').extra_actions[0].uid}")
    window._add_key_to("reload")
    check("'Add another key' waits for a key press", window._add_state == "capture")
    QTest.keyClick(window, KEY.Key_K)
    check("pressing it adds the key to THAT control, as an additional key", p.find_by_id("reload").triggers == ("R", "K") and window._add_state is None)

    # ================================================================================================
    print("18. Selecting a control shows all its keys on the picture")
    overlay.select(le.Hit("tap", 3, "", "lean"))
    pump(0.05)
    below = pos(0.20, 0.80)
    probe = layer().pixelColor(below.x(), below.y() + RADIUS + 23)
    check("a box listing every key of the selected control is drawn under its marker", probe.alpha() > 0, str(probe.alpha()))
    overlay.select(le.Hit("tap", 2, "", "reload"))
    pump(0.05)
    below = pos(0.70, 0.90)
    check("a control with extra keys shows it too (Reload now has R and K)", layer().pixelColor(below.x(), below.y() + RADIUS + 23).alpha() > 0)
    overlay.select(None)
    pump(0.05)
    below = pos(0.20, 0.80)
    check("nothing is drawn there when nothing is selected", layer().pixelColor(below.x(), below.y() + RADIUS + 23).alpha() == 0)

    # ================================================================================================
    print("19. Opening a profile never rewrites it; stick keys see additional keys")
    window._set_edit_mode(False)
    window.store.save(legacy_profile())
    legacy_before = file_bytes(LEGACY)
    window.store.save(advanced_profile())
    advanced_before = file_bytes()
    for name in (LEGACY, NAME, LEGACY, NAME):
        window._choose_profile(name)
        pump(0.05)
    check("choosing a profile (version 1 or 2) leaves its file byte for byte as it was", file_bytes(LEGACY) == legacy_before and file_bytes() == advanced_before)
    window._set_edit_mode(True)
    p = window.profile
    window._rebind_stick_key("up")
    window._on_stick_key("E")                                         # E is Lean's ADDITIONAL key
    check("a stick key cannot take a key that is only another control's additional key", p.sticks[0].keys["up"] == "W" and "another control" in controls.hint.text(),
          controls.hint.text())

    # ================================================================================================
    print("20. Pick up items: one key taps the first loot row several times")
    import app.ui.main_window as main_window_module
    p = fresh()
    real_get_int = main_window_module.QInputDialog.getInt

    def ask(answer):
        main_window_module.QInputDialog.getInt = staticmethod(lambda *a, **k: answer)
    try:
        ask((3, False))
        controls.addControl.emit("pickup")
        check("cancelling the question starts nothing", window._add_state is None and window._pickup_count == 0)
        ask((4, True))
        controls.addControl.emit("pickup")
    finally:
        main_window_module.QInputDialog.getInt = real_get_int
    check("it asks for ONE click, on the first row of the loot list", window._add_state == "place" and window._pickup_count == 4
          and "FIRST row" in overlay.banner, overlay.banner)
    QTest.mouseClick(video, Qt.MouseButton.LeftButton, pos=pos(0.84, 0.30))
    check("one click is enough, then it waits for the key", window._add_state == "key" and "2/2" in overlay.banner, overlay.banner)
    QTest.keyClick(window, KEY.Key_G)
    pick = p.find("G")
    check("the key became a 4-tap multi-tap on that one spot", pick is not None and pick.mode == "sequence" and len(pick.steps) == 4
          and len({(round(x, 3), round(y, 3)) for x, y, _ in pick.steps}) == 1 and abs(pick.x - 0.84) < 0.01 and abs(pick.y - 0.30) < 0.01
          and pick.label == "Pick up x4", str(pick))
    check("the flow ended cleanly and the control was saved", window._add_state is None and window._pickup_count == 0
          and on_disk().find("G") is not None and len(on_disk().find("G").steps) == 4)
    check("it appears in the controls table like any other key", controls.row_of(pick.id) >= 0)

    # ================================================================================================
    print("21. Smart pick-up: one key opens the loot list only if it is closed, then takes the items")
    import numpy as np
    from app.video.frame import YuvFrame

    def loot_frame(list_open: bool) -> YuvFrame:
        """A picture of the phone: grey ground, and (list_open) three yellow loot rows on the right, like PUBG Mobile's."""
        w, h = 1920, 864
        rgb = np.full((h, w, 3), (170, 160, 150), np.float32)
        if list_open:
            for index in range(3):
                y0 = int((0.308 + index * 0.097) * h)
                rgb[y0:y0 + int(0.088 * h), int(0.626 * w):int(0.755 * w)] = (232, 196, 40)
        r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        y = 0.2126 * r + 0.7152 * g + 0.0722 * b
        yy = np.clip(16 + y * 219 / 255, 0, 255).astype(np.uint8)
        u = np.clip(128 + (b - y) / 1.8556 * 224 / 255, 0, 255).astype(np.uint8)[::2, ::2]
        v = np.clip(128 + (r - y) / 1.5748 * 224 / 255, 0, 255).astype(np.uint8)[::2, ::2]
        return YuvFrame(w, h, yy.tobytes(), u.tobytes(), v.tobytes())

    def show(list_open: bool) -> None:
        video.set_frame(loot_frame(list_open), time.perf_counter())

    real_get_text = main_window_module.QInputDialog.getText

    def ask_text(answer):
        main_window_module.QInputDialog.getText = staticmethod(lambda *a, **k: answer)

    p = fresh()
    show(True)
    ask_text(("", True))
    ask((4, False))
    controls.addControl.emit("loot")
    check("cancelling the question starts nothing", window._add_state is None and window._loot_stage == 0)
    ask((3, True))
    controls.addControl.emit("loot")
    check("it asks for the hand button first", window._loot_stage == 1 and "hand" in overlay.banner and "ON THE PHONE" in overlay.banner, overlay.banner)
    QTest.mouseClick(video, Qt.MouseButton.LeftButton, pos=pos(0.742, 0.255))
    check("then for the first row, with the list open", window._loot_stage == 2 and "FIRST row" in overlay.banner, overlay.banner)
    show(False)
    QTest.mouseClick(video, Qt.MouseButton.LeftButton, pos=pos(0.69, 0.352))
    check("a click while the list is NOT on the picture is refused and explained (nothing is guessed)", window._loot_stage == 2
          and "cannot see the yellow loot list" in controls.hint.text() and not p.smart_loot, controls.hint.text())
    show(True)
    QTest.mouseClick(video, Qt.MouseButton.LeftButton, pos=pos(0.69, 0.352))
    check("with the list open the click is accepted and it waits for the key", window._loot_stage == 3 and window._add_state == "key")
    QTest.keyClick(window, KEY.Key_W)
    check("a movement-stick key is refused and the set-up ends without saving", window._add_state is None and not p.smart_loot
          and "movement-stick" in controls.hint.text(), controls.hint.text())
    ask_text(("W", True))
    controls.addControl.emit("loot")
    check("a pick key that is a movement-stick key is refused before anything starts", window._loot_stage == 0 and window._add_state is None
          and "movement-stick" in controls.hint.text(), controls.hint.text())
    ask_text(("H H", True))
    controls.addControl.emit("loot")
    check("the same pick key twice is refused", window._loot_stage == 0 and "twice" in controls.hint.text(), controls.hint.text())
    ask_text(("H K", True))
    controls.addControl.emit("loot")
    QTest.mouseClick(video, Qt.MouseButton.LeftButton, pos=pos(0.742, 0.255))
    QTest.mouseClick(video, Qt.MouseButton.LeftButton, pos=pos(0.69, 0.352))
    QTest.keyClick(window, KEY.Key_H)
    check("the open key cannot also be a pick key", window._add_state is None and not p.smart_loot and "pick keys" in controls.hint.text(),
          controls.hint.text())
    controls.addControl.emit("loot")
    QTest.mouseClick(video, Qt.MouseButton.LeftButton, pos=pos(0.742, 0.255))
    QTest.mouseClick(video, Qt.MouseButton.LeftButton, pos=pos(0.69, 0.352))
    QTest.keyClick(window, KEY.Key_G)
    saved = on_disk().smart_loot
    check("the key was saved with the profile: button, first row, count and a threshold measured from the picture",
          p.smart_loot is not None and saved == p.smart_loot and saved["key"] == "G" and saved["count"] == 3 and saved["pick_keys"] == ["H", "K"]
          and abs(saved["toggle"][0] - 0.742) < 0.01 and abs(saved["row"][1] - 0.352) < 0.01 and 0.3 < saved["threshold"] <= 0.45, str(saved))
    check("the router listens to it, and to the pick keys", {"G", "H", "K"} <= set(window.router.direct_hotkeys) and window._add_state is None)
    window._set_edit_mode(False)
    check("the checks have nothing to say about it", not [i for i in p.analyze() if i.code.startswith("smart_loot")])
    toggle_px = (0.742 * 1920, 0.255 * 864)
    row_px = (0.69 * 1920, 0.352 * 864)

    def downs() -> list[tuple]:
        return [t for t in touches() if t[0] == protocol.TOUCH_DOWN]

    def near_px(t, px, tolerance=3):
        return abs(t[2] - px[0]) <= tolerance and abs(t[3] - px[1]) <= tolerance

    window._set_mapping(True)
    pump(0.2)
    show(False)
    sent.clear()
    QTest.keyClick(window, KEY.Key_G)
    pump(0.1)
    show(True)                                                           # the list appears after the tap on the hand
    pump(0.9)
    d = downs()
    check("list closed: it taps the hand, then (the list being there) takes 3 items from the first row", len(d) == 4 and near_px(d[0], toggle_px)
          and all(near_px(t, row_px) for t in d[1:]), str(d))
    check("every finger was lifted again", engine.injector.active_count == 0)
    show(True)
    sent.clear()
    QTest.keyClick(window, KEY.Key_G)
    pump(0.9)
    d = downs()
    check("list already open: it does NOT tap the hand (that would close it) and takes 3 items", len(d) == 3 and all(near_px(t, row_px) for t in d), str(d))
    show(False)
    sent.clear()
    QTest.keyClick(window, KEY.Key_G)
    pump(0.9)
    d = downs()
    check("list closed and nothing appears (nothing nearby): only the hand is tapped, no blind taps on the rows", len(d) == 1 and near_px(d[0], toggle_px), str(d))
    show(False)
    sent.clear()
    QTest.keyClick(window, KEY.Key_G)
    pump(0.1)
    window._set_mapping(False)                                           # Esc / stop while it waits for the list
    show(True)
    pump(0.9)
    check("stopping the mapping while it waits cancels the rest: no row is ever tapped", len(downs()) == 1 and engine.injector.active_count == 0, str(downs()))
    window._set_mapping(True)
    pump(0.2)
    show(True)
    sent.clear()
    QTest.keyClick(window, KEY.Key_G)
    pump(0.1)
    window.router.window = type("A", (), {"isActiveWindow": staticmethod(lambda: True)})()
    engine.release_all()                                                 # what focus loss / rotation / disconnect do
    pump(0.9)
    check("focus loss (release_all) midway lifts the finger and drops the taps that had not happened yet", len(downs()) < 3 and engine.injector.active_count == 0,
          str(downs()))
    # ---- pick keys: H = 1st row, K = 2nd row, only while the list is open --------------------------------------------
    window._set_mapping(True)
    pump(0.2)
    row2_px = (0.69 * 1920, (0.352 + 0.098) * 864)
    show(True)
    sent.clear()
    QTest.keyClick(window, KEY.Key_H)
    pump(0.2)
    d = downs()
    check("H with the list open taps the 1st row once", len(d) == 1 and near_px(d[0], row_px), str(d))
    sent.clear()
    QTest.keyClick(window, KEY.Key_K)
    pump(0.2)
    d = downs()
    check("K taps the 2nd row once", len(d) == 1 and near_px(d[0], row2_px), str(d))
    show(False)
    sent.clear()
    QTest.keyClick(window, KEY.Key_K)
    pump(0.2)
    check("with the list CLOSED a pick key taps nothing (it would hit the game world) and says why", not downs()
          and "closed" in overlay._toast, f"{downs()} / {overlay._toast}")

    # ---- the wheel scrolls the open list; closed, it keeps its old job ------------------------------------------------
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent

    def wheel(delta: int) -> None:
        here = QPointF(pos(0.5, 0.5))
        event = QWheelEvent(here, here, QPoint(0, 0), QPoint(0, delta), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        app.sendEvent(video, event)

    show(True)
    pump(0.3)
    sent.clear()
    wheel(-120)
    wheel(-120)                                                          # a second notch straight away: only one finger at a time
    pump(0.5)
    t = touches()
    downs_now = [x for x in t if x[0] == protocol.TOUCH_DOWN]
    moves = [x for x in t if x[0] == protocol.TOUCH_MOVE]
    check("wheel DOWN with the list open: ONE finger goes down on the list, drags UPWARDS and lifts",
          len(downs_now) == 1 and len(moves) >= 5 and t[-1][0] == protocol.TOUCH_UP and moves[-1][3] < downs_now[0][3] - 100
          and abs(downs_now[0][2] - row_px[0]) <= 3 and engine.injector.active_count == 0, str((len(downs_now), len(moves), t[:1], moves[-1:])))
    check("... from about two rows below the first row up to the first row", abs(downs_now[0][3] - (0.352 + 0.196) * 864) <= 3
          and abs(moves[-1][3] - 0.352 * 864) <= 3, f"{downs_now[0]} {moves[-1]}")
    pump(0.2)
    sent.clear()
    wheel(120)
    pump(0.5)
    t = touches()
    moves = [x for x in t if x[0] == protocol.TOUCH_MOVE]
    check("wheel UP drags the other way (downwards)", moves and moves[-1][3] > [x for x in t if x[0] == protocol.TOUCH_DOWN][0][3] + 100, str(t[:1]))
    show(False)
    pump(0.3)
    sent.clear()
    wheel(-120)
    pump(0.3)
    check("with the list closed the wheel does NOT scroll anything (a weapon-switch wheel binding would still work)",
          not [x for x in touches() if x[0] == protocol.TOUCH_MOVE])
    window._set_mapping(False)
    main_window_module.QInputDialog.getText = real_get_text

    window._set_mapping(False)
    window._set_edit_mode(True)
    controls.addControl.emit("loot-remove")
    check("it can be removed again", p.smart_loot is None and on_disk().smart_loot is None and "G" not in window.router.direct_hotkeys)

    # ---- put everything back -------------------------------------------------------------------------
    window._set_edit_mode(False)
    window.hotkeys.update(saved_hotkeys)
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
