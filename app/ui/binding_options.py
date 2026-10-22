"""Dialog with every option of one control: its name, the KEYS that start it, its ACTIONS (what each does, where, after how long), what
happens when an action cannot start, which keys it SHARES with other controls, and what the profile checks say about it.

Opened with only a binding it is the original single-action dialog (what it does, and the settings of that mode). Opened with the
profile (`profile=`) it also edits triggers, additional actions and sharing. In that mode every change is made on a COPY of the
profile, step by step, through the same Qt-free operations the rest of the editor uses (app/mapping/binding_edit.py); OK adopts the copy
under the editor's checks (and asks before it changes who takes part in a sharing group), Cancel throws it away. So a refused, cancelled
or closed dialog can never leave the profile half-edited."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
                               QSpinBox, QTabWidget, QTableWidget, QVBoxLayout, QWidget)

from ..mapping import binding_edit as be
from ..mapping.binding_edit import PRIMARY, ProfileEditor
from ..mapping.layout_edit import DEFAULT_STEP_DELAY_MS
from ..mapping.profile import (ALSO_MODES, MAX_DELAY_MS, MODE_HELP, MODE_SHORT, MODES, ON_FAILURE, PINCH_MODES, TWO_POINT_MODES, Binding,
                               Profile, ProfileError)
from .key_capture import KeyCaptureDialog
from .key_conflict import KeyConflictDialog, conflict_options

ON_FAILURE_TEXT = {
    "continue": "Keep going: the other actions still run",
    "abort": "Stop: the control's other actions are cancelled",
}


def _percent(value: float = 0.0) -> QDoubleSpinBox:
    spin = QDoubleSpinBox(minimum=0, maximum=100, decimals=1, singleStep=0.5, suffix=" %")
    spin.setValue(value * 100)
    return spin


def ask_key_choice(parent, info, run, *, can_also: bool = False, title: str = "This key is already used") -> str:
    """Ask what to do about a key another control uses. `run(choice)` dry-runs a choice (binding_edit.Result); only choices that would
    work are offered, each explained with what the dry run says it would do. Returns replace / share / swap / also / cancel."""
    previews = {c: run(c) for c in info.choices}
    usable = tuple(c for c in info.choices if previews[c].ok)
    options = conflict_options(info.trigger, list(info.labels), usable, previews, can_also)
    if not options:
        return be.CANCEL
    dialog = KeyConflictDialog(parent, info.trigger, list(info.labels), options, title)
    dialog.exec()
    return dialog.choice


class ActionEditor(QWidget):
    """The settings of ONE action: what it does (mode), where, and the options of that mode; for an additional action also its delay.
    It edits the binding itself (the first action) or an Action alike: both carry the same field names. Settings that do not apply to
    the chosen mode are hidden, not shown disabled."""
    changed = Signal()                  # the mode or the delay was changed by the user (the action list shows both)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.spec = None
        self.first = True
        self._loaded: dict = {}

        self.mode_combo = QComboBox()
        for mode in MODES:
            self.mode_combo.addItem(MODE_SHORT[mode].capitalize(), mode)
        self.help_label = QLabel("")
        self.help_label.setWordWrap(True)
        self.help_label.setObjectName("muted")
        self.x_spin = _percent()
        self.y_spin = _percent()
        self.delay_spin = QSpinBox(minimum=0, maximum=MAX_DELAY_MS, singleStep=50, suffix=" ms")
        self.delay_spin.setToolTip("How long after the key is pressed this action starts. The first action always starts at once.")
        self.scope_check = QCheckBox("This is the scope / aim button (mouse sensitivity drops while it is on)")
        self.scope_check.setToolTip("Set 'While scoped in' under Settings > Mouse and walking to choose how much slower.")
        self.also_label = QLabel("")
        self.also_label.setObjectName("muted")
        self.also_label.setWordWrap(True)

        # repeat -----------------------------------------------------------------------------
        self.rate_spin = QDoubleSpinBox(minimum=1, maximum=30, decimals=0, suffix=" taps / second")
        self.repeat_box = QGroupBox("Repeat tap")
        QFormLayout(self.repeat_box).addRow("Speed", self.rate_spin)
        self.repeat_note = QLabel("Online games may treat auto-clicking as cheating. Use it where that is allowed.")
        self.repeat_note.setObjectName("muted")
        self.repeat_note.setWordWrap(True)
        self.repeat_box.layout().addRow(self.repeat_note)

        # swipe / drag -----------------------------------------------------------------------------
        self.end_x = _percent()
        self.end_y = _percent()
        self.swipe_ms = QSpinBox(minimum=40, maximum=3000, singleStep=20, suffix=" ms")
        self.swipe_box = QGroupBox("Swipe / drag")
        sw = QFormLayout(self.swipe_box)
        sw.addRow("Ends at X", self.end_x)
        sw.addRow("Ends at Y", self.end_y)
        sw.addRow("Takes", self.swipe_ms)

        # pinch ------------------------------------------------------------------------------------
        self.span_spin = _percent()
        self.span_spin.setMaximum(40)
        self.pinch_ms = QSpinBox(minimum=40, maximum=3000, singleStep=20, suffix=" ms")
        self.pinch_box = QGroupBox("Zoom (pinch)")
        pb = QFormLayout(self.pinch_box)
        pb.addRow("Finger distance", self.span_spin)
        pb.addRow("Takes", self.pinch_ms)

        # multi-tap ------------------------------------------------------------------------------------
        self.steps_table = QTableWidget(0, 3)
        self.steps_table.setHorizontalHeaderLabels(["X", "Y", "Wait before (ms)"])
        self.steps_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.steps_table.setMinimumHeight(140)
        self.steps_table.horizontalHeader().setStretchLastSection(True)
        add = QPushButton("Add point")
        add.clicked.connect(lambda: self._add_step_row(0.5, 0.5, DEFAULT_STEP_DELAY_MS))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_step_row)
        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        self.seq_box = QGroupBox("Multi-tap (touches each point in order)")
        sb = QVBoxLayout(self.seq_box)
        sb.addWidget(self.steps_table)
        sb.addLayout(buttons)

        self.form = QFormLayout()
        self.form.addRow("What it does", self.mode_combo)
        self.where_row = QWidget()
        wl = QHBoxLayout(self.where_row)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(QLabel("X"))
        wl.addWidget(self.x_spin)
        wl.addWidget(QLabel("Y"))
        wl.addWidget(self.y_spin)
        self.form.addRow("Where", self.where_row)
        self.form.addRow("Starts after", self.delay_spin)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addLayout(self.form)
        root.addWidget(self.help_label)
        root.addWidget(self.scope_check)
        root.addWidget(self.also_label)
        for box in (self.repeat_box, self.swipe_box, self.pinch_box, self.seq_box):
            root.addWidget(box)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.mode_combo.currentIndexChanged.connect(lambda _=0: self.changed.emit())
        self.delay_spin.valueChanged.connect(lambda _=0: self.changed.emit())

    # -- helpers -----------------------------------------------------------------------------
    def selected_mode(self) -> str:
        return self.mode_combo.currentData()

    def _add_step_row(self, x: float, y: float, delay: int) -> None:
        row = self.steps_table.rowCount()
        self.steps_table.insertRow(row)
        for col, value in ((0, x), (1, y)):
            self.steps_table.setCellWidget(row, col, _percent(value))
        wait = QSpinBox(minimum=0, maximum=5000, singleStep=50)
        wait.setValue(delay)
        wait.setEnabled(row > 0)
        self.steps_table.setCellWidget(row, 2, wait)

    def _remove_step_row(self) -> None:
        row = self.steps_table.currentRow()
        if self.steps_table.rowCount() > 1 and row >= 0:
            self.steps_table.removeRow(row)
            first_wait = self.steps_table.cellWidget(0, 2)
            first_wait.setValue(0)
            first_wait.setEnabled(False)

    def _mode_changed(self) -> None:
        mode = self.selected_mode()
        if mode is None:
            return
        self.help_label.setText(MODE_HELP[mode])
        self.repeat_box.setVisible(mode == "repeat")
        self.swipe_box.setVisible(mode in TWO_POINT_MODES)
        self.pinch_box.setVisible(mode in PINCH_MODES)
        self.seq_box.setVisible(mode == "sequence")
        self.form.setRowVisible(self.where_row, mode != "sequence")          # a multi-tap's first point IS its position
        count = len(self.spec.also) if self.spec is not None else 0
        self.also_label.setVisible(mode in ALSO_MODES)
        self.also_label.setText(f"Extra touch spots: {count}. They use this action's settings (they are not separate actions); "
                                "add or drag them on the picture.")
        if self.spec is not None:
            window = self.window()
            if isinstance(window, QDialog):
                window.adjustSize()

    # -- loading and writing -------------------------------------------------------------------
    def load(self, spec, first: bool) -> None:
        """Show this action. `spec` is the Binding (first action) or an Action."""
        self.spec, self.first = spec, first
        widgets = (self.mode_combo, self.x_spin, self.y_spin, self.delay_spin, self.scope_check, self.rate_spin, self.end_x, self.end_y,
                   self.swipe_ms, self.span_spin, self.pinch_ms)
        for widget in widgets:
            widget.blockSignals(True)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(spec.mode))
        self.x_spin.setValue(spec.x * 100)
        self.y_spin.setValue(spec.y * 100)
        self.delay_spin.setValue(0 if first else spec.delay_ms)
        self.scope_check.setChecked(spec.scope)
        self.rate_spin.setValue(spec.rate_hz)
        self.end_x.setValue((spec.x2 if spec.x2 is not None else spec.x + 0.15) * 100)
        self.end_y.setValue((spec.y2 if spec.y2 is not None else spec.y) * 100)
        self.swipe_ms.setValue(spec.duration_ms)
        self.span_spin.setValue(spec.span * 100)
        self.pinch_ms.setValue(spec.duration_ms)
        for widget in widgets:
            widget.blockSignals(False)
        self.steps_table.setRowCount(0)
        for sx, sy, delay in (spec.steps or [(spec.x, spec.y, 0)]):
            self._add_step_row(sx, sy, delay)
        self._loaded = {w: w.value() for w in (self.x_spin, self.y_spin, self.end_x, self.end_y, self.span_spin)}
        self.form.setRowVisible(self.delay_spin, not first)
        self.scope_check.setVisible(True)
        self._mode_changed()

    def _moved(self, spin: QDoubleSpinBox) -> bool:
        """Did the user change this value? (An untouched one is never written back, so opening and closing the dialog cannot nudge a
        stored coordinate by the rounding of a spin box.)"""
        return abs(spin.value() - self._loaded.get(spin, spin.value())) > 1e-9

    def apply(self) -> None:
        """Write the values into the action this editor shows."""
        spec = self.spec
        if spec is None:
            return
        spec.scope = self.scope_check.isChecked()
        mode = self.selected_mode()
        spec.set_mode(mode)
        if self._moved(self.x_spin):
            spec.x = self.x_spin.value() / 100
        if self._moved(self.y_spin):
            spec.y = self.y_spin.value() / 100
        if not self.first:
            spec.delay_ms = self.delay_spin.value()
        spec.rate_hz = self.rate_spin.value()
        if mode in TWO_POINT_MODES:
            if self._moved(self.end_x) or spec.x2 is None:
                spec.x2 = self.end_x.value() / 100
            if self._moved(self.end_y) or spec.y2 is None:
                spec.y2 = self.end_y.value() / 100
            spec.duration_ms = self.swipe_ms.value()
        if mode in PINCH_MODES:
            if self._moved(self.span_spin):
                spec.span = self.span_spin.value() / 100
            spec.duration_ms = self.pinch_ms.value()
        if mode == "sequence":
            steps = []
            for row in range(self.steps_table.rowCount()):
                steps.append((self.steps_table.cellWidget(row, 0).value() / 100, self.steps_table.cellWidget(row, 1).value() / 100,
                              0 if row == 0 else self.steps_table.cellWidget(row, 2).value()))
            if steps:
                spec.steps = steps
                spec.x, spec.y = steps[0][0], steps[0][1]
        spec.__post_init__()                                  # the model's own validation / clamping
        self._loaded = {w: w.value() for w in (self.x_spin, self.y_spin, self.end_x, self.end_y, self.span_spin)}


class BindingOptionsDialog(QDialog):
    def __init__(self, binding: Binding, parent=None, *, profile: Profile | None = None, reserved=None,
                 start_action: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Options for '{binding.input}'")
        self.setMinimumWidth(500)
        self._binding = binding
        self.profile = profile
        self.binding_id = binding.id
        self.advanced = profile is not None
        # hooks the window / tests can replace --------------------------------------------------------------
        self.capture_key = self._default_capture_key          # () -> a canonical key name, or None if cancelled
        self.choose_conflict = self._default_choose_conflict  # (info, run) -> replace / share / swap / cancel
        self.confirm_groups = self._default_confirm_groups    # (list of sentences) -> bool

        self.label_edit = QLineEdit(binding.label)
        self.label_edit.setPlaceholderText("Name shown on the screen, e.g. Reload")
        self.action_editor = ActionEditor()
        ed = self.action_editor
        # the widgets of the first action stay reachable under their old names
        for name in ("mode_combo", "help_label", "scope_check", "rate_spin", "repeat_box", "swipe_box", "pinch_box", "seq_box", "end_x",
                     "end_y", "swipe_ms", "span_spin", "pinch_ms", "steps_table", "x_spin", "y_spin", "delay_spin", "also_label"):
            setattr(self, name, getattr(ed, name))

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("tip")
        self.error_label.setVisible(False)

        root = QVBoxLayout(self)
        name_form = QFormLayout()
        name_form.addRow("Name", self.label_edit)
        root.addLayout(name_form)
        if not self.advanced:
            ed.load(binding, True)
            root.addWidget(ed)
            root.addWidget(self.buttons)
            return

        # ---- advanced: a working copy of the profile ------------------------------------------------------
        self.reserved = reserved
        self.working = copy.deepcopy(profile)
        self.wbinding = self.working.find_by_id(binding.id)
        if self.wbinding is None:
            raise ProfileError("That control is no longer in the profile")
        self.editor = ProfileEditor(self.working, reserved)
        self.real_editor = ProfileEditor(profile, reserved)
        self._ref = PRIMARY
        self.failure_combo = QComboBox()
        for value in ON_FAILURE:
            self.failure_combo.addItem(ON_FAILURE_TEXT[value], value)
        self.failure_combo.setCurrentIndex(self.failure_combo.findData(self.wbinding.on_failure))
        self.failure_combo.setToolTip("What happens when one of this control's actions cannot start (for example when the phone has no\n"
                                      "free finger left).")
        name_form.addRow("If an action cannot start", self.failure_combo)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_keys_tab(), "Keys")
        self.tabs.addTab(self._build_actions_tab(), "Actions")
        self.tabs.addTab(self._build_checks_tab(), "Checks")
        root.addWidget(self.tabs)
        root.addWidget(self.error_label)
        root.addWidget(self.buttons)
        self.setMinimumWidth(560)
        self._refresh_keys()
        self._refresh_actions(PRIMARY)
        self._refresh_checks()
        if start_action:                                       # opened from an action's marker: start on that action
            self.tabs.setCurrentIndex(1)
            self.select_action(start_action)

    # ===================================================================== default hooks
    def _default_capture_key(self) -> str | None:
        dialog = KeyCaptureDialog(self)
        return dialog.name if dialog.exec() == QDialog.DialogCode.Accepted else None

    def _default_choose_conflict(self, info, run) -> str:
        return ask_key_choice(self, info, run)

    def _default_confirm_groups(self, changes: list) -> bool:
        answer = QMessageBox.question(self, "Sharing will change", "\n\n".join(changes) + "\n\nContinue?")
        return answer == QMessageBox.StandardButton.Yes

    # ===================================================================== building the tabs
    def _build_keys_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel("Any of these keys starts this control. Adding a key does not add an action.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        self.trigger_list = QListWidget()
        self.trigger_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.trigger_list.setMinimumHeight(110)
        self.trigger_list.currentItemChanged.connect(lambda *_: self._update_key_buttons())
        self.keys_detail = QLabel("")
        self.keys_detail.setWordWrap(True)
        self.keys_detail.setObjectName("tip")
        self.add_key_btn = QPushButton("Add a key...")
        self.change_key_btn = QPushButton("Change...")
        self.first_key_btn = QPushButton("Make first")
        self.first_key_btn.setToolTip("The first key names this control's marker on the picture.")
        self.remove_key_btn = QPushButton("Remove")
        self.share_btn = QPushButton("Share this key...")
        self.share_btn.setToolTip("Let every control that uses this key run when it is pressed (saved in the profile).")
        self.unshare_btn = QPushButton("Stop sharing")
        self.add_key_btn.clicked.connect(self.add_key)
        self.change_key_btn.clicked.connect(self.change_key)
        self.first_key_btn.clicked.connect(self.make_first)
        self.remove_key_btn.clicked.connect(self.remove_key)
        self.share_btn.clicked.connect(self.share_key)
        self.unshare_btn.clicked.connect(self.stop_sharing)
        row1, row2 = QHBoxLayout(), QHBoxLayout()
        for button in (self.add_key_btn, self.change_key_btn, self.first_key_btn, self.remove_key_btn):
            row1.addWidget(button)
        for button in (self.share_btn, self.unshare_btn):
            row2.addWidget(button)
        row2.addStretch(1)
        for item in (note, self.trigger_list, self.keys_detail):
            layout.addWidget(item)
        layout.addLayout(row1)
        layout.addLayout(row2)
        return page

    def _build_actions_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel("Each action has its own mode, spot and delay. The first starts at once; the others start after their delay.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        self.action_list = QListWidget()
        self.action_list.setMinimumHeight(96)
        self.action_list.setMaximumHeight(140)
        self.action_list.currentItemChanged.connect(self._on_action_selected)
        self.action_editor.changed.connect(self._live_action_update)
        self.add_action_btn = QPushButton("Add an action")
        self.duplicate_action_btn = QPushButton("Duplicate")
        self.remove_action_btn = QPushButton("Remove")
        self.add_action_btn.clicked.connect(self.add_action)
        self.duplicate_action_btn.clicked.connect(self.duplicate_action)
        self.remove_action_btn.clicked.connect(self.remove_action)
        row = QHBoxLayout()
        for button in (self.add_action_btn, self.duplicate_action_btn, self.remove_action_btn):
            row.addWidget(button)
        layout.addWidget(note)
        layout.addWidget(self.action_list)
        layout.addLayout(row)
        box = QGroupBox("Selected action")
        QVBoxLayout(box).addWidget(self.action_editor)
        layout.addWidget(box)
        return page

    def _build_checks_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel("What the profile checks say about this control, and what your changes would do to sharing.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        self.checks = QListWidget()
        self.checks.setWordWrap(True)
        self.checks.setMinimumHeight(160)
        layout.addWidget(note)
        layout.addWidget(self.checks)
        return page

    # ===================================================================== what is shown
    def _keys(self) -> list[str]:
        return be.ordered_triggers(self.wbinding)

    def selected_key(self) -> str | None:
        item = self.trigger_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _info_for(self, trigger: str):
        return next((i for i in self.editor.sharing_info(self.binding_id) if i.trigger == trigger), None)

    def _key_text(self, index: int, trigger: str, info) -> str:
        text = f"{'★ ' if index == 0 else ''}{trigger}"
        if info is None or info.state == "private":
            return text
        others = [words for member_id, words in info.members if member_id != self.binding_id]
        if info.state == "shared":
            return f"{text}   -   shared with {', '.join(others)}"
        return f"{text}   -   also used by {', '.join(others)}, NOT shared" + ("" if info.runs else " (does nothing here)")

    def _refresh_keys(self, keep: str | None = None) -> None:
        keep = keep or self.selected_key()
        infos = {i.trigger: i for i in self.editor.sharing_info(self.binding_id)}
        self.trigger_list.blockSignals(True)
        self.trigger_list.clear()
        for index, trigger in enumerate(self._keys()):
            item = QListWidgetItem(self._key_text(index, trigger, infos.get(trigger)))
            item.setData(Qt.ItemDataRole.UserRole, trigger)
            self.trigger_list.addItem(item)
            if trigger == keep:
                self.trigger_list.setCurrentItem(item)
        if self.trigger_list.currentItem() is None and self.trigger_list.count():
            self.trigger_list.setCurrentRow(0)
        self.trigger_list.blockSignals(False)
        self._update_key_buttons()
        first = self._keys()[0] if self._keys() else self.wbinding.input
        self.setWindowTitle(f"Options for '{first}'")

    def _update_key_buttons(self) -> None:
        trigger = self.selected_key()
        count = self.trigger_list.count()
        info = self._info_for(trigger) if trigger else None
        self.change_key_btn.setEnabled(trigger is not None)
        self.first_key_btn.setEnabled(trigger is not None and trigger != (self._keys() or [""])[0])
        self.remove_key_btn.setEnabled(trigger is not None and count > 1)
        self.remove_key_btn.setToolTip("" if count > 1 else "A control needs at least one key. Delete the control to remove its last key.")
        self.share_btn.setEnabled(info is not None and info.state == "unauthorized")
        self.unshare_btn.setEnabled(info is not None and info.state == "shared")
        if info is None:
            self.keys_detail.setText("")
        elif info.state == "private":
            self.keys_detail.setText(f"{trigger} is used only by this control.")
        else:
            members = ", ".join(words for _id, words in info.members)
            if info.state == "shared":
                self.keys_detail.setText(f"{trigger} is SHARED on purpose by: {members}. Pressing it starts all of them, in list order.")
            else:
                winner = ", ".join(words for member_id, words in info.members if self._runs(trigger, member_id))
                self.keys_detail.setText(f"{trigger} is used by: {members}, but it is NOT shared, so only {winner or 'one of them'} runs. "
                                         "Choose 'Share this key' if all of them should run.")

    def _runs(self, trigger: str, binding_id: str) -> bool:
        info = next((i for i in self.editor.sharing_info(binding_id) if i.trigger == trigger), None)
        return bool(info and info.runs)

    def _action_text(self, number: int, ref: str, spec) -> str:
        delay = "at once" if ref == PRIMARY else (f"after {spec.delay_ms} ms" if spec.delay_ms else "at once")
        spots = f", {len(spec.also)} extra spot{'s' if len(spec.also) != 1 else ''}" if spec.also else ""
        return f"{number}.  {MODE_SHORT[spec.mode].capitalize()}  -  {delay}{spots}"

    def _refresh_actions(self, keep: str | None = None) -> None:
        keep = keep or self._ref
        self.action_list.blockSignals(True)
        self.action_list.clear()
        chosen = None
        for number, (ref, spec) in enumerate(ProfileEditor.action_refs(self.wbinding), 1):
            item = QListWidgetItem(self._action_text(number, ref, spec))
            item.setData(Qt.ItemDataRole.UserRole, ref)
            self.action_list.addItem(item)
            if ref == keep:
                chosen = item
        self.action_list.setCurrentItem(chosen or self.action_list.item(0))
        self.action_list.blockSignals(False)
        self._ref = self.action_list.currentItem().data(Qt.ItemDataRole.UserRole)
        spec = ProfileEditor.find_action(self.wbinding, self._ref)
        self.action_editor.load(spec, self._ref == PRIMARY)
        only_one = self.action_list.count() <= 1
        self.remove_action_btn.setEnabled(not only_one)
        self.remove_action_btn.setToolTip("A control needs at least one action. Delete the control to remove its last action."
                                          if only_one else "")

    def _refresh_checks(self) -> None:
        self.checks.clear()
        rows: list[str] = []
        for change in be.describe_group_changes(self.profile, self.working):
            rows.append("Sharing will change: " + change)
        for issue in be.issues_for(self.working, self.binding_id, self.reserved):
            rows.append(f"{issue.severity.upper()}: {issue.message}")
        if not rows:
            rows.append("Nothing to check: no problems found for this control.")
        for row in rows:
            self.checks.addItem(row)
        count = sum(1 for r in rows if r.startswith(("ERROR", "WARNING", "Sharing")))
        self.tabs.setTabText(2, f"Checks ({count})" if count else "Checks")

    def check_texts(self) -> list[str]:
        return [self.checks.item(i).text() for i in range(self.checks.count())]

    def _show(self, result: be.Result) -> None:
        text = result.message + ("  " + " ".join(result.notes) if result.notes else "")
        self.error_label.setText(text if not result.ok else "")
        self.error_label.setVisible(bool(text) and not result.ok)
        if result.ok and result.notes:
            self.error_label.setText(" ".join(result.notes))
            self.error_label.setVisible(True)

    # ===================================================================== keys
    def _flush(self) -> None:
        """Write what is typed in the dialog into the working copy (before an operation that copies it, and before OK)."""
        self.action_editor.spec = ProfileEditor.find_action(self.wbinding, self._ref)
        self.action_editor.apply()
        self.wbinding.label = self.label_edit.text().strip()
        self.wbinding.on_failure = self.failure_combo.currentData()

    def _after(self, result: be.Result, key: str | None = None) -> None:
        self._show(result)
        self._refresh_keys(key)
        self._refresh_actions(self._ref)
        self._refresh_checks()

    def _with_choice(self, first_try, key_for_message: str, context: str):
        """Run an operation; if the key belongs to another control, ask what to do, then run it again with the answer."""
        result = first_try(None, False)
        if not result.needs_choice:
            return result
        info = self.editor.inspect(key_for_message, self.binding_id, context)
        choice = self.choose_conflict(info, lambda c: first_try(c, True))
        if choice == be.CANCEL or not choice:
            return be.Result(False, "Cancelled - nothing changed.")
        return first_try(choice, False)

    def add_key(self) -> be.Result | None:
        name = self.capture_key()
        if not name:
            return None
        self._flush()
        result = self._with_choice(lambda choice, dry: self.editor.add_trigger(self.binding_id, name, choice, dry_run=dry), name, be.CONTEXT_ADD)
        self._after(result, name if result.ok else None)
        return result

    def change_key(self) -> be.Result | None:
        old = self.selected_key()
        if old is None:
            return None
        name = self.capture_key()
        if not name:
            return None
        self._flush()
        result = self._with_choice(lambda choice, dry: self.editor.change_trigger(self.binding_id, old, name, choice, dry_run=dry),
                                   name, be.CONTEXT_REBIND)
        self._after(result, name if result.ok else old)
        return result

    def remove_key(self) -> be.Result | None:
        key = self.selected_key()
        if key is None:
            return None
        self._flush()
        result = self.editor.remove_trigger(self.binding_id, key)
        self._after(result)
        return result

    def make_first(self) -> be.Result | None:
        key = self.selected_key()
        if key is None:
            return None
        self._flush()
        result = self.editor.make_first(self.binding_id, key)
        self._after(result, key)
        return result

    def share_key(self) -> be.Result | None:
        key = self.selected_key()
        info = self._info_for(key) if key else None
        if info is None or info.state != "unauthorized":
            return None
        self._flush()
        result = self.editor.share(key, [member_id for member_id, _ in info.members])
        self._after(result, key)
        return result

    def stop_sharing(self) -> be.Result | None:
        key = self.selected_key()
        if key is None:
            return None
        self._flush()
        result = self.editor.unshare(key, self.binding_id)
        self._after(result, key)
        return result

    # ===================================================================== actions
    def selected_ref(self) -> str:
        return self._ref

    def select_action(self, ref: str) -> None:
        for row in range(self.action_list.count()):
            if self.action_list.item(row).data(Qt.ItemDataRole.UserRole) == ref:
                self.action_list.setCurrentRow(row)
                return

    def _on_action_selected(self, current, _previous) -> None:
        if current is None:
            return
        self.action_editor.spec = ProfileEditor.find_action(self.wbinding, self._ref)         # write the OLD action first
        if self.action_editor.spec is not None:
            self.action_editor.apply()
        self._ref = current.data(Qt.ItemDataRole.UserRole)
        self.action_editor.load(ProfileEditor.find_action(self.wbinding, self._ref), self._ref == PRIMARY)
        self._refresh_action_texts()

    def _live_action_update(self) -> None:
        spec = ProfileEditor.find_action(self.wbinding, self._ref)
        if spec is None or self.action_editor.spec is not spec:
            return
        self.action_editor.apply()
        self._refresh_action_texts()

    def _refresh_action_texts(self) -> None:
        for row, (ref, spec) in enumerate(ProfileEditor.action_refs(self.wbinding)):
            if row < self.action_list.count():
                self.action_list.item(row).setText(self._action_text(row + 1, ref, spec))

    def add_action(self) -> be.Result:
        self._flush()
        result = self.editor.add_action(self.binding_id)
        self._after(result)
        if result.ok:
            self._refresh_actions(result.ref)
        return result

    def duplicate_action(self) -> be.Result:
        self._flush()
        result = self.editor.duplicate_action(self.binding_id, self._ref)
        self._after(result)
        if result.ok:
            self._refresh_actions(result.ref)
        return result

    def remove_action(self) -> be.Result:
        self._flush()
        ref = self._ref
        result = self.editor.remove_action(self.binding_id, ref)
        self._ref = PRIMARY
        self._after(result)
        return result

    # ===================================================================== finishing
    def selected_mode(self) -> str:
        return self.action_editor.selected_mode()

    def accept(self) -> None:
        if not self.advanced:
            return super().accept()
        try:
            self._flush()
        except ProfileError as exc:
            self._show(be.Result(False, str(exc)))
            return
        result = self.real_editor.apply_working(self.working, confirm=self.confirm_groups)
        if not result.ok:
            self._show(result)
            self.tabs.setCurrentIndex(2)
            self._refresh_checks()
            return
        super().accept()

    def apply(self, binding: Binding) -> None:
        """Legacy single-action dialog: write the dialog's values into `binding`. (With the profile, OK has already adopted everything.)"""
        if self.advanced:
            return
        binding.label = self.label_edit.text().strip()
        self.action_editor.spec = binding
        self.action_editor.apply()
