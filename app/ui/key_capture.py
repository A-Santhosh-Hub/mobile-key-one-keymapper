"""A small dialog that waits for ONE key, mouse button or wheel notch and names it exactly like the editor's own key capture does
(app/input/keys.py names, Ctrl/Shift/Alt held = a combination, a modifier pressed and released alone = that modifier).

The window's own capture (InputRouter.capture_callback) works only while the main window is active, so it cannot be used from inside
the binding-options dialog, which is modal. This dialog gives that dialog the same behaviour. A name can also be typed for people who
prefer that (Shift+E, MMB, WheelUp); either way the result is a canonical trigger name, or the dialog stays open and says why not."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit, QPushButton, QVBoxLayout

from ..input.keys import key_name, mouse_name
from ..input.router import InputRouter
from ..mapping.profile import MODIFIERS, ProfileError, canonical_trigger, make_combo


class KeyCaptureDialog(QDialog):
    def __init__(self, parent=None, prompt: str = "Press the key, click a mouse button, or scroll the wheel") -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose a key")
        self.setMinimumWidth(380)
        self.name: str | None = None
        self._alone: str | None = None             # a modifier pressed on its own: taken when it is released without another key
        self._wheel = 0
        self.prompt = QLabel(prompt)
        self.prompt.setWordWrap(True)
        self.prompt.setObjectName("tip")
        self.hint = QLabel("Hold Ctrl, Shift or Alt while you press a key to make a combination such as Shift+E. Esc cancels.")
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        self.typed = QLineEdit()
        self.typed.setPlaceholderText("...or type its name, e.g. Shift+E, MMB, WheelUp")
        self.typed.setFocusPolicy(Qt.FocusPolicy.ClickFocus)      # so a key press is NOT typed into the box
        self.typed.returnPressed.connect(self._use_typed)
        self.error = QLabel("")
        self.error.setObjectName("muted")
        self.error.setWordWrap(True)
        self.use_btn = QPushButton("Use the typed name")
        self.use_btn.clicked.connect(self._use_typed)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        for widget in (self.prompt, self.hint, self.typed, self.use_btn, self.error, self.buttons):
            layout.addWidget(widget)
        self.setFocus()

    # -- the result ------------------------------------------------------------------------------
    def _take(self, name: str) -> None:
        self.name = name
        self.accept()

    def _use_typed(self) -> None:
        text = self.typed.text().strip()
        try:
            self._take(canonical_trigger(text))
        except ProfileError as exc:
            self.error.setText(str(exc) if text else "Type a name first.")

    # -- what is pressed ---------------------------------------------------------------------------
    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            return super().keyPressEvent(event)
        if event.isAutoRepeat():
            return
        name = key_name(event)
        if name is None:
            return
        if name in MODIFIERS:
            self._alone = name                       # wait: a second key may follow
            return
        self._alone = None
        self._take(make_combo(InputRouter._modifier_names(event), name))

    def keyReleaseEvent(self, event) -> None:
        if event.isAutoRepeat():
            return
        name = key_name(event)
        if name in MODIFIERS and self._alone == name:
            self._alone = None
            self._take(name)

    def mousePressEvent(self, event) -> None:
        name = mouse_name(event.button())
        if name is not None:
            self._alone = None
            self._take(make_combo(InputRouter._modifier_names(event), name))

    def wheelEvent(self, event) -> None:
        self._wheel += event.angleDelta().y()
        if abs(self._wheel) >= 120:
            up = self._wheel > 0
            self._wheel = 0
            self._take(make_combo(InputRouter._modifier_names(event), "WheelUp" if up else "WheelDown"))
