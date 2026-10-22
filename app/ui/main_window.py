from __future__ import annotations

import copy
import logging
import time

import numpy as np
from PySide6.QtCore import QEvent, QEventLoop, QPoint, QSettings, Qt, QTimer
from PySide6.QtGui import QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QMenu, QComboBox, QDockWidget, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog,
                               QApplication, QLabel, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import __version__, hotkeys as hotkey_rules, paths, scene as scene_mod
from ..device.adb import Adb, AdbError, DeviceInfo
from ..device.foreground import ForegroundWatcher
from ..device.tracker import DeviceTracker
from ..input.engine import InputEngine
from ..input.rawmouse import RawMouse, clip_cursor, release_cursor
from ..input.router import InputRouter
from ..input.touch import TouchInjector
from ..mapping import binding_edit as be, layout_edit as le, loot as loot_mod, templates
from ..mapping.loot import SmartLoot
from ..mapping.autoload import profile_to_load, should_stop_mapping
from ..mapping.profile import (ALSO_MODES, MAX_ALSO, MODE_HELP, MODE_SHORT, PINCH_MODES, TWO_POINT_MODES, Binding, Look,
                               Profile,
                               ProfileError, ProfileStore, Stick, canonical_trigger, orientation_of)
from ..stream import protocol
from ..stream.session import StreamConfig
from ..stream.worker import MirrorWorker
from ..video import colorfit, look as look_mod
from ..video.widget import VideoWidget
from . import theme
from ..game_names import friendly_name
from .binding_options import ask_key_choice
from .controls_panel import ControlsPanel
from .log_panel import LogPanel
from .look_panel import LookPanel
from .new_profile_dialog import COPY, NewProfileDialog
from .overlay import MarkerOverlay
from .settings_dialog import SettingsDialog
from .simple_panel import SimplePanel, sidebar_width

log = logging.getLogger(__name__)

RESOLUTIONS = [("720 px", 720), ("1024 px", 1024), ("1280 px", 1280), ("1600 px", 1600), ("1920 px", 1920),
               ("Original  (the phone's full resolution, sharpest)", 0)]
FPS_CHOICES = [30, 45, 60, 90, 120]
DEFAULT_MBPS = 16                # new installs: a clear picture; older settings are kept as they were
DECODERS = [("Auto (recommended)", "auto"), ("Software (CPU)", "software"), ("D3D11VA (experimental)", "d3d11va"),
            ("DXVA2 (experimental)", "dxva2"), ("CUDA (experimental)", "cuda")]
MAX_AUTO_RETRIES = 5
SCENE_POLL_MS = 250              # how often smart mode looks at the picture
SHUTDOWN_WAIT_MS = 6000            # how long closing the window waits for background threads (UI stays responsive)
MARKER_STEP = 10               # percent per press of the opacity hotkeys


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# banner texts of the click-then-key flow, by mode
ADD_FIRST = {
    None: "1/2  Click the spot on the game screen, e.g. the map button",
    "swipe": "1/3  Click where the swipe STARTS",
    "drag": "1/3  Click where the drag STARTS",
    "pinch-out": "1/2  Click the centre of the zoom (e.g. the middle of the map)",
    "pinch-in": "1/2  Click the centre of the zoom (e.g. the middle of the map)",
    "sequence": "Click the FIRST spot of the multi-tap",
}

# (label, max size, fps, Mbps) - 'Custom' means the detailed options below do not match a preset
QUALITY_PRESETS = {
    "light": ("Light  -  slower PC or phone  (1280 px, 30 fps, 4 Mbps)", 1280, 30, 4),
    "smooth": ("Smooth  -  everyday  (1920 px, 60 fps, 8 Mbps)", 1920, 60, 8),
    "sharp": ("Sharp  -  clear picture, recommended  (1920 px, 60 fps, 16 Mbps)", 1920, 60, 16),
    "ultra": ("Ultra  -  full phone resolution  (original, 60 fps, 32 Mbps)", 0, 60, 32),
    "max": ("Max  -  best quality and smoothness  (original, 120 fps, 50 Mbps)", 0, 120, 50),
}

WELCOME = (
    "<div style='text-align:center'>"
    "<div style='font-size:24px; font-weight:700; color:#e8ebf4'>Play your phone games on your PC</div>"
    "<div style='font-size:14px; color:#8a93a8; margin-top:18px; line-height:170%'>"
    "<b style='color:#e8ebf4'>1</b>&nbsp; Plug your phone in with a USB cable<br>"
    "<b style='color:#e8ebf4'>2</b>&nbsp; On the phone, turn on <b style='color:#e8ebf4'>USB debugging</b> "
    "(Settings &gt; Developer options)<br>"
    "<b style='color:#e8ebf4'>3</b>&nbsp; Press <b style='color:#e8ebf4'>Connect</b> on the left"
    "</div></div>")
READY = ("<div style='text-align:center'><div style='font-size:22px; font-weight:700; color:#e8ebf4'>Your phone is ready"
         "</div><div style='font-size:14px; color:#8a93a8; margin-top:10px'>Press <b style='color:#e8ebf4'>Connect</b> "
         "on the left</div></div>")
ALLOW = ("<div style='text-align:center'><div style='font-size:22px; font-weight:700; color:#e8ebf4'>Look at your phone"
         "</div><div style='font-size:14px; color:#8a93a8; margin-top:10px'>Tap <b style='color:#e8ebf4'>Allow</b> on "
         "the 'Allow USB debugging?' message, and tick 'Always allow'.</div></div>")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Mobile Key One {__version__}")
        self.resize(1180, 820)
        self.adb = Adb()
        self.settings = QSettings(str(paths.data_dir() / "settings.ini"), QSettings.Format.IniFormat)

        self.devices: list[DeviceInfo] = []
        self.worker: MirrorWorker | None = None
        self._wanted_serial: str | None = None      # device the user wants mirrored (for auto-reconnect)
        self._fail_count = 0
        self._reapply = False                       # reconnect at once after 'Apply now'
        self._last_video_size = (0, 0)              # to tell a real screen change from the first size of a session
        self._rotation_paused = False               # key mapping was paused because the phone turned the wrong way
        self._retiring: set = set()                 # threads asked to stop that are still ending (kept alive until done)
        self.stuck_threads: list = []               # threads that did not end when the window closed (see closeEvent)
        self._device_summary = ""

        self.retry_timer = QTimer(self, interval=2000)
        self.retry_timer.timeout.connect(self._maybe_reconnect)
        self.stats_timer = QTimer(self, interval=1000)
        self.stats_timer.timeout.connect(self._update_stats)

        self._build_ui()
        self._setup_controls()
        self._load_settings()
        self._set_connected(False)

        self.tracker = DeviceTracker(self.adb, self)
        self.tracker.devicesChanged.connect(self._on_devices)
        self.tracker.trackerError.connect(lambda msg: self._set_status(f"ADB problem: {msg}", theme.ERROR))
        self.tracker.start()

        self._build_hotkeys()
        QShortcut(QKeySequence("Escape"), self, activated=self._on_escape)
        log.info("Mobile Key One %s started (adb: %s)", __version__, self.adb.path)

    # -- UI construction -------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.video = VideoWidget()
        self.video.set_placeholder(WELCOME)

        # step 1: phone ---------------------------------------------------------------------
        self.status_dot = QLabel("\u25cf")
        self.status_dot.setObjectName("statusDot")
        self.status_label = QLabel("Looking for your phone...")
        self.status_label.setWordWrap(True)
        self.device_combo = QComboBox()
        self.device_combo.setVisible(False)                 # only shown when several phones are plugged in
        self.device_combo.currentIndexChanged.connect(self._update_buttons)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._toggle_connection)
        self.refresh_btn = QPushButton("Restart the phone connection")
        self.refresh_btn.setToolTip("Restarts the helper that talks to the phone (fixes most 'phone not found' problems)")
        self.refresh_btn.clicked.connect(self._restart_adb)

        # video options (shown in Settings > Picture) -----------------------------------------------
        self.quality_combo = QComboBox()
        for key, (label, *_rest) in QUALITY_PRESETS.items():
            self.quality_combo.addItem(label, key)
        self.quality_combo.addItem("Custom (set below)", "custom")
        self.res_combo = QComboBox()
        for text, value in RESOLUTIONS:
            self.res_combo.addItem(text, value)
        self.fps_combo = QComboBox()
        for fps in FPS_CHOICES:
            self.fps_combo.addItem(f"{fps} fps", fps)
        self.bitrate_spin = QSpinBox(minimum=1, maximum=100, suffix=" Mbps")
        self.codec_combo = QComboBox()
        self.codec_combo.addItem("H.264 (most compatible)", "h264")
        self.codec_combo.addItem("H.265 (sharper at the same quality, if the phone supports it)", "h265")
        self.decoder_combo = QComboBox()
        for text, value in DECODERS:
            self.decoder_combo.addItem(text, value)
        self.smooth_check = QCheckBox("Smooth picture (adds in-between pictures)")
        self.smooth_check.setToolTip("Makes the picture on your PC smoother by generating a picture halfway between every two pictures the phone sends.\n"
                                     "It does not change the game's own frame rate, and every picture is shown half a picture later (a few milliseconds).\n"
                                     "It pauses itself when the stream is already fast enough for your screen, or when the graphics chip is too slow.")
        self.smooth_check.toggled.connect(self._on_smooth_toggled)
        self.smooth_note = QLabel("")
        self.smooth_note.setWordWrap(True)
        self.smooth_note.setStyleSheet(f"color: {theme.MUTED}; font-size: 11px;")
        self.touches_check = QCheckBox("Show touches on the phone")
        self.awake_check = QCheckBox("Keep the phone awake")
        self.reconnect_check = QCheckBox("Reconnect automatically")

        self.stream_box = QGroupBox("Details")
        form = QFormLayout(self.stream_box)
        form.setVerticalSpacing(10)
        form.addRow("Picture size", self.res_combo)
        form.addRow("Smoothness", self.fps_combo)
        form.addRow("Quality (bitrate)", self.bitrate_spin)
        form.addRow("Video format", self.codec_combo)
        form.addRow("Decoder", self.decoder_combo)
        form.addRow(self.smooth_check)
        form.addRow(self.smooth_note)
        form.addRow(self.touches_check)
        form.addRow(self.awake_check)
        form.addRow(self.reconnect_check)
        self.apply_stream_btn = QPushButton("Apply now  (reconnects the phone)")
        self.apply_stream_btn.setObjectName("primary")
        self.apply_stream_btn.setToolTip("Use the new size, smoothness and quality at once. The picture reconnects for a\n"
                                         "moment; press Start playing again afterwards.")
        self.apply_stream_btn.setEnabled(False)
        self.apply_stream_btn.clicked.connect(self._apply_stream_now)
        form.addRow(self.apply_stream_btn)
        self.look_panel = LookPanel()
        self.look_panel.lookChanged.connect(self._on_look_changed)
        self.look_panel.matchRequested.connect(self._match_colours)
        self.reset_video_btn = QPushButton("Reset picture settings")
        self.reset_video_btn.setToolTip(f"Back to the defaults: 1920 px, 60 fps, {DEFAULT_MBPS} Mbps, H.264, auto decoder.")
        self.reset_video_btn.clicked.connect(self._reset_video_settings)
        form.addRow(self.reset_video_btn)
        self.quality_combo.activated.connect(lambda _: self._apply_quality_preset(self.quality_combo.currentData()))
        for combo in (self.res_combo, self.fps_combo):
            combo.currentIndexChanged.connect(lambda _: self._sync_quality_combo())
        self.bitrate_spin.valueChanged.connect(lambda _: self._sync_quality_combo())

        # bar above the phone picture ---------------------------------------------------------------
        self.back_btn = QPushButton("\u25c0  Back")
        self.home_btn = QPushButton("\u25cf  Home")
        self.recents_btn = QPushButton("\u25a2  Recent apps")
        self.back_btn.clicked.connect(lambda: self._send_key(protocol.KEYCODE_BACK))
        self.home_btn.clicked.connect(lambda: self._send_key(protocol.KEYCODE_HOME))
        self.recents_btn.clicked.connect(lambda: self._send_key(protocol.KEYCODE_APP_SWITCH))
        self.screen_off_btn = QPushButton("Phone screen off")
        self.screen_off_btn.setCheckable(True)
        self.screen_off_btn.setToolTip("Turn the phone's own display off while the picture keeps coming to your PC "
                                       "(saves battery and heat)")
        self.screen_off_btn.toggled.connect(self._toggle_phone_screen)
        self.fullscreen_btn = QPushButton("Fullscreen  (F11)")
        self.fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        for button in (self.back_btn, self.home_btn, self.recents_btn, self.screen_off_btn, self.fullscreen_btn):
            button.setObjectName("tool")

        self.toolbar = QWidget()
        self.toolbar.setObjectName("toolbar")
        tl = QHBoxLayout(self.toolbar)
        tl.setContentsMargins(12, 8, 12, 8)
        tl.setSpacing(8)
        for button in (self.back_btn, self.home_btn, self.recents_btn):
            tl.addWidget(button)
        tl.addSpacing(10)
        tl.addWidget(self.screen_off_btn)
        tl.addStretch(1)
        tl.addWidget(self.fullscreen_btn)

        # controls, settings window, sidebar ---------------------------------------------------------
        self.controls = ControlsPanel()
        self.log_panel = LogPanel(self._diagnostics_text)
        self.log_panel.text.setMinimumHeight(220)
        self.show_stats_check = QCheckBox("Show technical details in the status bar (fps, delay)")
        self.show_stats_check.toggled.connect(self._on_show_stats_toggled)
        self.settings_btn = QPushButton("\u2699  Settings")
        self.settings_btn.setObjectName("tool")
        self.settings_dialog = SettingsDialog(self)
        self.settings_btn.clicked.connect(lambda: self.settings_dialog.show_tab(0))
        self.settings_dialog.opacity_slider.valueChanged.connect(lambda v: self._set_marker_opacity(v, toast=False))
        self.sidebar = SimplePanel(self, self.settings_btn)
        self.controls.edit_btn.toggled.connect(
            lambda on: on and QTimer.singleShot(60, lambda: self.sidebar.ensure_visible(self.controls.save_btn)))

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self.toolbar)
        rl.addWidget(self.video, 1)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.sidebar)
        layout.addWidget(right, 1)
        self.setCentralWidget(central)
        self.setMinimumSize(1060, 700)

        self.overlay = MarkerOverlay(self.video)

        self.stats_label = QLabel("")
        self.statusBar().addPermanentWidget(self.stats_label)
        self.statusBar().setSizeGripEnabled(False)

    # -- controls: profiles, key mapping, binding editor ------------------------------------
    def _setup_controls(self) -> None:
        self.store = ProfileStore(paths.data_dir() / "profiles")
        self.profile: Profile | None = None
        self._add_state: str | None = None          # None | place | key | stick-place | stick-key
        self._stick_direction = "up"
        self._look_corner = (0.0, 0.0)
        self._add_mode: str | None = None      # mode of the control being added (None = tap / hold by key)
        self._add_points: list[tuple[float, float]] = []
        self._pickup_count = 0                  # > 0 while a "pick up several items" control is being added
        self._loot_stage = 0                    # smart pick-up set-up: 0 = not adding, 1 = click the hand button, 2 = click the first row, 3 = press the key
        self._loot_count = 0
        self._loot_draft: dict | None = None
        self._also_target: tuple | None = None  # (binding id, action uid or None): what is about to get an extra spot
        self._suspend_was: bool | None = None   # the mouse was locked when the hold-to-free key went down
        self._scene_model = None                # smart mode: what the game's controls look like
        self._scene_tracker = scene_mod.SceneTracker()
        self._scene_applied: str | None = None
        self._scene_paused = False              # smart mode found a menu: keys paused, mouse is a finger
        self._scene_timer = QTimer(self, interval=SCENE_POLL_MS)
        self._scene_timer.timeout.connect(self._scene_tick)
        self.current_package = ""        # app in the phone's foreground
        self.foreground: ForegroundWatcher | None = None
        self._capture_wanted = False   # mouse capture is on (it is suspended while the window is inactive)
        self.raw_mouse = RawMouse(self._on_raw_mouse)
        self._add_pos = (0.5, 0.5)

        self.injector = TouchInjector(
            lambda: self.worker.control if self.worker else None,
            lambda: self.worker.video_size if self.worker else (0, 0),
        )
        self.engine = InputEngine(self.injector, self._schedule_precise)
        self.engine.on_press_changed = self.overlay.set_pressed
        self.router = InputRouter(self.engine, self.video, self, self)
        self.router.wheel_hook = self._loot_wheel
        self._loot_scroll_until = 0.0           # smart pick-up: no second scroll swipe before this time (time.monotonic)
        self.router.connected = lambda: self.worker is not None and self.worker.control is not None
        self.router.escape_callback = self._on_escape

        c = self.controls
        c.profileChosen.connect(self._choose_profile)
        c.newProfileRequested.connect(self._new_profile)
        c.deleteProfileRequested.connect(self._delete_profile)
        c.mappingToggled.connect(self._set_mapping)
        c.directTouchToggled.connect(self._on_direct_touch_toggled)
        c.addRequested.connect(self._toggle_add)
        c.removeRequested.connect(self._remove_binding)
        c.bindingsEdited.connect(self._profile_changed)
        c.bindingSelected.connect(self.overlay.set_selected_id)
        c.checksRequested.connect(lambda: self.settings_dialog.show_tab(2))
        c.diagnostics.bindingChosen.connect(self._goto_binding)
        c.stickPlaceRequested.connect(self._place_stick)
        c.stickRemoveRequested.connect(self._remove_stick)
        c.stickKeyRequested.connect(self._rebind_stick_key)
        c.stickEdited.connect(self._profile_changed)
        c.stickExtraRequested.connect(self._rebind_stick_extra)
        c.stickExtraCleared.connect(self._clear_stick_extra)
        c.smartToggled.connect(self._on_smart_toggled)
        c.smartLearnRequested.connect(self._learn_scene)
        c.lookPlaceRequested.connect(self._place_look)
        c.lookRemoveRequested.connect(self._remove_look)
        c.lookEdited.connect(self._profile_changed)
        c.captureToggled.connect(self._set_capture)
        c.linkRequested.connect(self._link_profile)
        c.editToggled.connect(self._set_edit_mode)
        c.saveRequested.connect(self._save_profile)
        c.undoRequested.connect(self._undo_changes)
        c.resetRequested.connect(self._reset_profile)
        c.stickResetRequested.connect(self._reset_stick)
        c.lookResetRequested.connect(self._reset_look)
        c.hotkeyRebind.connect(self._rebind_hotkey)
        c.hotkeysResetRequested.connect(self._reset_hotkeys)
        c.resetAllRequested.connect(self._reset_all_settings)
        c.addControl.connect(self._add_control)
        self.overlay.editChanged.connect(self._on_layout_edited)
        self.overlay.selectionChanged.connect(self._on_overlay_selection)
        self.overlay.rebindRequested.connect(self._rebind_hit)
        self.overlay.contextRequested.connect(self._show_context_menu)
        self._selection = None
        QShortcut(QKeySequence("Delete"), self, activated=self._delete_selected)
        c.unlinkRequested.connect(self._unlink_profile)
        c.autoLoadToggled.connect(lambda on: self.settings.setValue("auto_load", "true" if on else "false"))
        c.autoMappingToggled.connect(lambda on: self.settings.setValue("auto_mapping", "true" if on else "false"))
        c.set_auto(self.settings.value("auto_load", "true") == "true",
                   self.settings.value("auto_mapping", "false") == "true")

        if not self.store.names():
            self.store.save(Profile("Default"))
        self._refresh_profile_list(str(self.settings.value("profile", "Default")))

    def _schedule_precise(self, delay_ms: int, fn) -> None:
        """Single-shot timer with ~1 ms accuracy (the default coarse timer is far too sloppy for a ramp)."""
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.timeout.connect(fn)
        timer.timeout.connect(timer.deleteLater)
        timer.start(delay_ms)

    def _refresh_profile_list(self, preferred: str) -> None:
        names = self.store.names()
        name = preferred if preferred in names else names[0]
        self.controls.set_profiles(names, name)
        self._choose_profile(name)

    def _choose_profile(self, name: str) -> None:
        try:
            profile = self.store.load(name)
        except ProfileError as exc:
            QMessageBox.warning(self, "Profile", str(exc))
            return
        self._set_mapping(False)
        self.profile = profile
        self.engine.set_profile(profile)
        self._sync_loot_keys()
        if self.overlay.edit:
            self.overlay.set_edit(True, profile)
        self.controls.set_profile(profile)
        self._refresh_overlay(False)
        self.settings.setValue("profile", name)
        self._ensure_checkpoint()
        self._update_save_status()
        log.info("Profile '%s' loaded (%d bindings, %d sticks, %d look zones)", name, len(profile.bindings),
                 len(profile.sticks), len(profile.looks))

    def _new_profile(self) -> None:
        names = self.store.names()
        dialog = NewProfileDialog(self, self.profile.name if self.profile else None, self.current_package, names)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        name, template, link = dialog.result_values()
        try:
            if template == COPY and self.profile is not None:
                profile = Profile.from_dict(self.profile.to_dict())
                profile.name, profile.package = name, ""
            else:
                # the ready-made layout is a landscape shooter layout; an empty profile adapts to the first control
                profile = templates.build(template, name, "any" if template == templates.EMPTY else "landscape")
            self.store.save(profile)
            self.store.save_checkpoint(profile)
            if link and self.current_package:
                self.store.link_package(profile, self.current_package)
        except ProfileError as exc:
            QMessageBox.warning(self, "New profile", str(exc))
            return
        self._refresh_profile_list(name)
        self.controls.set_hint(f"Profile '{name}' created. Turn on 'Edit layout' and drag each marker onto the "
                               "matching button of your game.")

    def _delete_profile(self) -> None:
        if self.profile is None or len(self.store.names()) < 2:
            return
        answer = QMessageBox.question(self, "Delete profile", f"Delete profile '{self.profile.name}'?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete(self.profile.name)
        except ProfileError as exc:
            QMessageBox.warning(self, "Delete profile", str(exc))
            return
        self._refresh_profile_list("")

    def _profile_changed(self) -> None:
        """Bindings were edited: save, and apply to the running engine."""
        if self.profile is None:
            return
        try:
            self.store.save(self.profile)
        except ProfileError as exc:
            self.statusBar().showMessage(str(exc), 8000)
        self.engine.refresh_profile()
        self._sync_loot_keys()
        self._refresh_overlay(self.engine.mapping_active)
        self._update_save_status()
        self.controls.refresh_checks()

    # -- identity-safe editing helpers (see app/mapping/binding_edit.py) ---------------------------------------------
    def _reserved(self) -> dict:
        """{key: what the app uses it for}: the window hotkeys, which can never be control keys (the analyzer's `reserved`)."""
        return {key: hotkey_rules.HOTKEY_LABELS.get(action, action) for action, key in getattr(self, "hotkeys", {}).items()}

    def _sync_reserved(self) -> None:
        self.controls.reserved = self._reserved()
        self.controls.refresh_checks()

    def _editor(self) -> "be.ProfileEditor":
        return be.ProfileEditor(self.profile, self._reserved())

    def _confirm_group_change(self, changes: list) -> bool:
        """An operation would change who takes part in a sharing group: say so and let the user decide."""
        answer = QMessageBox.question(self, "Sharing will change", "\n\n".join(changes) + "\n\nContinue?")
        return answer == QMessageBox.StandardButton.Yes

    def _ask_key_choice(self, info, run, can_also: bool = False, title: str = "This key is already used") -> str:
        """The key is used by another control: replace / share / swap / add a spot / cancel (only what makes sense, see binding_edit)."""
        return ask_key_choice(self, info, run, can_also=can_also, title=title)

    def _key_owner(self, name: str) -> str | None:
        """What uses this key: the profile's own answer (stick, a control's first key) AND any control's additional key."""
        profile = self.profile
        if profile is None:
            return None
        return profile.key_owner(name) or ("another control" if be.users_of(profile, name) else None)

    def _binding_position(self, binding_id: str) -> int | None:
        """Where the ONE binding with this id is in the list now (None if gone or ambiguous)."""
        found = [i for i, b in enumerate(self.profile.bindings) if b.id == binding_id] if self.profile else []
        return found[0] if len(found) == 1 else None

    def _goto_binding(self, binding_id: str) -> None:
        """Select a control by id (from the checks panel): its table row and, while editing, its marker on the picture."""
        position = self._binding_position(binding_id)
        if position is None:
            self.controls.set_hint("That control is no longer in the profile.")
            return
        self.controls.select_binding(binding_id)
        if self.overlay.edit:
            self.overlay.select(le.Hit("tap", position, "", binding_id))

    def _set_mapping(self, on: bool) -> None:
        self._rotation_paused = False                        # a manual start / stop cancels a pending automatic resume
        if on and self.overlay.edit:
            self._set_edit_mode(False)
        if on:
            reason = self._mapping_blocker()
            if reason:
                self.controls.set_hint(reason)
                self.statusBar().showMessage(reason, 6000)
                on = False
        self.engine.set_mapping_active(on)
        if not on:
            self._set_capture(False)
        self.video.setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.ArrowCursor)
        self.overlay.set_banner("KEY MAPPING ON  -  Esc to release" if on else "")
        self._refresh_overlay(on)
        self.controls.set_mapping_checked(on)
        self.controls.capture_btn.setVisible(bool(on and self.engine.has_look()))
        if on:
            self.controls.set_hint("")
            if self.engine.has_look():
                self._set_capture(True)            # shooting mode: the mouse aims as soon as mapping is on
        self._suspend_was = None
        if on:
            self._scene_begin()
        else:
            self._scene_end()

    def _mapping_blocker(self) -> str:
        if self.worker is None or self.worker.control is None:
            return "Connect your phone first (step 1)."
        if self.profile is None or not (self.profile.bindings or self.profile.sticks or self.profile.looks):
            return ("These controls are empty. Press 'Edit controls' to add keys, or '+ New' to start from a "
                    "ready-made layout.")
        current = orientation_of(*self.worker.video_size)
        if self.profile.orientation not in ("any", current):
            return (f"These controls were made for {self.profile.orientation} mode, but the phone is in {current} mode. "
                    "Rotate the phone (or the game), or create new controls with '+ New'.")
        return ""

    # -- layout editor: drag markers, palette, context menu -------------------------------------
    EDIT_BANNER = ("EDIT LAYOUT  -  drag markers  -  double-click = change key  -  right-click = menu  -  "
                   "Delete = remove")

    def _set_edit_mode(self, on: bool) -> None:
        if on:
            if self.worker is None or self.worker.control is None or self.video.frame_size() == (0, 0):
                self.controls.set_hint("Connect to a phone first, then edit the layout.")
                self.controls.set_edit_checked(False)
                return
            if self.profile is None:
                self.controls.set_edit_checked(False)
                return
            if self.engine.mapping_active:
                self._set_mapping(False)
            self.overlay.set_edit(True, self.profile)
            self.overlay.set_banner(self.EDIT_BANNER)
            self.video.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            if self._add_state:
                self._end_add()
            self.overlay.set_edit(False)
            self.overlay.set_banner("")
            self._selection = None
        self.controls.set_edit_checked(on)

    def _on_layout_edited(self) -> None:
        self._profile_changed()
        if self.profile:
            self.controls.set_profile(self.profile)

    def _on_overlay_selection(self, hit) -> None:
        if hit is not None and self.profile is not None:
            hit = le.pin(self.profile, hit)                  # remember WHICH binding is selected, not only where it is in the list
        self._selection = hit
        row = (le.tap_position(self.profile, hit) if hit is not None and hit.kind == "tap" and self.profile is not None else None)
        self.overlay.set_selected_id("")
        if row is not None and hit.binding_id:
            self.controls.select_binding(hit.binding_id)
        elif row is not None:
            self.controls.table.selectRow(row)
        else:
            self.controls.table.clearSelection()

    def _delete_selected(self) -> None:
        if not self.overlay.edit or self.profile is None or self._selection is None or self._add_state:
            return
        self._delete_hit(self._selection)

    def _refuse_delete(self) -> None:
        log.warning("Delete refused: the selected control is no longer in the profile or cannot be identified")
        self.controls.set_hint("That control is no longer there (or cannot be identified), so nothing was deleted.")

    def _select_new(self, hit) -> None:
        self._on_layout_edited()
        self.overlay.select(hit)

    def _begin_key_capture(self, prompt: str, callback) -> None:
        """Wait for the next key press, mouse button or wheel notch and pass its name to `callback`."""
        self._add_state = "capture"
        self._set_hotkeys_enabled(False)
        self.router.place_callback = None
        self.router.capture_callback = callback
        self.overlay.set_passthrough(True)
        self.controls.set_adding(True)
        self.overlay.set_banner(prompt + "  (Esc cancels)")
        self.controls.set_hint(prompt)

    def _add_control(self, kind: str) -> None:
        if self._add_state:
            return
        if not self.overlay.edit:
            self._set_edit_mode(True)
            if not self.overlay.edit:
                return
        profile = self.profile
        if profile is None:
            return
        if kind == "stick":
            hit, removed = le.add_stick(profile) if not profile.sticks else (le.Hit("stick", 0, "center"), [])
            if removed:
                self.controls.set_hint(f"Tap bindings on {', '.join(removed)} were replaced by the stick.")
            self._select_new(hit)
        elif kind == "look":
            hit = le.add_look(profile) if not profile.looks else le.Hit("look", 0, "move")
            self._select_new(hit)
        elif kind == "custom":
            self._toggle_add()
        elif kind == "loot":
            self._begin_loot()
        elif kind == "loot-remove":
            self._remove_loot()
        elif kind == "pickup":
            self._begin_pickup()
        elif kind in MODE_SHORT and kind not in ("hold", "tap"):
            self._toggle_add(mode=kind)
        else:
            hit = le.add_preset(profile, kind)
            if hit is None:
                key, _, label = le.PRESETS[kind]
                existing = profile.find(key)
                if existing is not None:                  # already there: show it instead of refusing
                    self._select_new(le.Hit("tap", profile.bindings.index(existing)))
                    self.controls.set_hint(f"You already have '{existing.label or label}' on {key}. It is selected on "
                                           "the picture: drag it to move it, or press Delete to remove it.")
                    return
                self.controls.set_hint(f"'{key}' is already used. Pick another key for the new '{label}' control.")
                self._begin_key_capture(f"'{key}' is taken - press a different key for '{label}'",
                                        lambda name, k=kind: self._on_preset_key(k, name))
                return
            self._select_new(hit)
            self.controls.set_hint("Added. Drag it onto the matching button of the game.")

    def _add_binding_at_free_spot(self, name: str, mode: str = "tap", label: str = "") -> None:
        if self._hotkey_clash(name):
            return
        profile = self.profile
        if profile is None:
            self._end_add()
            return
        owner = profile.key_owner(name) or ("another control" if be.users_of(profile, name) else None)
        if owner:
            self._end_add()
            self.controls.set_hint(f"'{name}' is already used by {owner}.")
            return
        x, y = le.free_spot(profile)
        binding = Binding(name, x, y, mode, label)
        profile.add(binding)
        self._end_add()
        self._select_new(le.Hit("tap", len(profile.bindings) - 1, "", binding.id))
        self.controls.set_hint(f"'{name}' added. Drag it onto the button it should press.")

    def _on_custom_key(self, name: str) -> None:
        self._add_binding_at_free_spot(name)

    def _on_preset_key(self, kind: str, name: str) -> None:
        _, mode, label = le.PRESETS[kind]
        self._add_binding_at_free_spot(name, mode, label)

    def _rebind_hit(self, hit) -> None:
        if self._add_state or self.profile is None:
            return
        uid = le.split_part(hit.part)[0] if hit.kind == "tap" else None
        if uid is not None:                                   # an additional ACTION: its own options (its key is the control's)
            hit = le.pin(self.profile, hit)
            if hit.binding_id and self._binding_position(hit.binding_id) is not None:
                self.controls.open_options(hit.binding_id, ref=uid)
            return
        if hit.kind == "tap":
            self._begin_key_capture("Press the new key, mouse button or wheel direction",
                                    lambda name, h=hit: self._on_rebind(h, name))
        elif hit.kind == "stick" and hit.part in ("up", "left", "down", "right"):
            self._rebind_stick_key(hit.part)

    def _on_rebind(self, hit, name: str) -> None:
        """Change the first key of the control the hit is on to `name`. The control is found by its id; if another control uses the key
        the user chooses what happens (replace / share / swap), and nothing happens without a choice."""
        if self._hotkey_clash(name):
            return
        profile = self.profile
        hit = le.pin(profile, hit) if profile is not None else hit
        position = le.tap_position(profile, hit) if profile is not None and hit.kind == "tap" else None
        if profile is None or position is None:
            self._end_add()
            return
        binding = profile.bindings[position]
        old = binding.input
        editor = self._editor()

        def attempt(choice, dry=False):
            return editor.change_trigger(binding.id, old, name, choice, confirm=None if dry else self._confirm_group_change, dry_run=dry)
        result = attempt(None)
        if result.needs_choice:
            info = editor.inspect(name, binding.id, be.CONTEXT_REBIND, old=old)
            choice = self._ask_key_choice(info, lambda c: attempt(c, True))
            if choice == be.CANCEL or not choice:
                self._end_add()
                self.controls.set_hint("Nothing changed.")
                return
            result = attempt(choice)
        self._end_add()
        if not result.ok:
            self.controls.set_hint(result.message)
            return
        self._on_layout_edited()
        if self.overlay.edit:
            self.overlay.select(le.Hit("tap", self._binding_position(binding.id), "", binding.id))
        self.controls.set_hint(result.message.replace(f"{old} changed to {name}.", f"Key changed to '{name}'.")
                               + ("  " + " ".join(result.notes) if result.notes else ""))

    def _show_context_menu(self, hit, global_pos) -> None:
        profile = self.profile
        if profile is None or self._add_state:
            return
        menu = QMenu(self)
        hit = le.pin(profile, hit)                           # (the menu acts on THIS binding, whatever happens to the list meanwhile)
        if hit.kind == "tap":
            found = le.resolve(profile, hit)
            if found is None:
                self._refuse_delete()
                return
            binding, spec, rest = found
            bid = binding.id
            action_uid = le.split_part(hit.part)[0]
            menu.addAction("Options...  (keys, actions, sharing, speed, scope)" if action_uid is None else "Options of this action...",
                           lambda: self.controls.open_options(bid, ref=action_uid))
            menu.addAction(f"Change key (now '{binding.input}')...", lambda: self._rebind_hit(hit))
            menu.addAction("Add another key to this control...", lambda: self._add_key_to(bid))
            if spec.mode in ALSO_MODES:
                menu.addAction("Also touch another spot with this key...", lambda: self._begin_also(hit))
            menu.addAction("Add an action to this control", lambda: self._add_action_to(bid))
            other = "tap" if spec.mode == "hold" else "hold"
            menu.addAction(f"Mode: {other} (now {spec.mode})", lambda: self._toggle_mode(hit))
            part = le.split_part(hit.part)[1]
            menu.addAction("Delete this extra spot" if part.startswith("also") else
                           "Delete point" if part.startswith("step") else
                           "Delete this action" if spec is not binding else "Delete", lambda: self._delete_hit(hit))
            if spec is not binding:
                menu.addAction("Delete the whole control", lambda: self._delete_hit(le.Hit("tap", le.tap_position(profile, hit), "", bid)))
        elif hit.kind == "stick":
            if hit.part in ("up", "left", "down", "right"):
                menu.addAction(f"Change '{hit.part}' key...", lambda: self._rebind_hit(hit))
            menu.addAction("Delete the WASD stick", lambda: self._delete_hit(hit))
        else:
            menu.addAction("Delete the mouse-look area", lambda: self._delete_hit(hit))
        menu.exec(global_pos)

    def _add_key_to(self, binding_id: str) -> None:
        """Right-click > 'Add another key': press the key; a key another control uses asks what to do."""
        if self._add_state or self.profile is None:
            return
        self._begin_key_capture("Press the extra key, mouse button or wheel direction",
                                lambda name, i=binding_id: self._on_add_key(i, name))

    def _on_add_key(self, binding_id: str, name: str) -> None:
        if self._hotkey_clash(name):
            return
        profile = self.profile
        if profile is None or profile.find_by_id(binding_id) is None:
            self._end_add()
            return
        editor = self._editor()

        def attempt(choice, dry=False):
            return editor.add_trigger(binding_id, name, choice, confirm=None if dry else self._confirm_group_change, dry_run=dry)
        result = attempt(None)
        if result.needs_choice:
            info = editor.inspect(name, binding_id, be.CONTEXT_ADD)
            choice = self._ask_key_choice(info, lambda c: attempt(c, True))
            if choice == be.CANCEL or not choice:
                self._end_add()
                self.controls.set_hint("Nothing changed.")
                return
            result = attempt(choice)
        self._end_add()
        if result.ok:
            self._on_layout_edited()
            self.controls.set_hint(f"'{name}' now also starts this control." + ("  " + " ".join(result.notes) if result.notes else ""))
        else:
            self.controls.set_hint(result.message)

    def _add_action_to(self, binding_id: str) -> None:
        """Right-click > 'Add an action': one more action of the same control, placed next to it; drag it into place."""
        if self.profile is None:
            return
        result = self._editor().add_action(binding_id)
        if not result.ok:
            self.controls.set_hint(result.message)
            return
        position = self._binding_position(binding_id)
        self._on_layout_edited()
        if self.overlay.edit and position is not None:
            self.overlay.select(le.Hit("tap", position, le.action_part(result.ref), binding_id))
        self.controls.set_hint("Action added (the square marked 2, 3, ...). Drag it onto its spot; set its mode and delay in Options.")

    def _toggle_mode(self, hit) -> None:
        found = le.resolve(self.profile, le.pin(self.profile, hit)) if self.profile is not None else None
        if found is None:
            self._refuse_delete()
            return
        _binding, spec, _rest = found
        spec.mode = "tap" if spec.mode == "hold" else "hold"
        self._on_layout_edited()

    def _delete_hit(self, hit) -> None:
        """Delete what the hit points at. A control, an additional action or one of their spots is found by identity; a deletion that
        would change who takes part in a sharing group asks first; one that cannot be identified is refused and deletes nothing."""
        if self.profile is None:
            return
        hit = le.pin(self.profile, hit)
        if hit.kind != "tap":                                   # the stick and the look area are not part of any binding or sharing
            if not le.delete(self.profile, hit):
                self._refuse_delete()
                return
            self.overlay.select(None)
            self._on_layout_edited()
            return
        refused: list = []

        def change(working):
            if not le.delete(working, hit):
                refused.append(True)
                raise ProfileError("That control is no longer there (or cannot be identified), so nothing was deleted.")
        result = self._editor().run(change, confirm=self._confirm_group_change)
        if refused:
            self._refuse_delete()
            return
        if not result.ok:
            self.controls.set_hint(result.message)
            return
        self.overlay.select(None)
        self._on_layout_edited()

    # -- save / undo / reset ---------------------------------------------------------------------
    def _ensure_checkpoint(self) -> None:
        """Every profile gets a first restore point the first time it is opened."""
        if self.profile is not None and not self.store.has_checkpoint(self.profile.name):
            try:
                self.store.save_checkpoint(self.profile)
            except ProfileError as exc:
                log.warning("%s", exc)

    def _update_save_status(self) -> None:
        profile = self.profile
        if profile is None:
            self.controls.set_save_status("", False)
            self.controls.set_profile_actions_enabled(False, False)
            return
        dirty = self.store.has_unsaved_changes(profile)
        stamp = self.store.checkpoint_time(profile.name)
        when = time.strftime("%d %b %H:%M", time.localtime(stamp)) if stamp else "-"
        if dirty:
            text = "Your changes are kept automatically. Press Save to add a restore point."
        else:
            text = f"\u2714  Saved  ({when})"
        self.controls.set_save_status(text, dirty)
        self.controls.set_profile_actions_enabled(True, dirty)

    def _save_profile(self) -> None:
        if self.profile is None:
            return
        try:
            self.store.save(self.profile)
            self.store.save_checkpoint(self.profile)
        except ProfileError as exc:
            QMessageBox.warning(self, "Save", str(exc))
            return
        self._update_save_status()
        self.statusBar().showMessage(f"Profile '{self.profile.name}' saved", 5000)
        self.controls.set_hint("Saved. 'Undo changes' now returns to this point.")

    def _undo_changes(self) -> None:
        profile = self.profile
        if profile is None:
            return
        if not self.store.has_unsaved_changes(profile):
            self.controls.set_hint("Nothing to undo: the controls are the same as at the last Save.")
            return
        answer = QMessageBox.question(self, "Undo changes",
                                      f"Go back to the controls of '{profile.name}' as they were at the last Save?\n"
                                      "Changes made since then will be lost.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            restored = self.store.load_checkpoint(profile.name)
            self.store.save(restored)
        except ProfileError as exc:
            QMessageBox.warning(self, "Undo changes", str(exc))
            return
        self._refresh_profile_list(profile.name)
        self.controls.set_hint("Controls restored to the last Save.")

    def _reset_profile(self) -> None:
        profile = self.profile
        if profile is None:
            return
        answer = QMessageBox.question(
            self, "Reset profile",
            f"Replace all controls of '{profile.name}' with the standard shooter layout?\n"
            "(You can still press 'Undo changes' afterwards to get your previous controls back.)")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_mapping(False)
        templates.apply_template(profile, templates.STANDARD)
        self.overlay.select(None)
        self._profile_changed()
        self.controls.set_profile(profile)
        self.controls.set_hint("Reset to the standard shooter layout. Drag the markers onto your game's buttons.")

    def _reset_stick(self) -> None:
        profile = self.profile
        if profile is None or not profile.sticks:
            return
        stick, default = profile.sticks[0], templates.default_stick()
        clash = [k for k in default.keys.values() if k not in stick.key_set() and self._key_owner(k)]
        if clash:
            self.controls.set_hint(f"Cannot restore W A S D: {', '.join(clash)} is used by another control.")
            return
        stick.radius, stick.ramp_ms, stick.keys = default.radius, default.ramp_ms, dict(default.keys)
        self._profile_changed()
        self.controls.set_profile(profile)
        self.controls.set_hint("Stick reset: keys W A S D, radius 9 %, ramp 50 ms.")

    def _reset_look(self) -> None:
        profile = self.profile
        if profile is None or not profile.looks:
            return
        look, default = profile.looks[0], templates.default_look()
        look.sens_x, look.sens_y, look.accel, look.invert_y = default.sens_x, default.sens_y, default.accel, False
        self._profile_changed()
        self.controls.set_profile(profile)
        self.controls.set_hint("Mouse look reset: sensitivity 1.00 / 1.00, acceleration 0 %, Invert Y off.")

    # -- hotkeys ------------------------------------------------------------------------------------
    def _build_hotkeys(self) -> None:
        self.hotkeys = hotkey_rules.load(self.settings)
        actions = {"capture": self._toggle_capture, "cursor": self._toggle_capture, "mapping": self.controls.mapping_btn.toggle,
                   "fullscreen": self._toggle_fullscreen,
                   "opacity_down": lambda: self._step_marker_opacity(-MARKER_STEP),
                   "opacity_up": lambda: self._step_marker_opacity(MARKER_STEP),
                   "markers": self._toggle_markers, "smooth": self.smooth_check.toggle}
        self._hk_actions = actions
        self._hk_shortcuts = {name: QShortcut(QKeySequence(self.hotkeys[name]), self, activated=fn)
                              for name, fn in actions.items()}
        self._apply_hotkeys()

    def _apply_hotkeys(self) -> None:
        direct = {}
        for name, shortcut in self._hk_shortcuts.items():
            key = self.hotkeys[name]
            if key == hotkey_rules.DIRECT_KEY:            # the ~ key is run by the input router (works with Shift held)
                shortcut.setKey(QKeySequence())
                direct[key] = self._hk_actions[name]
            else:
                shortcut.setKey(QKeySequence(key))
        self._base_direct = direct
        self._sync_loot_keys()
        self.router.reserved_names = {k for a, k in self.hotkeys.items() if a not in hotkey_rules.HOLD_ACTIONS}
        self.router.hold_hotkeys = {self.hotkeys[a]: self._on_suspend for a in hotkey_rules.HOLD_ACTIONS}
        self.controls.set_hotkeys(self.hotkeys)
        self.fullscreen_btn.setText(f"Fullscreen  ({self.hotkeys['fullscreen']})")
        hotkey_rules.save(self.settings, self.hotkeys)
        self._sync_reserved()

    # -- smart pick-up of a loot list (app/mapping/loot.py) --------------------------------------------
    def _loot_config(self) -> "SmartLoot | None":
        profile = self.profile
        if profile is None or not profile.smart_loot:
            return None
        try:
            return SmartLoot.from_dict(profile.smart_loot)
        except ProfileError:
            return None                                       # (the checks panel says why)

    def _sync_loot_keys(self) -> None:
        """The smart pick-up key(s) of the current profile go to the router next to the window hotkeys (it runs them while mapping is on).
        A key that is also a window hotkey is never taken over."""
        if not hasattr(self, "hotkeys"):
            return
        keys = dict(getattr(self, "_base_direct", {}))
        config = self._loot_config()
        if config is not None:
            for key in config.keys():
                if key in self.hotkeys.values():
                    continue
                if key in config.pick_keys:
                    keys[key] = lambda index=config.pick_keys.index(key): self._loot_pick(index)
                else:
                    keys[key] = lambda close=(key == config.close_key): self._loot_press(close)
        self.router.direct_hotkeys = keys

    def _loot_press(self, close: bool = False) -> None:
        """The smart pick-up key was pressed: LOOK at the picture, then tap only what is needed (see loot.py)."""
        config = self._loot_config()
        frame = self.video.current_frame()
        if config is None or not self.engine.mapping_active:
            return
        if frame is None:
            self.overlay.show_toast("Smart pick-up: no picture yet")
            return
        opened = loot_mod.list_is_open(frame, config)
        if close:
            if opened:
                self.engine.run_taps([(config.toggle[0], config.toggle[1], 0)])
            return
        if opened:
            self.engine.run_taps(config.row_taps())
            return
        self.engine.run_taps([(config.toggle[0], config.toggle[1], 0)])          # closed: tap the hand to open it ...
        epoch = self.engine.epoch
        QTimer.singleShot(config.wait_ms, lambda: self._loot_after_open(epoch, config))

    def _loot_pick(self, index: int) -> None:
        """A pick key (G, H, K ...) was pressed: take the `index`-th row of the list. Only if the list is OPEN on the picture: with it closed the
        tap would land on the game world, so nothing is tapped and the user is told."""
        config = self._loot_config()
        if config is None or not self.engine.mapping_active:
            return
        frame = self.video.current_frame()
        if frame is None:
            self.overlay.show_toast("Smart pick-up: no picture yet")
        elif not loot_mod.list_is_open(frame, config):
            self.overlay.show_toast(f"The loot list is closed - press {config.key} to open it first")
        else:
            x, y = config.row_point(index)
            self.engine.run_taps([(x, y, 0)])

    def _loot_wheel(self, notches: int) -> bool:
        """The mouse wheel scrolls the loot list - but only while the list is OPEN on the picture. Otherwise the wheel does what it did before
        (change weapon ...). Returns True if it scrolled (or deliberately swallowed the notch because a scroll is still running)."""
        config = self._loot_config()
        if config is None or not config.scroll or not self.engine.mapping_active:
            return False
        if not loot_mod.list_is_open(self.video.current_frame(), config):
            return False
        now = time.monotonic()
        if now < self._loot_scroll_until:                       # one finger at a time: ignore notches while the last swipe is moving
            return True
        start, end = config.scroll_path(wheel_up=notches > 0)
        if self.engine.run_swipe(start, end, loot_mod.SCROLL_MS):
            self._loot_scroll_until = now + (loot_mod.SCROLL_MS + 60) / 1000.0
        return True

    def _loot_after_open(self, epoch: int, config: "SmartLoot") -> None:
        """... then, once the list had time to appear, take the items. Nothing happens if everything was cancelled meanwhile (Esc, focus
        loss, rotation) or if no list appeared (nothing nearby)."""
        if epoch != self.engine.epoch or not self.engine.mapping_active:
            return
        if loot_mod.list_is_open(self.video.current_frame(), config):
            self.engine.run_taps(config.row_taps())             # (nothing when count is 0: the pick keys choose)
        else:
            self.overlay.show_toast("Smart pick-up: no loot list - nothing nearby?")

    def _begin_loot(self) -> None:
        """Smart pick-up set-up: how many items, click the hand button, click the first row (list open), press the key."""
        if self._add_state:
            return
        count, ok = QInputDialog.getInt(self, "Smart pick-up",
                                        "How many items should the key take by itself after it opens the list?\n"
                                        "(0 = it only opens the list, and you choose the items with the pick keys)",
                                        0, loot_mod.COUNT_RANGE[0], loot_mod.COUNT_RANGE[1])
        if not ok:
            return
        picks, ok = QInputDialog.getText(self, "Smart pick-up",
                                         "Pick keys: the first key takes the 1st item of the list, the second key the 2nd, and so on.\n"
                                         "Separate them with spaces. Leave empty for none.", text=" ".join(self._free_pick_keys()))
        if not ok:
            return
        pick_keys, problem = self._check_pick_keys(picks)
        if problem:
            self.controls.set_hint(problem)
            return
        self._toggle_add(mode=None)
        if self._add_state != "place":
            return
        self._loot_stage, self._loot_count, self._loot_draft = 1, count, {"pick_keys": pick_keys}
        self.overlay.set_banner("1/3  Open the loot list ON THE PHONE first, then click the hand / X button here  (Esc cancels)")
        self.controls.set_hint("Stand next to some loot and tap the hand on the phone so the list is open. The hand turns into an X: click it on the "
                               "picture (clicks here only place points, they do not touch the phone).")

    def _free_pick_keys(self, wanted: int = 3) -> list[str]:
        """The first free keys among G H J K L ... : not used by a control, the stick or a window hotkey."""
        used = (self._profile_inputs() | set(self.hotkeys.values())) if self.profile is not None else set()
        return [k for k in "GHJKLNBYU" if k not in used][:wanted]

    def _check_pick_keys(self, text: str) -> tuple[list[str], str]:
        """The pick keys typed in the set-up: (keys, "") or ([], what is wrong)."""
        keys: list[str] = []
        profile = self.profile
        for word in text.replace(",", " ").split():
            try:
                name = canonical_trigger(word)
            except ProfileError as exc:
                return [], f"'{word}' is not a key: {exc}"
            if "+" in name.rstrip("+") or name in ("Shift", "Ctrl", "Alt"):
                return [], f"'{word}': use plain keys (no Shift, Ctrl or Alt) for the pick keys."
            if name in keys:
                return [], f"'{name}' is listed twice."
            if name in profile.stick_keys():
                return [], f"'{name}' is a movement-stick key. Choose another."
            if be.users_of(profile, name):
                return [], f"'{name}' is already a key of another control (for example Fire). Choose another, or remove that control first."
            if name in self.hotkeys.values():
                return [], f"'{name}' is a window hotkey. Choose another."
            keys.append(name)
        if len(keys) > loot_mod.MAX_PICK_KEYS:
            return [], f"At most {loot_mod.MAX_PICK_KEYS} pick keys."
        return keys, ""

    def _loot_place(self, pos: tuple[float, float]) -> None:
        if self._loot_stage == 1:
            self._loot_draft["toggle"] = pos
            self._loot_stage = 2
            self.overlay.set_preview(pos)
            self.overlay.set_banner("2/3  Now click the FIRST row of the open list (the top item)  (Esc cancels)")
            self.controls.set_hint("The list must be OPEN on the phone now: the app measures its colour from the picture.")
            return
        share = loot_mod.yellow_share(self.video.current_frame(), loot_mod.default_box(pos))
        if share < loot_mod.VISIBLE_MIN_SHARE:
            self.controls.set_hint(f"I cannot see the yellow loot list there (only {round(share * 100)} % of that area). Open the list next to "
                                   "some loot, then click the first row again.")
            return
        self._loot_draft.update(row=pos, share=share)
        self._loot_stage = 3
        self._set_hotkeys_enabled(False)
        self.router.capture_callback = self._on_capture
        self.router.place_callback = None
        self._add_state = "key"
        self.overlay.set_banner("3/3  Now press the key for smart pick-up (for example F)  (Esc cancels)")
        self.controls.set_hint("One key opens the list if it is closed and takes the items. Use a plain key (no Shift/Ctrl/Alt).")

    def _loot_finish(self, name: str) -> None:
        draft, count = self._loot_draft or {}, self._loot_count
        profile = self.profile
        problem = ""
        if "+" in name.rstrip("+") or name in ("Shift", "Ctrl", "Alt"):
            problem = "Use a plain key (no Shift, Ctrl or Alt) for smart pick-up."
        elif name in profile.stick_keys():
            problem = f"'{name}' is a movement-stick key."
        elif name in draft.get("pick_keys", []):
            problem = f"'{name}' is one of your pick keys. The open key must be a different key."
        elif be.users_of(profile, name):
            problem = f"'{name}' is already a key of another control. Pick another key (or remove that control first)."
        if problem:
            self._end_add()
            self.controls.set_hint(problem)
            return
        try:
            config = SmartLoot(key=name, toggle=draft["toggle"], row=draft["row"], count=count,
                               pick_keys=draft.get("pick_keys", []), threshold=loot_mod.calibrated_threshold(draft["share"]))
        except (KeyError, ProfileError) as exc:
            self._end_add()
            self.controls.set_hint(f"Smart pick-up could not be set up: {exc}")
            return
        if profile.orientation == "any":
            profile.orientation = orientation_of(*self.video.frame_size())
        profile.smart_loot = config.to_dict()
        self._end_add()
        self._profile_changed()
        self.controls.set_profile(profile)
        picks = ", ".join(config.pick_keys)
        self.controls.set_hint(f"Smart pick-up: {name} opens the loot list if it is closed"
                               + (f" and then takes {count} items" if count else "")
                               + (f". With the list open: {picks} take the 1st, 2nd, 3rd ... item, and the mouse wheel scrolls the list" if picks
                                  else ". With the list open the mouse wheel scrolls it")
                               + f". (Measured: the open list covers {round(draft['share'] * 100)} % of the look area.) Turn on Start playing to use it.")
        log.info("Smart pick-up: key %s, button (%.3f, %.3f), first row (%.3f, %.3f), %d items", name, *config.toggle, *config.row, count)

    def _remove_loot(self) -> None:
        if self.profile is None or not self.profile.smart_loot:
            self.controls.set_hint("This profile has no smart pick-up.")
            return
        self.profile.smart_loot = None
        self._profile_changed()
        self.controls.set_hint("Smart pick-up removed.")

    def _set_hotkeys_enabled(self, enabled: bool) -> None:
        for shortcut in getattr(self, "_hk_shortcuts", {}).values():
            shortcut.setEnabled(enabled)

    def _profile_inputs(self) -> set[str]:
        if self.profile is None:
            return set()
        return {t for b in self.profile.bindings for t in b.triggers} | self.profile.stick_keys()

    def _hotkey_clash(self, name: str) -> bool:
        """A window hotkey cannot also be a control key. Ends the add flow with a hint if it is."""
        if name in self.hotkeys.values():
            self._end_add()
            self.controls.set_hint(f"'{name}' is a window hotkey. Choose another key, or change the hotkey first.")
            return True
        return False

    def _rebind_hotkey(self, action: str) -> None:
        if self._add_state:
            return
        self._begin_key_capture(f"Press the new key for '{hotkey_rules.HOTKEY_LABELS[action]}'",
                                lambda name, a=action: self._on_hotkey_key(a, name))

    def _on_hotkey_key(self, action: str, name: str) -> None:
        error = hotkey_rules.validate(self.hotkeys, action, name, self._profile_inputs())
        self._end_add()
        if error:
            self.controls.set_hint(error)
            return
        self.hotkeys[action] = name
        self._apply_hotkeys()
        self.controls.set_hint(f"'{hotkey_rules.HOTKEY_LABELS[action]}' is now {name}.")

    def _reset_hotkeys(self) -> None:
        self.hotkeys = dict(hotkey_rules.DEFAULT_HOTKEYS)
        self._apply_hotkeys()
        self.controls.set_hint("Hotkeys are back to their defaults.")

    # -- smart mode: tell the game from its menus -----------------------------------------------------
    def _scene_model_for_profile(self):
        profile = self.profile
        if profile is None or not profile.scene or not profile.scene.get("enabled"):
            return None
        model = scene_mod.SceneModel.from_dict(profile.scene)
        return model if model is not None and model.usable else None

    def _scene_begin(self) -> None:
        """Called when playing starts: if smart mode is on for this profile, start watching the picture."""
        self._scene_model = self._scene_model_for_profile()
        self._scene_applied = None
        self._scene_tracker.reset()
        if self._scene_model is None:
            self._scene_timer.stop()
            return
        self._scene_tick(first=True)
        self._scene_timer.start()

    def _scene_end(self) -> None:
        self._scene_timer.stop()
        self._scene_model = None
        self._scene_applied = None
        was_paused = self._scene_paused
        self._scene_paused = False
        self.controls.set_smart_live("")
        if was_paused:
            self._refresh_overlay(self.engine.mapping_active)

    def _scene_tick(self, first: bool = False) -> None:
        model = self._scene_model
        if model is None:
            return
        frame = self.video.current_frame()
        if frame is None:
            if first:
                self._scene_apply(scene_mod.MENU)        # no picture to judge: stay safe
            return
        value = scene_mod.score(model, scene_mod.luma(frame.y, frame.width, frame.height))
        if first:
            self._scene_tracker.force(scene_mod.GAME if value >= self._scene_tracker.enter else scene_mod.MENU)
        state = self._scene_tracker.update(value)
        state = scene_mod.GAME if state == scene_mod.GAME else scene_mod.MENU      # unknown counts as menu
        self.controls.set_smart_live(("Now: the game screen" if state == scene_mod.GAME else "Now: a menu")
                                     + f"  (looks like your controls: {round(value * 100)} %)")
        if state != self._scene_applied:
            self._scene_apply(state)

    def _scene_apply(self, state: str) -> None:
        self._scene_applied = state
        if state == scene_mod.GAME:
            self._scene_paused = False
            self.engine.set_mapping_active(True)
            self._refresh_overlay(True)
            self.video.setCursor(Qt.CursorShape.CrossCursor)
            self.overlay.set_banner("KEY MAPPING ON  -  Esc to release")
            self.controls.capture_btn.setVisible(bool(self.engine.has_look()))
            if self.engine.has_look():
                if self.isActiveWindow():
                    self._set_capture(True)
                else:
                    self._capture_wanted = True              # takes effect when the window gets focus back
            log.info("Smart mode: game screen - keys on")
        else:
            self._scene_paused = True
            self._set_capture(False)
            self.engine.set_mapping_active(False)           # lifts every finger; keys are paused
            self._refresh_overlay(False)
            self.video.setCursor(Qt.CursorShape.ArrowCursor)
            self.overlay.set_banner("SMART MODE  -  menu: the mouse works like a finger, keys paused")
            self.controls.capture_btn.setVisible(False)
            log.info("Smart mode: menu - keys paused")

    def _learn_scene(self) -> None:
        profile = self.profile
        frame = self.video.current_frame()
        if profile is None or frame is None or self.worker is None:
            self.controls.set_hint("Connect your phone and open the game first.")
            return
        if self.engine.mapping_active or self._scene_paused:
            self.controls.set_hint("Stop playing first (Esc), then press 'Learn the game screen'.")
            return
        points = [(b.x, b.y) for b in profile.bindings] + [(s.x, s.y) for s in profile.sticks]
        model = scene_mod.learn(scene_mod.luma(frame.y, frame.width, frame.height), points)
        if not model.usable:
            self.controls.set_hint(f"Only {len(model.anchors)} of your keys sit on a clear button here (at least "
                                   f"{scene_mod.MIN_ANCHORS} are needed). Open the game's match screen with its buttons "
                                   "showing, then try again.")
            return
        profile.scene = {"enabled": True, **model.to_dict()}
        self._profile_changed()
        self.controls.set_smart(profile)
        self.controls.set_hint(f"Learned the game screen from {len(model.anchors)} buttons. Smart mode is ON: in a "
                               "match your mouse aims, in menus it works like a finger and the keys pause.")
        log.info("Smart mode learned: %d anchors for '%s'", len(model.anchors), profile.name)

    def _on_smart_toggled(self, on: bool) -> None:
        profile = self.profile
        if profile is None:
            return
        learned = scene_mod.SceneModel.from_dict(profile.scene) if profile.scene else None
        if on and (learned is None or not learned.usable):
            self.controls.set_smart(profile)
            self.controls.set_hint("Press 'Learn the game screen' first, while the game's own buttons are on screen.")
            return
        if profile.scene:
            profile.scene["enabled"] = on
            self._profile_changed()
        if self.engine.mapping_active or self._scene_paused:
            if on:
                self._scene_begin()
            else:
                if self._scene_paused:
                    self._scene_apply(scene_mod.GAME)
                self._scene_end()

    # -- hold-to-free-the-mouse key -----------------------------------------------------------------
    def _on_suspend(self, held: bool) -> None:
        if held:
            if self._suspend_was is None and self.engine.look_captured:
                self._suspend_was = True
                self._set_capture(False)
        elif self._suspend_was:
            self._suspend_was = None
            if self.engine.mapping_active:
                self._set_capture(True)

    # -- one key, two controls: what to do when a key is already used ------------------------------------
    def _begin_also(self, hit) -> None:
        """Right-click > 'Also touch another spot': click the extra spot on the picture."""
        profile = self.profile
        if profile is None or self._add_state:
            return
        found = le.resolve(profile, le.pin(profile, hit))
        if found is None:
            self._refuse_delete()
            return
        binding, spec, _rest = found
        if spec.mode not in ALSO_MODES:
            self.controls.set_hint("Only 'hold', 'tap' and 'toggle' keys can touch more than one spot.")
            return
        if len(spec.also) >= MAX_ALSO:
            self.controls.set_hint(f"A key can touch at most {MAX_ALSO + 1} spots.")
            return
        if not self._can_edit_screen():
            return
        self._set_mapping(False)
        self._add_state = "also"
        self._also_target = (binding.id, None if spec is binding else spec.uid)
        self.router.place_callback = self._on_also_place
        self.router.capture_callback = None
        self.overlay.set_passthrough(True)
        self.controls.set_adding(True)
        self.video.setCursor(Qt.CursorShape.CrossCursor)
        self.overlay.set_banner(f"Click the extra spot that '{binding.input}' should touch together with the first "
                                "one  (Esc cancels)")

    def _on_also_place(self, pos: tuple[float, float]) -> None:
        target = self._also_target
        profile = self.profile
        index = self._binding_position(target[0]) if target and profile is not None else None
        added = index is not None and le.add_also(profile, index, pos, target[1])
        self._end_add()
        if not added:
            return
        self._on_layout_edited()
        if self.overlay.edit:
            binding = profile.bindings[index]
            spec = binding if target[1] is None else next(a for a in binding.extra_actions if a.uid == target[1])
            part = f"also{len(spec.also) - 1}"
            self.overlay.select(le.Hit("tap", index, part if target[1] is None else le.action_part(target[1], part), binding.id))
        self.controls.set_hint("Added. The key now touches both spots together. Drag the small marker to adjust it.")

    # -- key marker opacity (like the emulators' 'control opacity') ---------------------------------
    def _set_marker_opacity(self, percent: int, toast: bool = True) -> None:
        percent = max(0, min(100, int(percent)))
        self.marker_opacity = percent
        self.overlay.set_opacity(percent / 100)
        self.settings_dialog.set_opacity_value(percent)
        self.settings.setValue("marker_opacity", percent)
        if toast:
            note = "" if (self.engine.mapping_active and not self.overlay.edit) else "  (applies while playing)"
            self.overlay.show_toast(f"Key markers {percent} %" + ("  -  hidden" if percent == 0 else "") + note)

    def _step_marker_opacity(self, delta: int) -> None:
        self._set_marker_opacity(self.marker_opacity + delta)

    def _toggle_markers(self) -> None:
        if self.marker_opacity > 0:
            self._marker_before = self.marker_opacity
            self._set_marker_opacity(0)
        else:
            self._set_marker_opacity(self._marker_before or 100)

    # -- picture look and colour check -----------------------------------------------------------------
    def _on_look_changed(self, look) -> None:
        self.video.set_look(look)
        look_mod.save(self.settings, look)

    def _match_colours(self) -> None:
        """Compare the video with a screenshot of the phone and pick the colour conversion that reproduces it."""
        frame = self.video.current_frame()
        if self.worker is None or frame is None:
            self.statusBar().showMessage("Connect your phone first.", 5000)
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            png = self.adb.screencap(self.worker.serial)
        except AdbError as exc:
            QApplication.restoreOverrideCursor()
            self.look_panel.match_btn.setText(f"Could not check: {exc}")
            QTimer.singleShot(6000, self._reset_match_text)
            return
        QApplication.restoreOverrideCursor()
        image = QImage.fromData(png).convertToFormat(QImage.Format.Format_RGB888)
        image = image.scaled(frame.width, frame.height, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        width, height = image.width(), image.height()
        truth = np.array(np.frombuffer(image.constBits(), np.uint8).reshape(height, image.bytesPerLine())[:, : width * 3],
                         copy=True).reshape(height, width, 3)
        best, errors, already_right = colorfit.choose(frame, truth)
        text = self._colour_verdict(best, errors, already_right, float(truth.mean()))
        log.info("Colour check: %s (errors %s)", text, {k: round(v, 2) for k, v in errors.items()})
        self.look_panel.match_btn.setText(text)
        QTimer.singleShot(9000, self._reset_match_text)

    def _colour_verdict(self, best: str, errors: dict, already_right: bool, truth_mean: float) -> str:
        lowest = min(errors.values())
        if truth_mean < 2.0:
            return "The screenshot was black (this game blocks screenshots). Try on the home screen."
        if lowest > 12.0:
            return "The picture was moving too much to compare. Try again on a still screen (home screen or a menu)."
        if already_right:
            return f"Colours already match the phone (difference {errors[best]:.1f} of 255). Nothing changed."
        look = self.look_panel.look().with_values(color_mode=best)
        self.look_panel.set_look(look, emit=True)
        return f"Fixed: the colours now use {look_mod.COLOR_MODES[best]} (difference {errors[best]:.1f} of 255)."

    def _reset_match_text(self) -> None:
        self.look_panel.match_btn.setText("Check the colours against the phone screen")

    def _apply_stream_now(self) -> None:
        """Use the new size / smoothness / quality at once: reconnect with them."""
        if self.worker is None:
            return
        self._save_settings()
        self._reapply = True
        self.statusBar().showMessage("Applying the new picture settings - reconnecting. Press Start playing again after.",
                                     8000)
        self._stop_worker()

    # -- app settings ---------------------------------------------------------------------------------
    def _on_direct_touch_toggled(self, on: bool) -> None:
        self.engine.direct_touch = on
        self.settings.setValue("mouse_finger", "true" if on else "false")

    def _reset_video_settings(self) -> None:
        self._apply_video_defaults()
        self._save_settings()
        self.statusBar().showMessage("Video settings reset to the defaults", 5000)

    def _apply_video_defaults(self) -> None:
        self._select(self.res_combo, 1920)
        self._select(self.fps_combo, 60)
        self.bitrate_spin.setValue(DEFAULT_MBPS)
        self._select(self.codec_combo, "h264")
        self._select(self.decoder_combo, "auto")
        self.smooth_check.setChecked(False)
        self.touches_check.setChecked(False)
        self.awake_check.setChecked(True)
        self.reconnect_check.setChecked(True)

    def _reset_all_settings(self) -> None:
        answer = QMessageBox.question(
            self, "Reset all settings",
            "Put every setting back to how the app came?\n\n"
            "- video options (size, frame rate, bitrate, codec, decoder, ...)\n"
            "- 'Mouse acts as a finger' and the auto-load options\n"
            "- the hotkeys\n\nYour profiles and controls are NOT changed.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._apply_video_defaults()
        self.controls.direct_check.setChecked(True)
        self.controls.set_auto(True, False)
        self.settings.setValue("auto_load", "true")
        self.settings.setValue("auto_mapping", "false")
        self._reset_hotkeys()
        self._set_marker_opacity(100, toast=False)
        self.look_panel.set_look(look_mod.PictureLook(), emit=True)
        self._save_settings()
        self.controls.set_hint("All settings are back to the defaults. Profiles were not changed.")
        self.statusBar().showMessage("Settings reset", 5000)

    # -- per-game profiles ---------------------------------------------------------------------
    def _auto_load_enabled(self) -> bool:
        return self.controls.auto_load_check.isChecked()

    def _start_foreground_watch(self, serial: str) -> None:
        self._stop_foreground_watch()
        watcher = ForegroundWatcher(self.adb, serial, parent=self)
        watcher.packageChanged.connect(self._on_package)
        self.foreground = watcher
        watcher.start()

    def _stop_foreground_watch(self) -> None:
        watcher = self.foreground
        if watcher is not None:
            self.foreground = None
            watcher.request_stop()                         # never wait for it here: it may be inside an adb call
            self._retire(watcher)
        self.current_package = ""
        self.controls.set_running("")

    def _retire(self, thread) -> None:
        """Keep a thread that was asked to stop alive until it has really ended (a running QThread must not be destroyed)."""
        self._retiring.add(thread)
        thread.finished.connect(lambda t=thread: self._retiring.discard(t))

    def _stop_worker(self) -> None:
        """Disconnect without waiting. The releases are queued first; the session sends them before it closes."""
        worker = self.worker
        if worker is None:
            return
        self.engine.release_all()
        worker.stop()

    def _on_package(self, package: str) -> None:
        self.current_package = package
        self.controls.set_running(package)
        log.info("Phone foreground app: %s", package)
        if should_stop_mapping(self.profile, package) and self.engine.mapping_active:
            self._set_mapping(False)
            self.controls.set_hint("Key mapping turned off: you left the game this profile is for.")
        if not self._auto_load_enabled() or self._add_state:
            return
        name = profile_to_load(self.store, package, self.profile)
        if name:
            log.info("Auto-loading profile '%s' for %s", name, package)
            self._refresh_profile_list(name)
            self.controls.set_hint(f"Profile '{name}' loaded automatically for {package}.")
            self.statusBar().showMessage(f"Profile '{name}' loaded for this game", 6000)
        if (self.controls.auto_mapping_check.isChecked() and self.profile and self.profile.package == package
                and not self.engine.mapping_active):
            self._set_mapping(True)

    def _link_profile(self) -> None:
        if self.profile is None or not self.current_package:
            self.controls.set_hint("Open the game on the phone first, then link.")
            return
        try:
            others = self.store.link_package(self.profile, self.current_package)
        except ProfileError as exc:
            self.controls.set_hint(str(exc))
            return
        self.controls.set_linked(self.profile.package)
        note = f" (removed from: {', '.join(others)})" if others else ""
        self.controls.set_hint(f"'{self.profile.name}' is now the profile for {self.current_package}{note}.")

    def _unlink_profile(self) -> None:
        if self.profile is None:
            return
        self.profile.package = ""
        self._profile_changed()
        self.controls.set_linked("")

    # -- mouse look: capture + editor ---------------------------------------------------------
    def _toggle_capture(self) -> None:
        if self.engine.mapping_active and self.engine.has_look():
            self._set_capture(not self._capture_wanted)

    def _set_capture(self, on: bool) -> None:
        """Hide + confine the cursor and send raw mouse movement to the engine as camera movement."""
        on = bool(on and self.engine.mapping_active and self.engine.has_look())
        self._capture_wanted = on
        if on:
            if not self.raw_mouse.start(int(self.winId())):
                self.controls.set_hint("Could not start raw mouse input; mouse aiming is unavailable.")
                self._capture_wanted = on = False
        if on:
            self._clip_to_video()
            self.video.setCursor(Qt.CursorShape.BlankCursor)
            self.engine.set_look_captured(True)
            self.overlay.set_banner(f"KEY MAPPING ON  -  mouse captured  -  {self._cursor_keys()} free cursor  -  Esc release")
        else:
            self.raw_mouse.stop()
            release_cursor()
            self.engine.set_look_captured(False)
            if self.engine.mapping_active:
                self.video.setCursor(Qt.CursorShape.CrossCursor)
                self.overlay.set_banner(f"KEY MAPPING ON  -  cursor free  -  {self._cursor_keys()} aim  -  Esc release")
        self.overlay.set_captured(on)
        self.controls.set_capture_checked(on)

    def _cursor_keys(self) -> str:
        """The keys that lock / free the mouse, for messages: '~ or F8'."""
        keys = [hotkey_rules.pretty(self.hotkeys[a]) for a in ("cursor", "capture")]
        return " or ".join(dict.fromkeys(keys))

    def _clip_to_video(self) -> None:
        ratio = self.video.devicePixelRatioF()
        top_left = self.video.mapToGlobal(QPoint(0, 0))
        left, top = round(top_left.x() * ratio), round(top_left.y() * ratio)
        clip_cursor(left + 2, top + 2, round(left + self.video.width() * ratio) - 2,
                    round(top + self.video.height() * ratio) - 2)

    def _on_raw_mouse(self, dx: int, dy: int) -> None:
        if self.isActiveWindow():
            self.engine.look_delta(dx, dy)

    def _place_look(self) -> None:
        if self._add_state:
            self._end_add()
            return
        if not self._can_edit_screen():
            return
        self._set_mapping(False)
        self._add_state = "look-1"
        self.router.place_callback = self._on_look_corner
        self.router.capture_callback = None
        self.controls.set_adding(True)
        self.video.setCursor(Qt.CursorShape.CrossCursor)
        self.overlay.set_banner("1/2  Click one corner of the camera area  (Esc cancels)")
        self.controls.set_hint("Pick an empty part of the game view, away from every on-screen button.")

    def _on_look_corner(self, pos: tuple[float, float]) -> None:
        if self._add_state == "look-1":
            self._look_corner = pos
            self._add_state = "look-2"
            self.overlay.set_banner("2/2  Click the opposite corner  (Esc cancels)")
            return
        if self.profile is None:
            self._end_add()
            return
        old = self.profile.looks[0] if self.profile.looks else None
        try:
            look = Look(self._look_corner[0], self._look_corner[1], pos[0], pos[1],
                        sens_x=old.sens_x if old else 1.0, sens_y=old.sens_y if old else 1.0,
                        accel=old.accel if old else 0.0, invert_y=old.invert_y if old else False)
        except ProfileError:
            self._end_add()
            self.controls.set_hint("That area is too small. Click two corners that are further apart.")
            return
        if self.profile.orientation == "any":
            self.profile.orientation = orientation_of(*self.video.frame_size())
        self.profile.looks = [look]
        self._end_add()
        self._profile_changed()
        self.controls.set_profile(self.profile)
        self.controls.set_hint("Camera area set. Turn on Key mapping (F9): the mouse then aims. "
                               "Adjust Sens X/Y to taste.")
        log.info("Look zone (%.3f, %.3f)-(%.3f, %.3f)", look.x0, look.y0, look.x1, look.y1)

    def _remove_look(self) -> None:
        if self.profile is None:
            return
        self.profile.looks.clear()
        self._profile_changed()
        self.controls.set_profile(self.profile)

    # -- stick: sprint / walk / auto-run keys ------------------------------------------------------
    STICK_EXTRA_TITLES = {"sprint": "Sprint key", "walk": "Walk key", "autorun": "Auto-run key"}

    def _rebind_stick_extra(self, kind: str) -> None:
        if self._add_state or self.profile is None or not self.profile.sticks:
            return
        self._set_mapping(False)
        self._begin_key_capture(f"Press the key for '{self.STICK_EXTRA_TITLES[kind]}' "
                                "(Shift, Ctrl or Alt work too)", lambda name, k=kind: self._on_stick_extra_key(k, name))

    def _on_stick_extra_key(self, kind: str, name: str) -> None:
        if self._hotkey_clash(name):
            return
        profile = self.profile
        if profile is None or not profile.sticks:
            self._end_add()
            return
        stick, attr = profile.sticks[0], f"{kind}_key"
        owner = self._key_owner(name)
        if owner and name != getattr(stick, attr):
            self._end_add()
            self.controls.set_hint(f"'{name}' is already used by {owner}. Remove or change that first.")
            return
        old = getattr(stick, attr)
        setattr(stick, attr, name)
        try:
            stick.__post_init__()
        except ProfileError as exc:
            setattr(stick, attr, old)
            self._end_add()
            self.controls.set_hint(str(exc))
            return
        self._end_add()
        self._on_layout_edited()
        self.controls.set_hint(f"{self.STICK_EXTRA_TITLES[kind]} is now '{name}'.")

    def _clear_stick_extra(self, kind: str) -> None:
        if self.profile is None or not self.profile.sticks:
            return
        setattr(self.profile.sticks[0], f"{kind}_key", "")
        self._on_layout_edited()
        self.controls.set_hint(f"{self.STICK_EXTRA_TITLES[kind]} cleared.")

    # -- movement stick editor ---------------------------------------------------------------
    def _refresh_overlay(self, active: bool) -> None:
        if self.profile and not self._scene_paused:
            self.overlay.set_state(self.profile.bindings, active, self.profile.sticks, self.profile.looks)
            self.overlay.set_loot(self._loot_config())
        else:
            self.overlay.set_state([], active, [], [])
            self.overlay.set_loot(None)

    def _can_edit_screen(self) -> bool:
        if self.worker is None or self.worker.control is None or self.video.frame_size() == (0, 0):
            self.controls.set_hint("Connect to a phone first.")
            return False
        return True

    def _place_stick(self) -> None:
        if self._add_state:
            self._end_add()
            return
        if not self._can_edit_screen():
            return
        self._set_mapping(False)
        self._add_state = "stick-place"
        self.router.place_callback = self._on_stick_place
        self.router.capture_callback = None
        self.controls.set_adding(True)
        self.video.setCursor(Qt.CursorShape.CrossCursor)
        self.overlay.set_banner("Click the CENTRE of the game's joystick  (Esc cancels)")
        self.controls.set_hint("Click the middle of the game's on-screen movement joystick.")

    def _on_stick_place(self, pos: tuple[float, float]) -> None:
        if self.profile is None:
            self._end_add()
            return
        if self.profile.orientation == "any":
            self.profile.orientation = orientation_of(*self.video.frame_size())
        old = self.profile.sticks[0] if self.profile.sticks else None
        if old:
            old.x, old.y = pos
            stick = old
            removed: list[str] = []
        else:
            stick = Stick(pos[0], pos[1])
            removed = self.profile.set_stick(stick)
        self._end_add()
        self._profile_changed()
        self.controls.set_profile(self.profile)
        note = f" (tap bindings on {', '.join(removed)} were removed)" if removed else ""
        self.controls.set_hint(f"Stick placed{note}. Match its Radius to the game's joystick circle, "
                               "then turn on Key mapping (F9).")
        log.info("Stick centre (%.3f, %.3f)", *pos)

    def _remove_stick(self) -> None:
        if self.profile is None:
            return
        self.profile.sticks.clear()
        self._profile_changed()
        self.controls.set_profile(self.profile)

    def _rebind_stick_key(self, direction: str) -> None:
        if self._add_state or self.profile is None or not self.profile.sticks:
            return
        self._set_mapping(False)
        self._set_hotkeys_enabled(False)
        self._add_state = "stick-key"
        self._stick_direction = direction
        self.router.place_callback = None
        self.router.capture_callback = self._on_stick_key
        self.controls.set_adding(True)
        self.overlay.set_banner(f"Press the key for '{direction}'  (Esc cancels)")
        self.controls.set_hint(f"Press the new key for '{direction}'.")

    def _on_stick_key(self, name: str) -> None:
        if self._hotkey_clash(name):
            return
        if self.profile is None or not self.profile.sticks:
            self._end_add()
            return
        stick = self.profile.sticks[0]
        direction = self._stick_direction
        used_by = self._key_owner(name)
        if name != stick.keys[direction] and used_by:
            self._end_add()
            self.controls.set_hint(f"'{name}' is already used by {used_by}. Remove that first.")
            return
        stick.keys[direction] = name
        self._end_add()
        self._profile_changed()
        self.controls.set_profile(self.profile)
        self.controls.set_hint(f"'{direction}' is now {name}.")

    def _toggle_add(self, mode: str | None = None) -> None:
        """Add a key the TC-Games way: click the spot on the game screen, then press the key for it.
        `mode` picks a different kind of control (swipe, zoom, multi-tap, repeat tap, ...)."""
        if self._add_state:
            self._end_add()
            return
        if self.worker is None or self.worker.control is None or self.video.frame_size() == (0, 0):
            self.controls.set_hint("Connect to a phone first, then add keys.")
            return
        self._set_mapping(False)
        self._add_mode = mode
        self._add_points = []
        self._add_state = "place"
        self.router.place_callback = self._on_place
        self.router.capture_callback = None
        self.overlay.set_passthrough(True)                 # clicks must reach the picture, not the editor layer
        self.controls.set_adding(True)
        self.video.setCursor(Qt.CursorShape.CrossCursor)
        self.overlay.set_banner(ADD_FIRST.get(mode, ADD_FIRST[None]) + "  (Esc cancels)")
        self.controls.set_hint("Click the button of the game that this key should press."
                               if mode is None else MODE_HELP[mode])

    def _begin_pickup(self) -> None:
        """"Pick up several items": ask how many, then click the FIRST row of the game's loot list and press the key."""
        if self._add_state:
            return
        count, ok = QInputDialog.getInt(self, "Pick up several items",
                                        "How many items should one key press pick up?\n(It taps the first row of the loot list that many times.)",
                                        le.PICKUP_DEFAULT, le.PICKUP_MIN, le.PICKUP_MAX)
        if not ok:
            return
        self._toggle_add(mode="sequence")
        if self._add_state != "place":                      # (not connected: _toggle_add said so)
            return
        self._pickup_count = count
        self.overlay.set_banner("1/2  Stand next to some loot, then click the FIRST row of the loot list  (Esc cancels)")
        self.controls.set_hint(f"One key will tap that row {count} times, one after the other: the list moves up after each pick-up, "
                               "so every tap takes the next item.")

    def _on_place(self, pos: tuple[float, float]) -> None:
        mode = self._add_mode
        if self._loot_stage:
            self._loot_place(pos)
            return
        if self._pickup_count:                              # one click is enough: the same spot is tapped several times
            self._add_points = [pos] * self._pickup_count
            self._add_pos = pos
            self.overlay.set_preview(pos)
            self._set_hotkeys_enabled(False)
            self.router.capture_callback = self._on_capture
            self.router.place_callback = None
            self._add_state = "key"
            self.overlay.set_banner(f"2/2  Now press the key that picks up {self._pickup_count} items  (Esc cancels)")
            self.controls.set_hint("Press the key (for example F). Hold Shift, Ctrl or Alt while pressing to make a combination.")
            return
        self._add_points.append(pos)
        self._add_pos = self._add_points[0]
        count = len(self._add_points)
        self.overlay.set_preview(list(self._add_points) if count > 1 else pos)
        if mode in TWO_POINT_MODES and count < 2:
            self.overlay.set_banner("2/3  Now click where it ENDS  (Esc cancels)")
            return
        self._set_hotkeys_enabled(False)                    # so that F-keys can be chosen as the new key
        self.router.capture_callback = self._on_capture
        if mode == "sequence":                              # keep clicking points; the first key press finishes
            self._add_state = "key"
            self.overlay.set_banner(f"{count} spot{'s' if count > 1 else ''} so far  -  click more spots in order, "
                                    "or PRESS THE KEY to finish  (Esc cancels)")
            self.controls.set_hint("Click each spot in order. Then press the key for the whole sequence.")
            return
        self._add_state = "key"
        self.router.place_callback = None
        last = "3/3" if mode in TWO_POINT_MODES else "2/2"
        self.overlay.set_banner(f"{last}  Now press the key, mouse button or wheel direction  (Esc cancels)")
        self.controls.set_hint("Press the key (for example M for the map). Hold Shift, Ctrl or Alt while pressing "
                               "to make a combination like Shift+W.")

    def _on_capture(self, name: str) -> None:
        if self.profile is None:
            self._end_add()
            return
        if self._hotkey_clash(name):
            return
        if self._loot_stage == 3:
            self._loot_finish(name)
            return
        editor = self._editor()
        info = editor.inspect(name, None, be.CONTEXT_NEW)
        if info.kind == "stick":
            self._end_add()
            self.controls.set_hint(f"'{name}' is already used by the movement stick. Pick another key.")
            return
        mode = self._add_mode or ("hold" if name == "LMB" else "tap")   # a normal click key taps; only fire is held
        points = list(self._add_points) or [self._add_pos]
        try:
            binding = (le.pickup_binding(name, points[0], self._pickup_count) if self._pickup_count
                       else le.binding_from_points(name, mode, points))
        except ProfileError as exc:
            self._end_add()
            self.controls.set_hint(str(exc))
            return
        choice = None
        if info.kind == "taken":
            users = [self.profile.find_by_id(i) for i in info.users]
            only = users[0] if len(users) == 1 else None
            can_also = (only is not None and mode in ("tap", "hold") and len(points) == 1 and only.mode in ALSO_MODES
                        and len(only.also) < MAX_ALSO)
            choice = self._ask_key_choice(
                info, lambda c: editor.add_binding(copy.deepcopy(binding), c, dry_run=True), can_also=can_also,
                title="This key is already used")
            if choice == be.CANCEL or not choice:
                self._end_add()
                self.controls.set_hint("Cancelled - nothing changed.")
                return
            if choice == "also":
                position = self._binding_position(only.id)
                le.add_also(self.profile, position, points[0])
                self._end_add()
                self._on_layout_edited()
                if self.overlay.edit:
                    self.overlay.select(le.Hit("tap", position, f"also{len(only.also) - 1}", only.id))
                self.controls.set_hint(f"'{name}' now touches both spots together. Drag either one to adjust it.")
                return
        if self.profile.orientation == "any":
            self.profile.orientation = orientation_of(*self.video.frame_size())
        result = editor.add_binding(binding, choice, confirm=self._confirm_group_change)
        if not result.ok:
            self._end_add()
            self.controls.set_hint(result.message)
            return
        hit = le.Hit("tap", self._binding_position(binding.id), "", binding.id)
        self._end_add()
        self._profile_changed()
        self.controls.set_profile(self.profile)
        if self.overlay.edit:
            self.overlay.select(hit)
        what = MODE_SHORT[mode] if mode not in ("hold", "tap") else "key"
        verb = {"replace": "rebound", "share": "added and shared"}.get(choice, "added")
        self.controls.set_hint(f"'{name}' {verb} ({what}) on the spot you clicked. "
                               "Drag it to adjust, double-click to change the key, right-click for Options."
                               + ("  " + " ".join(result.notes) if result.notes else ""))
        log.info("Binding %s (%s) -> (%.3f, %.3f)", name, mode, *points[0])

    def _remove_binding(self, binding_id: str) -> None:
        """Remove exactly the binding with this id (not "whatever uses this key"). If it cannot be identified, nothing is removed."""
        if self.profile is None:
            return
        result = self._editor().delete_binding(binding_id, confirm=self._confirm_group_change)
        if not result.ok:
            if result.group_changes:                                # the user said no to the sharing change
                self.controls.set_hint("Cancelled - nothing was deleted.")
            else:
                log.warning("Delete refused: no single control has the id %r", binding_id)
                self.controls.set_hint("That control could not be identified, so nothing was deleted.")
            self.controls.set_profile(self.profile)
            return
        self._profile_changed()
        self.controls.set_profile(self.profile)

    def _end_add(self) -> None:
        self._add_state = None
        self._add_mode = None
        self._add_points = []
        self._pickup_count = 0
        self._loot_stage, self._loot_count, self._loot_draft = 0, 0, None
        self.router.place_callback = None
        self.router.capture_callback = None
        self._set_hotkeys_enabled(True)
        self.controls.set_adding(False)
        self.overlay.set_passthrough(False)
        self.overlay.set_preview(None)
        self.overlay.set_banner(self.EDIT_BANNER if self.overlay.edit else "")
        self.video.setCursor(Qt.CursorShape.ArrowCursor)
        self._update_save_status()

    def _on_escape(self) -> None:
        if self._add_state:
            self._end_add()
            self.controls.set_hint("Cancelled.")
        elif self.engine.mapping_active or self._scene_paused:
            self._set_mapping(False)
        else:
            self._leave_fullscreen()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.ActivationChange and hasattr(self, "engine"):
            if not self.isActiveWindow():
                self.engine.release_all()    # never leave a finger stuck down when focus is lost
                if self._suspend_was:         # the hold-to-free key's release may never arrive: lock again on return
                    self._suspend_was = None
                    self._capture_wanted = True
                if self._capture_wanted:
                    self.raw_mouse.stop()    # free the mouse for other windows (capture resumes on return)
                    release_cursor()
            elif self._capture_wanted and self.engine.mapping_active:
                self._set_capture(True)
            if self.isActiveWindow():
                self._try_resume_after_rotation()
        super().changeEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        sidebar = getattr(self, "sidebar", None)
        if sidebar is not None:
            sidebar.setFixedWidth(sidebar_width(self.width()))      # the sidebar follows the window size
        if getattr(self, "_capture_wanted", False):
            QTimer.singleShot(0, self._clip_to_video)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if getattr(self, "_capture_wanted", False):
            QTimer.singleShot(0, self._clip_to_video)

    # -- settings --------------------------------------------------------------------------
    def _load_settings(self) -> None:
        s = self.settings
        self._select(self.res_combo, int(s.value("max_size", 1920)))
        self._select(self.fps_combo, int(s.value("max_fps", 60)))
        self.bitrate_spin.setValue(int(s.value("bitrate_mbps", DEFAULT_MBPS)))
        self._select(self.codec_combo, s.value("codec", "h264"))
        self._select(self.decoder_combo, s.value("decoder", "auto"))
        self.touches_check.setChecked(s.value("show_touches", "false") == "true")
        self.awake_check.setChecked(s.value("stay_awake", "true") == "true")
        self.reconnect_check.setChecked(s.value("auto_reconnect", "true") == "true")
        self.controls.direct_check.setChecked(s.value("mouse_finger", "true") == "true")
        self.show_stats_check.setChecked(s.value("show_stats", "false") == "true")
        self.smooth_check.blockSignals(True)                  # (no "Smooth picture on" message at start-up)
        self.smooth_check.setChecked(s.value("smooth_picture", "false") == "true")
        self.smooth_check.blockSignals(False)
        self.video.set_smooth(self.smooth_check.isChecked())
        self._update_smooth_note()
        self.look_panel.set_look(look_mod.load(s))
        self.video.set_look(self.look_panel.look())
        self._marker_before = 100                      # opacity to come back to after 'hide'
        self._set_marker_opacity(_to_int(s.value("marker_opacity", 100), 100), toast=False)
        self._sync_quality_combo()

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("max_size", self.res_combo.currentData())
        s.setValue("max_fps", self.fps_combo.currentData())
        s.setValue("bitrate_mbps", self.bitrate_spin.value())
        s.setValue("codec", self.codec_combo.currentData())
        s.setValue("decoder", self.decoder_combo.currentData())
        s.setValue("show_touches", "true" if self.touches_check.isChecked() else "false")
        s.setValue("stay_awake", "true" if self.awake_check.isChecked() else "false")
        s.setValue("auto_reconnect", "true" if self.reconnect_check.isChecked() else "false")
        s.setValue("mouse_finger", "true" if self.controls.direct_check.isChecked() else "false")
        s.setValue("show_stats", "true" if self.show_stats_check.isChecked() else "false")
        s.setValue("smooth_picture", "true" if self.smooth_check.isChecked() else "false")
        s.sync()

    def _apply_quality_preset(self, key: str) -> None:
        preset = QUALITY_PRESETS.get(key)
        if preset is None:
            return
        _, size, fps, mbps = preset
        self._select(self.res_combo, size)
        self._select(self.fps_combo, fps)
        self.bitrate_spin.setValue(mbps)
        self._sync_quality_combo()

    def _sync_quality_combo(self) -> None:
        """Show the preset that matches the detailed options, or 'Custom'."""
        current = (self.res_combo.currentData(), self.fps_combo.currentData(), self.bitrate_spin.value())
        key = next((k for k, (_, size, fps, mbps) in QUALITY_PRESETS.items() if (size, fps, mbps) == current), "custom")
        self.quality_combo.blockSignals(True)
        self._select(self.quality_combo, key)
        self.quality_combo.blockSignals(False)

    @staticmethod
    def _repolish(widget) -> None:
        """Make Qt re-read the style sheet after an object name change (so the button changes colour)."""
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _on_smooth_toggled(self, on: bool) -> None:
        self.video.set_smooth(on)
        self.settings.setValue("smooth_picture", "true" if on else "false")
        self._update_smooth_note()
        self.overlay.show_toast("Smooth picture " + ("on" if on else "off"))

    def _update_smooth_note(self) -> None:
        self.smooth_note.setText("Now: " + self.video.smooth_state() if self.smooth_check.isChecked() else "")

    def _on_show_stats_toggled(self, on: bool) -> None:
        if not on:
            self.stats_label.setText("")

    @staticmethod
    def _select(combo: QComboBox, data) -> None:
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _stream_config(self) -> StreamConfig:
        return StreamConfig(
            max_size=self.res_combo.currentData(), max_fps=self.fps_combo.currentData(),
            bit_rate=self.bitrate_spin.value() * 1_000_000, codec=self.codec_combo.currentData(),
            show_touches=self.touches_check.isChecked(), stay_awake=self.awake_check.isChecked(),
        )

    # -- devices ---------------------------------------------------------------------------
    def _on_devices(self, devices: list[DeviceInfo]) -> None:
        self.devices = devices
        current = self.device_combo.currentData()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for device in devices:
            self.device_combo.addItem(device.label, device.serial)
        index = self.device_combo.findData(current if current else self._wanted_serial)
        self.device_combo.setCurrentIndex(max(index, 0))
        self.device_combo.blockSignals(False)
        self.device_combo.setVisible(len(devices) > 1)
        self._refresh_device_status()
        self._update_buttons()
        self._maybe_reconnect()

    def _selected_device(self) -> DeviceInfo | None:
        serial = self.device_combo.currentData()
        return next((d for d in self.devices if d.serial == serial), None)

    def _refresh_device_status(self) -> None:
        if self.worker is not None or self.retry_timer.isActive():
            return
        device = self._selected_device()
        if not self.devices:
            self._set_status("No phone found yet. Plug it in with a USB cable.", theme.WARN)
            self.video.set_placeholder(WELCOME)
        elif device is None:
            self._set_status("Choose your phone from the list.", theme.WARN)
            self.video.set_placeholder(READY)
        elif device.usable:
            name = device.model.replace("_", " ") or device.serial
            self._set_status(f"Phone found: {name}. Ready to connect.", theme.OK)
            self.video.set_placeholder(READY)
        else:
            self._set_status(device.hint or f"The phone says: {device.state}", theme.WARN)
            self.video.set_placeholder(ALLOW if device.state in ("unauthorized", "authorizing") else WELCOME)

    def _restart_adb(self) -> None:
        log.info("Restarting the ADB server")
        try:
            self.adb.run("kill-server", check=False)
            self.adb.start_server()
        except AdbError as exc:
            self._set_status(f"Could not restart ADB: {exc}", theme.ERROR)

    # -- connection ------------------------------------------------------------------------
    def _toggle_connection(self) -> None:
        if self.worker is not None or self.retry_timer.isActive():
            self._wanted_serial = None
            self.retry_timer.stop()
            if self.worker is not None:
                self._stop_worker()
            else:
                self._set_connected(False)
                self._refresh_device_status()
            return
        device = self._selected_device()
        if device is None or not device.usable:
            return
        self._wanted_serial = device.serial
        self._fail_count = 0
        self._start_worker(device)

    def _start_worker(self, device: DeviceInfo) -> None:
        self._save_settings()
        config = self._stream_config()
        worker = MirrorWorker(self.adb, device.serial, config, self.decoder_combo.currentData(), self)
        worker.status.connect(lambda msg: self._set_status(msg, theme.MUTED))
        worker.connected.connect(self._on_connected)
        worker.videoSizeChanged.connect(self._on_video_size)
        worker.frameReady.connect(self._on_frame)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self.worker = worker
        self._device_summary = device.label
        self._set_connected(True)
        self._set_status("Connecting to your phone...", theme.MUTED)
        self.video.set_placeholder("<div style='font-size:20px; color:#8a93a8'>Connecting to your phone...</div>")
        worker.start()

    def _on_connected(self, name: str, codec: str, backend: str) -> None:
        self._fail_count = 0
        if self.worker is not None:
            self._start_foreground_watch(self.worker.serial)
        self._set_status(f"Connected: {name}", theme.OK)
        self.statusBar().showMessage(f"{codec.upper()} video, decoder: {backend}", 6000)
        self.stats_timer.start()
        profile = self.profile
        if profile is not None and not (profile.bindings or profile.sticks or profile.looks):
            self.controls.set_hint("Tip: press 'Edit controls' to add keys, or '+ New' to start from a ready-made "
                                   "layout.")

    def _on_video_size(self, width: int, height: int) -> None:
        self.statusBar().showMessage(f"Phone screen {width} x {height}")
        previous, self._last_video_size = self._last_video_size, (width, height)
        if previous == (0, 0) or previous == (width, height):
            return                                            # the first size of a session, or no change
        log.info("Phone screen changed from %dx%d to %dx%d", *previous, width, height)
        self.engine.screen_changed()                          # lift every finger (at the new size), cancel every action
        profile = self.profile
        orientation = orientation_of(width, height)
        playing = self.engine.mapping_active or self._scene_paused
        if playing and profile is not None and profile.orientation not in ("any", orientation):
            self._set_mapping(False)
            self._rotation_paused = True                      # (set after _set_mapping, which clears it)
            self.controls.set_hint(f"The phone turned to {orientation} mode, but these controls are for "
                                   f"{profile.orientation} mode, so key mapping was paused. It starts again by itself when "
                                   f"the phone turns back.")
            self.statusBar().showMessage("Key mapping paused: the phone turned", 6000)
        else:
            self._try_resume_after_rotation()
        self._refresh_overlay(self.engine.mapping_active)

    def _try_resume_after_rotation(self) -> None:
        """Start key mapping again when the phone is back in the orientation the controls were made for."""
        profile = self.profile
        if not self._rotation_paused or profile is None or self.worker is None:
            return
        if self._add_state or self.overlay.edit or not self.isActiveWindow():
            return                                            # never interrupt editing, or capture the mouse in the background
        if profile.orientation not in ("any", orientation_of(*self.worker.video_size)):
            return
        self._rotation_paused = False
        self._set_mapping(True)
        if self.engine.mapping_active:
            self.controls.set_hint("The phone is back in the right orientation: key mapping is on again.")
    def _on_frame(self) -> None:
        if self.worker is None:
            return
        frame, stamp = self.worker.take_frame()
        if frame is not None:
            self.video.set_frame(frame, stamp)

    def _on_failed(self, message: str) -> None:
        self._fail_count += 1
        self._set_status(message.splitlines()[0] if message else "Connection failed", theme.ERROR)
        self.statusBar().showMessage("Connection failed. Settings > Help & reset has more details.", 8000)

    def _on_worker_finished(self, worker: MirrorWorker) -> None:
        if worker is not self.worker:
            return
        self.worker = None
        self._last_video_size = (0, 0)
        self._set_edit_mode(False)
        self._stop_foreground_watch()
        if self._add_state:
            self._end_add()
        self._set_mapping(False)
        self.stats_timer.stop()
        self.stats_label.setText("")
        self.screen_off_btn.setChecked(False)
        self.video.clear()
        self._set_connected(False)
        if self._reapply:                                  # 'Apply now': come straight back with the new settings
            self._reapply = False
            device = next((d for d in self.devices if d.serial == self._wanted_serial), None)
            if device is not None and device.usable:
                self._start_worker(device)
                return
        if self._wanted_serial and self.reconnect_check.isChecked() and self._fail_count < MAX_AUTO_RETRIES:
            self.video.set_placeholder("<div style='font-size:20px; color:#ffb020'>Connection lost - reconnecting...</div>")
            self._set_status("Connection lost - waiting to reconnect...", theme.WARN)
            self.retry_timer.start()
            self.connect_btn.setText("Cancel reconnect")
            self.connect_btn.setObjectName("danger")
            self._repolish(self.connect_btn)
            self.connect_btn.setEnabled(True)
        else:
            self._wanted_serial = None
            self._refresh_device_status()

    def _maybe_reconnect(self) -> None:
        if self.worker is not None:
            self.retry_timer.stop()
            return
        if not self._wanted_serial:
            return
        device = next((d for d in self.devices if d.serial == self._wanted_serial), None)
        if device is not None and device.usable:
            self.retry_timer.stop()
            log.info("Reconnecting to %s", device.label)
            self._start_worker(device)

    def _set_connected(self, connected: bool) -> None:
        for widget in (self.device_combo, self.refresh_btn):
            widget.setEnabled(not connected)
        self.apply_stream_btn.setEnabled(connected)
        self.look_panel.set_connected(connected)
        for button in (self.back_btn, self.home_btn, self.recents_btn, self.screen_off_btn):
            button.setEnabled(connected)
        self.controls.mapping_btn.setEnabled(connected)
        self.controls.mapping_btn.setToolTip("Turn your keys and mouse on for the game. Esc stops." if connected
                                             else "Connect your phone first (step 1).")
        self.controls.edit_btn.setEnabled(connected)
        self.controls.edit_btn.setToolTip("Move, add or change keys right on the phone picture." if connected
                                          else "Connect your phone first (step 1).")
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        self.connect_btn.setObjectName("danger" if connected else "primary")
        self._repolish(self.connect_btn)
        self._update_buttons()

    def _update_buttons(self) -> None:
        if self.worker is not None:
            self.connect_btn.setEnabled(True)
            return
        device = self._selected_device()
        self.connect_btn.setEnabled(self.retry_timer.isActive() or (device is not None and device.usable))
        self._refresh_device_status()

    # -- phone controls --------------------------------------------------------------------
    def _send_key(self, keycode: int) -> None:
        worker = self.worker
        if worker is not None and worker.control is not None:
            worker.control.key(keycode)

    def _toggle_phone_screen(self, off: bool) -> None:
        worker = self.worker
        if worker is not None and worker.control is not None:
            worker.control.send(protocol.encode_set_display_power(not off))

    # -- misc ------------------------------------------------------------------------------
    def _update_stats(self) -> None:
        if self.worker is None:
            return
        s = self.worker.snapshot()
        self._update_smooth_note()
        if not self.show_stats_check.isChecked():
            self.stats_label.setText("")
            return
        smooth = f" | smooth: {self.video.smooth_state()}" if self.smooth_check.isChecked() else ""
        self.stats_label.setText(
            f"{s.fps:.0f} fps (shown {s.shown_fps:.0f}) | {s.mbps:.1f} Mbps | decode {s.decode_ms:.1f} ms | "
            f"prep {s.convert_ms:.1f} ms | PC latency {self.video.last_paint_latency_ms:.0f} ms | "
            f"dropped {s.dropped}{smooth}"
        )

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {theme.TEXT};")
        self.status_dot.setStyleSheet(f"color: {color};")

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self._leave_fullscreen()
        else:
            self.sidebar.hide()
            self.toolbar.hide()
            self.statusBar().hide()
            self.showFullScreen()

    def _leave_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.sidebar.show()
            self.toolbar.show()
            self.statusBar().show()

    def _diagnostics_text(self) -> str:
        lines = [f"Mobile Key One {__version__}", f"scrcpy server {paths.SCRCPY_VERSION}"]
        try:
            lines.append(self.adb.version())
        except AdbError as exc:
            lines.append(f"adb: {exc}")
        for device in self.devices:
            lines.append(f"device {device.label} state={device.state}")
            if device.usable:
                try:
                    lines.append("  " + ", ".join(f"{k}={v}" for k, v in self.adb.device_details(device.serial).items()))
                except AdbError as exc:
                    lines.append(f"  details unavailable: {exc}")
        if self.worker is not None:
            lines.append(f"mirroring: {self._device_summary}")
        return "\n".join(lines)

    def closeEvent(self, event) -> None:
        self._set_capture(False)
        self._wanted_serial = None
        self.retry_timer.stop()
        self._scene_timer.stop()
        self._save_settings()
        self._stop_foreground_watch()
        self._stop_worker()
        self.tracker.request_stop()
        self.hide()                                        # the window is gone at once; the threads end behind it
        self.stuck_threads = self._wait_for_threads([self.worker, self.tracker, *self._retiring], SHUTDOWN_WAIT_MS)
        if self.stuck_threads:
            log.warning("%d background thread(s) did not end in %d ms; the app will exit without them",
                        len(self.stuck_threads), SHUTDOWN_WAIT_MS)
        self.log_panel.detach()
        super().closeEvent(event)

    def _wait_for_threads(self, threads, timeout_ms: int) -> list:
        """Wait until the threads have ended while still processing events (the GUI thread is never blocked), at most
        `timeout_ms`. Returns the threads that are STILL running after that. They are never terminated (QThread.terminate
        crashes the process when the thread is inside Python code); main() leaves them behind with os._exit instead."""
        running = {t for t in threads if t is not None and callable(getattr(t, "isRunning", None)) and t.isRunning()}
        if running:
            loop = QEventLoop()

            def ended(thread) -> None:
                running.discard(thread)
                if not running:
                    loop.quit()
            for thread in list(running):
                thread.finished.connect(lambda t=thread: ended(t))
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            timer.start(timeout_ms)
            running = {t for t in running if t.isRunning()}
            if running:
                loop.exec()
            timer.stop()
        return [t for t in running if t.isRunning()]
