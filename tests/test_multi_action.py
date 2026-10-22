"""Several actions inside ONE binding (Phase 2, step 6).

One trigger (or any of a binding's triggers) starts a RUN that holds every action of the binding. The first action starts at
once; each additional action starts after its own `delay_ms`, always counted from the moment the binding was activated, never from
the end of another action. Every action keeps its own lifecycle and its own fingers (`("bind", id, action index, ...)`):

  hold / repeat / drag        sustained by the trigger: they stop when the last trigger that sustains them is let go, and a pending
                              one is CANCELLED if that happens before its start time (it never starts a hold nobody is holding)
  tap / double / swipe / pinch / sequence / toggle
                              not sustained: once scheduled they run to completion whatever the key does (a toggle stays on)
  a fresh activation while the run is still alive (nothing held, e.g. a toggle is on) applies each action's own rule: a toggle that is
  on switches off, an action that is finished starts again, an action that is still pending or running is left alone
  on_failure = "continue"     an action that cannot start is recorded; the others carry on
  on_failure = "abort"        it cancels the whole run: pending actions never start, every finger the run has down is lifted
                              (a tap that was already delivered cannot be taken back); other bindings are never touched
  scope                       `scope_active` is derived from every scope source, so one release cannot hide another that is still on
Not part of this step: several BINDINGS sharing a trigger all running (see test_shared_runtime.py)."""
import pytest

from app.input.engine import PRIMARY
from app.mapping.profile import Action, Binding, Look, Profile, Stick
from app.stream import protocol
from test_characterization_engine import SPECS, Rig, bound

D, U, M = protocol.TOUCH_DOWN, protocol.TOUCH_UP, protocol.TOUCH_MOVE
W, H = 2400, 1080


def multi(key, x=0.8, y=0.7, mode="hold", extras=(), triggers=(), **kw):
    return Binding(key, x, y, mode, extra_triggers=list(triggers), extra_actions=list(extras), **{**SPECS.get(mode, {}), **kw})


def act(mode, x, y, delay_ms=0, **kw):
    return Action(mode, x, y, delay_ms=delay_ms, **{**SPECS.get(mode, {}), **kw})


def px(x, y):
    return (int(x * W + 1e-6), int(y * H + 1e-6))


def at(r, x, y):
    return [t for t in r.touches() if (t[2], t[3]) == px(x, y)]


def ev(r):
    """[(kind, pointer id)] of everything sent so far."""
    return [(t[0], t[1]) for t in r.touches()]


def owner(binding, index, *more):
    return ("bind", binding.id, index, *more)


def mouse(r, name, pressed):
    return r.engine.mouse_button(name, pressed, (0.5, 0.5))


def filled(extra_bindings, taken):
    """A rig whose first `taken` pointer ids are held by plain holds on A, B, C, ..."""
    keys = "ABCDEFGHIJ"[:taken]
    r = Rig([Binding(k, 0.05 + 0.09 * i, 0.5, "hold") for i, k in enumerate(keys)] + extra_bindings)
    for k in keys:
        r.press(k)
    return r


# ===================================================================== one key, several actions, started in order
def test_one_key_starts_a_hold_and_a_tap_together_and_they_end_independently():
    r = Rig([multi("L", 0.8, 0.7, "hold", [act("tap", 0.5, 0.2)])])
    r.press("L")
    assert ev(r) == [(D, 0), (D, 1)]                               # both fingers down in the same instant, in order
    r.advance(41)
    assert ev(r) == [(D, 0), (D, 1), (U, 1)] and r.fingers() == 1   # the tap ended alone
    r.release("L")
    assert r.touches()[-1][:2] == (U, 0) and r.fingers() == 0


def test_zero_delay_actions_start_in_list_order_in_the_same_call_without_any_timer():
    r = Rig([multi("L", 0.1, 0.1, "hold", [act("hold", 0.2, 0.2), act("hold", 0.3, 0.3), act("hold", 0.4, 0.4)])])
    r.press("L")
    assert ev(r) == [(D, 0), (D, 1), (D, 2), (D, 3)]
    assert [t[2:4] for t in r.touches()] == [px(0.1, 0.1), px(0.2, 0.2), px(0.3, 0.3), px(0.4, 0.4)]
    assert r.timers == []                                          # nothing was scheduled: no timer hop for a delay of 0
    r.release("L")
    assert [t[:2] for t in r.touches()[4:]] == [(U, 0), (U, 1), (U, 2), (U, 3)]


def test_several_actions_overlap_in_time_with_independent_pointer_ids():
    r = Rig([multi("L", 0.1, 0.5, "hold", [act("swipe", 0.3, 0.5, x2=0.9, y2=0.5, duration_ms=160), act("tap", 0.5, 0.2)])])
    r.press("L", 20)
    assert ev(r)[:3] == [(D, 0), (D, 1), (D, 2)]                   # hold, swipe and tap all down at once, in list order
    assert {t[1] for t in r.touches() if t[0] == M} == {1}         # the swipe moves its own finger, nobody else's
    r.advance(30)
    assert (U, 2) in ev(r) and r.fingers() == 2                    # the tap has come and gone while hold and swipe go on
    r.advance(300)
    assert (U, 1) in ev(r) and r.fingers() == 1                    # the swipe ended by itself; the hold is still down
    assert [t[0] for t in r.touches() if t[1] == 0] == [D]
    r.release("L")
    assert r.fingers() == 0


def test_every_action_has_its_own_stable_owner_including_its_extra_spots_and_pinch_fingers():
    b = multi("L", 0.1, 0.5, "hold", [act("hold", 0.3, 0.3, also=[(0.31, 0.31)]), act("pinch-out", 0.6, 0.6, span=0.1)], also=[(0.11, 0.5)])
    r = Rig([b])
    b = r.profile.find("L")
    r.press("L", 10)
    inj = r.engine.injector
    expected = [owner(b, 0), owner(b, 0, "also", 0), owner(b, 1), owner(b, 1, "also", 0), owner(b, 2, "a"), owner(b, 2, "b")]
    assert all(inj.is_down(o) for o in expected) and inj.active_count == 6
    assert not inj.is_down(owner(b, 2))                            # a pinch has two fingers and no plain owner
    assert PRIMARY == 0 and owner(b, 0)[2] == 0 and owner(b, 1)[2] == 1


def test_the_first_action_alone_behaves_exactly_as_the_same_binding_without_extra_actions():
    plain, rich = Rig([bound("L", "repeat", x=0.8, y=0.7)]), Rig([multi("L", 0.8, 0.7, "repeat", [act("tap", 0.1, 0.1, delay_ms=5000)])])
    for rig in (plain, rich):
        rig.press("L", 600)
        rig.release("L", 200)
    assert [t for t in rich.touches() if (t[2], t[3]) == px(0.8, 0.7)] == plain.touches()


def test_an_extra_actions_own_settings_are_used_not_the_primarys():
    r = Rig([multi("L", 0.2, 0.2, "hold", [act("swipe", 0.1, 0.5, x2=0.7, y2=0.9, duration_ms=80), act("repeat", 0.6, 0.6, rate_hz=20)])])
    r.press("L", 300)
    moves = [t for t in r.touches() if t[0] == M and t[1] == 1]
    assert moves and (moves[-1][2], moves[-1][3]) == px(0.7, 0.9)                 # the swipe's own end point
    repeats = [t for t in at(r, 0.6, 0.6) if t[0] == D]
    assert 5 <= len(repeats) <= 7                                                   # ~20 taps per second for 0.3 s


def test_hold_repeat_toggle_and_tap_in_one_binding_each_follow_their_own_rules_on_release():
    r = Rig([multi("L", 0.1, 0.9, "hold", [act("repeat", 0.3, 0.9, rate_hz=10), act("toggle", 0.5, 0.9), act("tap", 0.7, 0.9)])])
    r.press("L", 20)
    r.release("L", 2000)
    assert r.fingers() == 1                                          # only the toggle's finger is still down
    assert len(at(r, 0.5, 0.9)) == 1 and at(r, 0.5, 0.9)[0][0] == D  # ... and it was never lifted
    assert len(at(r, 0.1, 0.9)) == 2 and len(at(r, 0.7, 0.9)) == 2   # hold: down/up; tap: down/up
    sent = len(r.touches())
    r.advance(2000)
    assert len(r.touches()) == sent                                  # the repeat really stopped


def test_a_hold_plus_a_one_shot_and_a_hold_plus_a_toggle_each_follow_their_own_rules():
    r = Rig([multi("L", 0.1, 0.1, "hold", [act("tap", 0.2, 0.2)])])
    r.press("L", 100)
    assert r.fingers() == 1 and ev(r) == [(D, 0), (D, 1), (U, 1)]
    r.release("L")
    assert r.fingers() == 0
    r = Rig([multi("L", 0.1, 0.1, "hold", [act("toggle", 0.2, 0.2)])])
    r.press("L")
    r.release("L", 100)
    assert r.fingers() == 1 and ev(r) == [(D, 0), (D, 1), (U, 0)]                  # the hold let go, the toggle stays on


# ===================================================================== delays
def test_a_delayed_action_starts_after_the_original_activation_not_after_the_first_action_ends():
    r = Rig([multi("L", 0.8, 0.7, "tap", [act("tap", 0.5, 0.2, delay_ms=60)])])
    r.press("L", 59)
    assert ev(r) == [(D, 0), (U, 0)]                                 # the first tap ended at 40 ms; nothing else yet
    r.advance(1)
    assert r.touches()[-1][:2] == (D, 0)                             # 60 ms after the key press (pointer id reused)
    assert (r.touches()[-1][2], r.touches()[-1][3]) == px(0.5, 0.2)
    r.advance(100)
    assert r.kinds() == [D, U, D, U] and r.fingers() == 0


def test_two_actions_with_different_delays_each_start_on_their_own_time_from_the_same_origin():
    r = Rig([multi("F", 0.1, 0.9, "hold", [act("tap", 0.5, 0.5, delay_ms=60), act("swipe", 0.2, 0.3, x2=0.8, y2=0.3, delay_ms=120, duration_ms=80)])])
    r.press("F", 59)
    assert at(r, 0.5, 0.5) == [] and at(r, 0.2, 0.3) == []
    r.advance(1)
    assert [t[0] for t in at(r, 0.5, 0.5)] == [D] and at(r, 0.2, 0.3) == []             # t = 60: the tap
    r.advance(59)
    assert at(r, 0.2, 0.3) == []                                                        # t = 119: the swipe is not due yet
    r.advance(1)
    assert [t[0] for t in at(r, 0.2, 0.3)] == [D]                                       # t = 120, though the tap ended at 100
    r.advance(200)
    assert any(t[0] == M for t in r.touches()) and r.fingers() == 1                      # the swipe ended; the hold goes on
    r.release("F")
    assert r.fingers() == 0


def test_a_delay_is_counted_from_the_activation_even_when_earlier_actions_are_long_running():
    r = Rig([multi("F", 0.1, 0.1, "sequence", [act("tap", 0.5, 0.5, delay_ms=100)], steps=[(0.1, 0.1, 0), (0.2, 0.2, 400)])])
    r.press("F", 99)
    assert at(r, 0.5, 0.5) == []
    r.advance(1)
    assert [t[0] for t in at(r, 0.5, 0.5)] == [D]                                       # the sequence is still busy: the tap starts at 100


@pytest.mark.parametrize("mode", ["hold", "repeat", "drag"])
def test_a_delayed_sustained_action_is_cancelled_when_its_trigger_is_released_before_it_starts(mode):
    r = Rig([multi("F", 0.1, 0.9, "tap", [act(mode, 0.5, 0.5, delay_ms=100, **({"x2": 0.8, "y2": 0.5} if mode == "drag" else {}))])])
    r.press("F", 50)
    r.release("F", 1000)
    assert at(r, 0.5, 0.5) == [] and r.fingers() == 0               # it never started: nobody was holding the key at 100 ms
    r.press("F", 150)                                               # a later press starts it normally
    assert at(r, 0.5, 0.5)
    r.release("F", 500)
    assert r.fingers() == 0
    n = len(r.touches())
    r.advance(2000)
    assert len(r.touches()) == n                                    # and it stops with the key (a repeat too)


@pytest.mark.parametrize("mode, downs", [("tap", 1), ("double", 2), ("swipe", 1), ("pinch-out", 2), ("sequence", 2), ("toggle", 1)])
def test_a_delayed_one_shot_or_toggle_still_starts_after_the_key_was_released_and_runs_to_the_end(mode, downs):
    r = Rig([multi("F", 0.1, 0.9, "hold", [act(mode, 0.5, 0.5, delay_ms=100)])])
    r.press("F", 20)
    r.release("F", 1000)
    assert r.kinds().count(D) == 1 + downs                           # the hold, and the delayed action
    assert r.fingers() == (1 if mode == "toggle" else 0)             # everything ended by itself; only a toggle stays on


def test_the_delayed_actions_of_a_mixed_binding_follow_their_policy_hold_ends_toggle_stays_one_shot_completes():
    r = Rig([multi("F", 0.1, 0.9, "hold", [act("toggle", 0.5, 0.9), act("tap", 0.7, 0.9, delay_ms=60)])])
    r.press("F", 20)
    assert ev(r) == [(D, 0), (D, 1)]
    r.release("F")
    assert ev(r) == [(D, 0), (D, 1), (U, 0)]                         # the hold let go at once, the toggle stays down
    r.advance(300)
    assert ev(r) == [(D, 0), (D, 1), (U, 0), (D, 0), (U, 0)] and r.fingers() == 1      # the tap still came, at 60 ms
    assert [t[0] for t in at(r, 0.5, 0.9)] == [D]                                       # the toggle was never lifted


def test_the_mixed_example_a_second_activation_applies_each_actions_own_rule():
    r = Rig([multi("F", 0.1, 0.9, "hold", [act("toggle", 0.5, 0.9), act("tap", 0.7, 0.9, delay_ms=60)])])
    r.press("F", 20)
    r.release("F", 500)                                              # hold ended, toggle on, tap done (the run lives on for the toggle)
    assert r.fingers() == 1
    before = len(r.touches())
    r.press("F")                                                     # a fresh activation, nothing is held
    after = [(t[0], t[2:4]) for t in r.touches()[before:]]
    assert after == [(D, px(0.1, 0.9)), (U, px(0.5, 0.9))]           # the hold starts again, the toggle switches OFF (in list order)
    r.advance(59)
    assert len(r.touches()) == before + 2
    r.advance(1)
    assert r.touches()[-1][0] == D and r.touches()[-1][2:4] == px(0.7, 0.9)           # the tap is scheduled from THIS activation
    r.release("F", 500)
    assert r.fingers() == 0                                          # hold up, toggle off, tap over: the run has ended
    r.press("F", 20)
    assert [t[2:4] for t in r.touches()[-2:]] == [px(0.1, 0.9), px(0.5, 0.9)] and r.fingers() == 2     # and the toggle is on again
    r.release("F", 500)
    assert r.fingers() == 1


def test_an_action_that_is_still_pending_or_running_is_not_started_twice_by_a_new_activation():
    r = Rig([multi("F", 0.1, 0.9, "hold", [act("tap", 0.5, 0.5, delay_ms=100), act("toggle", 0.6, 0.6)])])
    r.press("F", 20)
    r.release("F", 10)
    r.press("F", 10)                                                 # the delayed tap is still pending; the toggle is switched OFF
    r.release("F", 1000)
    assert len([t for t in at(r, 0.5, 0.5) if t[0] == D]) == 1       # one delayed tap from the first activation, not two
    assert [t[0] for t in at(r, 0.6, 0.6)] == [D, U]


def test_a_stale_timer_of_an_earlier_activation_cannot_double_a_repeat_that_is_started_again():
    r = Rig([multi("F", 0.1, 0.9, "tap", [act("repeat", 0.5, 0.5, rate_hz=10)])])
    r.press("F", 30)
    r.release("F", 20)                                               # the repeat ended; its next-tap timer (due at 100) is still queued
    r.press("F", 970)                                                # a new activation before that timer fires
    taps = [t for t in at(r, 0.5, 0.5) if t[0] == D]
    assert 10 <= len(taps) <= 12, len(taps)                          # ~10 taps per second, not twice that


# ===================================================================== cancellation of pending work
def test_release_all_cancels_actions_that_have_not_started_yet():
    uncancelled = Rig([multi("L", 0.8, 0.7, "tap", [act("tap", 0.5, 0.2, delay_ms=300)])])
    uncancelled.press("L", 1000)
    assert uncancelled.kinds() == [D, U, D, U]                       # left alone, the delayed tap does happen
    r = Rig([multi("L", 0.8, 0.7, "tap", [act("tap", 0.5, 0.2, delay_ms=300)])])
    r.press("L", 100)
    r.engine.release_all()
    sent = len(r.touches())
    r.advance(1000)
    assert len(r.touches()) == sent == 2                             # only the first tap ever happened


@pytest.mark.parametrize("how", ["release_all", "mapping_off", "screen_changed", "disconnect"])
def test_focus_loss_mapping_off_rotation_and_disconnect_cancel_every_pending_and_active_action(how):
    r = Rig([multi("F", 0.1, 0.9, "hold", [act("tap", 0.5, 0.5, delay_ms=200), act("repeat", 0.6, 0.6, delay_ms=300, rate_hz=10),
                                           act("toggle", 0.7, 0.7), act("swipe", 0.2, 0.2, x2=0.9, y2=0.2, delay_ms=400)])])
    r.press("F", 100)
    live = r.control
    if how == "disconnect":
        r.control = None
    {"release_all": r.engine.release_all, "mapping_off": lambda: r.engine.set_mapping_active(False),
     "screen_changed": lambda: (setattr(r, "size", (1080, 2400)), r.engine.screen_changed()),
     "disconnect": r.engine.release_all}[how]()
    assert r.fingers() == 0
    r.control = live
    sent = len(r.touches())
    r.advance(5000)
    assert len(r.touches()) == sent and r.fingers() == 0            # no obsolete timer ever starts anything
    assert at(r, 0.5, 0.5) == [] and at(r, 0.6, 0.6) == [] and at(r, 0.2, 0.2) == []


def test_a_rotation_lifts_every_action_of_a_run_at_the_new_size_each_exactly_once():
    r = Rig([multi("F", 0.1, 0.9, "hold", [act("toggle", 0.5, 0.9), act("hold", 0.3, 0.3, also=[(0.4, 0.4)])])])
    r.press("F", 20)
    r.size = (1080, 2400)
    r.engine.screen_changed()
    ups = [t for t in r.touches() if t[0] == U]
    assert sorted(t[1] for t in ups) == [0, 1, 2, 3] and all(t[4:] == (1080, 2400) for t in ups) and r.fingers() == 0


def test_a_run_that_has_ended_never_activates_again_by_itself():
    r = Rig([multi("F", 0.1, 0.9, "tap", [act("tap", 0.5, 0.5, delay_ms=60), act("double", 0.6, 0.6, delay_ms=120)])])
    r.press("F", 1000)
    sent = len(r.touches())
    assert r.fingers() == 0
    r.release("F")
    r.advance(10000)
    assert len(r.touches()) == sent and r.timers == []


# ===================================================================== failures
def test_continue_an_action_that_cannot_start_is_recorded_and_the_others_carry_on():
    r = filled([multi("L", 0.8, 0.7, "hold", [act("hold", 0.5, 0.2), act("hold", 0.3, 0.2)])], taken=8)
    assert r.press("L") is True
    assert r.fingers() == 10                                         # the first action and the first extra got the last two pointers
    assert r.kinds().count(D) == 10                                  # the second extra could not start
    assert r.engine.action_failures == 1 and r.engine.recent_failures[-1] == ("L", 2, "no free touch pointer")
    r.release("L")
    assert r.fingers() == 8 and r.kinds().count(U) == 2              # exactly the two that went down are lifted


def test_continue_when_the_first_action_cannot_start_the_extra_actions_still_do():
    # nine pointers are taken: the first action (a pinch, two fingers) cannot start, the extra tap (one finger) can
    r = filled([multi("L", 0.8, 0.7, "pinch-out", [act("tap", 0.5, 0.2)])], taken=9)
    assert r.press("L", 100) is True
    assert r.engine.action_failures == 1 and r.engine.recent_failures[-1] == ("L", 0, "no free touch pointer")
    assert [t[0] for t in at(r, 0.5, 0.2)] == [D, U]                 # the extra tap was delivered in spite of it
    assert r.fingers() == 9                                          # nothing of the failed pinch stays down (its first finger went back)
    r.release("A")                                                   # now two pointers are free: the pinch fits, the tap has none left
    r.release("L")
    r.press("L", 600)
    assert r.engine.action_failures == 2 and r.engine.recent_failures[-1] == ("L", 1, "no free touch pointer")
    # downs: 9 fillers, the failed pinch's first finger (put down, then taken back), the extra tap, the pinch's two fingers
    assert r.fingers() == 8 and len([t for t in r.touches() if t[0] == D]) == 9 + 1 + 1 + 2


def test_continue_when_every_action_fails_nothing_stays_down_and_the_binding_can_be_activated_again():
    r = filled([multi("L", 0.8, 0.7, "hold", [act("hold", 0.5, 0.2), act("tap", 0.3, 0.2)])], taken=10)
    assert r.press("L") is True
    assert r.fingers() == 10 and r.engine.action_failures == 3
    assert [t for t in r.touches() if t[2:4] in (px(0.8, 0.7), px(0.5, 0.2), px(0.3, 0.2))] == []
    assert "L" not in r.pressed_log[-1]                              # nothing of it ever showed
    r.release("A")                                                   # a finger frees up; no release of L is needed
    r.press("L")
    assert at(r, 0.8, 0.7) and r.fingers() == 10                     # it starts normally now (the first action got the free pointer)
    r.release("L")
    assert r.fingers() == 9


def test_abort_a_failing_action_cancels_the_pending_ones_and_lifts_what_the_run_has_down():
    r = filled([multi("L", 0.8, 0.7, "hold", [act("hold", 0.5, 0.2), act("hold", 0.3, 0.2), act("tap", 0.1, 0.1, delay_ms=100)],
                      on_failure="abort")], taken=8)
    r.press("L")
    assert r.fingers() == 8                                          # the two that went down were lifted again at once
    assert [e for e in ev(r)[8:]] == [(D, 8), (D, 9), (U, 8), (U, 9)]
    r.advance(1000)
    assert at(r, 0.1, 0.1) == [] and r.fingers() == 8                # the pending tap never started
    assert r.engine.action_failures == 1
    for key in "ABCDEFGH":                                           # nobody else's finger was touched
        assert r.engine.injector.is_down(("bind", r.profile.find(key).id, 0))


def test_abort_stops_a_swipe_in_flight_and_a_toggle_but_cannot_take_back_a_tap_already_delivered():
    fillers = [Binding(k, 0.05 + 0.09 * i, 0.95, "hold") for i, k in enumerate("ABCDEFGH")]
    r = Rig([multi("L", 0.1, 0.9, "tap", [act("swipe", 0.2, 0.2, x2=0.9, y2=0.2, duration_ms=400), act("toggle", 0.6, 0.6),
                                          act("hold", 0.3, 0.3, delay_ms=300)], on_failure="abort")] + fillers)
    r.press("L", 100)                                                # the tap came and went; the swipe moves, the toggle is down
    for k in "ABCDEFGH":
        r.press(k)
    assert r.fingers() == 10
    delivered = [t[0] for t in at(r, 0.1, 0.9)]
    assert delivered == [D, U]
    r.advance(250)                                                   # t = 350: the delayed hold is due, there is no pointer
    assert r.engine.action_failures == 1 and r.fingers() == 8        # the swipe and the toggle were lifted, the fillers stay
    assert [t[0] for t in at(r, 0.1, 0.9)] == [D, U]                 # the delivered tap is simply history
    n = len(r.touches())
    r.advance(5000)
    assert len(r.touches()) == n                                     # the swipe's motion ticks are dead


def test_abort_stops_a_running_repeat_for_good():
    fillers = [Binding(k, 0.05 + 0.09 * i, 0.95, "hold") for i, k in enumerate("ABCDEFGHI")]
    r = Rig(fillers + [multi("L", 0.5, 0.5, "repeat", [act("hold", 0.3, 0.3, delay_ms=100)], rate_hz=10, on_failure="abort")])
    for k in "ABCDEFGHI":
        r.press(k)
    r.press("L", 250)                                                # the repeat taps with the last pointer; the delayed hold finds none
    assert r.engine.action_failures == 1
    taps = len(at(r, 0.5, 0.5))
    assert taps >= 1 and r.fingers() == 9
    r.advance(3000)
    assert len(at(r, 0.5, 0.5)) == taps and r.fingers() == 9         # the repeat never taps again, though L is still held
    r.release("L")
    assert r.fingers() == 9


def test_abort_never_touches_another_binding_the_stick_look_or_the_direct_finger():
    holds = [Binding(k, 0.05 + 0.09 * i, 0.95, "hold") for i, k in enumerate("GHIJK")]
    r = Rig(holds + [multi("L", 0.8, 0.7, "hold", [act("hold", 0.5, 0.2), act("hold", 0.3, 0.2)], on_failure="abort")],
            stick=Stick(0.15, 0.7, ramp_ms=0), look=Look(0.5, 0.2, 0.9, 0.8))
    for k in "GHIJK":
        r.press(k)
    r.press("W")
    assert r.engine.mouse_button("LMB", True, (0.9, 0.1)) is True        # the direct finger (before the mouse is captured)
    r.engine.set_look_captured(True)
    r.engine.look_delta(5, 0)
    r.advance(10)
    assert r.fingers() == 8                                            # 5 holds, stick, direct, look: two pointers are left
    r.press("L")                                                       # L needs three: its first two go down, the third fails ...
    assert r.engine.action_failures == 1
    assert r.fingers() == 8                                            # ... and the abort lifted the two again
    inj = r.engine.injector
    assert inj.is_down(("stick", 0)) and inj.is_down(("look", 0)) and inj.is_down(("mouse", "direct"))
    assert all(inj.is_down(("bind", r.profile.find(k).id, 0)) for k in "GHIJK")
    assert [t[:2] for t in r.touches()[-2:]] == [(U, 8), (U, 9)]


def test_a_disconnected_control_channel_fails_every_action_and_the_binding_recovers_when_it_is_back():
    for policy, failures in (("continue", 3), ("abort", 1)):
        r = Rig([multi("L", 0.8, 0.7, "hold", [act("hold", 0.5, 0.2), act("tap", 0.3, 0.2, delay_ms=50)], on_failure=policy)])
        live = r.control
        r.control = None
        assert r.press("L", 200) is True
        # continue tries all three actions (the delayed one too); abort gives up at the first failure and never tries the rest
        assert r.fingers() == 0 and live.touches() == [] and r.engine.action_failures == failures, policy
        r.control = live
        r.release("L")
        r.press("L", 10)
        assert at(r, 0.8, 0.7) and at(r, 0.5, 0.2), policy
        r.release("L")
        assert r.fingers() == 0


def test_a_delayed_action_that_fails_is_recorded_and_under_continue_the_rest_goes_on():
    fillers = [Binding(k, 0.05 + 0.09 * i, 0.95, "hold") for i, k in enumerate("ABCDEFGHI")]
    r = Rig([multi("L", 0.8, 0.7, "hold", [act("hold", 0.5, 0.2, delay_ms=100)])] + fillers)
    r.press("L", 10)
    for k in "ABCDEFGHI":
        r.press(k)
    assert r.fingers() == 10
    r.advance(200)
    assert r.engine.action_failures == 1 and r.engine.recent_failures[-1] == ("L", 1, "no free touch pointer")
    assert r.fingers() == 10 and r.engine.injector.is_down(("bind", r.profile.find("L").id, 0))      # the first action is still down
    r.release("L")
    assert r.fingers() == 9


def test_a_delayed_action_that_fails_under_abort_lifts_the_whole_run():
    fillers = [Binding(k, 0.05 + 0.09 * i, 0.95, "hold") for i, k in enumerate("ABCDEFGHI")]
    r = Rig([multi("L", 0.8, 0.7, "hold", [act("hold", 0.5, 0.2, delay_ms=100)], on_failure="abort")] + fillers)
    r.press("L", 10)
    for k in "ABCDEFGHI":
        r.press(k)
    r.advance(200)
    assert r.engine.action_failures == 1 and r.fingers() == 9 and not r.engine.injector.is_down(("bind", r.profile.find("L").id, 0))
    r.release("L")
    assert r.fingers() == 9


# ===================================================================== sequence and repeat inside a run
def test_a_repeat_and_a_sequence_in_one_binding_each_follow_their_own_lifecycle():
    r = Rig([multi("L", 0.1, 0.9, "repeat", [act("sequence", 0.5, 0.5, steps=[(0.2, 0.2, 0), (0.3, 0.3, 300), (0.4, 0.4, 100)])], rate_hz=10)])
    r.press("L", 200)
    r.release("L", 10)                                               # the repeat stops now; the sequence is a one-shot and carries on
    taps_when_released = len(at(r, 0.1, 0.9))
    r.advance(3000)
    assert len(at(r, 0.1, 0.9)) == taps_when_released                # no more repeat taps
    assert [t[0] for t in at(r, 0.2, 0.2)] == [D, U] and [t[0] for t in at(r, 0.3, 0.3)] == [D, U] and [t[0] for t in at(r, 0.4, 0.4)] == [D, U]
    assert r.fingers() == 0 and r.timers == []


def test_a_repeat_keeps_going_while_any_trigger_of_the_binding_is_held():
    r = Rig([multi("L", 0.1, 0.9, "repeat", [act("tap", 0.5, 0.5)], triggers=["M"], rate_hz=10)])
    r.press("L", 200)
    r.press("M", 200)
    r.release("L", 300)
    n = len(at(r, 0.1, 0.9))
    r.advance(300)
    assert len(at(r, 0.1, 0.9)) > n                                  # M still sustains it
    r.release("M")
    n = len(at(r, 0.1, 0.9))
    r.advance(1000)
    assert len(at(r, 0.1, 0.9)) == n and r.fingers() == 0


# ===================================================================== scope
def scope_rig(**kw):
    return Rig([Binding("RMB", 0.9, 0.5, "hold", scope=True), Binding("Z", 0.8, 0.4, "hold", scope=True)], **kw)


@pytest.mark.parametrize("first, second", [("RMB", "Z"), ("Z", "RMB")])
def test_two_scope_buttons_keep_the_scope_on_until_the_last_of_them_is_released(first, second):
    r = scope_rig()
    r.engine.mouse_button("RMB", True, None)
    r.press("Z")
    assert r.engine.scope_active is True
    for name in (first,):
        (r.engine.mouse_button(name, False, None) if name == "RMB" else r.release(name))
    assert r.engine.scope_active is True                             # the other one is still held
    (r.engine.mouse_button(second, False, None) if second == "RMB" else r.release(second))
    assert r.engine.scope_active is False and r.fingers() == 0


def test_the_look_sensitivity_stays_scoped_while_any_scope_button_is_held():
    r = Rig([Binding("RMB", 0.9, 0.5, "hold", scope=True), Binding("Z", 0.8, 0.4, "hold", scope=True)], look=Look(0.5, 0.2, 0.9, 0.8, ads_scale=0.5))
    r.engine.set_look_captured(True)

    def travel():
        r.advance(300)
        n = len(r.touches())
        r.engine.look_delta(20, 0)
        r.advance(10)
        return [t for t in r.touches()[n:] if t[0] == M][-1][2] - 1680

    assert travel() == 20
    r.engine.mouse_button("RMB", True, None)
    r.press("Z")
    assert travel() == 10
    r.release("Z")
    assert travel() == 10                                            # RMB still scoped
    r.engine.mouse_button("RMB", False, None)
    assert travel() == 20


def test_a_toggle_scope_and_a_hold_scope_do_not_cancel_each_other():
    r = Rig([Binding("T", 0.9, 0.5, "toggle", scope=True), Binding("Z", 0.8, 0.4, "hold", scope=True)])
    r.press("T")
    r.release("T")
    assert r.engine.scope_active is True
    r.press("Z")
    r.release("Z")
    assert r.engine.scope_active is True                             # the toggle is still on
    r.press("Z")
    r.press("T")                                                     # toggle off while Z is held
    r.release("T")
    assert r.engine.scope_active is True
    r.release("Z")
    assert r.engine.scope_active is False


def test_a_one_shot_scope_button_and_a_hold_scope_button_are_independent():
    r = Rig([Binding("Q", 0.9, 0.5, "tap", scope=True), Binding("Z", 0.8, 0.4, "hold", scope=True)])
    r.press("Q", 100)
    r.release("Q")
    assert r.engine.scope_active is True                             # latched on by the tap
    r.press("Z")
    r.release("Z")
    assert r.engine.scope_active is True                             # releasing the hold does not switch the tap's scope off
    r.press("Z")
    r.press("Q", 100)                                                # the tap flips its latch off, the hold keeps scope on
    r.release("Q")
    assert r.engine.scope_active is True
    r.release("Z")
    assert r.engine.scope_active is False


def test_scope_of_one_action_of_a_multi_action_binding_follows_that_actions_lifecycle():
    r = Rig([multi("L", 0.1, 0.9, "hold", [act("hold", 0.5, 0.5, scope=True), act("toggle", 0.6, 0.6, scope=True)])])
    r.press("L")
    assert r.engine.scope_active is True
    r.release("L")
    assert r.engine.scope_active is True                             # the toggle's scope stays on after the hold's is gone
    r.press("L")                                                     # a fresh activation switches the toggle off, the hold's scope returns
    assert r.engine.scope_active is True
    r.release("L")
    assert r.engine.scope_active is False


def test_every_cleanup_path_clears_all_scope_sources():
    for how in ("release_all", "screen_changed", "mapping_off"):
        r = Rig([Binding("T", 0.9, 0.5, "toggle", scope=True), Binding("Z", 0.8, 0.4, "hold", scope=True), Binding("Q", 0.7, 0.4, "tap", scope=True)])
        r.press("T")
        r.press("Z")
        r.press("Q", 100)
        assert r.engine.scope_active is True
        {"release_all": r.engine.release_all, "screen_changed": r.engine.screen_changed,
         "mapping_off": lambda: r.engine.set_mapping_active(False)}[how]()
        assert r.engine.scope_active is False, how
        r.engine.set_mapping_active(True)
        assert r.engine.scope_active is False


def test_a_failed_scope_action_never_leaves_scope_on():
    r = filled([Binding("Z", 0.8, 0.4, "repeat", scope=True)], taken=10)
    r.press("Z")
    assert r.engine.scope_active is False and r.fingers() == 10


# ===================================================================== no action releases another's pointers
def test_cancelling_one_binding_never_lifts_a_finger_that_belongs_to_another():
    r = Rig([multi("G", 0.1, 0.9, "hold", [act("hold", 0.2, 0.9)]), Binding("H", 0.5, 0.5, "hold")])
    r.press("G")
    r.press("H")
    r.release("G")
    assert sorted(t[1] for t in r.touches() if t[0] == U) == [0, 1]  # only G's two fingers
    assert r.fingers() == 1 and [t[0] for t in r.touches() if t[1] == 2] == [D]      # H's finger (pointer 2) is untouched


def test_an_action_that_ends_or_fails_releases_only_its_own_fingers():
    r = Rig([multi("L", 0.1, 0.1, "hold", [act("tap", 0.2, 0.2), act("hold", 0.3, 0.3, also=[(0.31, 0.3)])])],
            stick=Stick(0.15, 0.7, ramp_ms=0), look=Look(0.5, 0.2, 0.9, 0.8))
    b = r.profile.find("L")
    r.press("W")
    r.engine.set_look_captured(True)
    r.engine.look_delta(5, 0)
    r.press("L", 100)                                                # the tap came and went
    inj = r.engine.injector
    assert inj.is_down(owner(b, 0)) and inj.is_down(owner(b, 2)) and inj.is_down(owner(b, 2, "also", 0))
    assert inj.is_down(("stick", 0)) and inj.is_down(("look", 0)) and not inj.is_down(owner(b, 1))
    r.release("L")
    assert not inj.is_down(owner(b, 0)) and not inj.is_down(owner(b, 2)) and inj.is_down(("stick", 0)) and inj.is_down(("look", 0))


# ===================================================================== triggers and wheel with several actions
def test_several_triggers_and_several_actions_share_one_run():
    r = Rig([multi("E", 0.1, 0.1, "hold", [act("hold", 0.2, 0.2), act("tap", 0.3, 0.3, delay_ms=50)], triggers=["F", "MMB"])])
    r.press("F", 100)
    r.press("E")
    mouse(r, "MMB", True)
    assert r.kinds().count(D) == 3 and r.fingers() == 2              # one run: the two holds and the (finished) tap, started once
    r.release("F")
    mouse(r, "MMB", False)
    assert r.fingers() == 2                                          # E still sustains both holds
    r.release("E")
    assert r.fingers() == 0 and r.kinds().count(U) == 3


def test_a_wheel_notch_runs_every_action_as_a_one_shot_and_nothing_waits_for_a_release():
    r = Rig([multi("E", 0.1, 0.1, "hold", [act("repeat", 0.2, 0.2), act("drag", 0.3, 0.3, x2=0.6, y2=0.3, duration_ms=40),
                                           act("tap", 0.5, 0.5, delay_ms=60)], triggers=["WheelUp"])])
    assert r.engine.wheel_binding(+1) is True
    r.advance(1000)
    assert r.fingers() == 0 and r.kinds().count(D) == r.kinds().count(U) == 4          # hold->tap, repeat->tap, drag->swipe, tap
    assert r.timers == []


# ===================================================================== the highlight and bookkeeping
def test_the_marker_stays_lit_while_any_action_of_the_run_is_showing():
    r = Rig([multi("E", 0.1, 0.1, "tap", [act("hold", 0.2, 0.2), act("tap", 0.3, 0.3, delay_ms=100)])])
    r.press("E", 20)
    assert r.pressed_log[-1] == frozenset({"E"})
    r.advance(100)                                                   # the first tap ended; the hold goes on
    assert r.pressed_log[-1] == frozenset({"E"})
    r.release("E", 100)                                              # the delayed tap runs 100..140
    r.advance(100)
    assert r.pressed_log[-1] == frozenset() and r.fingers() == 0


def test_nothing_is_left_in_the_running_table_when_a_multi_action_run_has_ended():
    for extras in ([act("tap", 0.2, 0.2, delay_ms=70)], [act("sequence", 0.2, 0.2)], [act("pinch-in", 0.3, 0.3, delay_ms=10)]):
        r = Rig([multi("E", 0.1, 0.1, "tap", extras)])
        r.press("E", 3000)
        assert r.engine._runs == {} and r.fingers() == 0
        r.press("E", 20)
        r.engine.release_all()
        assert r.engine._runs == {} and r.fingers() == 0


# ===================================================================== what stays as it was
def test_two_separate_bindings_on_one_input_still_run_only_the_last_one_and_their_extra_actions_follow_it():
    p = Profile("p")
    p.bindings = [multi("R", 0.1, 0.1, "hold", [act("tap", 0.2, 0.2)]), multi("R", 0.9, 0.9, "hold", [act("tap", 0.8, 0.8)])]
    r = Rig()
    r.engine.set_profile(p)
    r.press("R", 100)
    assert at(r, 0.9, 0.9) and at(r, 0.8, 0.8) and not at(r, 0.1, 0.1) and not at(r, 0.2, 0.2)


def test_a_version_1_binding_is_run_exactly_as_before():
    p = Profile.from_dict({"name": "v1", "bindings": [{"input": "R", "x": 0.7, "y": 0.9, "mode": "tap", "also": [[0.1, 0.1]]}]})
    r = Rig()
    r.engine.set_profile(p)
    r.press("R", 100)
    assert ev(r) == [(D, 0), (D, 1), (U, 0), (U, 1)] and r.engine.action_failures == 0


def test_a_version_2_profile_with_several_actions_loaded_from_a_file_runs_them_all():
    p = Profile.from_dict({"version": 2, "name": "v2", "bindings": [
        {"type": "binding", "id": "b", "triggers": ["F"], "on_failure": "continue", "actions": [
            {"mode": "hold", "x": 0.1, "y": 0.9}, {"mode": "tap", "x": 0.5, "y": 0.5, "delay_ms": 60},
            {"mode": "swipe", "x": 0.2, "y": 0.3, "x2": 0.8, "y2": 0.3, "duration_ms": 80, "delay_ms": 120}]}]})
    r = Rig()
    r.engine.set_profile(p)
    r.press("F", 59)
    assert [t[0] for t in r.touches()] == [D]
    r.advance(1)
    assert at(r, 0.5, 0.5)
    r.advance(60)
    assert at(r, 0.2, 0.3)
    r.advance(300)
    r.release("F")
    assert r.fingers() == 0 and r.kinds().count(D) == r.kinds().count(U) == 3


# ===================================================================== gaps found by the mutation check
def test_a_scope_action_that_is_still_pending_does_not_count_as_scope():
    r = Rig([multi("L", 0.1, 0.9, "tap", [act("hold", 0.5, 0.5, delay_ms=100, scope=True)])])
    r.press("L", 50)
    assert r.engine.scope_active is False                            # nothing is scoping yet: the hold has not started
    r.advance(60)
    assert r.engine.scope_active is True                             # now it is, and the key is still held
    r.release("L")
    assert r.engine.scope_active is False


def test_a_pending_scope_hold_that_is_cancelled_never_turns_scope_on():
    r = Rig([multi("L", 0.1, 0.9, "tap", [act("hold", 0.5, 0.5, delay_ms=100, scope=True)])])
    r.press("L", 50)
    r.release("L", 1000)
    assert r.engine.scope_active is False and r.fingers() == 0


def test_a_wheel_notch_that_switches_a_toggle_on_leaves_no_trigger_behind_so_the_next_key_press_switches_it_off():
    r = Rig([multi("E", 0.1, 0.1, "toggle", [act("hold", 0.5, 0.5)], triggers=["WheelUp"])])
    r.engine.wheel_binding(+1)                                       # the toggle goes on (the hold became a quick tap)
    r.advance(100)
    assert r.fingers() == 1                                          # the tap is over; the toggle is on
    r.press("E")                                                     # nothing physical was holding it: this is a fresh press
    assert [t[0] for t in at(r, 0.1, 0.1)] == [D, U]                 # the toggle switched OFF (it would have been ignored as a join)
    assert r.fingers() == 1 and at(r, 0.5, 0.5)[-1][0] == D           # and the finished hold started again for this fresh press
    r.release("E")
    assert r.fingers() == 0


def test_a_repeated_key_down_of_the_held_trigger_applies_each_actions_own_rule_the_toggle_flips_the_hold_does_not_restart():
    # Same legacy rule as a single toggle (the router never delivers key repeat): hold = absorbed, toggle = flips.
    r = Rig([multi("F", 0.1, 0.9, "hold", [act("toggle", 0.5, 0.9)])])
    r.press("F")
    assert r.fingers() == 2
    r.press("F")
    assert r.fingers() == 1 and [t[0] for t in at(r, 0.1, 0.9)] == [D]       # the hold stayed down, once; the toggle switched off
    r.release("F")
    assert r.fingers() == 0


def test_a_release_is_reported_as_handled_while_the_run_has_a_sustained_action_and_not_for_a_toggle_alone():
    r = Rig([multi("F", 0.1, 0.9, "hold", [act("toggle", 0.5, 0.9)], triggers=["G"])])
    r.press("F")
    r.press("G")
    assert r.release("F") is True                                    # G still sustains the hold; the key was meant for a binding
    assert r.release("G") is True and r.fingers() == 1
    assert r.release("G") is False                                   # nothing holds anything any more
    toggle_only = Rig([multi("T", 0.1, 0.9, "toggle", [act("toggle", 0.5, 0.9)])])
    toggle_only.press("T")
    assert toggle_only.release("T") is False                         # as for a single toggle: the release is not "handled"
