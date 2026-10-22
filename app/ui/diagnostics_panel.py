"""Lists what Profile.analyze() says about the controls, and lets the user jump to the controls it is about.

The rules are NOT repeated here: the panel shows the analyzer's own issues (code, severity, message, the controls concerned) as they
are. It only adds a short tag in front of the sharing-related ones, so that a key shared ON PURPOSE (the profile says so) cannot be
mistaken for a repeated key that nobody set up. The analyzer uses the same resolver as the input engine, so what is listed here is
what will really run."""
from __future__ import annotations

from typing import Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from ..mapping.binding_edit import describe
from ..mapping.profile import Issue, Profile
from . import theme

SEVERITY_ICON = {"error": "⛔", "warning": "⚠", "info": "ℹ"}
SEVERITY_COLOR = {"error": theme.ERROR, "warning": theme.WARN, "info": theme.MUTED}
CODE_TAG = {
    "shared_trigger": "Shared on purpose",
    "duplicate_trigger_not_shared": "Same key, NOT shared",
    "shadowed_by_later_binding": "Same key, NOT shared",
    "shared_trigger_not_executed": "Same key, NOT shared",
    "shared_trigger_partially_authorized": "Sharing incomplete",
    "shared_trigger_conflict": "Shared keys clash",
    "sharing_metadata_invalid": "Sharing is stale",
    "sharing_not_executed": "Sharing not supported",
    "extra_triggers_not_executed": "Not supported yet",
    "extra_actions_not_executed": "Not supported yet",
}


def issue_line(issue: Issue) -> str:
    tag = CODE_TAG.get(issue.code, "")
    return f"{SEVERITY_ICON.get(issue.severity, '')} {tag + ': ' if tag else ''}{issue.message}"


class DiagnosticsPanel(QWidget):
    bindingChosen = Signal(str)             # the id of a control the user asked to go to

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.profile: Profile | None = None
        self.issues: list[Issue] = []
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.show_info = QCheckBox("Also show notes")
        self.show_info.setChecked(True)
        self.show_info.toggled.connect(lambda _: self._fill())
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setMinimumHeight(120)
        self.list.currentItemChanged.connect(lambda *_: self._update_go())
        self.list.itemActivated.connect(self._activated)
        self.go_label = QLabel("Go to:")
        self.go_label.setObjectName("muted")
        self.go_row = QHBoxLayout()
        self.go_row.addWidget(self.go_label)
        self.go_buttons: list[QPushButton] = []
        self.go_row.addStretch(1)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(self.summary, 1)
        top.addWidget(self.show_info)
        root.addLayout(top)
        root.addWidget(self.list)
        root.addLayout(self.go_row)
        self._update_go()

    # -- content ----------------------------------------------------------------------------------
    def refresh(self, profile: Profile | None, reserved: Mapping[str, str] | None = None) -> list[Issue]:
        """Analyze the profile again and show the result. Keeps the selected issue if it is still there."""
        self.profile = profile
        self.issues = profile.analyze(reserved=reserved) if profile is not None else []
        self._fill()
        return self.issues

    def counts(self) -> dict[str, int]:
        out = {"error": 0, "warning": 0, "info": 0}
        for issue in self.issues:
            out[issue.severity] = out.get(issue.severity, 0) + 1
        return out

    def problem_count(self) -> int:
        counts = self.counts()
        return counts["error"] + counts["warning"]

    def _fill(self) -> None:
        keep = self.current_issue()
        self.list.blockSignals(True)
        self.list.clear()
        for issue in self.issues:
            if issue.severity == "info" and not self.show_info.isChecked():
                continue
            item = QListWidgetItem(issue_line(issue))
            item.setData(Qt.ItemDataRole.UserRole, issue)
            item.setForeground(QColor(SEVERITY_COLOR.get(issue.severity, theme.TEXT)))
            names = ", ".join(self._words(i) for i in issue.binding_ids)
            item.setToolTip((f"Controls: {names}\n" if names else "") + issue.message)
            self.list.addItem(item)
            if keep is not None and issue == keep:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        counts = self.counts()
        if not self.issues:
            self.summary.setText("✔  No problems found.")
        else:
            parts = [f"{counts[s]} {label}" for s, label in (("error", "error(s)"), ("warning", "warning(s)"), ("info", "note(s)")) if counts[s]]
            self.summary.setText(" · ".join(parts))
        self._update_go()

    def _words(self, binding_id: str) -> str:
        binding = self.profile.find_by_id(binding_id) if self.profile is not None else None
        return describe(binding) if binding is not None else f"a control ({binding_id})"

    def texts(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def current_issue(self) -> Issue | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def select_issue_row(self, row: int) -> None:
        self.list.setCurrentRow(row)

    # -- going to a control --------------------------------------------------------------------------
    def _update_go(self) -> None:
        for button in self.go_buttons:
            self.go_row.removeWidget(button)
            button.deleteLater()
        self.go_buttons = []
        issue = self.current_issue()
        ids = list(dict.fromkeys(issue.binding_ids)) if issue is not None else []
        self.go_label.setVisible(bool(ids))
        for position, binding_id in enumerate(ids):
            button = QPushButton(self._words(binding_id))
            button.setObjectName("flat")
            button.setToolTip("Select this control")
            button.clicked.connect(lambda _=False, i=binding_id: self.bindingChosen.emit(i))
            self.go_row.insertWidget(1 + position, button)
            self.go_buttons.append(button)

    def _activated(self, item: QListWidgetItem) -> None:
        issue = item.data(Qt.ItemDataRole.UserRole)
        if issue is not None and issue.binding_ids:
            self.bindingChosen.emit(issue.binding_ids[0])
