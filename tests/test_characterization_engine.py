"""Characterization tests: what the input engine does TODAY (Phase 2, step 1).

These tests document existing behaviour so that the multi-trigger / multi-action rewrite can be proven not to change
what already works. They are written against the public engine API only (key_event, mouse_button, wheel_binding,
look_delta, release_all, screen_changed, set_mapping_active) plus the injector's finger count, never the private action
table, so they survive the planned change of the engine's internal bookkeeping.

Tests marked AMBIGUOUS pin behaviour that is questionable (it may be a defect, or deliberate). They record what the code
does now; they are NOT a statement that it is right. If Phase 2 changes one of them on purpose, change the test in the
same commit and say so. The tests of NEW behaviour live in test_multi_trigger.py, test_multi_action.py and test_shared_runtime.py, never here.

Behaviour that other files already pin (the individual mode semantics, stick ramps, combination basics, rotation of a
single finger, the look finger's idle lift, ...) is not repeated here; this file fills the gaps around it.
"""
import pytest

from app.input.engine import InputEngine, TAP_DURATION_MS
from app.input.touch import TouchInjector
from app.mapping.profile import MODES, Binding, Look, Profile, Stick
from app.stream import protocol
from test_mapping import FakeControl

W, H = 2400, 1080
D, U, M = protocol.TOUCH_DOWN, protocol.TOUCH_UP, protocol.TOUCH_MOVE
PORTRAIT = (1080, 2400)


class Rig:
    """Engine + injector + manual timers. `size` and `control` can be changed by the test (rotation, disconnect)."""

    def __init__(self, bindings=(), stick=None, look=None, size=(W, H)):
        self.size = size
        self.control = FakeControl()
        self.now = 0.0
        self.timers = []
        injector = TouchInjector(lambda: self.control, lambda: self.size)
        self.engine = InputEngine(injector, lambda ms, fn: self.timers.append((self.now + ms, fn)),
                                  clock=lambda: 100.0 + self.now / 1000)
        self.profile = Profile("t")
        for binding in bindings:
            self.profile.add(binding)
        if stick:
            self.profile.set_stick(stick)
        if look:
            self.profile.looks.append(look)
        self.engine.set_profile(self.profile)
        self.engine.set_mapping_active(True)
        self.pressed_log = []
        self.engine.on_press_changed = lambda keys: self.pressed_log.append(frozenset(keys))

    def advance(self, ms):
        end = self.now + ms
        while True:
            due = sorted((t for t in self.timers if t[0] <= end), key=lambda t: t[0])
            if not due:
                break
            when, fn = due[0]
            self.timers.remove(due[0])
            self.now = when
            fn()
        self.now = end

    def press(self, name, ms=0):
        result = self.engine.key_event(name, True)
        self.advance(ms)
        return result

    def release(self, name, ms=0):
        result = self.engine.key_event(name, False)
        self.advance(ms)
        return result

    def touches(self):
        return self.control.touches()

    def kinds(self):
        return [t[0] for t in self.touches()]

    def fingers(self):
        return self.engine.injector.active_count

    def pointer_events(self):
        """[(kind, pointer id)] in the order they were sent."""
        return [(t[0], t[1]) for t in self.touches()]


# one binding of every mode, on its own key, built the way a profile file would hold it
SPECS = {
    "hold": {}, "tap": {}, "toggle": {}, "double": {}, "repeat": {"rate_hz": 10},
    "swipe": {"x2": 0.6, "y2": 0.5}, "drag": {"x2": 0.6, "y2": 0.5},
    "pinch-out": {"span": 0.1}, "pinch-in": {"span": 0.1},
    "sequence": {"steps": [(0.1, 0.1, 0), (0.2, 0.2, 300)]},
}


def bound(key, mode, x=0.5, y=0.5, **kw):
    return Binding(key, x, y, mode, **{**SPECS[mode], **kw})


def test_the_specs_cover_every_mode_there_is():
    assert set(SPECS) == set(MODES)                    # a new mode must get characterization tests too


# ===================================================================== every mode, end to end
@pytest.mark.parametrize("mode", list(SPECS))
def test_every_mode_ends_with_every_finger_up_and_every_down_matched_by_an_up(mode):
    r = Rig([bound("K", mode)])
    r.press("K")
    r.release("K")
    if mode == "toggle":
        r.press("K")                                   # the second press lets go
        r.release("K")
    r.advance(3000)
    assert r.fingers() == 0
    assert r.kinds().count(D) == r.kinds().count(U) >= 1


@pytest.mark.parametrize("mode", list(SPECS))
def test_release_all_in_the_middle_of_any_mode_lifts_everything_and_cancels_the_timers(mode):
    r = Rig([bound("K", mode)])
    r.press("K", 20)
    r.engine.release_all()
    assert r.fingers() == 0
    sent = r.pointer_events()
    r.advance(3000)                                    # no timer scheduled before release_all may do anything now
    assert r.pointer_events() == sent and r.fingers() == 0
    downs = sorted(p for k, p in sent if k == D)
    ups = sorted(p for k, p in sent if k == U)
    assert downs == ups                                # each finger that went down was lifted exactly once


@pytest.mark.parametrize("mode, downs", [("tap", 1), ("double", 2), ("pinch-out", 2), ("sequence", 2)])
def test_one_shot_modes_finish_by_themselves_even_if_the_key_is_released_at_once(mode, downs):
    r = Rig([bound("K", mode)])
    r.press("K")
    r.release("K")
    r.advance(3000)
    assert r.kinds().count(D) == downs == r.kinds().count(U) and r.fingers() == 0


def test_a_one_shot_key_that_stays_down_fires_once_the_engine_relies_on_the_router_to_drop_os_auto_repeat():
    r = Rig([bound("K", "tap")])
    r.press("K", 1000)
    assert r.kinds() == [D, U]                         # held: one tap
    r.press("K", 1000)                                 # the same key-down again WITHOUT a release in between
    assert r.kinds() == [D, U, D, U]                   # AMBIGUOUS: the engine itself does not filter repeats (router.py does)


def test_a_repeated_key_down_while_a_hold_is_running_is_ignored_so_the_release_still_lifts_the_finger():
    r = Rig([bound("K", "hold")])
    r.press("K")
    r.press("K")
    r.press("K")
    assert r.kinds() == [D] and r.fingers() == 1
    r.release("K")
    assert r.kinds() == [D, U] and r.fingers() == 0                   # without the guard the action is dropped and the finger sticks


def test_a_hold_is_lifted_exactly_once_and_a_second_release_does_nothing():
    r = Rig([bound("K", "hold")])
    r.press("K")
    assert r.release("K") is True
    assert r.release("K") is False
    assert r.kinds() == [D, U]


def test_key_event_says_whether_it_handled_the_event():
    r = Rig([bound("K", "hold")], stick=Stick(0.15, 0.7, ramp_ms=0))
    assert r.press("K") is True and r.release("K") is True
    assert r.press("Z") is False and r.release("Z") is False          # not mapped
    assert r.press("W") is True and r.release("W") is True            # the stick takes it
    r.engine.set_mapping_active(False)
    assert r.press("K") is False and r.release("K") is False          # mapping off: nothing is handled
    assert r.touches()[-1][0] == U and r.fingers() == 0


# ===================================================================== combinations and precedence
def test_a_combination_matches_only_the_exact_set_of_held_modifiers():
    r = Rig([Binding("Ctrl+Shift+W", 0.2, 0.2, "hold"), Binding("W", 0.5, 0.5, "hold")])
    for held, expected_x in (([], 1200), (["Shift"], 1200), (["Ctrl"], 1200), (["Ctrl", "Shift"], 480),
                             (["Ctrl", "Shift", "Alt"], 1200)):
        r.engine.release_all()
        r.control.messages.clear()
        for name in held:
            r.press(name)
        r.press("W")
        assert r.touches()[0][2] == expected_x, held
        r.engine.release_all()


def test_a_combination_and_a_plain_binding_for_the_same_key_are_two_separate_bindings():
    p = Profile("p")
    p.add(Binding("W", 0.5, 0.5, "hold"))
    p.add(Binding("Shift+W", 0.2, 0.2, "hold"))
    assert [b.input for b in p.bindings] == ["W", "Shift+W"]
    assert p.find("W") is not p.find("Shift+W")


def test_a_modifier_pressed_after_a_plain_hold_began_does_not_take_that_hold_over():
    r = Rig([Binding("W", 0.5, 0.5, "hold"), Binding("Shift+W", 0.2, 0.2, "hold")])
    r.press("W")
    r.press("Shift")
    r.release("Shift")
    assert r.kinds() == [D] and r.fingers() == 1       # the plain hold neither restarts nor ends
    r.release("W")
    assert r.kinds() == [D, U]


def test_a_modifier_can_itself_be_bound_and_still_form_combinations():
    r = Rig([Binding("Shift", 0.5, 0.5, "hold"), Binding("Shift+W", 0.2, 0.2, "tap")])
    r.press("Shift")
    r.press("W", 100)
    assert r.pointer_events() == [(D, 0), (D, 1), (U, 1)]             # Shift's own hold, then the combination's tap
    r.release("Shift")
    assert r.fingers() == 0


def test_mouse_button_combinations_beat_the_plain_button_and_direct_touch():
    r = Rig([Binding("Ctrl+LMB", 0.2, 0.2, "tap"), Binding("RMB", 0.8, 0.8, "hold")])
    r.press("Ctrl")
    assert r.engine.mouse_button("LMB", True, (0.5, 0.5)) is True
    r.advance(100)
    assert r.touches()[0][2:4] == (480, 216)                          # the combination's spot, not the click position
    r.engine.mouse_button("LMB", False, (0.5, 0.5))
    r.release("Ctrl")
    r.control.messages.clear()
    assert r.engine.mouse_button("LMB", True, (0.5, 0.5)) is True     # no binding for plain LMB: direct touch
    assert r.touches()[0][2:4] == (1200, 540)
    r.engine.mouse_button("LMB", False, (0.5, 0.5))
    assert r.fingers() == 0


def test_a_mouse_button_release_finds_its_action_even_if_the_modifiers_changed_meanwhile():
    r = Rig([Binding("LMB", 0.5, 0.5, "hold")])
    r.engine.mouse_button("LMB", True, None)
    r.press("Shift")                                                  # Shift+LMB would be a different lookup now
    assert r.engine.mouse_button("LMB", False, None) is True
    assert r.kinds() == [D, U] and r.fingers() == 0


def test_key_names_are_case_sensitive_and_only_the_modifier_order_is_normalised():
    # AMBIGUOUS: the router always sends canonical names, so this only matters to hand-edited profile files.
    assert Binding("shift+w", 0.5, 0.5).input == "shift+w"            # not turned into Shift+W
    assert Binding("Shift+Ctrl+W", 0.5, 0.5).input == "Ctrl+Shift+W"
    r = Rig([Binding("w", 0.5, 0.5, "hold")])
    assert r.press("W") is False and r.press("w") is True


# ===================================================================== stick together with modifiers
def test_a_stick_key_with_a_modifier_held_and_no_combination_only_moves_the_stick():
    r = Rig([Binding("R", 0.7, 0.9, "tap")], stick=Stick(0.15, 0.7, radius=0.1, ramp_ms=0))
    r.press("Shift")
    assert r.press("W") is True
    assert r.kinds() == [D, M] and r.fingers() == 1


def test_a_modifier_that_is_also_the_sprint_key_still_forms_combinations_with_stick_keys():
    r = Rig([Binding("Shift+W", 0.8, 0.3, "hold")],
            stick=Stick(0.15, 0.7, radius=0.1, ramp_ms=0, sprint_key="Shift", sprint_scale=1.5))
    r.press("Shift")
    r.press("W")
    assert r.fingers() == 2                                           # the stick finger and the combination's finger
    ups_before = r.kinds().count(U)
    r.release("W")
    assert r.fingers() == 0 and r.kinds().count(U) == ups_before + 2


def test_stick_keys_cannot_start_plain_bindings_even_if_one_is_forced_into_the_profile():
    p = Profile("p")
    p.set_stick(Stick(0.15, 0.7, ramp_ms=0))
    p.bindings.append(Binding("W", 0.5, 0.5, "hold"))                 # bypasses the checks in Profile.set_stick
    r = Rig()
    r.profile = p
    r.engine.set_profile(p)
    r.press("W")
    assert r.fingers() == 1                                           # only the stick finger: the stick takes the key first


# ===================================================================== duplicate triggers inside the engine
def test_if_two_bindings_share_an_input_the_last_one_in_the_list_wins_silently():
    # AMBIGUOUS: Profile.add prevents this, but profile.bindings can be edited directly; the engine keeps the last one.
    p = Profile("p")
    p.bindings = [Binding("R", 0.1, 0.1, "hold"), Binding("R", 0.9, 0.9, "hold")]
    r = Rig()
    r.engine.set_profile(p)
    r.press("R")
    assert r.touches()[0][2:4] == (2160, 972) and r.fingers() == 1


# ===================================================================== Binding.also and partial activation
def filled(extra_bindings, taken):
    """A rig whose first `taken` pointer ids are already used by plain holds on A, B, C, ..."""
    keys = "ABCDEFGHIJ"[:taken]
    r = Rig([Binding(k, 0.05 + 0.09 * i, 0.5, "hold") for i, k in enumerate(keys)] + extra_bindings)
    for k in keys:
        r.press(k)
    return r


def test_extra_spots_get_the_next_free_pointer_ids_in_order_after_the_main_one():
    r = Rig([Binding("L", 0.8, 0.7, "hold", also=[(0.1, 0.4), (0.2, 0.5)])])
    r.press("L")
    assert r.pointer_events() == [(D, 0), (D, 1), (D, 2)]
    assert [t[2:4] for t in r.touches()] == [(1920, 756), (240, 432), (480, 540)]


def test_when_the_screen_runs_out_of_pointers_the_extra_spots_fail_silently_and_the_rest_still_work():
    # AMBIGUOUS: partial activation is neither reported nor undone; only a log line is written.
    r = filled([Binding("L", 0.8, 0.7, "hold", also=[(0.1, 0.4), (0.2, 0.5), (0.3, 0.6)])], taken=8)
    assert r.press("L") is True
    assert r.fingers() == 10                                          # the main spot and the first extra got the last two
    assert [e for e in r.pointer_events()[8:]] == [(D, 8), (D, 9)]    # the other two extras were simply never sent
    r.release("L")
    assert r.pointer_events()[10:] == [(U, 8), (U, 9)] and r.fingers() == 8     # only what went down is lifted


def test_when_the_main_spot_cannot_be_touched_nothing_starts_and_the_key_can_be_pressed_again():
    r = filled([Binding("L", 0.8, 0.7, "hold", also=[(0.1, 0.4)])], taken=10)
    assert r.press("L") is True                                       # AMBIGUOUS: reported as handled although nothing was touched
    assert r.fingers() == 10 and r.kinds().count(D) == 10
    assert "L" not in r.pressed_log[-1]
    r.release("A")                                                    # a pointer frees up
    assert r.press("L") is True and r.fingers() == 10                 # no release of L was needed: it is not "stuck"
    assert r.touches()[-1][:2] == (D, 0) and r.touches()[-1][2:4] == (1920, 756)


def test_a_dead_control_connection_means_a_press_touches_nothing_and_leaves_no_stuck_action():
    r = Rig([Binding("L", 0.8, 0.7, "hold", also=[(0.1, 0.4)])])
    live = r.control
    r.control = None
    r.press("L")
    assert r.fingers() == 0
    r.control = live
    r.release("L")
    r.press("L")
    assert r.pointer_events() == [(D, 0), (D, 1)]                     # works normally once the connection is back


@pytest.mark.parametrize("mode", ["double", "repeat", "drag", "swipe", "sequence"])
def test_modes_that_cannot_use_extra_spots_touch_only_the_main_spot(mode):
    r = Rig([bound("K", mode, also=[(0.9, 0.9)])])
    r.press("K", 20)
    r.release("K", 3000)
    assert {p for _k, p in r.pointer_events()} == {0}


def test_every_extra_spot_of_a_toggle_is_lifted_by_one_release_all_and_the_toggle_starts_fresh():
    r = Rig([Binding("T", 0.5, 0.5, "toggle", also=[(0.2, 0.2), (0.3, 0.3)])])
    r.press("T")
    r.release("T")
    assert r.fingers() == 3
    r.engine.release_all()
    assert r.fingers() == 0
    r.press("T")                                                      # a fresh press turns it ON again, not off
    assert r.fingers() == 3


def test_one_binding_going_up_never_lifts_another_bindings_fingers_and_ids_are_reused_lowest_first():
    r = Rig([Binding("A", 0.1, 0.1, "hold", also=[(0.2, 0.2)]), Binding("B", 0.3, 0.3, "hold"),
             Binding("C", 0.4, 0.4, "hold")])
    r.press("A")
    r.press("B")
    assert r.pointer_events() == [(D, 0), (D, 1), (D, 2)]             # A: 0 and 1, B: 2
    r.release("A")
    assert r.pointer_events()[3:] == [(U, 0), (U, 1)] and r.fingers() == 1      # B's finger (2) is untouched
    r.press("C")
    assert r.pointer_events()[-1] == (D, 0)                           # lowest free id first
    r.release("B")
    assert r.pointer_events()[-1] == (U, 2) and r.fingers() == 1


# ===================================================================== the mouse wheel
def test_a_wheel_notch_is_a_one_shot_version_of_the_binding_mode():
    r = Rig([bound("WheelUp", "hold"), bound("WheelDown", "drag")])
    assert r.engine.wheel_binding(+1) is True
    r.advance(TAP_DURATION_MS + 1)
    assert r.kinds() == [D, U]                                        # hold -> a quick tap, no key release needed
    r.engine.wheel_binding(-1)
    r.advance(1000)
    assert r.touches()[-1][0] == U and r.touches()[-1][2:4] == (1440, 540)    # drag -> a swipe that lets go at its end
    assert r.fingers() == 0


def test_a_repeat_binding_taps_once_per_wheel_notch():
    r = Rig([bound("WheelUp", "repeat")])
    r.engine.wheel_binding(+1)
    r.advance(1000)
    assert r.kinds() == [D, U]


def test_a_toggle_binding_on_the_wheel_stays_a_toggle_each_notch_flips_it():
    r = Rig([bound("WheelUp", "toggle")])
    r.engine.wheel_binding(+1)
    r.advance(500)
    assert r.kinds() == [D] and r.fingers() == 1
    r.engine.wheel_binding(+1)
    assert r.kinds() == [D, U] and r.fingers() == 0


def test_a_wheel_notch_that_arrives_while_the_same_binding_is_still_running_is_dropped():
    # AMBIGUOUS: a fast scroll loses notches that land inside the 40 ms tap.
    r = Rig([bound("WheelUp", "tap")])
    r.engine.wheel_binding(+1)
    r.engine.wheel_binding(+1)
    r.advance(100)
    assert r.kinds() == [D, U]
    r.engine.wheel_binding(+1)                                        # once it has finished, the next notch works
    r.advance(100)
    assert r.kinds() == [D, U, D, U]


def test_the_wheel_does_nothing_without_a_binding_without_movement_or_with_mapping_off():
    r = Rig([bound("WheelUp", "tap")])
    assert r.engine.has_wheel_binding() is True
    assert r.engine.wheel_binding(0) is False
    assert r.engine.wheel_binding(-1) is False                        # only WheelUp is bound
    r.engine.set_mapping_active(False)
    assert r.engine.wheel_binding(+1) is False and r.engine.has_wheel_binding() is False
    assert r.touches() == []


def test_the_overlay_is_told_about_a_wheel_notch_and_about_its_end():
    r = Rig([bound("WheelUp", "tap")])
    r.engine.wheel_binding(+1)
    assert "WheelUp" in r.pressed_log[-1]
    r.advance(100)
    assert "WheelUp" not in r.pressed_log[-1]


# ===================================================================== scope state
@pytest.mark.parametrize("mode", ["tap", "double"])
def test_a_scope_button_that_taps_flips_the_scope_once_per_activation(mode):
    r = Rig([bound("RMB", mode, scope=True)])
    for expected in (True, False, True):
        r.engine.mouse_button("RMB", True, None)
        r.engine.mouse_button("RMB", False, None)
        r.advance(500)
        assert r.engine.scope_active is expected


def test_a_scope_that_could_not_be_touched_does_not_turn_scope_on():
    r = filled([Binding("RMB", 0.9, 0.5, "hold", scope=True)], taken=10)
    r.engine.mouse_button("RMB", True, None)
    assert r.engine.scope_active is False


def test_two_scope_buttons_keep_scope_on_until_both_are_released_and_either_order_works():
    # CHANGED in Phase 2 step 6. This used to pin a probable defect (releasing Z switched scope off while RMB was still held).
    # scope_active is now derived from every scope source; test_multi_action.py has the full set of scope tests.
    for first, second in (("Z", "RMB"), ("RMB", "Z")):
        r = Rig([Binding("RMB", 0.9, 0.5, "hold", scope=True), Binding("Z", 0.8, 0.4, "hold", scope=True)])
        r.engine.mouse_button("RMB", True, None)
        r.press("Z")
        for name in (first, second):
            assert r.engine.scope_active is True
            (r.engine.mouse_button(name, False, None) if name == "RMB" else r.release(name))
        assert r.engine.scope_active is False and r.fingers() == 0


def test_a_hold_scope_on_the_wheel_latches_because_the_wheel_runs_it_as_a_flipping_tap():
    # AMBIGUOUS (probable defect): the tap equivalent flips the flag but nothing ever clears it on release.
    r = Rig([Binding("WheelDown", 0.9, 0.5, "hold", scope=True)])
    r.engine.wheel_binding(-1)
    r.advance(100)
    assert r.engine.scope_active is True and r.fingers() == 0
    r.engine.wheel_binding(-1)
    r.advance(100)
    assert r.engine.scope_active is False


@pytest.mark.parametrize("how", ["release_all", "screen_changed", "mapping_off"])
def test_scope_state_is_cleared_by_every_cleanup_path(how):
    r = Rig([Binding("RMB", 0.9, 0.5, "toggle", scope=True)])
    r.engine.mouse_button("RMB", True, None)
    assert r.engine.scope_active is True
    {"release_all": r.engine.release_all, "screen_changed": r.engine.screen_changed,
     "mapping_off": lambda: r.engine.set_mapping_active(False)}[how]()
    assert r.engine.scope_active is False and r.fingers() == 0


# ===================================================================== mouse look
def test_only_the_first_look_zone_is_used_if_a_profile_has_several():
    r = Rig(look=Look(0.1, 0.1, 0.3, 0.3))
    r.profile.looks.append(Look(0.6, 0.1, 0.9, 0.3))
    r.engine.set_profile(r.profile)
    r.engine.set_look_captured(True)
    r.engine.look_delta(5, 0)
    r.advance(10)
    assert r.touches()[0][2:4] == (480, 216)                          # the centre of the FIRST zone
    assert r.fingers() == 1


def test_the_look_finger_is_separate_from_button_fingers_and_release_all_discards_unsent_movement():
    r = Rig([Binding("A", 0.1, 0.9, "hold")], look=Look(0.5, 0.2, 0.9, 0.8))
    r.engine.set_look_captured(True)
    r.press("A")
    r.engine.look_delta(10, 0)
    r.advance(10)
    assert r.fingers() == 2 and {p for _k, p in r.pointer_events()} == {0, 1}
    r.engine.look_delta(50, 0)                                        # not flushed yet ...
    r.engine.release_all()                                            # ... and now the screen must be clean
    sent = len(r.touches())
    r.advance(1000)
    assert len(r.touches()) == sent and r.fingers() == 0
    assert r.engine.look_captured is True                             # focus loss does not free the mouse; mapping off does


def test_turning_mapping_off_also_ends_mouse_capture():
    r = Rig(look=Look(0.5, 0.2, 0.9, 0.8))
    r.engine.set_look_captured(True)
    assert r.engine.look_captured is True
    r.engine.set_mapping_active(False)
    assert r.engine.look_captured is False and r.engine.look_delta(5, 5) is False


# ===================================================================== cleanup paths (focus loss, disconnect, rotation)
def busy_rig():
    """A button hold, a toggle, a repeat, the stick, a direct mouse finger and the look finger, all down at once."""
    r = Rig([Binding("G", 0.1, 0.9, "hold", also=[(0.2, 0.9)]), Binding("T", 0.3, 0.9, "toggle"),
             Binding("F", 0.5, 0.9, "repeat", rate_hz=10)],
            stick=Stick(0.15, 0.7, radius=0.1, ramp_ms=0), look=Look(0.5, 0.2, 0.9, 0.8))
    r.press("G")
    r.press("T")
    r.release("T")
    r.press("W")
    assert r.engine.mouse_button("LMB", True, (0.9, 0.1)) is True     # direct touch (before the mouse is captured)
    r.engine.set_look_captured(True)
    r.engine.look_delta(10, 0)
    r.advance(10)
    r.press("F")                                                      # the repeat's finger is down at this instant
    return r


def test_release_all_lifts_every_kind_of_finger_once_clears_the_highlights_and_play_continues():
    r = busy_rig()
    assert r.fingers() == 7                                           # G+extra, T, stick, direct, look, repeat
    r.engine.release_all()
    assert r.fingers() == 0 and r.pressed_log[-1] == frozenset()
    downs = sorted(p for k, p in r.pointer_events() if k == D)
    ups = sorted(p for k, p in r.pointer_events() if k == U)
    assert downs == ups
    assert r.engine.mapping_active is True                            # focus loss pauses nothing: the keys keep working
    sent = len(r.touches())
    r.advance(3000)
    assert len(r.touches()) == sent                                   # no repeat tap, no ramp, no idle lift comes later
    r.press("G")
    assert r.touches()[-1][:2] == (D, 1) and r.fingers() == 2


def test_a_key_released_after_release_all_does_nothing_and_a_key_still_held_needs_a_new_press():
    r = busy_rig()
    r.engine.release_all()
    sent = len(r.touches())
    assert r.release("G") is False and r.release("F") is False
    assert len(r.touches()) == sent


def test_a_lost_connection_during_cleanup_is_harmless_and_the_engine_forgets_the_fingers():
    # The UPs cannot reach the phone; ControlChannel.close(graceful=True) is what lifts them there (see
    # test_characterization_channel.py). The engine must not keep ghosts that would block pointer ids later.
    r = busy_rig()
    r.control = None
    r.engine.release_all()
    assert r.fingers() == 0
    r.control = FakeControl()
    r.press("G")
    assert r.pointer_events() == [(D, 0), (D, 1)]


def test_a_rotation_lifts_the_extra_spots_of_a_held_toggle_at_the_new_size_and_the_toggle_restarts_off():
    r = Rig([Binding("T", 0.5, 0.5, "toggle", also=[(0.25, 0.5)])])
    r.press("T")
    r.release("T")
    r.size = PORTRAIT
    r.engine.screen_changed()
    ups = [t for t in r.touches() if t[0] == U]
    assert sorted(t[1] for t in ups) == [0, 1] and all(t[4:] == PORTRAIT for t in ups)
    assert (ups[0][2], ups[0][3]) == (540, 1200) or (ups[1][2], ups[1][3]) == (540, 1200)
    assert r.fingers() == 0
    r.press("T")                                                      # a fresh toggle press turns it on again
    assert r.fingers() == 2 and all(t[4:] == PORTRAIT for t in r.touches()[-2:])


def test_a_rotation_mid_way_through_a_wheel_pinch_and_a_hold_leaves_no_finger_and_no_late_messages():
    r = Rig([bound("WheelUp", "pinch-out", duration_ms=400), Binding("A", 0.1, 0.9, "hold")])
    r.press("A")
    r.engine.wheel_binding(+1)
    r.advance(40)
    r.size = PORTRAIT
    r.engine.screen_changed()
    assert r.fingers() == 0
    sent = len(r.touches())
    r.advance(3000)
    assert len(r.touches()) == sent
    assert all(t[4:] == PORTRAIT for t in r.touches()[-3:])           # the three lifts used the new size
