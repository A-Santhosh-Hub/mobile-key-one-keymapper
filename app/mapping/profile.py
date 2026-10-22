"""Control profiles: which key / mouse button taps which point of the phone screen.

Positions are stored as fractions (0..1) of the video frame, so a profile works at any
resolution. Profiles are plain JSON files, one per game.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

PROFILE_VERSION = 1                 # the legacy format: one flat record per binding, no binding ids in the file
PROFILE_VERSION_2 = 2               # binding records with `triggers` / `actions` / `on_failure` and a persisted binding id
LATEST_PROFILE_VERSION = PROFILE_VERSION_2      # the newest format this app can read; anything above is refused untouched

# What a key does when pressed:
#   hold      touch the point while the key is down            tap       one quick touch
#   toggle    first press touches and keeps touching, next press lets go
#   repeat    touch again and again while the key is held (auto-click)      double   two quick taps
#   swipe     drag from the point to a second point and let go             drag     same, but stays at the end until release
#   pinch-out two fingers move apart (zoom in)                             pinch-in two fingers move together (zoom out)
#   sequence  touch several points in order, with delays between them
MODES = ("hold", "tap", "toggle", "repeat", "double", "swipe", "drag", "pinch-out", "pinch-in", "sequence")
MODE_SHORT = {"hold": "hold", "tap": "tap", "toggle": "toggle", "repeat": "repeat", "double": "double tap",
              "swipe": "swipe", "drag": "drag", "pinch-out": "zoom in", "pinch-in": "zoom out", "sequence": "multi-tap"}
MODE_HELP = {
    "hold": "Touches the spot for as long as the key is held (fire, aim).",
    "tap": "One quick touch each time the key is pressed (reload, jump, map).",
    "toggle": "Press once to start touching and keep touching, press again to let go (sprint lock, hold-to-toggle buttons).",
    "repeat": "Taps again and again while the key is held (auto-click). Some online games treat this as cheating.",
    "double": "Two quick taps for each key press.",
    "swipe": "Drags from the spot to a second spot and lets go (slide, lean, throw).",
    "drag": "Drags from the spot to a second spot and stays there until the key is released.",
    "pinch-out": "Two fingers move apart around the spot (zoom in on a map).",
    "pinch-in": "Two fingers move together around the spot (zoom out on a map).",
    "sequence": "Touches several spots one after another (open a menu, pick an item).",
}
TWO_POINT_MODES = ("swipe", "drag")
ALSO_MODES = ("hold", "tap", "toggle")     # modes where one key can touch extra spots at the same time
MAX_ALSO = 4
PINCH_MODES = ("pinch-out", "pinch-in")
ORIENTATIONS = ("any", "portrait", "landscape")
ON_FAILURE = ("continue", "abort")         # what a binding does when one of its actions cannot start (see Binding.on_failure)
MAX_DELAY_MS = 60_000                      # longest delay before an additional action starts
MAX_TOUCH_POINTERS = 10                    # fingers the phone can track at once (equals stream.protocol.MAX_POINTERS)
SAME_SPOT = 0.005                          # two touch points closer than this (fraction of the screen) are "the same spot"

# What the input engine executes TODAY. Profile.analyze() warns about stored settings the engine ignores; the engine step
# adds its capabilities here so those warnings disappear exactly when the behaviour exists.
#   "extra_triggers"  the engine reacts to a binding's additional triggers
#   "extra_actions"   the engine starts a binding's additional actions
#   "shared_triggers" the engine runs EVERY binding of an explicitly authorized sharing group (Profile.shared_triggers); several
#                     bindings on one trigger WITHOUT that authorization still run only the winner of trigger_owners
RUNTIME_SUPPORTS: frozenset = frozenset({"extra_triggers", "extra_actions", "shared_triggers"})

ALWAYS_RESERVED = {"Esc": "Esc always stops playing and releases the mouse"}     # see hotkeys.validate

MODIFIERS = ("Ctrl", "Shift", "Alt")       # canonical order inside a combination name, e.g. "Ctrl+Shift+W"


def split_combo(name: str) -> tuple[frozenset, str]:
    """'Shift+W' -> ({'Shift'}, 'W'). A name without modifiers gives an empty set."""
    mods: list[str] = []
    rest = name
    changed = True
    while changed:
        changed = False
        for modifier in MODIFIERS:
            if rest.startswith(modifier + "+") and len(rest) > len(modifier) + 1:
                mods.append(modifier)
                rest = rest[len(modifier) + 1:]
                changed = True
                break
    return frozenset(mods), rest


def make_combo(mods, base: str) -> str:
    return "+".join([m for m in MODIFIERS if m in mods] + [base])


class ProfileError(ValueError):
    pass


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def valid_binding_id(value) -> bool:
    return isinstance(value, str) and _ID_PATTERN.match(value) is not None


def new_binding_id() -> str:
    return "b" + uuid.uuid4().hex[:10]


def new_action_uid() -> str:
    """A runtime identity for one additional action (never written to a file: see Action.uid)."""
    return "a" + uuid.uuid4().hex[:8]


def canonical_trigger(name) -> str:
    """The canonical spelling of an input name ('Shift+Ctrl+W' -> 'Ctrl+Shift+W'). Raises ProfileError if it is unusable."""
    if not isinstance(name, str) or not name:
        raise ProfileError("A binding needs an input key")
    mods, base = split_combo(name)
    if mods and base in MODIFIERS:
        raise ProfileError("A key combination needs a normal key after the modifiers (e.g. Shift+W)")
    return make_combo(mods, base)


def _clean_spots(spots) -> list:
    cleaned = []
    for spot in list(spots)[:MAX_ALSO]:
        try:
            ax, ay = spot
            cleaned.append((_clamp01(ax), _clamp01(ay)))
        except (TypeError, ValueError) as exc:
            raise ProfileError(f"Invalid extra spot {spot!r}") from exc
    return cleaned


def _clean_steps(steps) -> list:
    cleaned = []
    for step in steps:
        try:
            sx, sy, delay = step
            cleaned.append((_clamp01(sx), _clamp01(sy), max(0, min(5000, int(delay)))))
        except (TypeError, ValueError) as exc:
            raise ProfileError(f"Invalid multi-tap step {step!r}") from exc
    return cleaned


def _normalise_action_fields(a) -> None:
    """Clamp and complete the fields that describe what an action does. Works on a Binding or an Action alike: both
    carry the same field names, so the two can never drift apart."""
    a.x, a.y = _clamp01(a.x), _clamp01(a.y)
    a.rate_hz = max(1.0, min(30.0, float(a.rate_hz)))
    a.duration_ms = max(40, min(3000, int(a.duration_ms)))
    a.span = max(0.02, min(0.40, float(a.span)))
    a.scope = bool(a.scope)
    a.also = _clean_spots(a.also)
    if a.mode in TWO_POINT_MODES:
        if a.x2 is None or a.y2 is None:
            a.x2, a.y2 = _clamp01(a.x + 0.15), a.y
        a.x2, a.y2 = _clamp01(a.x2), _clamp01(a.y2)
    if a.mode == "sequence":
        a.steps = _clean_steps(a.steps) or [(a.x, a.y, 0)]


def _action_tail(a) -> dict:
    """The mode-specific fields of an action as they are written to a file, in the fixed order of the version-1 format."""
    data: dict = {}
    if a.scope:
        data["scope"] = True
    if a.mode in TWO_POINT_MODES:
        data.update(x2=round(a.x2, 5), y2=round(a.y2, 5), duration_ms=a.duration_ms)
    if a.mode == "repeat":
        data["rate_hz"] = a.rate_hz
    if a.mode in PINCH_MODES:
        data.update(span=round(a.span, 4), duration_ms=a.duration_ms)
    if a.mode == "sequence":
        data["steps"] = [[round(sx, 5), round(sy, 5), delay] for sx, sy, delay in a.steps]
    if a.also:
        data["also"] = [[round(ax, 5), round(ay, 5)] for ax, ay in a.also]
    return data


def _with_unknown(data: dict, unknown: dict) -> dict:
    """Append the fields this version does not understand; a known field is never overwritten by one of them."""
    for key, value in unknown.items():
        data.setdefault(key, value)
    return data


def _split_unknown(raw: dict, known: set) -> dict:
    return {key: value for key, value in raw.items() if key not in known}


def check_delay_ms(value) -> int:
    """A delay before an additional action starts: a whole number of milliseconds, 0..MAX_DELAY_MS."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError(f"An action delay must be a whole number of milliseconds, not {value!r}")
    if not 0 <= value <= MAX_DELAY_MS:
        raise ProfileError(f"An action delay must be between 0 and {MAX_DELAY_MS} ms, not {value}")
    return value


def check_on_failure(value) -> str:
    if value not in ON_FAILURE:
        raise ProfileError(f"Unknown failure policy {value!r}; use one of {', '.join(ON_FAILURE)}")
    return value


SEVERITIES = ("error", "warning", "info")       # most serious first


@dataclass(frozen=True)
class Issue:
    """One finding of Profile.analyze(). Plain data, so a UI can list it, sort it or turn it into JSON."""
    code: str                                  # stable machine name, e.g. "trigger_is_stick_key"
    severity: str                              # "error": cannot work / is invalid, "warning": will not do what it says, "info"
    message: str                               # one plain sentence for the user
    binding_ids: tuple = ()                    # the bindings concerned (Binding.id), in list order
    positions: tuple = ()                      # the same bindings' 0-based positions in Profile.bindings
    trigger: str = ""                          # the input concerned, if any

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message,
                "binding_ids": list(self.binding_ids), "positions": list(self.positions), "trigger": self.trigger}


_ACTION_KEYS = {"mode", "x", "y", "x2", "y2", "rate_hz", "duration_ms", "span", "scope", "steps", "also", "delay_ms"}
_BINDING_KEYS_V1 = {"type", "input", "x", "y", "mode", "label", "x2", "y2", "rate_hz", "duration_ms", "span", "scope",
                    "steps", "also"}
_BINDING_KEYS_V2 = {"type", "id", "label", "triggers", "actions", "on_failure"}
_PROFILE_KEYS = {"id", "version", "name", "orientation", "package", "scene", "bindings", "sticks", "looks", "smart_loot"}
_SHARING_ENTRY_KEYS = {"trigger", "bindings"}


@dataclass
class Action:
    """One thing a binding does: the same fields as the binding's own primary action, plus a start delay.

    An additional action (`Binding.extra_actions`) has its own mode and settings. `delay_ms` is how long after the
    binding is activated it starts (0 = at once); it is always measured from the activation, never from the end of another
    action. `also` are extra touch spots that share THIS action's mode, exactly like `Binding.also`.
    Fields a newer version wrote that this one does not know are kept in `unknown` and written back.
    """
    mode: str
    x: float
    y: float
    x2: float | None = None
    y2: float | None = None
    rate_hz: float = 10.0
    duration_ms: int = 160
    span: float = 0.10
    scope: bool = False
    steps: list = field(default_factory=list)
    also: list = field(default_factory=list)
    delay_ms: int = 0
    unknown: dict = field(default_factory=dict, compare=False, repr=False)
    uid: str = field(default="", compare=False, repr=False)       # runtime identity inside its binding; NEVER written to a file

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ProfileError(f"Unknown binding mode: {self.mode!r}")
        check_delay_ms(self.delay_ms)
        _normalise_action_fields(self)
        if not self.uid:
            self.uid = new_action_uid()

    def set_mode(self, mode: str) -> None:
        """Change the mode and fill in what the new mode needs (a second point, a first step), like Binding.set_mode."""
        if mode not in MODES:
            raise ProfileError(f"Unknown binding mode: {mode!r}")
        self.mode = mode
        if mode in TWO_POINT_MODES and (self.x2 is None or self.y2 is None):
            self.x2, self.y2 = _clamp01(self.x + 0.15), self.y
        if mode == "sequence" and not self.steps:
            self.steps = [(self.x, self.y, 0)]

    def to_dict(self, primary: bool = False) -> dict:
        data = {"mode": self.mode, "x": round(self.x, 5), "y": round(self.y, 5), **_action_tail(self)}
        if not primary:
            data["delay_ms"] = self.delay_ms                   # the primary action always starts at once: nothing to write
        return _with_unknown(data, self.unknown)

    @staticmethod
    def from_dict(data: dict, primary: bool = False) -> "Action":
        try:
            if not isinstance(data, dict):
                raise ProfileError(f"An action must be an object, not {data!r}")
            if primary and data.get("delay_ms", 0) != 0:
                raise ProfileError("The first action starts at once; only additional actions can have a delay")
            return Action(
                mode=str(data["mode"]), x=float(data["x"]), y=float(data["y"]),
                x2=None if data.get("x2") is None else float(data["x2"]),
                y2=None if data.get("y2") is None else float(data["y2"]),
                rate_hz=float(data.get("rate_hz", 10.0)), duration_ms=int(data.get("duration_ms", 160)),
                span=float(data.get("span", 0.10)), scope=bool(data.get("scope", False)),
                steps=[tuple(step) for step in data.get("steps", [])],
                also=[tuple(spot) for spot in data.get("also", [])],
                delay_ms=data.get("delay_ms", 0),
                unknown=_split_unknown(data, _ACTION_KEYS))
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ProfileError):
                raise
            raise ProfileError(f"Invalid action {data!r}: {exc}") from exc


@dataclass
class Binding:
    """One logical control: one or more inputs (triggers) that start one or more actions.

    The fields up to `also` are the binding's FIRST trigger (`input`) and its PRIMARY action, exactly as before, so every
    existing caller keeps working. The newer capabilities are additive:
      extra_triggers   more inputs that mean the same thing as `input` ("F", "MMB", "Shift+E")
      extra_actions    more actions with their own mode and settings (see Action); `also` stays "more spots for the
                       primary action", it is not an action
      on_failure       "continue": an action that cannot start does not stop the others; "abort": it stops the binding
      id               identity inside the profile. Version-1 files do not store it (it is made up when the profile is
                       loaded and stays the same for the life of that object); version-2 files store it.
    `triggers` and `actions` are read-only views over all of it. The input engine runs all of it: any trigger starts the binding
    and every action starts (the first at once, the others after their `delay_ms`). Several BINDINGS on one trigger all run only
    when the profile explicitly authorizes it (Profile.shared_triggers, see trigger_runners); otherwise one of them wins
    (trigger_owners).
    Fields a newer version wrote that this one does not know are kept (`unknown`, `primary_unknown`) and written back.
    """
    input: str                      # "W", "Space", "LMB", "WheelUp", "Shift+W", ...
    x: float
    y: float
    mode: str = "hold"
    label: str = ""
    x2: float | None = None         # swipe / drag: where the finger goes
    y2: float | None = None
    rate_hz: float = 10.0           # repeat: taps per second
    duration_ms: int = 160          # swipe / drag / pinch: how long the movement takes
    span: float = 0.10              # pinch: finger distance from the centre (fraction of the shorter screen side)
    scope: bool = False             # this is the scope / aim button: mouse sensitivity drops while it is active
    steps: list = field(default_factory=list)      # sequence: [(x, y, delay_ms before this tap), ...]
    also: list = field(default_factory=list)       # hold / tap / toggle: extra spots touched together [(x, y), ...]
    id: str = field(default="", compare=False)     # "" = make one up
    extra_triggers: list = field(default_factory=list)
    extra_actions: list = field(default_factory=list)
    on_failure: str = "continue"
    unknown: dict = field(default_factory=dict, compare=False, repr=False)
    primary_unknown: dict = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.input:
            raise ProfileError("A binding needs an input key")
        if self.mode not in MODES:
            raise ProfileError(f"Unknown binding mode: {self.mode!r}")
        mods, base = split_combo(self.input)
        if mods and base in MODIFIERS:
            raise ProfileError("A key combination needs a normal key after the modifiers (e.g. Shift+W)")
        self.input = make_combo(mods, base)
        _normalise_action_fields(self)
        if self.id and not valid_binding_id(self.id):
            raise ProfileError(f"Invalid binding id {self.id!r}")
        if not self.id:
            self.id = new_binding_id()
        check_on_failure(self.on_failure)
        triggers = [self.input]
        for name in self.extra_triggers:
            name = canonical_trigger(name)
            if name in triggers:
                raise ProfileError(f"{name} is listed twice for the same control")
            triggers.append(name)
        self.extra_triggers = triggers[1:]
        for extra in self.extra_actions:
            if not isinstance(extra, Action):
                raise ProfileError(f"An additional action must be an Action, not {extra!r}")
        self.extra_actions = list(self.extra_actions)

    def set_mode(self, mode: str) -> None:
        """Change the mode and fill in what the new mode needs (a second point, a first step)."""
        if mode not in MODES:
            raise ProfileError(f"Unknown binding mode: {mode!r}")
        self.mode = mode
        if mode in TWO_POINT_MODES and (self.x2 is None or self.y2 is None):
            self.x2, self.y2 = _clamp01(self.x + 0.15), self.y
        if mode == "sequence" and not self.steps:
            self.steps = [(self.x, self.y, 0)]

    @property
    def combo(self) -> tuple[frozenset, str]:
        return split_combo(self.input)

    # -- canonical views ---------------------------------------------------------------------------------
    @property
    def triggers(self) -> tuple[str, ...]:
        """Every input that starts this binding, the first one first."""
        return (self.input, *self.extra_triggers)

    @property
    def actions(self) -> tuple[Action, ...]:
        """Every action of this binding, the primary one first. The primary action is a SNAPSHOT of the binding's own
        fields (change the binding, not the snapshot); the additional ones are the live objects of `extra_actions`."""
        return (self.primary_action(), *self.extra_actions)

    def primary_action(self) -> Action:
        return Action(self.mode, self.x, self.y, self.x2, self.y2, self.rate_hz, self.duration_ms, self.span, self.scope,
                      list(self.steps), list(self.also), 0, dict(self.primary_unknown))

    def drop_first_action(self) -> int:
        """Remove the binding's FIRST action: the next additional action becomes the first (its fields move into the binding's own, it now
        starts at once, and the fields a newer writer kept for it move with it). Returns the delay it had. Raises ProfileError if there is
        no additional action (a binding cannot be left with none)."""
        if not self.extra_actions:
            raise ProfileError("A control needs at least one action")
        nxt = self.extra_actions.pop(0)
        for name in ("mode", "x", "y", "x2", "y2", "rate_hz", "duration_ms", "span", "scope", "steps", "also"):
            setattr(self, name, getattr(nxt, name))
        self.primary_unknown = dict(nxt.unknown)
        self.__post_init__()
        return nxt.delay_ms

    # -- editing the triggers --------------------------------------------------------------------------------
    def add_trigger(self, name: str) -> str:
        """Add another input that starts this binding; returns its canonical name. Raises ProfileError if the name is
        unusable or the binding already has it. Nothing else changes."""
        name = canonical_trigger(name)
        if name in self.triggers:
            raise ProfileError(f"{name} is already one of this control's keys")
        self.extra_triggers.append(name)
        return name

    def remove_trigger(self, name: str) -> str:
        """Remove one input from this binding. Returns "removed" (an additional trigger went; the first one is unchanged)
        or "promoted" (the FIRST trigger went and the next one became the first; the action is exactly as it was).
        Raises ProfileError if the binding does not have that input, or if it is the only one (a binding cannot be left
        without a trigger; remove the binding instead)."""
        name = canonical_trigger(name)
        if name not in self.triggers:
            raise ProfileError(f"{name} is not one of this control's keys")
        if len(self.triggers) == 1:
            raise ProfileError(f"{name} is the only key of this control; remove the control instead")
        if name == self.input:
            self.input, self.extra_triggers = self.extra_triggers[0], self.extra_triggers[1:]
            return "promoted"
        self.extra_triggers.remove(name)
        return "removed"

    @property
    def needs_v2(self) -> bool:
        """True if the binding uses something the version-1 file format cannot hold."""
        return bool(self.extra_triggers or self.extra_actions or self.on_failure != "continue")

    # -- files ---------------------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        """The binding as a file record: the flat version-1 record, or (only if it needs it) the version-2 record.
        A Profile always writes ONE representation for all its bindings (see Profile.to_dict)."""
        return self.to_dict_v2() if self.needs_v2 else self.to_dict_v1()

    def to_dict_v1(self) -> dict:
        data = {"type": "tap", "input": self.input, "x": round(self.x, 5), "y": round(self.y, 5),
                "mode": self.mode, "label": self.label, **_action_tail(self)}
        return _with_unknown(data, self.unknown)

    def to_dict_v2(self) -> dict:
        data = {"type": "binding", "id": self.id, "label": self.label, "triggers": list(self.triggers),
                "actions": [self.primary_action().to_dict(primary=True), *(a.to_dict() for a in self.extra_actions)],
                "on_failure": self.on_failure}
        return _with_unknown(data, self.unknown)

    @staticmethod
    def from_dict(data: dict) -> "Binding":
        """A version-1 (flat) binding record."""
        try:
            return Binding(
                input=str(data["input"]), x=float(data["x"]), y=float(data["y"]),
                mode=str(data.get("mode", "hold")), label=str(data.get("label", "")),
                x2=None if data.get("x2") is None else float(data["x2"]),
                y2=None if data.get("y2") is None else float(data["y2"]),
                rate_hz=float(data.get("rate_hz", 10.0)), duration_ms=int(data.get("duration_ms", 160)),
                span=float(data.get("span", 0.10)), scope=bool(data.get("scope", False)),
                steps=[tuple(step) for step in data.get("steps", [])],
                also=[tuple(spot) for spot in data.get("also", [])],
                unknown=_split_unknown(data, _BINDING_KEYS_V1))
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ProfileError):
                raise
            raise ProfileError(f"Invalid binding {data!r}: {exc}") from exc

    @staticmethod
    def from_dict_v2(data: dict) -> "Binding":
        """A version-2 binding record. A missing or unusable id is left empty here; Profile.from_dict gives it a new one."""
        try:
            if not isinstance(data, dict):
                raise ProfileError(f"A binding must be an object, not {data!r}")
            triggers, actions = data["triggers"], data["actions"]
            if not isinstance(triggers, list) or not triggers or not all(isinstance(t, str) for t in triggers):
                raise ProfileError("A binding needs a list of one or more triggers")
            if not isinstance(actions, list) or not actions:
                raise ProfileError("A binding needs a list of one or more actions")
            primary = Action.from_dict(actions[0], primary=True)
            extras = [Action.from_dict(a) for a in actions[1:]]
            raw_id = data.get("id")
            return Binding(
                input=triggers[0], x=primary.x, y=primary.y, mode=primary.mode, label=str(data.get("label", "")),
                x2=primary.x2, y2=primary.y2, rate_hz=primary.rate_hz, duration_ms=primary.duration_ms,
                span=primary.span, scope=primary.scope, steps=primary.steps, also=primary.also,
                id=raw_id if valid_binding_id(raw_id) else "",
                extra_triggers=triggers[1:], extra_actions=extras, on_failure=data.get("on_failure", "continue"),
                unknown=_split_unknown(data, _BINDING_KEYS_V2), primary_unknown=primary.unknown)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ProfileError):
                raise
            raise ProfileError(f"Invalid binding {data!r}: {exc}") from exc


DIRECTIONS = ("up", "left", "down", "right")
DEFAULT_STICK_KEYS = {"up": "W", "left": "A", "down": "S", "right": "D"}
STICK_EXTRAS = ("sprint", "walk", "autorun")


@dataclass
class Stick:
    """A virtual joystick: four keys drag one finger away from (x, y) by up to `radius`.

    `radius` is a fraction of the shorter screen side, so the stick is round on any aspect ratio.
    `ramp_ms` is how long the finger takes to travel from the centre to full deflection.
    Optional keys: `sprint_key` held = push the stick `sprint_scale` times further (many games sprint when the
    stick is pushed to its edge), `walk_key` held = push only `walk_scale` of the way (slow, quiet walking),
    `autorun_key` pressed = keep walking forward until a backward key (or the key again) cancels it.
    """
    x: float
    y: float
    radius: float = 0.09
    keys: dict = field(default_factory=lambda: dict(DEFAULT_STICK_KEYS))
    ramp_ms: int = 50
    label: str = "Move"
    sprint_key: str = ""
    sprint_scale: float = 1.35
    walk_key: str = ""
    walk_scale: float = 0.5
    autorun_key: str = ""

    def __post_init__(self) -> None:
        self.x, self.y = _clamp01(self.x), _clamp01(self.y)
        self.radius = max(0.01, min(0.5, float(self.radius)))
        self.ramp_ms = max(0, min(500, int(self.ramp_ms)))
        self.sprint_scale = max(1.0, min(4.0, float(self.sprint_scale)))
        self.walk_scale = max(0.1, min(0.95, float(self.walk_scale)))
        if set(self.keys) != set(DIRECTIONS) or not all(self.keys.values()):
            raise ProfileError("A stick needs a key for up, left, down and right")
        if len(set(self.keys.values())) != 4:
            raise ProfileError("The four stick keys must all be different")
        extras = [k for k in (self.sprint_key, self.walk_key, self.autorun_key) if k]
        if len(set(extras)) != len(extras):
            raise ProfileError("The sprint, walk and auto-run keys must all be different")
        if set(extras) & set(self.keys.values()):
            raise ProfileError("The sprint, walk and auto-run keys cannot be the same as a direction key")

    def extra_keys(self) -> dict[str, str]:
        return {name: key for name, key in (("sprint", self.sprint_key), ("walk", self.walk_key),
                                            ("autorun", self.autorun_key)) if key}

    def key_set(self) -> set[str]:
        return set(self.keys.values()) | set(self.extra_keys().values())

    def to_dict(self) -> dict:
        return {"type": "stick", "x": round(self.x, 5), "y": round(self.y, 5), "radius": round(self.radius, 4),
                "keys": dict(self.keys), "ramp_ms": self.ramp_ms, "label": self.label,
                "sprint_key": self.sprint_key, "sprint_scale": round(self.sprint_scale, 3),
                "walk_key": self.walk_key, "walk_scale": round(self.walk_scale, 3), "autorun_key": self.autorun_key}

    @staticmethod
    def from_dict(data: dict) -> "Stick":
        try:
            return Stick(x=float(data["x"]), y=float(data["y"]), radius=float(data.get("radius", 0.09)),
                         keys=dict(data.get("keys", DEFAULT_STICK_KEYS)), ramp_ms=int(data.get("ramp_ms", 50)),
                         label=str(data.get("label", "Move")),
                         sprint_key=str(data.get("sprint_key", "")), sprint_scale=float(data.get("sprint_scale", 1.35)),
                         walk_key=str(data.get("walk_key", "")), walk_scale=float(data.get("walk_scale", 0.5)),
                         autorun_key=str(data.get("autorun_key", "")))
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ProfileError):
                raise
            raise ProfileError(f"Invalid stick {data!r}: {exc}") from exc


@dataclass
class Look:
    """Mouse look: mouse movement drags one finger inside the zone (x0, y0)-(x1, y1).

    Sensitivity is phone-screen pixels per mouse count. When the finger nears the zone edge it is lifted and
    put back in the centre, so the camera can keep turning in one direction. `ads_scale` multiplies the
    sensitivity while a binding marked as the scope button is active (1.0 = no change).
    """
    x0: float
    y0: float
    x1: float
    y1: float
    sens_x: float = 1.0
    sens_y: float = 1.0
    accel: float = 0.0           # 0 = none .. 1 = strong: fast flicks are amplified
    invert_y: bool = False
    label: str = "Look"
    ads_scale: float = 1.0

    MIN_SIZE = 0.06

    def __post_init__(self) -> None:
        a, b = sorted((_clamp01(self.x0), _clamp01(self.x1)))
        c, d = sorted((_clamp01(self.y0), _clamp01(self.y1)))
        if b - a < self.MIN_SIZE or d - c < self.MIN_SIZE:
            raise ProfileError("The look area is too small")
        self.x0, self.x1, self.y0, self.y1 = a, b, c, d
        self.sens_x = max(0.05, min(20.0, float(self.sens_x)))
        self.sens_y = max(0.05, min(20.0, float(self.sens_y)))
        self.accel = max(0.0, min(1.0, float(self.accel)))
        self.invert_y = bool(self.invert_y)
        self.ads_scale = max(0.1, min(1.5, float(self.ads_scale)))

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    def to_dict(self) -> dict:
        return {"type": "look", "x0": round(self.x0, 5), "y0": round(self.y0, 5), "x1": round(self.x1, 5),
                "y1": round(self.y1, 5), "sens_x": round(self.sens_x, 3), "sens_y": round(self.sens_y, 3),
                "accel": round(self.accel, 3), "invert_y": self.invert_y, "label": self.label,
                "ads_scale": round(self.ads_scale, 3)}

    @staticmethod
    def from_dict(data: dict) -> "Look":
        try:
            return Look(x0=float(data["x0"]), y0=float(data["y0"]), x1=float(data["x1"]), y1=float(data["y1"]),
                        sens_x=float(data.get("sens_x", 1.0)), sens_y=float(data.get("sens_y", 1.0)),
                        accel=float(data.get("accel", 0.0)), invert_y=bool(data.get("invert_y", False)),
                        label=str(data.get("label", "Look")), ads_scale=float(data.get("ads_scale", 1.0)))
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ProfileError):
                raise
            raise ProfileError(f"Invalid look area {data!r}: {exc}") from exc


def trigger_owners(bindings) -> dict:
    """Which binding the input engine runs for each trigger: {canonical trigger: Binding}. One binding per trigger, always.

    Several bindings can name the same trigger, but running all of them is not supported yet, so exactly one wins:
      * a binding's FIRST input beats an additional trigger of any binding (so a control that worked before keeps working
        when another control is given an extra key);
      * among first inputs, and among additional triggers, the LATER binding in the list wins (the rule the engine has
        always had for a repeated input).
    The engine builds its lookup from this and Profile.analyze() reports the losers from it, so they cannot disagree."""
    firsts: dict = {}
    extras: dict = {}
    for binding in bindings:
        firsts[make_combo(*split_combo(binding.input))] = binding
        for trigger in binding.extra_triggers:
            extras[make_combo(*split_combo(trigger))] = binding
    return {**extras, **firsts}


def _triggers_of(binding: Binding) -> set:
    return {make_combo(*split_combo(t)) for t in binding.triggers if isinstance(t, str) and t}


def effective_sharing(bindings, shared) -> dict:
    """The sharing groups that really exist: {canonical trigger: [bindings, in list order]}.

    `shared` is Profile.shared_triggers ({trigger: [binding ids]}), the explicit authorization. A participant counts only if its id
    names exactly ONE binding of the list and that binding uses the trigger (as its first input or an additional one); a group with
    fewer than two participants is not a group. Nothing else ever makes bindings share a trigger: repeated inputs alone do not."""
    counts: dict = {}
    for binding in bindings:
        counts[binding.id] = counts.get(binding.id, 0) + 1
    groups: dict = {}
    for trigger, ids in (shared or {}).items():
        trigger = make_combo(*split_combo(trigger))
        wanted = {i for i in ids if counts.get(i) == 1}
        members = [b for b in bindings if b.id in wanted and trigger in _triggers_of(b)]
        if len(members) >= 2:
            groups[trigger] = members
    return groups


def trigger_runners(bindings, shared=None) -> dict:
    """Which bindings the input engine runs for each trigger: {canonical trigger: (Binding, ...)}, in list order.

      * a trigger with an authorized sharing group (see effective_sharing) runs EVERY participant of the group; any other binding
        that uses the same trigger without being authorized does not run;
      * every other trigger runs exactly the one binding trigger_owners picks (the legacy rule), so profiles without sharing
        behave exactly as they always did.
    The engine builds its lookup from this and Profile.analyze() reports from it, so they cannot disagree."""
    runners = {trigger: (binding,) for trigger, binding in trigger_owners(bindings).items()}
    for trigger, members in effective_sharing(bindings, shared).items():
        runners[trigger] = tuple(members)
    return runners


def _name(binding: Binding) -> str:
    return f"'{binding.label}' ({binding.input})" if binding.label else f"the control for {binding.input}"


def _actions_of(binding: Binding) -> list:
    """(label, action-like object, delay) for every action, read WITHOUT validating: analyze() must be able to look at a
    binding somebody changed by hand. The primary action is the binding itself (it carries the same field names)."""
    return [("the first action", binding, 0)] + [(f"action {n}", a, getattr(a, "delay_ms", 0))
                                                 for n, a in enumerate(binding.extra_actions, 2)]


_RANGES = {"rate_hz": (1.0, 30.0), "duration_ms": (40, 3000), "span": (0.02, 0.40)}


def _check_action(add, index, binding, label, a, delay) -> None:
    where = [(index, binding)]
    if not isinstance(a, Action) and a is not binding:
        add("unsupported_action", "error", f"{_name(binding)}: {label} is not an action.", where)
        return
    mode = getattr(a, "mode", None)
    if mode not in MODES:
        add("invalid_mode", "error", f"{_name(binding)}: {label} has an unknown mode {mode!r}.", where)
        return
    try:
        check_delay_ms(delay)
    except ProfileError as exc:
        add("invalid_action_delay", "error", f"{_name(binding)}: {label}: {exc}", where)
    points = [(a.x, a.y)] + list(a.also)
    if mode in TWO_POINT_MODES:
        if a.x2 is None or a.y2 is None:
            add("missing_end_point", "error", f"{_name(binding)}: {label} ({mode}) needs an end point.", where)
        else:
            points.append((a.x2, a.y2))
    if mode == "sequence":
        points += [(sx, sy) for sx, sy, _ in a.steps]
    for point in points:
        try:
            ok = all(0.0 <= float(v) <= 1.0 for v in point)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            add("coordinate_out_of_range", "error", f"{_name(binding)}: {label} has a position {point!r} outside the screen.", where)
            break
    for field_name, (low, high) in _RANGES.items():
        try:
            ok = low <= getattr(a, field_name) <= high
        except TypeError:
            ok = False
        if not ok:
            add("setting_out_of_range", "error",
                f"{_name(binding)}: {label} has {field_name} = {getattr(a, field_name)!r}; it must be {low} to {high}.", where)
    if len(a.also) > MAX_ALSO:
        add("too_many_extra_spots", "error", f"{_name(binding)}: {label} has {len(a.also)} extra spots (at most {MAX_ALSO}).", where)
    if a.also and mode not in ALSO_MODES:
        add("extra_spots_ignored", "warning",
            f"{_name(binding)}: extra spots only work with {', '.join(ALSO_MODES)}; {label} ({mode}) ignores them.", where)
    if mode in ("hold", "repeat", "drag") and any(t in ("WheelUp", "WheelDown") for t in binding.triggers):
        add("wheel_runs_as_one_shot", "info",
            f"{_name(binding)}: a mouse-wheel notch runs {mode} as a quick one-shot, not for as long as it is turned.", where)


def _spots(a) -> list:
    points = [(a.x, a.y)] + list(a.also)
    if a.mode in TWO_POINT_MODES and a.x2 is not None and a.y2 is not None:
        points.append((a.x2, a.y2))
    if a.mode == "sequence":
        points += [(sx, sy) for sx, sy, _ in a.steps]
    return points


def _close(first: list, second: list) -> bool:
    try:
        return any(abs(ax - bx) <= SAME_SPOT and abs(ay - by) <= SAME_SPOT for ax, ay in first for bx, by in second)
    except (TypeError, ValueError):
        return False


def _clash(a: Binding, b: Binding) -> str:
    """Why two bindings that share an input should not both run, or "" if they can."""
    good = lambda binding: [x for _, x, _ in _actions_of(binding) if getattr(x, "mode", None) in MODES]
    first, second = good(a), good(b)
    if any(_close(_spots(x), _spots(y)) for x in first for y in second):
        return "they touch the same spot"
    if any(x.scope for x in first) and any(y.scope for y in second):
        return "both are scope buttons, so the scope would switch twice"
    return ""


def _pointers(binding: Binding) -> int:
    """Fingers the binding puts down at the same time: every action's, counted together (the worst case)."""
    total = 0
    for _, a, _ in _actions_of(binding):
        mode = getattr(a, "mode", None)
        if mode in PINCH_MODES:
            total += 2
        elif mode in MODES:
            total += 1 + (len(a.also) if mode in ALSO_MODES else 0)
    return total


def _holds(binding: Binding) -> bool:
    """True if the binding can keep a finger down (as opposed to a quick one-shot)."""
    return any(getattr(a, "mode", None) in ("hold", "toggle", "repeat", "drag") for _, a, _ in _actions_of(binding))


@dataclass
class Profile:
    name: str
    orientation: str = "any"
    bindings: list[Binding] = field(default_factory=list)
    sticks: list[Stick] = field(default_factory=list)
    looks: list[Look] = field(default_factory=list)
    package: str = ""          # Android app id this profile belongs to ("" = not linked to a game)
    scene: dict | None = None  # smart mode: {"enabled": bool, + the learned look of the game's controls (app/scene.py)}
    id: str = ""               # permanent identity, written by ProfileStore; "" until the profile is first saved.
                               # A copy made with to_dict() / from_dict() has no id, so it becomes a new profile.
    schema_version: int = field(default=1, compare=False)         # the file format this profile was LOADED from (1 or 2)
    repairs: list = field(default_factory=list, compare=False, repr=False)    # what loading had to fix (see from_dict)
    unknown: dict = field(default_factory=dict, compare=False, repr=False)    # top-level fields of a newer writer, kept
    notes: list = field(default_factory=list, compare=False, repr=False)      # what loading noticed but did not change
    shared_triggers: dict = field(default_factory=dict, compare=False)        # EXPLICIT sharing: {trigger: [binding ids]}; see share_trigger
    shared_unknown: dict = field(default_factory=dict, compare=False, repr=False)   # fields of a newer writer inside sharing entries, kept
    smart_loot: dict | None = None             # smart pick-up of a game's loot list (see app/mapping/loot.py); None = not set up

    @property
    def required_version(self) -> int:
        """The file format to save in: 2 if a binding uses a version-2-only capability, if two bindings share a first
        input (the flat version-1 form cannot keep both: it is read as "one binding per input"), or if the profile was
        loaded from a version-2 file (so its stored binding ids and any unknown fields are not lost); otherwise the legacy
        1, which is then byte-for-byte what it always was."""
        if (self.schema_version >= PROFILE_VERSION_2 or self.has_shared_inputs() or any(b.needs_v2 for b in self.bindings)
                or self.sharing_groups()):
            return PROFILE_VERSION_2
        return PROFILE_VERSION

    def has_shared_inputs(self) -> bool:
        """True if two bindings have the same first input."""
        seen = set()
        for binding in self.bindings:
            if binding.input in seen:
                return True
            seen.add(binding.input)
        return False

    def find(self, input_name: str) -> Binding | None:
        return next((b for b in self.bindings if b.input == input_name), None)

    # -- finding ------------------------------------------------------------------------------------------
    def bindings_for(self, trigger: str) -> list[Binding]:
        """Every binding that lists `trigger` among its triggers (its first one or an additional one), in list order.
        The name is read like a Binding input ('Shift+Ctrl+E' = 'Ctrl+Shift+E'); an unusable name matches nothing."""
        try:
            trigger = canonical_trigger(trigger)
        except ProfileError:
            return []
        return [b for b in self.bindings if trigger in b.triggers]

    def find_by_id(self, binding_id: str) -> Binding | None:
        return next((b for b in self.bindings if b.id == binding_id), None)

    def _need(self, binding_id: str) -> Binding:
        binding = self.find_by_id(binding_id)
        if binding is None:
            raise ProfileError(f"There is no control with id {binding_id!r}")
        return binding

    # -- explicit sharing ---------------------------------------------------------------------------------
    def sharing_groups(self) -> dict:
        """The sharing groups that really exist: {trigger: [bindings in list order]} (see effective_sharing)."""
        return effective_sharing(self.bindings, self.shared_triggers)

    def runners(self) -> dict:
        """{trigger: (bindings that run)} exactly as the input engine resolves it (see trigger_runners)."""
        return trigger_runners(self.bindings, self.shared_triggers)

    def is_shared(self, trigger: str) -> bool:
        return make_combo(*split_combo(trigger)) in self.sharing_groups()

    def share_trigger(self, trigger: str, binding_ids) -> list[str]:
        """AUTHORIZE the named bindings to run together on `trigger`: pressing it starts every one of them, in list order.
        This is the only way (besides add_trigger(shared=True) and add_shared(authorize=True)) that two bindings ever share a
        trigger at run time; repeated inputs alone never do. Everything is checked first, so a refusal changes nothing:
        at least two bindings, each named by an id that belongs to exactly one binding, each really using the trigger, and the
        trigger not being a stick key. Bindings already authorized for it stay. Returns the ids now authorized, in list order."""
        trigger = canonical_trigger(trigger)
        trigger = make_combo(*split_combo(trigger))
        ids = list(dict.fromkeys(binding_ids))
        if len(ids) < 2:
            raise ProfileError("Sharing needs at least two controls")
        if trigger in self.stick_keys():
            raise ProfileError(f"{trigger} is a movement-stick key")
        for binding_id in ids:
            if sum(1 for b in self.bindings if b.id == binding_id) != 1:
                raise ProfileError(f"There is no single control with id {binding_id!r}")
            if trigger not in _triggers_of(self.find_by_id(binding_id)):
                raise ProfileError(f"The control {binding_id!r} does not use {trigger}")
        merged = list(dict.fromkeys([*self.shared_triggers.get(trigger, []), *ids]))
        self.shared_triggers[trigger] = merged
        return [b.id for b in self.bindings if b.id in merged]

    def unshare_trigger(self, trigger: str, binding_id: str | None = None) -> bool:
        """Withdraw the authorization: of one binding (then the group goes if fewer than two remain) or, without an id, of the
        whole group. The bindings themselves are never touched. Returns False if there was nothing to withdraw."""
        trigger = make_combo(*split_combo(trigger))
        ids = self.shared_triggers.get(trigger)
        if ids is None or (binding_id is not None and binding_id not in ids):
            return False
        if binding_id is not None:
            ids = [i for i in ids if i != binding_id]
        if binding_id is None or len(ids) < 2:
            del self.shared_triggers[trigger]
            self.shared_unknown.pop(trigger, None)
        else:
            self.shared_triggers[trigger] = ids
        return True

    def prune_sharing(self) -> list[str]:
        """Drop authorizations that no longer describe anything: ids that are gone or ambiguous, bindings that no longer use the
        trigger, groups of fewer than two. Called by every operation that can make them stale, and by the editor after it deletes
        a binding. Returns what it removed, as sentences. Never adds an authorization."""
        notes: list[str] = []
        counts: dict = {}
        for b in self.bindings:
            counts[b.id] = counts.get(b.id, 0) + 1
        by_id = {b.id: b for b in self.bindings if counts[b.id] == 1}
        for trigger in list(self.shared_triggers):
            ids = self.shared_triggers[trigger]
            kept = [i for i in dict.fromkeys(ids) if i in by_id and trigger in _triggers_of(by_id[i])]
            if len(kept) < 2:
                del self.shared_triggers[trigger]
                self.shared_unknown.pop(trigger, None)
                notes.append(f"Sharing of {trigger} ended: fewer than two controls still take part")
            elif kept != ids:
                self.shared_triggers[trigger] = kept
                notes.append(f"Sharing of {trigger} lost {len(ids) - len(kept)} control(s) that no longer take part")
        return notes

    def _authorize(self, trigger: str) -> None:
        """Authorize every binding that uses `trigger` (if there are at least two)."""
        trigger = make_combo(*split_combo(trigger))
        users = [b.id for b in self.bindings if trigger in _triggers_of(b)]
        if len(users) >= 2 and sum(1 for i in users if sum(1 for b in self.bindings if b.id == i) == 1) == len(users):
            self.shared_triggers[trigger] = list(dict.fromkeys([*self.shared_triggers.get(trigger, []), *users]))

    # -- adding and removing bindings ---------------------------------------------------------------------
    def add_shared(self, binding: Binding, *, authorize: bool = False) -> Binding:
        """Add a binding WITHOUT replacing the others that use its first input: every binding stays, in list order, the new one
        last. (`add` is the one that replaces.) Adding does not by itself make them run together: that needs authorize=True
        (every input the new binding has in common with others becomes an authorized sharing group) or share_trigger().
        Without it the old rule applies and only one of them runs. Returns the binding."""
        if any(b.id == binding.id for b in self.bindings):
            binding.id = new_binding_id()
        self.bindings.append(binding)
        if authorize:
            for trigger in sorted(_triggers_of(binding)):
                self._authorize(trigger)
        return binding

    def remove_by_id(self, binding_id: str) -> Binding | None:
        """Remove exactly the binding with this id and return it (None if there is none). No other binding is touched,
        even one with the same inputs; the others keep their order. Sharing groups it belonged to lose it (and go if fewer
        than two remain)."""
        for index, binding in enumerate(self.bindings):
            if binding.id == binding_id:
                del self.bindings[index]
                self.prune_sharing()
                return binding
        return None

    # -- editing the triggers of one binding --------------------------------------------------------------
    def _check_trigger_free(self, trigger: str, binding: Binding, shared: bool) -> None:
        if trigger in self.stick_keys():
            raise ProfileError(f"{trigger} is a movement-stick key")
        others = [b for b in self.bindings if b is not binding and trigger in b.triggers]
        if others and not shared:
            raise ProfileError(f"{trigger} is already used by another control; share it on purpose with shared=True")

    def add_trigger(self, binding_id: str, trigger: str, *, shared: bool = False) -> str:
        """Give the binding another input. A stick key is never allowed. An input that another binding already uses is
        refused unless `shared=True` (sharing is always a choice); that other binding is never touched either way.
        With shared=True the trigger becomes an authorized sharing group of every control that uses it (see share_trigger).
        Returns the canonical name."""
        binding = self._need(binding_id)
        trigger = canonical_trigger(trigger)
        if trigger in binding.triggers:
            raise ProfileError(f"{trigger} is already one of this control's keys")
        self._check_trigger_free(trigger, binding, shared)
        added = binding.add_trigger(trigger)
        if shared:
            self._authorize(added)                                  # shared=True is the explicit authorization: every user takes part
        return added

    def remove_trigger(self, binding_id: str, trigger: str, *, remove_binding: bool = False) -> str:
        """Remove one input from one binding (matched by id, so no other binding can be affected). Returns "removed",
        "promoted" (the first input went, the next one took its place, the action is unchanged) or "binding_removed".
        If it is the binding's only input: ProfileError, or with `remove_binding=True` the binding goes (and only it)."""
        binding = self._need(binding_id)
        trigger = canonical_trigger(trigger)
        if trigger not in binding.triggers:
            raise ProfileError(f"{trigger} is not one of this control's keys")
        if len(binding.triggers) == 1:
            if not remove_binding:
                raise ProfileError(f"{trigger} is the only key of this control; remove the control instead")
            self.remove_by_id(binding_id)
            return "binding_removed"
        result = binding.remove_trigger(trigger)
        self.prune_sharing()                                         # it no longer uses that trigger: it leaves that group
        return result

    def move_trigger(self, trigger: str, from_id: str, to_id: str, *, shared: bool = False) -> None:
        """Take an input away from one binding and give it to another, as one step: everything is checked first, so a
        refusal changes nothing. The source must keep at least one other input; no binding is ever deleted."""
        source, target = self._need(from_id), self._need(to_id)
        trigger = canonical_trigger(trigger)
        if source is target:
            raise ProfileError("Choose two different controls")
        if trigger not in source.triggers:
            raise ProfileError(f"{trigger} is not one of the first control's keys")
        if trigger in target.triggers:
            raise ProfileError(f"{trigger} is already one of the second control's keys")
        if len(source.triggers) == 1:
            raise ProfileError(f"{trigger} is the only key of the first control; it would be left with none")
        others = [b for b in self.bindings if b is not source and b is not target and trigger in b.triggers]
        if others and not shared:
            raise ProfileError(f"{trigger} is already used by another control; share it on purpose with shared=True")
        source.remove_trigger(trigger)
        target.add_trigger(trigger)                                  # (the destination is NOT authorized by moving: see share_trigger)
        self.prune_sharing()

    def add(self, binding: Binding) -> None:
        """Add a binding, replacing any existing binding for the same (first) input. (To keep the others, add_shared.)"""
        self.bindings = [b for b in self.bindings if b.input != binding.input]
        if any(b.id == binding.id for b in self.bindings):         # an id is unique inside a profile
            binding.id = new_binding_id()
        self.bindings.append(binding)
        self.prune_sharing()

    def _shared_input_notes(self) -> list[str]:
        """For a version-1 file that lists one input more than once: what was kept (all of it). Older versions kept only
        the last binding of such a file and said nothing; the profile is now saved in the version-2 format, which can hold
        them all (the original file is copied to backup-before-v2/ first)."""
        places: dict = {}
        for number, binding in enumerate(self.bindings, 1):
            places.setdefault(binding.input, []).append(number)
        return [f"Bindings {', '.join(map(str, numbers))} all use the input {name!r}; all of them were kept, in this order. "
                "The profile will be saved in the version-2 format" for name, numbers in places.items() if len(numbers) > 1]

    def _ensure_unique_ids(self) -> list[str]:
        """Give a new id to every binding whose id repeats an earlier one (the first keeps it). Returns what it did."""
        seen, notes = set(), []
        for number, binding in enumerate(self.bindings, 1):
            if binding.id in seen:
                old, binding.id = binding.id, new_binding_id()
                notes.append(f"Binding {number} ({binding.input}) repeated the id {old!r}; it got a new one")
            seen.add(binding.id)
        return notes

    def remove(self, input_name: str) -> None:
        """Remove EVERY binding whose first input is `input_name`. To delete one particular binding use remove_by_id (the editor
        does): several bindings can share an input."""
        self.bindings = [b for b in self.bindings if b.input != input_name]
        self.prune_sharing()

    def lookup(self) -> dict[str, Binding]:
        return {b.input: b for b in self.bindings}

    def stick_keys(self) -> set[str]:
        return {k for stick in self.sticks for k in stick.key_set()}

    def key_owner(self, input_name: str) -> str | None:
        """Describes what an input is already used for ('another control' / 'the movement stick'), or None."""
        if self.find(input_name):
            return "another control"
        if input_name in self.stick_keys():
            return "the movement stick"
        return None

    def set_stick(self, stick: Stick) -> list[str]:
        """Replace the profile's stick. Tap bindings on its keys are removed; returns their names."""
        keys = stick.key_set()
        clash = [b.input for b in self.bindings if b.input in keys]
        self.bindings = [b for b in self.bindings if b.input not in keys]
        for binding in self.bindings:                              # an additional trigger on a stick key goes too
            taken = [t for t in binding.extra_triggers if t in keys]
            if taken:
                binding.extra_triggers = [t for t in binding.extra_triggers if t not in keys]
                clash += taken
        self.sticks = [stick]
        self.prune_sharing()
        return clash

    # -- analysis -----------------------------------------------------------------------------------------
    def analyze(self, *, reserved: Mapping[str, str] | None = None, runtime_supports: Collection[str] | None = None) -> list[Issue]:
        """Look for configuration problems. Pure: nothing in the profile is changed, and the result is always the same
        for the same profile (checks run in a fixed order, each in list order, then errors first, then warnings, then info).

        reserved          inputs the application itself uses, as {input: what it does}; build it from the window hotkeys,
                          e.g. {key: HOTKEY_LABELS[action] for action, key in hotkeys.items()}. Esc is always reserved.
        runtime_supports  what the input engine executes (default RUNTIME_SUPPORTS, nothing yet); settings it ignores
                          are reported as warnings so the user is not told a control works when it does not.

        Several bindings sharing an input is not an error: it is reported as info ("shared_trigger"), and as a warning
        only when the shared actions really clash (same spot, or two scope buttons)."""
        supports = RUNTIME_SUPPORTS if runtime_supports is None else frozenset(runtime_supports)
        blocked = {**ALWAYS_RESERVED, **(reserved or {})}
        issues: list[Issue] = []

        def add(code, severity, message, bindings=(), trigger=""):
            issues.append(Issue(code, severity, message, tuple(b.id for _, b in bindings),
                                tuple(i for i, _ in bindings), trigger))

        indexed = list(enumerate(self.bindings))
        stick_keys = self.stick_keys()

        # -- ids
        counts: dict = {}
        for _, b in indexed:
            counts[b.id] = counts.get(b.id, 0) + 1
        for i, b in indexed:
            if not b.id:
                add("missing_binding_id", "error", f"{_name(b)} has no id.", [(i, b)])
            elif not valid_binding_id(b.id):
                add("invalid_binding_id", "error", f"{_name(b)} has an unusable id {b.id!r}.", [(i, b)])
        for value, count in counts.items():
            if count > 1 and value:
                same = [(i, b) for i, b in indexed if b.id == value]
                add("duplicate_binding_id", "error", f"{count} controls share the id {value!r}.", same)

        # -- each binding on its own
        for i, b in indexed:
            canonical, seen = [], set()
            for t in b.triggers:
                try:
                    name = canonical_trigger(t)
                except ProfileError as exc:
                    add("invalid_trigger", "error", f"{_name(b)} has an unusable key {t!r}: {exc}", [(i, b)], str(t))
                    continue
                if name in seen:
                    add("duplicate_trigger_in_binding", "error", f"{_name(b)} lists {name} more than once.", [(i, b)], name)
                seen.add(name)
                canonical.append(name)
            if b.on_failure not in ON_FAILURE:
                add("invalid_failure_policy", "error", f"{_name(b)} has an unknown failure policy {b.on_failure!r}.", [(i, b)])
            for label, a, delay in _actions_of(b):
                _check_action(add, i, b, label, a, delay)
            if b.extra_triggers and "extra_triggers" not in supports:
                add("extra_triggers_not_executed", "warning",
                    f"{_name(b)} has more than one key, but only {b.input} works for now; the others are saved and do nothing yet.",
                    [(i, b)])
            if b.extra_actions and "extra_actions" not in supports:
                add("extra_actions_not_executed", "warning",
                    f"{_name(b)} has more than one action, but only the first runs for now; the others are saved and do nothing yet.",
                    [(i, b)])
            spots = [_spots(a) for _, a, _ in _actions_of(b) if getattr(a, "mode", None) in MODES]
            for first in range(len(spots)):
                for second in range(first + 1, len(spots)):
                    if _close(spots[first], spots[second]):
                        add("actions_same_spot", "warning",
                            f"Two actions of {_name(b)} touch the same spot; one finger on it is enough.", [(i, b)])

        # -- inputs used by several bindings
        # `runners` is what the engine really does (the same function it uses); an authorization only counts if the engine supports it
        shared = self.shared_triggers if "shared_triggers" in supports else {}
        runners = trigger_runners(self.bindings, shared)
        groups = effective_sharing(self.bindings, shared)
        users: dict = {}
        for i, b in indexed:
            for t in dict.fromkeys(b.triggers):
                users.setdefault(make_combo(*split_combo(t)) if isinstance(t, str) else t, []).append((i, b))
        for trigger, group in users.items():
            if len(group) < 2:
                continue
            running = [(i, b) for i, b in group if any(b is r for r in runners.get(trigger, ()))]
            running_at = {i for i, _ in running}
            names = ", ".join(_name(b) for _, b in group)
            if trigger in groups and len(running) == len(group):
                add("shared_trigger", "info", f"{trigger} is shared on purpose by {len(group)} controls ({names}); "
                    "they run in list order.", group, trigger)
            elif trigger in groups:
                for i, b in group:
                    if i not in running_at:
                        add("shared_trigger_partially_authorized", "warning",
                            f"{_name(b)} also uses {trigger}, which {len(running)} other controls share on purpose, but it is not "
                            "one of them, so it does nothing; add it to the sharing or give it another key.", running + [(i, b)], trigger)
            else:
                add("duplicate_trigger_not_shared", "warning",
                    f"{len(group)} controls use {trigger} ({names}) but sharing it was not set up, so only "
                    f"{_name(runners[trigger][0])} runs.", group, trigger)
            for first in range(len(running)):
                for second in range(first + 1, len(running)):
                    pair = [running[first], running[second]]
                    why = _clash(running[first][1], running[second][1])
                    if why:
                        add("shared_trigger_conflict", "warning",
                            f"{trigger} starts {_name(pair[0][1])} and {_name(pair[1][1])}, which clash: {why}.", pair, trigger)
            need = sum(_pointers(b) for _, b in running)
            if need > MAX_TOUCH_POINTERS:
                add("trigger_exceeds_pointers", "warning",
                    f"Pressing {trigger} would need {need} fingers at once; the phone follows at most {MAX_TOUCH_POINTERS}.",
                    running, trigger)
            if trigger not in groups:                                 # unauthorized duplicates: say exactly which ones do nothing
                for i, b in group:
                    if i in running_at:
                        continue
                    winner_index = next(n for n, c in indexed if c is runners[trigger][0])
                    if isinstance(b.input, str) and make_combo(*split_combo(b.input)) == trigger:
                        add("shadowed_by_later_binding", "warning",
                            f"{_name(b)} will not run: {_name(runners[trigger][0])} also uses {b.input} and is later in the list, "
                            "and only the last one works unless sharing is set up.", [(i, b), (winner_index, runners[trigger][0])], b.input)
                    else:
                        winner = runners[trigger][0]
                        why = "a first key beats an additional key" if make_combo(*split_combo(winner.input)) == trigger \
                            else "the later control in the list wins"
                        add("shared_trigger_not_executed", "warning",
                            f"{_name(b)} also lists {trigger}, but {_name(winner)} uses it too and only one control runs per key "
                            f"unless sharing is set up ({why}); {trigger} does nothing for {_name(b)}.",
                            [(i, b), (winner_index, winner)], trigger)
        if "shared_triggers" not in supports:
            for trigger, ids in self.shared_triggers.items():
                members = effective_sharing(self.bindings, {trigger: ids})
                if trigger in members:
                    add("sharing_not_executed", "warning",
                        f"Sharing of {trigger} is set up, but the input engine does not run it yet; only one of the controls works.",
                        [(i, b) for i, b in indexed if any(b is m for m in members[trigger])], trigger)
        for trigger, ids in self.shared_triggers.items():
            problems = []
            for binding_id in dict.fromkeys(ids):
                count = sum(1 for b in self.bindings if b.id == binding_id)
                if count == 0:
                    problems.append(f"{binding_id!r} is not a control of this profile")
                elif count > 1:
                    problems.append(f"{binding_id!r} names {count} controls")
                elif make_combo(*split_combo(trigger)) not in _triggers_of(self.find_by_id(binding_id)):
                    problems.append(f"{binding_id!r} does not use {trigger}")
            if not problems and len(set(ids)) < 2:
                problems.append("fewer than two controls are listed")
            if problems:
                add("sharing_metadata_invalid", "warning",
                    f"The sharing of {trigger} is not valid ({'; '.join(problems)}); it is ignored and removed the next time "
                    "the profile is saved.", [(i, b) for i, b in indexed if b.id in ids], trigger)

        # -- sticks, application hotkeys, look zones
        owner: dict = {}
        for n, stick in enumerate(self.sticks, 1):
            keys = list(stick.keys.values()) + list(stick.extra_keys().values())
            if set(stick.keys) != set(DIRECTIONS) or not all(stick.keys.values()):
                add("stick_keys_invalid", "error", f"Movement stick {n} needs a key for up, left, down and right.")
            for key in dict.fromkeys(keys):
                if keys.count(key) > 1:
                    add("stick_key_duplicate", "error", f"Movement stick {n} uses {key} for more than one thing.", (), key)
                if key in owner and owner[key] != n:
                    add("stick_key_shared", "error", f"{key} belongs to movement sticks {owner[key]} and {n}.", (), key)
                owner.setdefault(key, n)
        for key in dict.fromkeys(stick_keys):
            if key in blocked:
                add("stick_key_is_app_hotkey", "error", f"The movement stick uses {key}, which is {blocked[key]}.", (), key)
        for i, b in indexed:
            for t in dict.fromkeys(b.triggers):
                if t in stick_keys:
                    add("trigger_is_stick_key", "error", f"{_name(b)} uses {t}, which is a movement-stick key.", [(i, b)], t)
                if t in blocked:
                    add("trigger_is_app_hotkey", "error", f"{_name(b)} uses {t}, which is {blocked[t]}.", [(i, b)], t)
                mods, base = split_combo(t) if isinstance(t, str) else (frozenset(), "")
                if mods and (base in users or base in stick_keys):
                    add("combination_overrides_plain_key", "info",
                        f"{t} works while {base} is also used: with {', '.join(m for m in MODIFIERS if m in mods)} held, "
                        f"{t} is used instead of {base}.", [(i, b)], t)
        if len(self.looks) > 1:
            add("extra_look_zones_ignored", "warning",
                f"The profile has {len(self.looks)} mouse-look areas; only the first one is used.")

        # -- fingers
        for i, b in indexed:
            need = _pointers(b)
            if need > MAX_TOUCH_POINTERS:
                add("binding_exceeds_pointers", "error",
                    f"{_name(b)} needs {need} fingers at once; the phone follows at most {MAX_TOUCH_POINTERS}.", [(i, b)])
        held = len(self.sticks) + min(1, len(self.looks)) + sum(_pointers(b) for _, b in indexed if _holds(b))
        if held > MAX_TOUCH_POINTERS:
            add("pointer_budget_exceeded_if_all_held", "info",
                f"If every control that stays down were held together they would need {held} fingers "
                f"(the phone follows {MAX_TOUCH_POINTERS}); in practice only some are held at once.")

        # -- smart pick-up
        if self.smart_loot:
            from .loot import SmartLoot                          # (loot imports this module: import it late)
            try:
                loot = SmartLoot.from_dict(self.smart_loot)
            except ProfileError as exc:
                add("smart_loot_invalid", "error", f"The smart pick-up setting cannot be used: {exc}")
            else:
                for key in loot.keys():
                    if key in users:
                        add("smart_loot_key_conflict", "error", f"Smart pick-up uses {key}, which is also a key of "
                            f"{', '.join(_name(b) for _, b in users[key])}; they would fight over it.", users[key], key)
                    elif key in stick_keys:
                        add("smart_loot_key_conflict", "error", f"Smart pick-up uses {key}, which is a movement-stick key.", (), key)
                    elif key in blocked:
                        add("smart_loot_key_conflict", "error", f"Smart pick-up uses {key}, which is {blocked[key]}.", (), key)

        # -- what loading noticed
        for note in self.repairs:
            add("repaired_on_load", "warning", note)
        for note in self.notes:
            add("load_note", "info", note)
        order = {name: rank for rank, name in enumerate(SEVERITIES)}
        issues.sort(key=lambda issue: order[issue.severity])               # stable: the check order is kept inside a level
        return issues

    def to_dict(self) -> dict:
        """The profile as a file record. Version 1 (the legacy flat bindings, no binding ids) unless `required_version`
        says 2, in which case ALL bindings are written in the version-2 form. The permanent profile `id` is added by
        ProfileStore, not here."""
        version = self.required_version
        data = {"version": version, "name": self.name, "orientation": self.orientation,
                "package": self.package,
                **({"scene": self.scene} if self.scene else {}),
                **({"smart_loot": self.smart_loot} if self.smart_loot else {}),
                "bindings": [b.to_dict_v2() if version >= PROFILE_VERSION_2 else b.to_dict_v1() for b in self.bindings],
                "sticks": [s.to_dict() for s in self.sticks],
                "looks": [l.to_dict() for l in self.looks]}
        groups = self.sharing_groups()                                 # only authorizations that still describe something
        if groups:
            data["shared_triggers"] = [
                _with_unknown({"trigger": trigger, "bindings": [b.id for b in members]}, self.shared_unknown.get(trigger, {}))
                for trigger, members in groups.items()]
        return _with_unknown(data, self.unknown)

    @staticmethod
    def from_dict(data: dict, repair: bool = False) -> "Profile":
        """Read a profile record (version 1 or 2).

        By default anything malformed raises ProfileError and nothing is guessed. With `repair=True` a malformed
        binding is skipped, and a binding that uses a stick key is skipped (or loses that additional trigger), instead
        of failing the whole profile. Either way a version-2 binding id that is missing, unusable or repeated is replaced
        (the first binding keeps a repeated id). Everything that was fixed or skipped is listed in `profile.repairs`;
        ProfileStore.repair() is what saves such a profile, after backing up the original file."""
        if not isinstance(data, dict) or "name" not in data:
            raise ProfileError("Not a profile file")
        version = int(data.get("version", 1))
        if version > LATEST_PROFILE_VERSION:
            raise ProfileError("This profile was made by a newer version of the app")
        orientation = data.get("orientation", "any")
        if orientation not in ORIENTATIONS:
            orientation = "any"
        scene = data.get("scene")
        smart_loot = data.get("smart_loot")
        v2 = version >= PROFILE_VERSION_2
        profile = Profile(name=str(data["name"]), orientation=orientation, package=str(data.get("package", "")),
                          scene=scene if isinstance(scene, dict) else None, id=str(data.get("id") or ""),
                          smart_loot=smart_loot if isinstance(smart_loot, dict) else None,
                          schema_version=PROFILE_VERSION_2 if v2 else PROFILE_VERSION,
                          unknown=_split_unknown(data, _PROFILE_KEYS | ({"shared_triggers"} if v2 else set())))
        for number, raw in enumerate(data.get("bindings", []), 1):
            try:
                if not isinstance(raw, dict):
                    raise ProfileError(f"A binding must be an object, not {raw!r}")
                kind = raw.get("type", "binding" if v2 else "tap")
                if kind != ("binding" if v2 else "tap"):
                    log.warning("Skipping unsupported binding type %r", kind)
                    continue
                binding = Binding.from_dict_v2(raw) if v2 else Binding.from_dict(raw)
            except ProfileError as exc:
                if not repair:
                    raise
                profile.repairs.append(f"Skipped binding {number}: {exc}")
                continue
            if v2 and not valid_binding_id(raw.get("id")):
                profile.repairs.append(f"Binding {number} ({binding.input}) had no usable id; it got one")
            profile.bindings.append(binding)                 # read as written: no binding is ever replaced or dropped here
        if not v2:
            profile.notes += profile._shared_input_notes()
        profile.repairs += profile._ensure_unique_ids()
        if v2 and "shared_triggers" in data:
            profile._read_sharing(data["shared_triggers"], repair)
        for raw in data.get("sticks", []):
            stick = Stick.from_dict(raw)
            keys = stick.key_set()
            if any(t in keys for b in profile.bindings for t in b.triggers):
                if not repair:
                    raise ProfileError("A stick key is also used by a tap binding")
                for binding in list(profile.bindings):
                    if binding.input in keys:
                        profile.bindings.remove(binding)
                        profile.repairs.append(f"Skipped binding {binding.input}: it uses a stick key")
                    elif any(t in keys for t in binding.extra_triggers):
                        binding.extra_triggers = [t for t in binding.extra_triggers if t not in keys]
                        profile.repairs.append(f"Binding {binding.input} lost a trigger that is a stick key")
            profile.sticks.append(stick)
        for raw in data.get("looks", []):
            profile.looks.append(Look.from_dict(raw))
        profile.repairs += profile.prune_sharing()               # (also after the stick check may have removed bindings)
        return profile

    def _read_sharing(self, raw, repair: bool) -> None:
        """Read the explicit sharing authorizations of a version-2 file. Malformed entries are an error (or, with repair, skipped).
        Whether they still describe something is decided by prune_sharing after everything is loaded."""
        try:
            if not isinstance(raw, list):
                raise ProfileError("shared_triggers must be a list")
        except ProfileError as exc:
            if not repair:
                raise
            self.repairs.append(f"Ignored the sharing list: {exc}")
            return
        for number, entry in enumerate(raw, 1):
            try:
                if (not isinstance(entry, dict) or not isinstance(entry.get("trigger"), str) or not entry["trigger"]
                        or not isinstance(entry.get("bindings"), list) or not all(isinstance(i, str) for i in entry["bindings"])):
                    raise ProfileError(f"Invalid sharing entry {number}: {entry!r}")
            except ProfileError as exc:
                if not repair:
                    raise
                self.repairs.append(f"Skipped sharing entry {number}: {exc}")
                continue
            trigger = make_combo(*split_combo(entry["trigger"]))
            known = self.shared_triggers.get(trigger, [])
            self.shared_triggers[trigger] = list(dict.fromkeys([*known, *entry["bindings"]]))
            extra = _split_unknown(entry, _SHARING_ENTRY_KEYS)
            if extra:
                self.shared_unknown.setdefault(trigger, {}).update(extra)


def orientation_of(width: int, height: int) -> str:
    return "landscape" if width > height else "portrait"


BACKUP_FOLDER = "backup-before-ids"
REPAIR_BACKUP_FOLDER = "backup-before-repair"
UPGRADE_BACKUP_FOLDER = "backup-before-v2"


def new_profile_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class _Entry:
    """One profile file as found on disk."""
    id: str
    name: str
    path: Path
    package: str


class ProfileStore:
    """Profiles as JSON files in one folder.

    Identity. Every profile has a permanent `id` stored inside its file; the file NAME is only where it happens to
    live. Two different profiles can therefore never end up in the same file, whatever their names look like:
    a name like 'COD: BR' and 'COD? BR' maps to the same safe file name, so the second profile gets a suffixed
    file name instead of overwriting the first. Saving a profile whose NAME belongs to a different profile is refused.
    A profile object without an id (made by a script, a test or an old caller) that is saved under an existing
    name updates that profile, as it always did.

    Migration. Files written before ids existed are given one the first time the folder is opened, in place (same
    file name), after the original is copied to `backup-before-ids/`. If a copy cannot be made the file is left
    untouched and works with a stable in-memory id. Restore points (`saved/`) are migrated the same way. Files from a
    NEWER version of the app are never modified.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            self.migrate_ids()
        except OSError as exc:                                  # never prevent the app from starting
            log.warning("Could not add ids to the profile files: %s", exc)

    @staticmethod
    def filename(name: str) -> str:
        safe = re.sub(r"[^\w\- .]", "_", name).strip(" .")
        if not safe:
            raise ProfileError("Profile name is empty")
        return safe + ".json"

    # -- reading the folder ---------------------------------------------------------------------------
    @staticmethod
    def _read_json(file: Path) -> dict | None:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and "name" in data else None

    @staticmethod
    def _synthetic_id(file: Path) -> str:
        """A stable stand-in id for a file that has none yet (migration could not write one)."""
        return "legacy-" + hashlib.sha1(file.name.lower().encode("utf-8")).hexdigest()[:10]

    def _files(self) -> list[Path]:
        return sorted(self.folder.glob("*.json"), key=lambda f: f.name.lower())

    def _scan(self) -> list[_Entry]:
        entries, seen = [], set()
        for file in self._files():
            data = self._read_json(file)
            if data is None:
                log.warning("Ignoring unreadable profile file %s", file.name)
                continue
            pid = str(data.get("id") or "")
            if not pid or pid in seen:                           # unmigrated, or a copied file with the same id
                pid = self._synthetic_id(file)
            seen.add(pid)
            entries.append(_Entry(pid, str(data["name"]), file, str(data.get("package", ""))))
        return entries

    def _entry(self, name: str, entries: list[_Entry] | None = None) -> _Entry | None:
        matches = [e for e in (entries if entries is not None else self._scan()) if e.name.lower() == name.lower()]
        if len(matches) > 1:
            log.warning("Several profile files are called '%s'; using %s", name, matches[0].path.name)
        return matches[0] if matches else None

    def _entry_of(self, profile: "Profile", entries: list[_Entry] | None = None) -> _Entry | None:
        entries = entries if entries is not None else self._scan()
        if profile.id:
            found = next((e for e in entries if e.id == profile.id), None)
            if found is not None:
                return found
        return self._entry(profile.name, entries)

    # -- migration ------------------------------------------------------------------------------------
    def _backup(self, file: Path, folder: str = BACKUP_FOLDER) -> bool:
        destination = self.folder / folder / file.relative_to(self.folder)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            while destination.exists():                          # never overwrite an earlier backup
                count += 1
                suffix = f"{int(time.time())}" + (f"-{count}" if count > 1 else "")
                destination = destination.with_name(f"{file.stem}.{suffix}{file.suffix}")
            shutil.copy2(file, destination)
            return True
        except OSError as exc:
            log.warning("Could not back up %s (%s); leaving it as it is", file.name, exc)
            return False

    def _file_version(self, file: Path) -> int:
        data = self._read_json(file)
        try:
            return int(data.get("version", 1)) if data else PROFILE_VERSION
        except (TypeError, ValueError):
            return PROFILE_VERSION

    def _write_json(self, target: Path, data: dict) -> None:
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp, target)                                 # atomic: a crash never leaves a half-written file

    def migrate_ids(self) -> list[str]:
        """Give every profile file a permanent id (see the class docstring). Returns the names it changed."""
        changed, seen = [], set()
        for file in self._files():
            data = self._read_json(file)
            if data is None:
                continue
            try:
                newer = int(data.get("version", 1)) > LATEST_PROFILE_VERSION
            except (TypeError, ValueError):
                newer = False
            pid = str(data.get("id") or "")
            if newer or (pid and pid not in seen):
                if pid:
                    seen.add(pid)
                continue
            if not self._backup(file):
                continue
            data["id"] = new_profile_id()
            try:
                self._write_json(file, data)
            except OSError as exc:
                log.warning("Could not add an id to %s: %s", file.name, exc)
                continue
            seen.add(data["id"])
            changed.append(str(data["name"]))
            checkpoint = self.folder / "saved" / file.name
            restore = self._read_json(checkpoint) if checkpoint.is_file() else None
            if restore is not None and self._backup(checkpoint):
                restore["id"] = data["id"]
                try:
                    self._write_json(checkpoint, restore)
                except OSError as exc:
                    log.warning("Could not add an id to the restore point of %s: %s", file.name, exc)
        if changed:
            log.info("Profile ids added to: %s (originals kept in %s)", ", ".join(changed), BACKUP_FOLDER)
        return changed

    def repair(self, name: str) -> "Profile":
        """Load a profile with `Profile.from_dict(repair=True)`. If that had to fix or skip anything, the original file is
        first copied to `backup-before-repair/` (never overwriting an earlier backup) and only then replaced by the
        repaired profile; if the copy cannot be made, nothing is changed and ProfileError is raised. A file that needed no
        repair is not touched. A file from a newer version is refused and left exactly as it is. The restore point
        (`saved/`) is never modified."""
        entry = self._entry(name)
        if entry is None:
            raise ProfileError(f"Could not read profile '{name}': there is no such profile")
        try:
            raw = json.loads(entry.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProfileError(f"Could not read profile '{name}': {exc}") from exc
        profile = Profile.from_dict(raw, repair=True)
        profile.id = entry.id
        if not profile.repairs:
            return profile
        if not self._backup(entry.path, REPAIR_BACKUP_FOLDER):
            raise ProfileError(f"Could not back up '{name}' before repairing it; nothing was changed")
        self.save(profile)
        log.info("Profile '%s' repaired (original kept in %s): %s", name, REPAIR_BACKUP_FOLDER, "; ".join(profile.repairs))
        return profile

    # -- queries --------------------------------------------------------------------------------------
    def path(self, name: str) -> Path:
        entry = self._entry(name)
        return entry.path if entry else self.folder / self.filename(name)

    def names(self) -> list[str]:
        return sorted((e.name for e in self._scan()), key=str.lower)

    def exists(self, name: str) -> bool:
        return self._entry(name) is not None

    def find_by_package(self, package: str) -> str | None:
        """Name of the profile linked to an Android app id, or None."""
        if not package:
            return None
        return next((e.name for e in self._scan() if e.package == package), None)

    def load(self, name: str) -> "Profile":
        entry = self._entry(name)
        if entry is None:
            raise ProfileError(f"Could not read profile '{name}': there is no such profile")
        return self._load_file(entry.path, entry.id, f"Could not read profile '{name}'")

    @staticmethod
    def _load_file(path: Path, pid: str, what: str) -> "Profile":
        try:
            profile = Profile.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            if isinstance(exc, ProfileError):
                raise
            raise ProfileError(f"{what}: {exc}") from exc
        profile.id = pid
        return profile

    # -- writing --------------------------------------------------------------------------------------
    def _free_path(self, name: str, pid: str, entries: list[_Entry]) -> Path:
        """A file for a NEW profile: the plain name if it is free, otherwise the name plus the profile's id."""
        base = self.folder / self.filename(name)
        taken = {e.path.name.lower() for e in entries}
        for candidate in (base, base.with_name(f"{base.stem}-{pid[:6]}.json"), base.with_name(f"{base.stem}-{pid}.json")):
            if candidate.name.lower() not in taken and not candidate.exists():      # also protects unreadable files
                return candidate
        raise ProfileError(f"Could not find a free file name for the profile '{name}'")

    def save(self, profile: "Profile") -> None:
        entries = self._scan()
        same_name = self._entry(profile.name, entries)
        if not profile.id:
            profile.id = same_name.id if same_name else new_profile_id()
        if same_name is not None and same_name.id != profile.id:
            raise ProfileError(f"A different profile named '{same_name.name}' already exists. Choose another name; "
                               "nothing was overwritten.")
        existing = next((e for e in entries if e.id == profile.id), None)
        target = existing.path if existing else self._free_path(profile.name, profile.id, entries)
        document = {"id": profile.id, **profile.to_dict()}
        if existing is not None and document["version"] > self._file_version(existing.path):
            # the file is about to change format: keep the version-1 original (never overwriting an earlier copy)
            if not self._backup(existing.path, UPGRADE_BACKUP_FOLDER):
                raise ProfileError(f"Could not back up '{profile.name}' before saving it in the version-2 format; "
                                   "nothing was changed.")
        try:
            self._write_json(target, document)
        except OSError as exc:
            raise ProfileError(f"Could not save profile '{profile.name}': {exc}") from exc

    def link_package(self, profile: "Profile", package: str) -> list[str]:
        """Link `profile` to an app id (saving it). Other profiles lose that link; returns their names."""
        entries = self._scan()
        mine = self._entry_of(profile, entries)
        unlinked = []
        for entry in entries:
            if mine is not None and entry.id == mine.id:
                continue
            if package and entry.package == package:
                other = self._load_file(entry.path, entry.id, f"Could not read profile '{entry.name}'")
                other.package = ""
                self.save(other)
                unlinked.append(entry.name)
        profile.package = package
        self.save(profile)
        return unlinked

    def delete(self, name: str) -> None:
        entry = self._entry(name)
        if entry is None:
            return
        for path in (entry.path, self.folder / "saved" / entry.path.name):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ProfileError(f"Could not delete profile '{name}': {exc}") from exc

    # -- restore points ("Save" in the UI) -------------------------------------------------------
    # Edits are written to the profile file at once (nothing is ever lost). A restore point is a second copy that
    # only changes when the user presses Save, so accidental edits can be undone with "Undo changes". It lives in
    # saved/ under the same file name as its profile, so it always follows the profile, not the profile's name.
    def _checkpoint_for(self, name: str, profile: "Profile | None" = None) -> Path | None:
        """Where the restore point of an EXISTING profile lives; None if there is no such profile."""
        entry = self._entry_of(profile) if profile is not None else self._entry(name)
        return self.folder / "saved" / entry.path.name if entry else None

    def checkpoint_path(self, name: str) -> Path:
        return self._checkpoint_for(name) or self.folder / "saved" / self.filename(name)      # (for a profile not saved yet)

    def has_checkpoint(self, name: str) -> bool:
        target = self._checkpoint_for(name)
        return target is not None and target.is_file()

    def save_checkpoint(self, profile: "Profile") -> float:
        target = self._checkpoint_for(profile.name, profile)
        if target is None:                                      # never guess a file name: save the profile first
            self.save(profile)
            target = self._checkpoint_for(profile.name, profile)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._write_json(target, {"id": profile.id, **profile.to_dict()} if profile.id else profile.to_dict())
        except OSError as exc:
            raise ProfileError(f"Could not save a restore point for '{profile.name}': {exc}") from exc
        return time.time()

    def load_checkpoint(self, name: str) -> "Profile":
        entry = self._entry(name)
        target = self._checkpoint_for(name)
        if target is None:
            raise ProfileError(f"No restore point for '{name}': there is no such profile")
        try:
            profile = Profile.from_dict(json.loads(target.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            if isinstance(exc, ProfileError):
                raise
            raise ProfileError(f"No restore point for '{name}': {exc}") from exc
        if entry is not None:
            profile.id = entry.id
        return profile

    def checkpoint_time(self, name: str) -> float | None:
        target = self._checkpoint_for(name)
        try:
            return target.stat().st_mtime if target is not None else None
        except OSError:
            return None

    def has_unsaved_changes(self, profile: "Profile") -> bool:
        """True if the profile differs from its restore point (a profile without one counts as unchanged)."""
        target = self._checkpoint_for(profile.name, profile)
        if target is None or not target.is_file():
            return False
        try:
            return self._load_file(target, profile.id, "restore point").to_dict() != profile.to_dict()
        except ProfileError:
            return False
