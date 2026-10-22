"""The question asked when a key is already used by another control: what should happen to it?

The choices are only those that have a clear meaning for the situation (see binding_edit.KeyConflict.choices):
  Replace  - the others lose the key (a control whose ONLY key it is goes, and the text says so);
  Share    - the key is explicitly shared: every control that uses it runs when it is pressed (saved in the profile);
  Swap     - when two controls simply exchange their first keys;
  Add spot - a NEW control whose key is used by exactly one tap/hold/toggle control touches both spots together;
  Cancel   - nothing changes.
A repeated key is never shared without the user choosing Share."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QLabel, QPushButton, QVBoxLayout

from ..mapping.binding_edit import CANCEL, REPLACE, SHARE, SWAP

ALSO = "also"


def conflict_options(trigger: str, users: list[str], choices, previews: dict, can_also: bool = False, context: str = "add") -> list[tuple]:
    """[(choice, button text, explanation)] in the order shown. `previews` = {choice: binding_edit.Result of a dry run}: the explanation
    lists exactly what the choice would do, taken from the editing layer, not guessed here."""
    who = ", ".join(users)
    out: list[tuple] = []
    if REPLACE in choices:
        notes = list(previews.get(REPLACE).notes) if previews.get(REPLACE) else []
        text = f"{trigger} is taken away from {who}."
        out.append((REPLACE, f"Replace  -  {trigger} is used only here", " ".join([text, *notes])))
    if SHARE in choices:
        changes = list(previews.get(SHARE).group_changes) if previews.get(SHARE) else []
        text = (f"Pressing {trigger} starts {who} and this control, each with its own finger, in the order of the list. "
                "This is saved as a sharing group.")
        out.append((SHARE, f"Share  -  {trigger} runs both", " ".join([text, *changes])))
    if SWAP in choices:
        out.append((SWAP, "Swap the two keys", f"This control gets {trigger} and {who} gets this control's old key."))
    if can_also:
        out.append((ALSO, "Add a spot to the existing control", f"{trigger} keeps ONE control and now touches both spots together."))
    return out


class KeyConflictDialog(QDialog):
    def __init__(self, parent, trigger: str, users: list[str], options: list[tuple], title: str = "This key is already used") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.choice = CANCEL
        self.buttons: dict[str, QPushButton] = {}
        layout = QVBoxLayout(self)
        heading = QLabel(f"'{trigger}' already controls {', '.join(users)}.\nWhat should happen?")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        for choice, text, explanation in options:
            frame = QFrame()
            inner = QVBoxLayout(frame)
            inner.setContentsMargins(0, 4, 0, 4)
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, c=choice: self._pick(c))
            note = QLabel(explanation)
            note.setObjectName("muted")
            note.setWordWrap(True)
            inner.addWidget(button)
            inner.addWidget(note)
            layout.addWidget(frame)
            self.buttons[choice] = button
        cancel = QPushButton("Cancel  -  change nothing")
        cancel.clicked.connect(lambda: self._pick(CANCEL))
        cancel.setDefault(True)
        self.buttons[CANCEL] = cancel
        layout.addWidget(cancel, 0, Qt.AlignmentFlag.AlignRight)

    def _pick(self, choice: str) -> None:
        self.choice = choice
        self.accept() if choice != CANCEL else self.reject()
