"""Decides what a key / mouse event does: mapped touches in many modes, direct finger, scroll, back/home.

Modes (see mapping.profile.MODES): hold, tap, toggle, repeat, double, swipe, drag, pinch-out, pinch-in, sequence.
Combination keys ("Shift+W") are looked up first, so a held modifier can give a key a different meaning.

A binding can have several triggers (Binding.triggers: E, F, MMB ...) and several actions (its primary action and
Binding.extra_actions). Whatever starts the binding starts ONE run of all its actions: the first at once, the others after
their own delay_ms (always counted from the moment the binding was activated; actions that become due at the same instant start
in list order, whatever the timer system does with equal deadlines). While the run is going another trigger joins it
instead of starting a second copy. Every action has its own lifecycle and its own fingers:
  hold / repeat / drag   last until the last trigger that sustains them is let go, and are never started late (a pending one is
                         cancelled if its trigger is released before its start time)
  tap / double / swipe / pinch / sequence / toggle
                         once scheduled they are not cancelled by a key release (a toggle stays on until a fresh activation)
Separate bindings that share a trigger all run ONLY if the profile explicitly authorizes that sharing (Profile.shared_triggers);
then pressing the trigger starts every participant, each with its own run, actions and fingers, in binding-list order. Without
that authorization only the winner of trigger_owners runs, as it always did.
"""
from __future__ import annotations

import heapq
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from ..mapping.profile import ALSO_MODES, MODIFIERS, Binding, Look, Profile, Stick, make_combo, split_combo, trigger_runners
from ..stream import protocol
from .touch import TouchInjector

log = logging.getLogger(__name__)

TAP_DURATION_MS = 40
DOUBLE_TAP_GAP_MS = 60                  # pause between the two taps of a double tap
MOTION_TICK_MS = 8                      # swipe / drag / pinch move the finger this often
STICK_TICK_MS = 8                       # how often a ramping stick moves its finger
LOOK_FLUSH_MS = 4                       # mouse movement is batched and sent at most every 4 ms (250 Hz)
LOOK_IDLE_MS = 120                      # the look finger lifts after this long without mouse movement
LOOK_EDGE_MARGIN = 0.12                 # re-centre when the finger gets within 12% of the zone edge
LOOK_ACCEL_REF = 2.0                    # mouse speed (counts per ms) at which acceleration starts to bite
LOOK_ACCEL_MAX = 3.0                    # ... capped at this many reference speeds
PINCH_NEAR = 0.4                        # the close position of a pinch, as a fraction of the far position
DIRECT_OWNER = ("mouse", "direct")
PRIMARY = 0                             # index of a binding's first action (extra actions are 1, 2, ...); part of the name of its fingers
SCOPE_HELD_MODES = ("hold", "repeat", "drag", "toggle")      # scope buttons that keep scope on while they are active

# modes that follow the key (they last until it is released)
FOLLOWS_KEY = ("hold", "repeat", "drag")
# when a wheel notch triggers a mode that normally follows the key, it runs as a one-shot instead
WHEEL_EQUIVALENT = {"hold": "tap", "repeat": "tap", "drag": "swipe"}


@dataclass
class _LookRuntime:
    look: Look
    owner: tuple
    down: bool = False
    pos: tuple = (0.0, 0.0)                         # finger position in pixels
    pending: tuple = (0.0, 0.0)                     # mouse counts not yet sent
    flush_scheduled: bool = False
    last_flush: float = 0.0
    generation: int = 0                             # invalidates idle-lift timers already scheduled
    resets: int = 0                                 # how many times the finger was re-centred (diagnostics)


@dataclass
class _StickRuntime:
    stick: Stick
    owner: tuple
    held: set = field(default_factory=set)          # directions currently held: "up", "left", ...
    mods: set = field(default_factory=set)          # "sprint" / "walk" while their key is held
    autorun: bool = False                           # walking forward by itself until cancelled
    offset: tuple = (0.0, 0.0)                      # finger offset from the centre, in pixels
    down: bool = False
    generation: int = 0                             # invalidates ramp ticks that are already scheduled


@dataclass(eq=False)
class _Action:
    """One action of a binding in ONE activation cycle: its settings (`spec`), its mode and its fingers.

    `spec` is the binding itself for the first action (it carries the same field names) and a profile Action for an additional
    one. Every finger is named ("bind", binding key, action index, ...): the owners of one action are lifted by that action alone
    and can never collide with another action's fingers, another binding's, the movement stick's ("stick", i), mouse look's
    ("look", i) or the direct mouse finger (DIRECT_OWNER).
    A new _Action is made every time the action (re)starts, so a timer left over from an earlier cycle (it holds the old object)
    can never touch the current one. state: "pending" (waiting for its delay) -> "active" -> "done" (finished, cancelled, failed)."""
    run: "_Run" = field(repr=False)
    spec: object                                    # Binding (first action) or Action (additional action)
    index: int
    mode: str                                       # effective mode (a wheel notch turns hold / repeat / drag into one-shots)
    owners: list                                    # finger owners used by this action
    epoch: int
    state: str = "pending"
    lit: bool = False                               # it contributes to the overlay highlight of its run

    @property
    def active(self) -> bool:
        return self.state == "active"

    @property
    def sustained(self) -> bool:
        """Lasts only while a trigger that started it is held (hold / repeat / drag)."""
        return self.mode in FOLLOWS_KEY


@dataclass(eq=False)
class _Run:
    """One activation of a binding: the triggers that sustain it and the actions it started.

    A binding is identified by `key` (its runtime id, see InputEngine._keys), never by the input that started it. A run lives
    until none of its actions is pending or active (a toggle that is on keeps it alive)."""
    binding: Binding
    key: str
    marker: str                                     # the name the overlay lights up: the binding's first input
    trigger: str                                    # the trigger that STARTED it ("R", "Shift+W", "F", "WheelUp")
    holders: dict = field(default_factory=dict)     # trigger -> physical keys it needs down (only while something is sustained)
    actions: list = field(default_factory=list)     # _Action per action index (the current cycle of each)
    lit: int = 0                                    # how many of its actions are showing in the overlay highlight
    activating: bool = False                        # actions are being (re)started right now: the run cannot end yet
    ended: bool = False

    def held_keys(self) -> set:
        """Every physical key some trigger of this run still needs."""
        return set().union(*self.holders.values()) if self.holders else set()

    def alive(self) -> bool:
        return any(a.state != "done" for a in self.actions)


class InputEngine:
    """Qt-free. `schedule(delay_ms, fn)` runs fn later (QTimer.singleShot in the app)."""

    def __init__(self, injector: TouchInjector, schedule: Callable[[int, Callable[[], None]], None],
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.injector = injector
        self._schedule = schedule
        self._clock = clock
        self._looks: list[_LookRuntime] = []
        self.look_captured = False           # the mouse is captured and drives the camera
        self._lookup: dict[str, tuple] = {}                           # trigger without modifiers -> ((binding, trigger), ...)
        self._combos: dict[tuple[frozenset, str], tuple] = {}         # triggers like Shift+W -> ((binding, trigger), ...)
        self._sticks: list[_StickRuntime] = []
        self._stick_keys: dict[str, tuple[_StickRuntime, str]] = {}
        self._stick_extra: dict[str, tuple[_StickRuntime, str]] = {}    # sprint / walk / autorun keys
        self._profile: Profile | None = None
        self.mapping_active = False          # keyboard/mouse bindings are applied
        self.direct_touch = True             # the mouse acts as a finger on the phone screen
        self.on_press_changed: Callable[[set[str]], None] | None = None
        self._pressed: set[str] = set()
        self._mods: set[str] = set()         # modifier keys physically held (Ctrl / Shift / Alt)
        self._runs: dict[str, _Run] = {}                                # running bindings by binding key (at most one run per binding)
        self._keys: dict[int, str] = {}                                 # id(binding) -> its unique runtime key (see refresh_profile)
        self._epoch = 0
        self._starts: list = []              # heap of (deadline ms, sequence, action): delayed actions waiting for their time
        self._start_seq = 0
        self._tap_seq = 0                                   # numbers the one-off taps of run_taps (their fingers are ("tap", n))
        self._scope_latch = False            # switched by one-shot scope buttons (tap / double / swipe); see scope_active
        self.action_failures = 0             # actions that could not start or continue (no free pointer, no connection, ...)
        self.recent_failures: deque = deque(maxlen=20)    # (binding input, action index, reason), oldest first

    @property
    def scope_active(self) -> bool:
        """The scope / aim button is on: look sensitivity uses ads_scale. Derived from EVERY scope source, so one release can
        never switch scope off while another scope button is still active: on while any hold / repeat / drag / toggle scope action is
        active, or while a one-shot scope button has switched it on (that one flips each time it is pressed)."""
        return self._scope_latch or any(a.state == "active" and a.spec.scope and a.mode in SCOPE_HELD_MODES
                                        for run in self._runs.values() for a in run.actions)

    # -- configuration ---------------------------------------------------------------------
    def set_profile(self, profile: Profile | None) -> None:
        self._profile = profile
        self.refresh_profile()

    def refresh_profile(self) -> None:
        """Call after the current profile's bindings were edited."""
        self.release_all()
        profile = self._profile
        self._lookup, self._combos, self._keys = {}, {}, {}
        used: set[str] = set()
        for number, binding in enumerate(profile.bindings if profile else []):
            key = binding.id                                  # the runtime id: not stored in version-1 files, unique in a profile
            if not key or key in used:                        # ... unless the list was edited by hand: never let two bindings share fingers
                key = f"{binding.id}#{number}"
                while key in used:
                    key += "'"
            used.add(key)
            self._keys[id(binding)] = key
        runners = trigger_runners(profile.bindings, profile.shared_triggers) if profile else {}
        for trigger, bindings in runners.items():
            mods, base = split_combo(trigger)                 # every trigger of a binding, the first one and the additional ones
            entry = tuple((binding, trigger) for binding in bindings)        # one binding, or every participant of a sharing group
            if mods:
                self._combos[(mods, base)] = entry
            else:
                self._lookup[base] = entry
        self._sticks = [_StickRuntime(s, ("stick", i)) for i, s in enumerate(profile.sticks if profile else [])]
        self._looks = [_LookRuntime(lk, ("look", i)) for i, lk in enumerate(profile.looks if profile else [])]
        self._stick_keys = {key: (rt, direction) for rt in self._sticks for direction, key in rt.stick.keys.items()}
        self._stick_extra = {key: (rt, kind) for rt in self._sticks for kind, key in rt.stick.extra_keys().items()}

    def set_mapping_active(self, active: bool) -> None:
        if not active:
            self.release_all()
            self.look_captured = False
        self.mapping_active = active

    def has_look(self) -> bool:
        return bool(self._looks)

    def set_look_captured(self, captured: bool) -> None:
        """Turn mouse-look on or off. Only has an effect while key mapping is active and a look zone exists."""
        captured = bool(captured and self.mapping_active and self._looks)
        if not captured:
            for runtime in self._looks:
                self._look_lift(runtime)
        self.look_captured = captured

    # -- binding lookup -------------------------------------------------------------------
    def _resolve(self, base: str) -> tuple:
        """((binding, trigger), ...) for `base` given the modifiers held right now: a combination wins over the plain key. One
        entry, or one per participant of a sharing group (in binding-list order); empty if nothing is bound."""
        if self._mods:
            combo = self._combos.get((frozenset(self._mods), base))
            if combo is not None:
                return combo
        return self._lookup.get(base, ())

    def _resolve_combo_only(self, base: str) -> tuple:
        if not self._mods:
            return ()
        return self._combos.get((frozenset(self._mods), base), ())

    def _bases(self) -> set[str]:
        return set(self._lookup) | {base for _, base in self._combos}

    # -- events ----------------------------------------------------------------------------
    def key_event(self, name: str, pressed: bool) -> bool:
        """Returns True if the event was handled by a binding or the stick."""
        if not self.mapping_active:
            return False
        if name in MODIFIERS:
            (self._mods.add if pressed else self._mods.discard)(name)
        handled = False

        stick_key = self._stick_keys.get(name)
        if stick_key is not None:
            runtime, direction = stick_key
            if pressed and direction == "down" and runtime.autorun:
                runtime.autorun = False                       # walking backwards cancels auto-run
            (runtime.held.add if pressed else runtime.held.discard)(direction)
            self._set_pressed(name, pressed)
            self._stick_update(runtime)
            handled = True
        extra = self._stick_extra.get(name)
        if extra is not None:
            runtime, kind = extra
            if kind == "autorun":
                if pressed:
                    runtime.autorun = not runtime.autorun
            else:
                (runtime.mods.add if pressed else runtime.mods.discard)(kind)
            self._set_pressed(name, pressed)
            self._stick_update(runtime)
            handled = True

        if not pressed:
            for run in [r for r in self._runs.values() if r.holders]:
                gone = [t for t, keys in run.holders.items() if name in keys]
                if not gone:
                    continue
                for trigger in gone:                          # this trigger no longer sustains the run
                    del run.holders[trigger]
                sustained = [a for a in run.actions if a.sustained and a.state != "done"]
                if sustained:                                 # a toggle outlives its keys; hold / repeat / drag do not
                    handled = True
                if not run.holders:
                    for action in sustained:                  # the last trigger is gone: they stop (pending ones never start)
                        if action.state == "pending":
                            action.state = "done"
                        else:
                            self._finish(action)
                    self._check_end(run)
            return handled

        hits = self._resolve_combo_only(name) if handled else self._resolve(name)
        for binding, trigger in hits:                         # every binding that takes part, in list order
            self._start(binding, trigger, self._required_for(trigger), impulse=False)
            handled = True
        return handled

    @staticmethod
    def _required_for(trigger: str) -> set[str]:
        """The physical keys a trigger needs down: its key and its modifiers."""
        mods, base = split_combo(trigger)
        return {base} | set(mods)

    def mouse_button(self, name: str, pressed: bool, pos: tuple[float, float] | None) -> bool:
        """`pos` is the normalized position on the phone screen, or None when outside the picture."""
        if self.mapping_active and (self._resolve(name) or
                                    any(name in r.held_keys() for r in self._runs.values())):
            return self.key_event(name, pressed)
        if not self.direct_touch or self.look_captured:
            return False                         # while aiming, the cursor is hidden: no click-to-touch
        if name == "LMB":
            if pressed:
                return pos is not None and self.injector.down(DIRECT_OWNER, *pos)
            return self.injector.up(DIRECT_OWNER)
        if not self.mapping_active:             # right/middle click act as Back/Home like scrcpy
            if name == "RMB":
                return self.injector.back_or_screen_on(pressed)
            if name == "MMB":
                return self.injector.key(protocol.KEYCODE_HOME, pressed)
        return False

    def mouse_move(self, pos: tuple[float, float]) -> None:
        if self.injector.is_down(DIRECT_OWNER):
            self.injector.move(DIRECT_OWNER, *pos)

    def has_wheel_binding(self) -> bool:
        return self.mapping_active and bool(self._bases() & {"WheelUp", "WheelDown"})

    def wheel_binding(self, vertical: float) -> bool:
        """A mouse-wheel notch (+ = scrolled up) triggers the binding for WheelUp / WheelDown.
        Returns True if a binding handled it."""
        if not self.mapping_active or vertical == 0:
            return False
        name = "WheelUp" if vertical > 0 else "WheelDown"
        hits = self._resolve(name)
        if not hits:
            return False
        for binding, trigger in hits:
            self._start(binding, trigger, set(), impulse=True)
        return True

    def wheel(self, pos: tuple[float, float], hscroll: float, vscroll: float) -> bool:
        return self.direct_touch and self.injector.scroll(*pos, hscroll, vscroll)

    def screen_changed(self) -> None:
        """The phone's screen changed size or orientation (a rotation, a resolution change).

        Everything that was running belonged to the old screen: every finger is lifted (at the new size, see
        TouchInjector.release_stale), every running action, stick ramp, look finger and timer is cancelled. Keys that are
        still held do nothing until they are pressed again, so nothing is applied to the wrong place."""
        log.info("Screen changed: cancelling %d running binding(s)", len(self._runs))
        self.release_all()

    def release_all(self) -> None:
        self._epoch += 1                          # every timer scheduled before now becomes a no-op
        self._runs.clear()
        self._starts.clear()                      # delayed actions that were still waiting are gone for good
        self._mods.clear()
        self._scope_latch = False
        for runtime in self._looks:
            runtime.generation += 1
            runtime.down = False
            runtime.pending = (0.0, 0.0)
        for runtime in self._sticks:
            runtime.held.clear()
            runtime.mods.clear()
            runtime.autorun = False
            runtime.down = False
            runtime.offset = (0.0, 0.0)
            runtime.generation += 1
        self.injector.release_all()
        if self._pressed:
            self._pressed.clear()
            self._notify()

    # -- one-off taps (smart pick-up) --------------------------------------------------------
    @property
    def epoch(self) -> int:
        """Changes whenever everything is cancelled (release_all). Something scheduled outside the engine compares it to know it is stale."""
        return self._epoch

    def run_taps(self, taps: list[tuple[float, float, int]]) -> bool:
        """Tap these spots, each `(x, y, delay_ms from now)`, outside any binding: one finger per tap, down for TAP_DURATION_MS. Only while
        mapping is on. Cancelled like everything else: release_all (mapping off, focus loss, rotation, disconnect) lifts the fingers and drops
        the taps that have not happened yet. Returns False if mapping is off."""
        if not self.mapping_active:
            return False
        epoch = self._epoch
        for x, y, delay in taps:
            self._tap_seq += 1
            owner = ("tap", self._tap_seq)

            def down(owner=owner, x=x, y=y) -> None:
                if epoch == self._epoch and self.mapping_active:
                    if self.injector.down(owner, x, y):
                        self._schedule(TAP_DURATION_MS, lambda: self.injector.up(owner) if epoch == self._epoch else None)
            if delay <= 0:
                down()
            else:
                self._schedule(delay, down)
        return True

    def run_swipe(self, start: tuple[float, float], end: tuple[float, float], duration_ms: int) -> bool:
        """One finger swipes from `start` to `end` outside any binding (scrolling a list). Same rules as run_taps: only while mapping is on, and
        release_all lifts the finger and stops the motion. Returns False if mapping is off."""
        if not self.mapping_active:
            return False
        epoch = self._epoch
        self._tap_seq += 1
        owner = ("tap", self._tap_seq)
        if not self.injector.down(owner, *start):
            return False
        steps = max(2, duration_ms // MOTION_TICK_MS)

        def step(i: int) -> None:
            if epoch != self._epoch:
                return
            t = i / steps
            self.injector.move(owner, start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
            if i < steps:
                self._schedule(MOTION_TICK_MS, lambda: step(i + 1))
            else:
                self.injector.up(owner)
        self._schedule(MOTION_TICK_MS, lambda: step(1))
        return True

    # -- actions -----------------------------------------------------------------------------
    def _alive(self, action: _Action) -> bool:
        return action.state == "active" and action.epoch == self._epoch

    def _after(self, delay_ms: int, action: _Action, fn: Callable[[], None]) -> None:
        self._schedule(delay_ms, lambda: fn() if self._alive(action) else None)

    def _start(self, binding: Binding, trigger: str, required: set[str], impulse: bool) -> None:
        """A trigger of `binding` was pressed (or a wheel notch turned: `impulse`)."""
        key = self._keys.get(id(binding), binding.id)
        run = self._runs.get(key)
        if run is None:
            run = _Run(binding, key, binding.input, trigger)
            self._runs[key] = run
        elif run.holders and trigger not in run.holders:
            # another trigger is still down: this press JOINS the activation instead of starting a new one
            if not impulse:
                run.holders[trigger] = frozenset(required)
            return
        self._activate(run, trigger, frozenset(required), impulse)

    def _activate(self, run: _Run, trigger: str, required: frozenset, impulse: bool) -> None:
        """Start every action of the binding that is not already running; the others follow their own rules:
        a running toggle is switched off, anything else that is still pending or running is left alone (a one-shot is never
        started twice at once, a hold that is down stays as it is). The first action starts at once, the rest at once or
        after their delay_ms counted from NOW (this activation), in list order, with no timer in between for a delay of 0."""
        binding = run.binding
        run.activating = True
        activated_ms = round(self._clock() * 1000)            # the origin of every delay of this activation
        started: list[_Action] = []
        for index, spec in enumerate([binding, *binding.extra_actions]):
            current = run.actions[index] if index < len(run.actions) else None
            if current is not None and current.state != "done":
                if current.mode == "toggle" and current.state == "active":
                    self._finish(current)                     # a fresh activation switches an active toggle off
                continue
            mode = WHEEL_EQUIVALENT.get(spec.mode, spec.mode) if impulse else spec.mode
            action = _Action(run, spec, index, mode, self._owners(run, index, spec, mode), self._epoch)
            if current is None:
                run.actions.append(action)
            else:
                run.actions[index] = action                   # a new cycle: timers of the old one are dead for good
            started.append(action)
            delay = 0 if index == PRIMARY else spec.delay_ms
            if delay <= 0:
                self._begin(action)
            else:
                self._start_later(action, activated_ms + delay, delay)
            if run.ended:
                break                                         # a failure with on_failure="abort" cancelled the whole run
        if (not impulse and not run.ended
                and any(a.state != "done" and (a.sustained or a.mode == "toggle") for a in started)):
            run.holders[trigger] = required                   # these keys keep the sustained actions (and a toggle's join rule)
        run.activating = False
        self._check_end(run)

    @staticmethod
    def _owners(run: _Run, index: int, spec, mode: str) -> list:
        owner = ("bind", run.key, index)
        if mode in ("pinch-out", "pinch-in"):
            return [owner + ("a",), owner + ("b",)]
        owners: list = [owner]
        if mode in ALSO_MODES:
            owners += [owner + ("also", i) for i in range(len(spec.also))]
        return owners

    def _begin(self, action: _Action) -> None:
        action.state = "active"
        getattr(self, "_do_" + action.mode.replace("-", "_"))(action)

    def _start_later(self, action: _Action, deadline_ms: int, delay_ms: int) -> None:
        """Queue a delayed action. The order in which delayed actions start is OURS, not the timer system's: by absolute deadline,
        and for the same deadline in the order they were queued (an activation queues its actions in list order, activations in
        the order they happened). The injected scheduler only wakes us up; whichever wake-up comes first starts everything that is
        due, in that order, so equal deadlines can never be reordered by how the scheduler treats ties."""
        self._start_seq += 1
        heapq.heappush(self._starts, (deadline_ms, self._start_seq, action))
        epoch = self._epoch
        self._schedule(delay_ms, lambda: self._start_due(deadline_ms) if epoch == self._epoch else None)

    def _start_due(self, deadline_ms: int) -> None:
        limit = max(deadline_ms, round(self._clock() * 1000))   # (the wake-up's own deadline is always honoured)
        while self._starts and self._starts[0][0] <= limit:
            _, _, action = heapq.heappop(self._starts)
            self._begin_late(action)

    def _begin_late(self, action: _Action) -> None:
        """A delayed action's time has come. It starts only if it is still wanted: not cancelled, not from an old epoch."""
        if action.state != "pending" or action.epoch != self._epoch or action.run.ended:
            return
        self._begin(action)
        self._check_end(action.run)

    def _check_end(self, run: _Run) -> None:
        if run.activating or run.ended or run.alive():
            return
        run.ended = True
        run.holders.clear()
        if self._runs.get(run.key) is run:
            del self._runs[run.key]

    def _mark(self, action: _Action) -> None:
        """The action is showing: the overlay lights the binding's marker for as long as any of its actions does."""
        if action.lit:
            return
        action.lit = True
        action.run.lit += 1
        if action.run.lit == 1:
            self._set_pressed(action.run.marker, True)

    def _marker_held_by_another(self, run: _Run) -> bool:
        """Does ANOTHER live run light the same marker name? Controls that share a key (an explicitly authorized group) are all named
        after it, so one of them ending must not switch off the marker while a longer one (a hold) is still showing."""
        return any(other is not run and other.lit > 0 and other.marker == run.marker for other in self._runs.values())

    def _unlight(self, run: _Run) -> None:
        """The run no longer shows anything: its marker goes out, unless another run of the same name still shows."""
        if self._marker_held_by_another(run):
            self._notify()                                    # (same notification as before, the name simply stays lit)
        else:
            self._set_pressed(run.marker, False)

    def _unmark(self, action: _Action) -> None:
        if not action.lit:
            return
        action.lit = False
        action.run.lit -= 1
        if action.run.lit == 0:
            self._unlight(action.run)

    def _finish(self, action: _Action) -> None:
        if action.state != "active":
            return
        action.state = "done"
        for owner in action.owners:
            self.injector.up(owner)
        self._unmark(action)
        self._check_end(action.run)

    def _touch_down(self, action: _Action, x: float | None = None, y: float | None = None, owner=None) -> bool:
        b = action.spec
        if not self.injector.down(owner or action.owners[0], b.x if x is None else x, b.y if y is None else y):
            self._abort(action)
            return False
        if owner is None and x is None and action.mode in ALSO_MODES:      # the action's extra spots, with their own fingers
            for extra_owner, (ex, ey) in zip(action.owners[1:], b.also):
                self.injector.down(extra_owner, ex, ey)
        return True

    def _abort(self, action: _Action) -> None:
        """The action could not start or go on (no free finger, no connection, no screen size). It ends here and now, nothing
        of it stays down. What else happens is the binding's on_failure: "continue" leaves its other actions alone, "abort"
        cancels the whole run (the pending ones, and every finger the run has down). A tap that was already delivered cannot be
        taken back."""
        run = action.run
        action.state = "done"
        self.action_failures += 1
        reason = ("no free touch pointer" if self.injector.active_count >= protocol.MAX_POINTERS
                  else "the touch could not be sent")
        self.recent_failures.append((run.marker, action.index, reason))
        log.warning("Action %d of %s could not run: %s", action.index, run.marker, reason)
        if action.lit:                                        # (an action that had already lit its marker)
            action.lit = False
            run.lit -= 1
            if run.lit == 0 and run.binding.extra_actions:    # a binding with one action keeps its old, un-cleared highlight
                self._unlight(run)
        if run.binding.on_failure == "abort":
            self._cancel_run(run)
        else:
            self._check_end(run)

    def _cancel_run(self, run: _Run) -> None:
        """Stop everything this run has pending or down, and end it."""
        run.holders.clear()
        for action in run.actions:
            if action.state == "active":
                for owner in action.owners:
                    self.injector.up(owner)
            if action.state != "done":
                action.state = "done"
            if action.lit:
                action.lit = False
                run.lit -= 1
        if run.lit == 0 and not run.ended:
            self._unlight(run)
        run.ended = True
        if self._runs.get(run.key) is run:
            del self._runs[run.key]

    def _scope_flip(self, action: _Action) -> None:
        if action.spec.scope:
            self._scope_latch = not self._scope_latch

    # hold / tap / toggle / double / repeat -------------------------------------------------------
    def _do_hold(self, action: _Action) -> None:
        if self._touch_down(action):
            self._mark(action)

    def _do_tap(self, action: _Action) -> None:
        if self._touch_down(action):
            self._mark(action)
            self._scope_flip(action)
            self._after(TAP_DURATION_MS, action, lambda: self._finish_tap(action))

    def _finish_tap(self, action: _Action) -> None:
        for owner in action.owners:
            self.injector.up(owner)
        action.state = "done"
        self._unmark(action)
        self._check_end(action.run)

    def _do_toggle(self, action: _Action) -> None:
        if self._touch_down(action):
            self._mark(action)

    def _do_double(self, action: _Action) -> None:
        if not self._touch_down(action):
            return
        self._mark(action)
        self._scope_flip(action)
        owner = action.owners[0]
        self._after(TAP_DURATION_MS, action, lambda: self.injector.up(owner))
        self._after(TAP_DURATION_MS + DOUBLE_TAP_GAP_MS, action, lambda: self._touch_down(action))
        self._after(2 * TAP_DURATION_MS + DOUBLE_TAP_GAP_MS, action, lambda: self._finish_tap(action))

    def _do_repeat(self, action: _Action) -> None:
        self._mark(action)
        self._repeat_step(action)

    def _repeat_step(self, action: _Action) -> None:
        if not self._alive(action):
            return
        period = max(int(1000 / action.spec.rate_hz), 34)
        hold = min(TAP_DURATION_MS, period // 2)
        if self._touch_down(action):
            self._after(hold, action, lambda: self.injector.up(action.owners[0]))
            self._after(period, action, lambda: self._repeat_step(action))

    # swipe / drag / pinch / sequence ------------------------------------------------------------
    def _motion(self, action: _Action, steps: int, apply: Callable[[float], None], done: Callable[[], None]) -> None:
        def step(i: int) -> None:
            if not self._alive(action):
                return
            apply(i / steps)
            if i < steps:
                self._schedule(MOTION_TICK_MS, lambda: step(i + 1))
            else:
                done()
        self._schedule(MOTION_TICK_MS, lambda: step(1))

    def _do_swipe(self, action: _Action, hold_at_end: bool = False) -> None:
        b = action.spec
        if not self._touch_down(action):
            return
        self._mark(action)
        if not hold_at_end:
            self._scope_flip(action)                              # a drag holds scope while it is down; a swipe flips it
        owner = action.owners[0]
        steps = max(2, b.duration_ms // MOTION_TICK_MS)
        self._motion(action, steps,
                     lambda t: self.injector.move(owner, b.x + (b.x2 - b.x) * t, b.y + (b.y2 - b.y) * t),
                     (lambda: None) if hold_at_end else (lambda: self._finish(action)))

    def _do_drag(self, action: _Action) -> None:
        self._do_swipe(action, hold_at_end=True)

    def _do_pinch_out(self, action: _Action) -> None:
        self._pinch(action, PINCH_NEAR, 1.0)

    def _do_pinch_in(self, action: _Action) -> None:
        self._pinch(action, 1.0, PINCH_NEAR)

    def _pinch(self, action: _Action, start: float, end: float) -> None:
        b = action.spec
        w, h = self.injector.size()
        if w <= 0 or h <= 0:
            self._abort(action)
            return
        far = b.span * min(w, h) / w                               # horizontal distance of one finger, normalized

        def place(factor: float, down: bool) -> bool:
            ok = True
            for owner, sign in ((action.owners[0], -1), (action.owners[1], 1)):
                x = b.x + sign * far * factor
                ok = (self.injector.down(owner, x, b.y) if down else self.injector.move(owner, x, b.y)) and ok
            return ok

        if not place(start, True):
            self._abort(action)
            for owner in action.owners:
                self.injector.up(owner)
            return
        self._mark(action)
        steps = max(2, b.duration_ms // MOTION_TICK_MS)
        self._motion(action, steps, lambda t: place(start + (end - start) * t, False), lambda: self._finish(action))

    def _do_sequence(self, action: _Action) -> None:
        b = action.spec
        self._mark(action)
        t = 0
        last = len(b.steps) - 1
        for index, (x, y, delay) in enumerate(b.steps):
            t += delay if index == 0 else max(delay, TAP_DURATION_MS + 20)       # the previous tap must have ended
            self._after(t, action, lambda x=x, y=y: self._touch_down(action, x, y))
            self._after(t + TAP_DURATION_MS, action,
                        (lambda: self._finish(action)) if index == last else (lambda: self.injector.up(action.owners[0])))
        if not b.steps:
            self._finish(action)

    # -- movement stick --------------------------------------------------------------------
    def _effective_held(self, runtime: _StickRuntime) -> set:
        return runtime.held | ({"up"} if runtime.autorun else set())

    def _stick_target(self, runtime: _StickRuntime) -> tuple[float, float]:
        """Finger offset (pixels) the held keys ask for. Opposite keys cancel; diagonals keep full radius.
        The sprint key pushes further (sprint_scale), the walk key less far (walk_scale)."""
        held = self._effective_held(runtime)
        vx = ("right" in held) - ("left" in held)
        vy = ("down" in held) - ("up" in held)
        length = math.hypot(vx, vy)
        if length == 0:
            return (0.0, 0.0)
        w, h = self.injector.size()
        radius = runtime.stick.radius * min(w, h)
        if "sprint" in runtime.mods:
            radius *= runtime.stick.sprint_scale
        elif "walk" in runtime.mods:
            radius *= runtime.stick.walk_scale
        return (vx / length * radius, vy / length * radius)

    def _stick_update(self, runtime: _StickRuntime) -> None:
        stick = runtime.stick
        if runtime.down and not self.injector.is_down(runtime.owner):
            runtime.down = False                      # the injector dropped it (e.g. the screen rotated)
        if not self._effective_held(runtime):         # all keys up: the finger lifts at once
            runtime.generation += 1
            if runtime.down:
                self.injector.up(runtime.owner)
                runtime.down = False
            runtime.offset = (0.0, 0.0)
            return
        if not runtime.down:
            if not self.injector.down(runtime.owner, stick.x, stick.y):
                return
            runtime.down = True
            runtime.offset = (0.0, 0.0)
        runtime.generation += 1
        self._stick_step(runtime, runtime.generation)

    def _stick_step(self, runtime: _StickRuntime, generation: int) -> None:
        if generation != runtime.generation or not runtime.down:
            return
        tx, ty = self._stick_target(runtime)
        ox, oy = runtime.offset
        w, h = self.injector.size()
        reach = max(runtime.stick.radius * min(w, h), math.hypot(tx, ty))      # sprint pushes beyond the ring
        ramp = runtime.stick.ramp_ms
        step = reach if ramp <= 0 else reach * STICK_TICK_MS / ramp
        dx, dy = tx - ox, ty - oy
        distance = math.hypot(dx, dy)
        if distance <= step:
            ox, oy = tx, ty
        else:
            ox, oy = ox + dx / distance * step, oy + dy / distance * step
        runtime.offset = (ox, oy)
        self.injector.move(runtime.owner, runtime.stick.x + ox / w, runtime.stick.y + oy / h)
        if (ox, oy) != (tx, ty):
            self._schedule(STICK_TICK_MS, lambda: self._stick_step(runtime, generation))

    # -- mouse look ------------------------------------------------------------------------
    def look_delta(self, dx: float, dy: float) -> bool:
        """Raw relative mouse movement in counts. Batched, then sent as finger moves."""
        if not (self.mapping_active and self.look_captured and self._looks):
            return False
        runtime = self._looks[0]
        px, py = runtime.pending
        runtime.pending = (px + dx, py + dy)
        if not runtime.flush_scheduled:
            runtime.flush_scheduled = True
            runtime.last_flush = self._clock()          # start of this batch: dt = how long the batch took
            self._schedule(LOOK_FLUSH_MS, lambda: self._look_flush(runtime))
        return True

    def _look_zone_px(self, look: Look) -> tuple[float, float, float, float]:
        w, h = self.injector.size()
        return look.x0 * w, look.y0 * h, look.x1 * w, look.y1 * h

    def _look_lift(self, runtime: _LookRuntime) -> None:
        runtime.generation += 1
        runtime.pending = (0.0, 0.0)
        if runtime.down:
            self.injector.up(runtime.owner)
            runtime.down = False

    def _look_flush(self, runtime: _LookRuntime) -> None:
        runtime.flush_scheduled = False
        if not (self.mapping_active and self.look_captured):
            runtime.pending = (0.0, 0.0)
            return
        counts_x, counts_y = runtime.pending
        runtime.pending = (0.0, 0.0)
        now = self._clock()
        dt_ms = max((now - runtime.last_flush) * 1000.0, 1.0)
        runtime.last_flush = now
        if counts_x == 0 and counts_y == 0:
            return
        look = runtime.look
        # acceleration: fast flicks travel further than slow movements
        speed = math.hypot(counts_x, counts_y) / dt_ms
        gain = 1.0 + look.accel * min(speed / LOOK_ACCEL_REF, LOOK_ACCEL_MAX)
        if self.scope_active:
            gain *= look.ads_scale                       # slower, steadier aim while scoped in
        dx = counts_x * look.sens_x * gain
        dy = counts_y * look.sens_y * gain * (-1.0 if look.invert_y else 1.0)

        w, h = self.injector.size()
        if w <= 0 or h <= 0:
            return
        if runtime.down and not self.injector.is_down(runtime.owner):
            runtime.down = False                  # the injector dropped it (screen rotated)
        zx0, zy0, zx1, zy1 = self._look_zone_px(look)
        cx, cy = (zx0 + zx1) / 2, (zy0 + zy1) / 2
        mx, my = (zx1 - zx0) * LOOK_EDGE_MARGIN, (zy1 - zy0) * LOOK_EDGE_MARGIN
        inner = (zx0 + mx, zy0 + my, zx1 - mx, zy1 - my)

        def inside(x: float, y: float) -> bool:
            return inner[0] <= x <= inner[2] and inner[1] <= y <= inner[3]

        if runtime.down and not inside(runtime.pos[0] + dx, runtime.pos[1] + dy):
            # Reached the edge: lift and land again in the centre, then apply this movement from there.
            self.injector.up(runtime.owner)
            runtime.down = False
            runtime.resets += 1
        if not runtime.down:
            if not self.injector.down(runtime.owner, cx / w, cy / h):
                return
            runtime.down = True
            runtime.pos = (cx, cy)
        x = min(max(runtime.pos[0] + dx, inner[0]), inner[2])
        y = min(max(runtime.pos[1] + dy, inner[1]), inner[3])
        runtime.pos = (x, y)
        self.injector.move(runtime.owner, x / w, y / h)

        runtime.generation += 1
        generation = runtime.generation
        self._schedule(LOOK_IDLE_MS, lambda: self._look_idle(runtime, generation))

    def _look_idle(self, runtime: _LookRuntime, generation: int) -> None:
        if generation == runtime.generation and runtime.down:
            self.injector.up(runtime.owner)
            runtime.down = False

    # -- internals -------------------------------------------------------------------------
    def _set_pressed(self, name: str, down: bool) -> None:
        (self._pressed.add if down else self._pressed.discard)(name)
        self._notify()

    def _notify(self) -> None:
        if self.on_press_changed:
            self.on_press_changed(set(self._pressed))
