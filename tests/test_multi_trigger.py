"""Several triggers, one logical binding, one primary action (Phase 2, step 5).

A binding with triggers E, F and the middle mouse button has ONE run: whichever trigger comes first starts it, the others join
it, and what ends it depends on the mode:
  hold / repeat / drag   last until the LAST trigger that sustains them is let go (a trigger's sustaining keys are its key and its
                         modifiers); a key-down of a trigger that is already down changes nothing
  tap / double / swipe / pinch / sequence
                         a trigger pressed while the run is still going is absorbed (no competing copy); once it has finished the
                         next press starts it again immediately (no debounce)
  toggle                 a trigger pressed while another still holds the activation joins it; a fresh press (everything let go)
                         switches it off, the next one on again. A repeated key-down of the trigger that is down still flips it,
                         exactly as before: the router never delivers OS key repeat, and changing it would alter legacy traces.
  mouse wheel            an impulse: runs the one-shot version, never sustains anything
NOT in this step (see test_shared_runtime.py): additional actions, and several bindings that share a trigger all running.
Every explicit expectation here was written from the rules above; test_engine_traces.py pins the same scenarios byte for byte."""
import itertools

import pytest

from app.input.engine import PRIMARY, InputEngine
from app.mapping.profile import Action, Binding, Profile, Stick, trigger_owners
from app.stream import protocol
from test_characterization_engine import SPECS, Rig, bound

D, U, M = protocol.TOUCH_DOWN, protocol.TOUCH_UP, protocol.TOUCH_MOVE
W, H = 2400, 1080


def multi(key, x=0.62, y=0.55, mode="hold", triggers=("F",), **kw):
    return Binding(key, x, y, mode, extra_triggers=list(triggers), **{**SPECS.get(mode, {}), **kw})


def downs(r):
    return r.kinds().count(D)


def at(r, x, y):
    return [t for t in r.touches() if (t[2], t[3]) == (int(x * W + 1e-6), int(y * H + 1e-6))]


def mouse(r, name, pressed):
    return r.engine.mouse_button(name, pressed, (0.5, 0.5))


# ===================================================================== hold: one finger, released with the last trigger
@pytest.mark.parametrize("first, second", [("E", "F"), ("F", "E")])
def test_two_keys_for_one_hold_release_in_either_order_and_lift_only_when_both_are_up(first, second):
    r = Rig([multi("E")])
    r.press("E")
    r.press("F")
    assert r.kinds() == [D] and r.fingers() == 1                    # the second key joins: no second finger
    r.release(first)
    assert r.kinds() == [D] and r.fingers() == 1                    # one trigger still down: the action stays down
    r.release(second)
    assert r.kinds() == [D, U] and r.fingers() == 0


@pytest.mark.parametrize("order", list(itertools.permutations(["E", "F", "G"])))
def test_a_third_trigger_joins_and_every_release_order_ends_with_exactly_one_up(order):
    r = Rig([multi("E", triggers=["F", "G"])])
    for name in ("E", "F", "G"):
        r.press(name)
    assert r.kinds() == [D]
    for name in order[:-1]:
        r.release(name)
        assert r.kinds() == [D] and r.fingers() == 1                # still sustained by the others
    r.release(order[-1])
    assert r.kinds() == [D, U] and r.fingers() == 0


def test_a_key_and_a_mouse_button_can_share_one_action():
    r = Rig([multi("E", triggers=["MMB"])])
    r.press("E")
    assert mouse(r, "MMB", True) is True
    r.release("E")
    assert r.kinds() == [D] and r.fingers() == 1
    mouse(r, "MMB", False)
    assert r.kinds() == [D, U]


@pytest.mark.parametrize("order", list(itertools.permutations(["E", "MMB", "RMB"])))
def test_mapped_mouse_buttons_and_keys_share_the_run_in_any_order(order):
    r = Rig([multi("E", triggers=["MMB", "RMB"])])
    press = {"E": lambda: r.press("E"), "MMB": lambda: mouse(r, "MMB", True), "RMB": lambda: mouse(r, "RMB", True)}
    release = {"E": lambda: r.release("E"), "MMB": lambda: mouse(r, "MMB", False), "RMB": lambda: mouse(r, "RMB", False)}
    for name in ("E", "MMB", "RMB"):
        press[name]()
    for name in order[:-1]:
        release[name]()
        assert r.kinds() == [D]
    release[order[-1]]()
    assert r.kinds() == [D, U] and r.fingers() == 0


def test_a_mapped_extra_mouse_button_is_a_trigger_not_a_direct_finger():
    r = Rig([multi("E", triggers=["MMB"])])
    assert mouse(r, "MMB", True) is True
    assert r.fingers() == 1 and r.touches()[0][2:4] == (1488, 594)          # the binding's spot, not the click position (0.5, 0.5)
    mouse(r, "MMB", False)
    assert r.kinds() == [D, U] and r.fingers() == 0


def test_a_trigger_that_is_released_and_pressed_again_starts_a_new_run_after_the_old_one_ended():
    r = Rig([multi("E")])
    r.press("E")
    r.release("E")
    r.press("F")
    r.release("F")
    assert r.kinds() == [D, U, D, U]


def test_the_second_trigger_can_be_the_one_that_starts_the_run():
    r = Rig([multi("E")])
    r.press("F")
    r.press("E")
    r.release("F")
    assert r.kinds() == [D]
    r.release("E")
    assert r.kinds() == [D, U]


@pytest.mark.parametrize("mode", ["hold", "repeat", "drag"])
def test_pressing_a_trigger_again_while_it_is_down_never_adds_an_activation(mode):
    r = Rig([multi("E", mode=mode)])
    for name in ("E", "E", "F", "F", "E"):
        r.press(name)
    before = downs(r)
    assert r.fingers() == 1 and before == 1
    r.release("E")
    r.advance(300 if mode == "repeat" else 0)
    assert r.fingers() == 1 and (mode != "repeat" or downs(r) > before)      # F still sustains it (a repeat keeps tapping)
    r.release("F")
    r.advance(1000)
    assert r.fingers() == 0 and r.kinds()[-1] == U


def test_pressing_a_trigger_again_while_it_is_down_never_fires_twice():
    r = Rig([multi("E")])
    for name in ("E", "E", "F", "F", "E"):
        r.press(name)
    assert r.kinds() == [D]
    r.release("E")
    assert r.kinds() == [D]                                        # F is still down: the action stays down
    r.release("F")
    assert r.kinds() == [D, U]


def test_key_releases_of_unrelated_keys_change_nothing():
    r = Rig([multi("E")])
    r.press("E")
    r.release("Z")
    r.release("F")                                                  # F was never down
    assert r.kinds() == [D] and r.fingers() == 1


# ===================================================================== modifier combinations as triggers
def test_a_modifier_combination_is_a_trigger_and_releasing_its_modifier_ends_that_trigger():
    r = Rig([multi("E", triggers=["Shift+G"])])
    r.press("Shift")
    r.press("G")
    assert r.kinds() == [D] and r.fingers() == 1
    r.release("Shift")                                              # the combination needs Shift: it no longer sustains the run
    assert r.kinds() == [D, U] and r.fingers() == 0


def test_releasing_the_base_key_of_a_combination_ends_that_trigger_too():
    r = Rig([multi("E", triggers=["Shift+G"])])
    r.press("Shift")
    r.press("G")
    r.release("G")
    assert r.kinds() == [D, U]
    r.release("Shift")
    assert r.kinds() == [D, U]


def test_a_released_modifier_ends_only_the_combination_while_another_trigger_still_sustains_the_run():
    r = Rig([multi("E", triggers=["Shift+G"])])
    r.press("E")
    r.press("Shift")
    r.press("G")                                                    # joins as the combination
    assert r.kinds() == [D]
    r.release("Shift")
    assert r.kinds() == [D] and r.fingers() == 1                    # E keeps it down
    r.release("E")
    assert r.kinds() == [D, U]


def test_a_trigger_started_by_a_combination_survives_while_the_plain_extra_trigger_is_held():
    r = Rig([multi("E", triggers=["Shift+G", "F"])])
    r.press("Shift")
    r.press("G")
    r.press("F")
    r.release("Shift")
    assert r.kinds() == [D]
    r.release("F")
    assert r.kinds() == [D, U]


def test_the_exact_modifier_set_is_required_and_a_missing_combination_falls_back_to_the_plain_key():
    r = Rig([multi("E", x=0.2, mode="tap", triggers=["Ctrl+Shift+G"]), bound("G", "tap", x=0.8)])
    r.press("Ctrl")
    r.press("G", 100)                                               # only Ctrl: no such combination: the plain G binding
    assert [t[2] for t in r.touches()] == [1920, 1920]
    r.release("G")
    r.press("Shift")
    r.press("G", 100)                                               # Ctrl+Shift+G: the extra trigger of E
    assert r.touches()[-2][2] == 480


def test_a_modifier_combination_still_beats_the_plain_key_when_the_plain_key_is_a_trigger_of_another_binding():
    r = Rig([multi("E", x=0.2, y=0.2, mode="tap"), Binding("Shift+E", 0.8, 0.8, "tap")])
    r.press("Shift")
    r.press("E", 100)
    assert len(at(r, 0.8, 0.8)) == 2 and at(r, 0.2, 0.2) == []     # the combination fired, the plain one did not
    r.release("E")
    r.release("Shift")
    r.press("F", 100)
    assert len(at(r, 0.2, 0.2)) == 2                               # and the other trigger reaches the first binding


def test_a_modifier_that_is_itself_a_trigger_joins_like_any_key():
    r = Rig([multi("E", triggers=["Shift"])])
    r.press("Shift")
    r.press("E")
    assert r.kinds() == [D]
    r.release("Shift")
    assert r.kinds() == [D]
    r.release("E")
    assert r.kinds() == [D, U]


# ===================================================================== repeat
def test_repeat_starts_with_the_first_trigger_and_goes_on_until_the_last_is_released():
    r = Rig([multi("E", mode="repeat", rate_hz=10)])
    r.press("E", 250)
    r.press("F", 250)
    taps_before = downs(r)
    r.release("E", 500)                                             # F alone keeps the repeat going
    assert downs(r) > taps_before + 3
    r.release("F")
    sent = len(r.touches())
    r.advance(2000)
    assert len(r.touches()) == sent and r.fingers() == 0


def test_the_repeat_interval_is_the_same_whichever_triggers_are_down():
    single, joined = Rig([bound("E", "repeat", rate_hz=10)]), Rig([multi("E", x=0.5, y=0.5, mode="repeat", rate_hz=10)])
    single.press("E", 1000)
    joined.press("E", 300)
    joined.press("F", 700)
    assert [t[:4] for t in single.touches()] == [t[:4] for t in joined.touches()]
    assert 9 <= downs(joined) <= 11


def test_a_repeat_never_leaves_a_finger_down_between_taps_with_two_triggers():
    r = Rig([multi("E", mode="repeat", rate_hz=30)])
    r.press("E")
    r.press("F", 200)
    kinds = r.kinds()
    assert all(a != b for a, b in zip(kinds, kinds[1:])) and kinds[0] == D


# ===================================================================== one-shots
ONE_SHOT_DOWNS = {"tap": 1, "double": 2, "swipe": 1, "pinch-out": 2, "pinch-in": 2, "sequence": 2}


@pytest.mark.parametrize("mode", list(ONE_SHOT_DOWNS))
def test_another_trigger_pressed_while_a_one_shot_is_running_does_not_start_a_competing_copy(mode):
    r = Rig([multi("E", mode=mode)])
    r.press("F", 10)
    r.press("E", 10)
    r.release("F", 10)
    r.release("E", 3000)
    assert downs(r) == ONE_SHOT_DOWNS[mode] and r.kinds().count(U) == downs(r) and r.fingers() == 0


@pytest.mark.parametrize("mode", list(ONE_SHOT_DOWNS))
def test_a_genuine_press_after_the_one_shot_has_finished_starts_it_again(mode):
    r = Rig([multi("E", mode=mode)])
    r.press("E", 3000)
    r.release("E")
    assert downs(r) == ONE_SHOT_DOWNS[mode]
    r.press("F", 3000)
    assert downs(r) == 2 * ONE_SHOT_DOWNS[mode] and r.fingers() == 0


def test_a_one_shot_tap_is_not_repeated_by_a_second_trigger_pressed_while_it_is_running():
    r = Rig([multi("E", mode="tap")])
    r.press("F", 10)                                               # the extra trigger starts the tap ...
    assert r.kinds() == [D]
    r.press("E", 10)                                               # ... and the first one joins it inside its 40 ms
    r.advance(200)
    assert r.kinds() == [D, U]


def test_a_new_activation_is_possible_the_moment_the_previous_run_has_finished():
    r = Rig([multi("E", mode="tap")])
    r.press("E", 41)                                               # the tap is over after 40 ms
    assert r.kinds() == [D, U]
    r.press("F")                                                   # no fixed debounce window after completion
    assert r.kinds() == [D, U, D]
    r.advance(100)
    assert r.kinds() == [D, U, D, U]


def test_there_is_no_debounce_at_the_exact_instant_the_run_ends():
    r = Rig([multi("E", mode="tap")])
    r.press("E", 40)                                               # the finishing timer fires at 40 ms
    assert r.kinds() == [D, U]
    r.press("F")
    assert r.kinds() == [D, U, D] and r.touches()[-1][0] == D


def test_a_run_started_by_a_wheel_notch_absorbs_a_key_of_the_same_binding_until_it_ends():
    r = Rig([multi("E", mode="hold", triggers=["WheelUp"])])
    r.engine.wheel_binding(+1)
    r.press("E", 10)                                               # inside the wheel's 40 ms tap
    r.advance(200)
    assert r.kinds() == [D, U] and r.fingers() == 0
    r.release("E")


# ===================================================================== toggle
def test_a_toggle_with_two_triggers_is_flipped_by_either_and_ignores_releases():
    r = Rig([multi("E", mode="toggle")])
    r.press("E")
    r.release("E")
    assert r.kinds() == [D]
    r.press("F")
    r.release("F")
    assert r.kinds() == [D, U] and r.fingers() == 0


def test_a_toggle_pressed_through_a_second_trigger_while_the_first_is_down_joins_instead_of_switching_off():
    r = Rig([multi("E", mode="toggle")])
    r.press("E")
    r.press("F")
    assert r.kinds() == [D] and r.fingers() == 1                    # joined: still on
    r.release("E")
    r.release("F")
    assert r.kinds() == [D] and r.fingers() == 1                    # on after every key is up
    r.press("E")                                                    # a fresh press now switches it off
    assert r.kinds() == [D, U]
    r.release("E")
    r.press("F")                                                    # and the next fresh press on again
    assert r.kinds() == [D, U, D]


@pytest.mark.parametrize("release_order", [("E", "F"), ("F", "E")])
def test_a_toggle_stays_on_whatever_order_the_triggers_are_released_in(release_order):
    r = Rig([multi("E", mode="toggle")])
    r.press("E")
    r.press("F")
    for name in release_order:
        r.release(name)
    assert r.kinds() == [D] and r.fingers() == 1
    r.press("F")
    r.release("F")
    assert r.kinds() == [D, U] and r.fingers() == 0


def test_a_toggle_switched_off_by_a_trigger_that_is_still_down_is_switched_on_by_the_next_fresh_press():
    r = Rig([multi("E", mode="toggle", triggers=["F", "G"])])
    r.press("E")
    r.release("E")
    r.press("F")                                                    # off (F stays down)
    assert r.kinds() == [D, U]
    r.press("G")                                                    # a different, fresh trigger: on again
    assert r.kinds() == [D, U, D]
    r.release("F")
    r.release("G")
    assert r.fingers() == 1


def test_a_toggle_started_from_a_combination_counts_its_modifier_as_part_of_the_trigger():
    r = Rig([multi("E", mode="toggle", triggers=["Shift+G"])])
    r.press("Shift")
    r.press("G")
    r.press("E")                                                    # E joins while the combination is down
    assert r.kinds() == [D]
    r.release("Shift")                                              # the combination stops sustaining; E still does
    r.press("F")                                                    # F is not a trigger: nothing
    r.release("E")
    r.press("E")                                                    # everything was let go: this switches it off
    assert r.kinds() == [D, U]


def test_a_repeated_key_down_of_the_toggle_trigger_that_is_down_still_flips_it_as_it_always_did():
    # Pinned legacy behaviour (see the module docstring): unreachable through the router, which drops OS key repeat.
    r = Rig([bound("E", "toggle")])
    r.press("E")
    r.press("E")
    assert r.kinds() == [D, U]
    multi_r = Rig([multi("E", mode="toggle")])
    multi_r.press("E")
    multi_r.press("E")
    assert multi_r.kinds() == [D, U]


def test_the_toggle_of_a_single_trigger_binding_is_exactly_what_it_was():
    r = Rig([bound("X", "toggle")])
    r.press("X")
    r.release("X", 500)
    assert r.kinds() == [D]
    r.press("X")
    r.release("X")
    r.press("X")
    assert r.kinds() == [D, U, D]


def test_a_toggle_with_a_wheel_trigger_flips_on_every_notch_when_no_key_is_down():
    r = Rig([multi("E", mode="toggle", triggers=["WheelUp"])])
    r.engine.wheel_binding(+1)
    r.engine.wheel_binding(+1)
    r.engine.wheel_binding(+1)
    assert r.kinds() == [D, U, D]


def test_a_wheel_notch_while_a_key_holds_the_toggle_joins_it_and_changes_nothing():
    r = Rig([multi("E", mode="toggle", triggers=["WheelUp"])])
    r.press("E")
    r.engine.wheel_binding(+1)
    assert r.kinds() == [D]
    r.release("E")
    r.engine.wheel_binding(+1)                                      # nothing holds it now: the notch switches it off
    assert r.kinds() == [D, U]


# ===================================================================== wheel triggers stay impulses
def test_a_wheel_extra_trigger_runs_the_one_shot_version_and_nothing_waits_for_a_release():
    r = Rig([multi("E", mode="hold", triggers=["WheelUp"])])
    assert r.engine.has_wheel_binding() is True
    assert r.engine.wheel_binding(+1) is True
    r.advance(40)
    assert r.kinds() == [D, U] and r.fingers() == 0
    assert r.engine.wheel_binding(+1) is True                       # and again once it has finished
    r.advance(100)
    assert r.kinds() == [D, U, D, U]


@pytest.mark.parametrize("mode, expected", [("hold", [D, U]), ("repeat", [D, U]), ("drag", [D, M, U])])
def test_wheel_equivalents_are_unchanged_for_a_multi_trigger_binding(mode, expected):
    r = Rig([multi("E", mode=mode, triggers=["WheelUp"], x2=0.9, y2=0.5, duration_ms=40) if mode == "drag"
             else multi("E", mode=mode, triggers=["WheelUp"])])
    r.engine.wheel_binding(+1)
    r.advance(2000)
    kinds = r.kinds()
    assert kinds[0] == D and kinds[-1] == U and r.fingers() == 0 and (mode != "hold" or kinds == expected)


def test_a_wheel_notch_does_not_join_a_held_run_and_is_dropped_like_it_always_was():
    r = Rig([multi("E", mode="hold", triggers=["WheelUp"])])
    r.press("E")
    r.engine.wheel_binding(+1)
    r.advance(200)
    assert r.kinds() == [D] and r.fingers() == 1
    r.release("E")
    assert r.kinds() == [D, U]                                      # E alone decided: the wheel was never a holder


def test_a_wheel_combination_trigger_needs_its_modifier_at_the_time_of_the_notch_only():
    r = Rig([multi("E", mode="tap", triggers=["Ctrl+WheelUp"])])
    assert r.engine.wheel_binding(+1) is False                      # no Ctrl: no trigger
    r.press("Ctrl")
    assert r.engine.wheel_binding(+1) is True
    r.advance(100)
    assert r.kinds() == [D, U]


# ===================================================================== cleanup
@pytest.mark.parametrize("how", ["release_all", "screen_changed", "mapping_off"])
def test_every_cleanup_path_lifts_a_run_held_by_two_triggers_once_and_forgets_both_triggers(how):
    r = Rig([multi("E")])
    r.press("E")
    r.press("F")
    {"release_all": r.engine.release_all, "screen_changed": r.engine.screen_changed,
     "mapping_off": lambda: r.engine.set_mapping_active(False)}[how]()
    assert r.kinds() == [D, U] and r.fingers() == 0
    r.engine.set_mapping_active(True)
    r.release("E")
    r.release("F")
    assert r.kinds() == [D, U]                                     # the old key-ups change nothing
    r.press("F")
    assert r.kinds() == [D, U, D]                                  # a fresh press starts a fresh run


def test_release_all_cancels_a_repeat_held_by_two_triggers_and_its_timers():
    r = Rig([multi("E", mode="repeat", rate_hz=10)])
    r.press("E", 150)
    r.press("F", 150)
    r.engine.release_all()
    sent = len(r.touches())
    r.advance(3000)
    assert len(r.touches()) == sent and r.fingers() == 0


def test_focus_loss_with_a_toggle_on_and_a_trigger_held_leaves_nothing_down_and_starts_fresh():
    r = Rig([multi("E", mode="toggle")])
    r.press("E")
    r.press("F")
    r.engine.release_all()
    assert r.fingers() == 0 and r.kinds() == [D, U]
    r.press("F")
    assert r.kinds() == [D, U, D]                                  # on again: no stale "F is down" state


def test_a_lost_connection_during_cleanup_leaves_no_ghost_triggers_and_a_new_press_works():
    r = Rig([multi("E")])
    live = r.control
    r.press("E")
    r.press("F")
    r.control = None
    r.engine.release_all()
    assert r.fingers() == 0
    r.control = live
    r.press("F")
    assert [t[:2] for t in r.touches()] == [(D, 0), (D, 0)]


def test_a_rotation_mid_run_lifts_at_the_new_size_and_the_triggers_start_over():
    r = Rig([multi("E", also=[(0.2, 0.2)])])
    r.press("E")
    r.press("F")
    r.size = (1080, 2400)
    r.engine.screen_changed()
    ups = [t for t in r.touches() if t[0] == U]
    assert len(ups) == 2 and all(t[4:] == (1080, 2400) for t in ups) and r.fingers() == 0
    r.release("E")
    r.release("F")
    r.press("F")
    assert r.fingers() == 2 and r.touches()[-1][4:] == (1080, 2400)


def test_no_timer_of_a_cancelled_multi_trigger_run_can_touch_the_screen_later():
    for mode in ("repeat", "swipe", "drag", "pinch-out", "sequence", "double"):
        r = Rig([multi("E", mode=mode)])
        r.press("E", 20)
        r.press("F", 20)
        r.engine.release_all()
        sent = len(r.touches())
        r.advance(5000)
        assert len(r.touches()) == sent and r.fingers() == 0, mode


# ===================================================================== pointer ownership
def owner(binding, *more):
    return ("bind", binding.id, PRIMARY, *more)


def test_the_fingers_belong_to_the_binding_and_never_to_a_physical_trigger():
    r = Rig([multi("E", triggers=["F", "MMB"], also=[(0.1, 0.1)])])
    b = r.profile.find("E")
    r.press("F")
    r.press("E")
    inj = r.engine.injector
    assert inj.is_down(owner(b)) and inj.is_down(owner(b, "also", 0)) and inj.active_count == 2
    for trigger in ("E", "F", "MMB"):
        assert not inj.is_down(("input", trigger)) and not inj.is_down(("bind", trigger, PRIMARY))


def test_two_triggers_never_make_two_fingers_for_one_action():
    r = Rig([multi("E", triggers=["F", "G", "MMB"])])
    for name in ("E", "F", "G"):
        r.press(name)
    mouse(r, "MMB", True)
    assert r.fingers() == 1 and r.kinds() == [D]


def test_cancelling_one_binding_never_lifts_the_fingers_of_another_binding_the_stick_look_or_direct_touch():
    r = Rig([multi("E", triggers=["F"]), bound("G", "hold", x=0.1, y=0.9), bound("LMB", "hold", x=0.8, y=0.7)],
            stick=Stick(0.15, 0.7, ramp_ms=0), look=__import__("app.mapping.profile", fromlist=["Look"]).Look(0.5, 0.2, 0.9, 0.8))
    e, g = r.profile.find("E"), r.profile.find("G")
    r.press("G")
    r.press("W")
    r.engine.set_look_captured(True)
    r.engine.look_delta(5, 0)
    r.advance(10)
    r.press("F")
    r.press("E")
    inj = r.engine.injector
    assert inj.active_count == 4
    r.release("F")
    assert inj.is_down(owner(e)) and inj.active_count == 4         # E still sustains it
    r.release("E")
    assert not inj.is_down(owner(e))
    assert inj.is_down(owner(g)) and inj.is_down(("stick", 0)) and inj.is_down(("look", 0)) and inj.active_count == 3


def test_a_cancellation_lifts_every_binding_exactly_once_whatever_triggers_started_them():
    r = Rig([multi("E"), multi("R", triggers=["T"], y=0.2), bound("G", "hold", x=0.1, y=0.9)])
    for name in ("E", "F", "T", "G"):
        r.press(name)
    assert r.fingers() == 3
    r.engine.release_all()
    ups = sorted(t[1] for t in r.touches() if t[0] == U)
    downs_ = sorted(t[1] for t in r.touches() if t[0] == D)
    assert ups == downs_ == [0, 1, 2]


# ===================================================================== overlay highlighting
def test_the_binding_is_highlighted_by_its_first_input_whichever_trigger_started_it():
    r = Rig([multi("E", triggers=["F", "Shift+G", "MMB"])])
    r.press("F")
    assert r.pressed_log[-1] == frozenset({"E"})                    # the marker the overlay knows (binding.input), not "F"
    r.press("E")
    r.release("F")
    assert r.pressed_log[-1] == frozenset({"E"})                    # still lit: E sustains it
    r.release("E")
    assert r.pressed_log[-1] == frozenset()


def test_a_combination_or_mouse_trigger_also_lights_the_first_inputs_marker():
    r = Rig([multi("E", triggers=["Shift+G", "MMB"])])
    r.press("Shift")
    r.press("G")
    assert r.pressed_log[-1] == frozenset({"E"})
    r.release("G")
    mouse(r, "MMB", True)
    assert "E" in r.pressed_log[-1]
    mouse(r, "MMB", False)
    assert r.pressed_log[-1] == frozenset()


def test_a_toggle_stays_highlighted_until_it_is_switched_off_and_a_wheel_notch_lights_the_marker_briefly():
    r = Rig([multi("E", mode="toggle", triggers=["F"]), multi("R", mode="tap", triggers=["WheelUp"])])
    r.press("F")
    r.release("F")
    assert r.pressed_log[-1] == frozenset({"E"})
    r.press("E")
    assert r.pressed_log[-1] == frozenset()
    r.release("E")
    r.engine.wheel_binding(+1)
    assert r.pressed_log[-1] == frozenset({"R"})
    r.advance(100)
    assert r.pressed_log[-1] == frozenset()


def test_the_highlight_name_is_fixed_when_the_run_starts_even_if_the_binding_is_edited_later():
    r = Rig([multi("E")])
    b = r.profile.find("E")
    r.press("F")
    b.input = "Q"                                                  # an edit while running (the editor refreshes the engine first)
    r.release("F")
    assert r.pressed_log[-1] == frozenset()                         # the lit name is cleared, none is left behind


def test_the_overlay_rule_matches_the_engine_names():
    # app/ui/overlay.py lights a marker when `binding.input in pressed`; the engine reports exactly binding.input.
    r = Rig([multi("E", triggers=["F"]), bound("R", "tap")])
    r.press("F")
    lit = [b for b in r.profile.bindings if b.input in r.pressed_log[-1]]
    assert [b.input for b in lit] == ["E"]


# ===================================================================== combos and the stick
def stick_rig(bindings):
    return Rig(bindings, stick=Stick(0.15, 0.7, radius=0.1, ramp_ms=0, sprint_key="Alt"))


def test_stick_keys_still_belong_to_the_stick_and_a_combination_trigger_on_one_still_works():
    r = stick_rig([multi("R", x=0.8, y=0.3, triggers=["Shift+W", "Z"])])
    r.press("W")
    assert r.kinds() == [D, M] and r.fingers() == 1                 # the stick only
    r.press("Shift")
    r.release("W")
    r.press("W")
    assert r.fingers() == 2                                         # the stick again and the binding through Shift+W
    assert at(r, 0.8, 0.3)
    r.release("Shift")
    assert r.fingers() == 1                                         # the combination ended, the stick still walks
    r.release("W")
    assert r.fingers() == 0


def test_an_extra_trigger_that_is_a_stick_key_never_starts_the_binding():
    r = stick_rig([multi("R", triggers=["W"])])
    r.press("W")
    r.press("R")
    r.release("W")
    r.release("R")
    assert r.fingers() == 0
    assert len([t for t in at(r, 0.62, 0.55) if t[0] == D]) == 1     # only R started it, once


def test_extra_triggers_and_the_stick_modifier_keys_do_not_disturb_each_other():
    r = stick_rig([multi("R", x=0.8, y=0.3, triggers=["Alt+Z"])])
    r.press("Alt")
    r.press("W")
    r.press("Z")
    assert r.fingers() == 2
    r.release("Z")
    assert r.fingers() == 1
    r.release("W")
    r.release("Alt")
    assert r.fingers() == 0


# ===================================================================== what is NOT run
def test_additional_actions_run_with_several_triggers_and_belong_to_the_same_single_run():
    # CHANGED in step 6 (additional actions used to be inert); the full multi-action tests are in test_multi_action.py.
    r = Rig([multi("E", extra_actions=[Action("tap", 0.1, 0.1), Action("hold", 0.9, 0.9, delay_ms=50)])])
    r.press("E", 20)
    r.press("F", 200)                                               # joins the run: no second copy of any action
    assert r.kinds().count(D) == 3 and r.fingers() == 2
    r.release("E")
    assert r.fingers() == 2                                         # F still sustains the holds
    r.release("F")
    assert r.fingers() == 0 and len(at(r, 0.1, 0.1)) == 2 and len(at(r, 0.9, 0.9)) == 2


def test_two_bindings_sharing_a_first_input_still_run_only_the_last_one():
    p = Profile("p")
    p.bindings = [Binding("R", 0.1, 0.1, "hold"), Binding("R", 0.9, 0.9, "hold")]
    r = Rig()
    r.engine.set_profile(p)
    r.press("R")
    assert r.fingers() == 1 and r.touches()[0][2:4] == (2160, 972)


def test_a_first_input_beats_another_bindings_additional_trigger_on_the_same_key():
    p = Profile("p", bindings=[Binding("E", 0.1, 0.1, "hold"), Binding("R", 0.9, 0.9, "hold", extra_triggers=["E"])])
    r = Rig()
    r.engine.set_profile(p)
    r.press("E")
    assert r.fingers() == 1 and r.touches()[0][2:4] == (240, 108)    # E's own binding, not R's additional trigger
    r.release("E")
    assert r.fingers() == 0


def test_among_additional_triggers_the_later_binding_in_the_list_wins():
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1, "hold", extra_triggers=["F"]), Binding("B", 0.9, 0.9, "hold", extra_triggers=["F"])])
    r = Rig()
    r.engine.set_profile(p)
    r.press("F")
    assert r.fingers() == 1 and r.touches()[0][2:4] == (2160, 972)
    r.release("F")
    r.press("A")                                                    # the first one is still reachable by its own first input
    assert r.touches()[-1][2:4] == (240, 108)


def test_trigger_owners_is_the_rule_the_engine_uses_and_one_binding_per_trigger_always_wins():
    a = Binding("E", 0.1, 0.1, extra_triggers=["F", "Shift+G"], id="a")
    b = Binding("F", 0.2, 0.2, extra_triggers=["H"], id="b")
    c = Binding("R", 0.3, 0.3, extra_triggers=["H", "Ctrl+Shift+G", "Shift+G"], id="c")
    owners = trigger_owners([a, b, c])
    assert {t: x.id for t, x in owners.items()} == {"E": "a", "F": "b", "R": "c", "H": "c", "Shift+G": "c", "Ctrl+Shift+G": "c"}
    assert {t: x.id for t, x in trigger_owners([c, b, a]).items()}["H"] == "b"        # the later binding wins among additional triggers
    for trigger, binding in owners.items():                                          # the engine agrees for every one of them
        r = Rig()
        r.engine.set_profile(Profile("p", bindings=[a, b, c]))
        r.engine.set_mapping_active(True)
        mods, base = trigger.rsplit("+", 1) if "+" in trigger else ("", trigger)
        for m in filter(None, mods.split("+")):
            r.press(m)
        r.press(base, 10)
        assert r.touches() and r.touches()[0][2:4] == (int(binding.x * W + 1e-6), int(binding.y * H + 1e-6)), trigger


# ===================================================================== version-1 and version-2 profiles
def test_a_version_1_profile_is_run_exactly_as_before():
    p = Profile.from_dict({"name": "v1", "bindings": [
        {"input": "R", "x": 0.7, "y": 0.9, "mode": "tap"}, {"input": "G", "x": 0.2, "y": 0.9, "mode": "hold"}]})
    r = Rig()
    r.engine.set_profile(p)
    r.press("R", 100)
    r.press("G", 10)
    r.release("G")
    assert r.kinds() == [D, U, D, U] and r.engine.mapping_active


def test_a_version_2_profile_loaded_from_a_file_runs_all_its_triggers():
    p = Profile.from_dict({"version": 2, "name": "v2", "bindings": [
        {"type": "binding", "id": "interact", "triggers": ["E", "F", "MMB"], "on_failure": "continue",
         "actions": [{"mode": "hold", "x": 0.3, "y": 0.4}]}]})
    r = Rig()
    r.engine.set_profile(p)
    r.press("F")
    assert mouse(r, "MMB", True) is True
    r.release("F")
    assert r.fingers() == 1
    mouse(r, "MMB", False)
    assert r.kinds() == [D, U] and r.touches()[0][2:4] == (720, 432)


# ===================================================================== gaps found by the mutation check
def test_a_mapped_mouse_button_release_still_finds_its_run_after_the_modifiers_changed_single_trigger():
    # Legacy behaviour that no earlier test pinned: the release is routed by what the run still needs down.
    r = Rig([Binding("Ctrl+LMB", 0.5, 0.5, "hold")])
    r.press("Ctrl")
    assert mouse(r, "LMB", True) is True and r.fingers() == 1
    r.press("Shift")                                                # Ctrl+Shift+LMB matches nothing, and there is no plain LMB binding
    assert mouse(r, "LMB", False) is True
    assert r.kinds() == [D, U] and r.fingers() == 0


def test_a_mapped_mouse_button_release_still_finds_its_run_after_the_modifiers_changed_when_it_is_an_extra_trigger():
    r = Rig([multi("E", triggers=["Ctrl+LMB"])])
    r.press("Ctrl")
    assert mouse(r, "LMB", True) is True
    r.press("Shift")
    assert mouse(r, "LMB", False) is True
    assert r.kinds() == [D, U] and r.fingers() == 0


def test_a_wheel_notch_that_joins_a_held_toggle_leaves_nothing_behind_that_blocks_the_next_fresh_press():
    r = Rig([multi("E", mode="toggle", triggers=["WheelUp"])])
    r.press("E")
    r.engine.wheel_binding(+1)                                      # joins while E holds the toggle
    r.release("E")
    r.press("E")                                                    # a fresh press must switch it off: the wheel holds nothing
    assert r.kinds() == [D, U] and r.fingers() == 0


# ===================================================================== scope behaviour with several triggers
def test_a_hold_scope_with_two_triggers_stays_on_until_the_last_trigger_is_released():
    r = Rig([multi("RMB", mode="hold", triggers=["Q"], scope=True)])
    r.engine.mouse_button("RMB", True, None)
    assert r.engine.scope_active is True
    r.press("Q")
    r.engine.mouse_button("RMB", False, None)
    assert r.engine.scope_active is True                            # Q still holds it
    r.release("Q")
    assert r.engine.scope_active is False and r.fingers() == 0


def test_a_tap_scope_flips_once_per_run_however_many_triggers_press_it():
    r = Rig([multi("RMB", mode="tap", triggers=["Q"], scope=True)])
    r.engine.mouse_button("RMB", True, None)
    r.press("Q", 10)                                                # joins the running tap: no second flip
    r.engine.mouse_button("RMB", False, None)
    r.release("Q", 200)
    assert r.engine.scope_active is True
    r.press("Q", 200)                                               # a later press is a new run: flips back
    assert r.engine.scope_active is False


def test_a_toggle_scope_follows_the_toggle_state_across_triggers():
    r = Rig([multi("RMB", mode="toggle", triggers=["Q"], scope=True)])
    r.engine.mouse_button("RMB", True, None)
    r.press("Q")                                                    # joins: still on
    assert r.engine.scope_active is True
    r.engine.mouse_button("RMB", False, None)
    r.release("Q")
    assert r.engine.scope_active is True
    r.press("Q")                                                    # fresh press: off
    assert r.engine.scope_active is False
