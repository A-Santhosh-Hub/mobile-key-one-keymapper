"""Several separate bindings sharing a trigger all run (Phase 2, step 7), but ONLY where the profile says so.

Pressing a trigger that has an explicitly authorized sharing group (Profile.share_trigger, add_shared(authorize=True),
add_trigger(shared=True), or the "shared_triggers" list of a version-2 file) starts every participant, in binding-list order. Each
participant has its OWN run: its own actions, delays, holders, toggle state and fingers ("bind", id, action index, ...). Repeated inputs
without that authorization behave exactly as before: only the winner of trigger_owners runs.

(History: this file replaces the last expected-failure spec, `test_two_bindings_that_share_a_key_both_fire_in_list_order_and_both_let_go`.
It built its profile with two bindings on one key and no authorization and expected both to fire. This step deliberately does NOT do
that, because repeated inputs alone must never run together; the same behaviour is asserted below for an AUTHORIZED group, and the
unauthorized case is asserted to keep its single winner.)"""
import itertools

import pytest

from app.mapping.profile import Action, Binding, Look, Profile, Stick
from app.stream import protocol
from test_action_scheduling import TieRig
from test_characterization_engine import SPECS, Rig

D, U, M = protocol.TOUCH_DOWN, protocol.TOUCH_UP, protocol.TOUCH_MOVE
W, H = 2400, 1080
ORDERS = ["fifo", "lifo", 1, 2, 3]


def px(x, y):
    return (int(x * W + 1e-6), int(y * H + 1e-6))


def act(mode, x, y, delay_ms=0, **kw):
    return Action(mode, x, y, delay_ms=delay_ms, **{**SPECS.get(mode, {}), **kw})


def b(key, x, y, mode="hold", id=None, extras=(), triggers=(), **kw):
    return Binding(key, x, y, mode, id=id or "", extra_triggers=list(triggers), extra_actions=list(extras), **{**SPECS.get(mode, {}), **kw})


def shared(bindings, groups=None, rig=Rig, **kw):
    """A rig running these bindings; `groups` = {trigger: [ids]} to authorize (default: none)."""
    p = Profile("p")
    p.bindings = list(bindings)
    for trigger, ids in (groups or {}).items():
        p.share_trigger(trigger, ids)
    r = rig(**kw)
    r.engine.set_profile(p)
    r.profile = p
    return r


def downs(r):
    return [(t[1], t[2:4]) for t in r.touches() if t[0] == D]


def spots(r):
    return [s for _p, s in downs(r)]


def ev(r):
    return [(t[0], t[1]) for t in r.touches()]


def mouse(r, name, pressed):
    return r.engine.mouse_button(name, pressed, (0.5, 0.5))


# ===================================================================== the basic feature
def test_two_authorized_bindings_sharing_one_key_both_fire_in_list_order_and_both_let_go():
    r = shared([b("R", 0.1, 0.1, id="a"), b("R", 0.9, 0.9, id="b")], {"R": ["a", "b"]})
    r.press("R")
    assert [(t[0], t[1], t[2]) for t in r.touches()] == [(D, 0, 240), (D, 1, 2160)]
    r.release("R")
    assert r.kinds() == [D, D, U, U] and r.fingers() == 0


def test_without_the_authorization_the_same_two_bindings_still_run_only_the_last_one():
    r = shared([b("R", 0.1, 0.1, id="a"), b("R", 0.9, 0.9, id="b")])
    r.press("R")
    assert spots(r) == [px(0.9, 0.9)] and r.fingers() == 1
    r.release("R")
    assert r.fingers() == 0


def test_three_authorized_bindings_sharing_a_key_all_run_in_list_order():
    r = shared([b("F", 0.1, 0.1, "hold", "a"), b("F", 0.5, 0.5, "tap", "b"), b("F", 0.9, 0.9, "toggle", "c")], {"F": ["c", "a", "b"]})
    r.press("F", 100)
    assert spots(r) == [px(0.1, 0.1), px(0.5, 0.5), px(0.9, 0.9)]            # list order, not the order the ids were given in
    assert [pid for pid, _ in downs(r)] == [0, 1, 2]
    r.release("F")
    assert r.fingers() == 1 and r.kinds().count(U) == 2                      # the hold and the tap are over, the toggle stays on


@pytest.mark.parametrize("ids", list(itertools.permutations(["a", "b", "c"])))
def test_the_run_order_is_the_list_order_whatever_order_the_group_was_written_in(ids):
    r = shared([b("F", 0.1, 0.1, id="a"), b("F", 0.2, 0.2, id="b"), b("F", 0.3, 0.3, id="c")], {"F": list(ids)})
    r.press("F")
    assert spots(r) == [px(0.1, 0.1), px(0.2, 0.2), px(0.3, 0.3)]


def test_each_participant_owns_its_own_fingers_and_releases_only_those():
    a, c = b("F", 0.1, 0.1, id="a", also=[(0.11, 0.1)]), b("F", 0.9, 0.9, id="c")
    r = shared([a, c], {"F": ["a", "c"]})
    r.press("F")
    inj = r.engine.injector
    assert inj.is_down(("bind", "a", 0)) and inj.is_down(("bind", "a", 0, "also", 0)) and inj.is_down(("bind", "c", 0))
    r.profile.bindings[1].mode = "tap"                      # (c is a hold; nothing to change: just prove owners are per binding)
    r.release("F")
    assert not inj.is_down(("bind", "a", 0)) and not inj.is_down(("bind", "c", 0)) and r.fingers() == 0
    assert [t[1] for t in r.touches() if t[0] == U] == [0, 1, 2]


def test_binding_a_with_e_and_f_and_binding_b_with_only_f_example_from_the_brief():
    r = shared([b("E", 0.1, 0.5, id="a", triggers=["F"]), b("F", 0.9, 0.5, id="b")], {"F": ["a", "b"]})
    r.press("E")
    assert ev(r) == [(D, 0)] and r.fingers() == 1                            # only A
    r.press("F")
    assert ev(r) == [(D, 0), (D, 1)]                                         # B starts; F joins A's run: no second finger for A
    r.release("F")
    assert ev(r) == [(D, 0), (D, 1), (U, 1)] and r.fingers() == 1            # B lets go; A is held by E
    assert r.engine.injector.is_down(("bind", "a", 0)) and not r.engine.injector.is_down(("bind", "b", 0))
    r.release("E")
    assert ev(r)[-1] == (U, 0) and r.fingers() == 0


def test_the_same_example_pressed_in_the_other_order_and_released_in_every_order():
    for press, release in itertools.product(itertools.permutations(["E", "F"]), itertools.permutations(["E", "F"])):
        r = shared([b("E", 0.1, 0.5, id="a", triggers=["F"]), b("F", 0.9, 0.5, id="b")], {"F": ["a", "b"]})
        for name in press:
            r.press(name)
        assert r.fingers() == 2, (press, release)
        for name in release:
            r.release(name)
        assert r.fingers() == 0 and r.kinds().count(D) == r.kinds().count(U) == 2, (press, release)


# ===================================================================== every mode on both sides
@pytest.mark.parametrize("mode", list(SPECS))
def test_every_mode_works_next_to_a_hold_on_a_shared_key(mode):
    r = shared([b("F", 0.1, 0.9, mode, "a"), b("F", 0.9, 0.9, "hold", "b")], {"F": ["a", "b"]})
    r.press("F", 300)
    assert (px(0.9, 0.9)) in spots(r) and r.engine.injector.is_down(("bind", "b", 0))      # B holds whatever A does
    r.release("F", 3000)
    assert not r.engine.injector.is_down(("bind", "b", 0))
    assert r.fingers() == (1 if mode == "toggle" else 0)
    assert r.kinds().count(D) >= r.kinds().count(U)


@pytest.mark.parametrize("mode", list(SPECS))
def test_every_mode_on_both_sides_ends_with_nothing_down_after_a_cancel(mode):
    r = shared([b("F", 0.1, 0.9, mode, "a"), b("F", 0.9, 0.9, mode, "b")], {"F": ["a", "b"]})
    r.press("F", 20)
    r.engine.release_all()
    sent = len(r.touches())
    r.advance(3000)
    assert len(r.touches()) == sent and r.fingers() == 0
    assert sorted(t[1] for t in r.touches() if t[0] == D) == sorted(t[1] for t in r.touches() if t[0] == U)


def test_repeat_on_one_side_keeps_going_until_the_trigger_is_released_and_the_hold_on_the_other_side_is_untouched():
    r = shared([b("F", 0.1, 0.1, "repeat", "a", rate_hz=10), b("F", 0.9, 0.9, "hold", "b")], {"F": ["a", "b"]})
    r.press("F", 500)
    taps = len([s for s in spots(r) if s == px(0.1, 0.1)])
    assert 4 <= taps <= 7 and r.engine.injector.is_down(("bind", "b", 0))
    r.release("F", 1000)
    assert len([s for s in spots(r) if s == px(0.1, 0.1)]) == taps
    assert r.fingers() == 0 and r.timers == []


def test_a_toggle_and_a_hold_sharing_a_key_each_keep_their_own_state_across_presses():
    r = shared([b("F", 0.1, 0.1, "toggle", "a"), b("F", 0.9, 0.9, "hold", "b")], {"F": ["a", "b"]})
    r.press("F")
    r.release("F")
    assert r.fingers() == 1 and r.engine.injector.is_down(("bind", "a", 0))          # toggle on, hold gone
    r.press("F")
    assert r.engine.injector.is_down(("bind", "b", 0)) and not r.engine.injector.is_down(("bind", "a", 0))    # toggle off, hold down again
    r.release("F")
    assert r.fingers() == 0


def test_a_run_of_one_binding_is_not_restarted_or_cancelled_by_the_other_one_ending():
    r = shared([b("F", 0.1, 0.1, "tap", "a"), b("F", 0.9, 0.9, "hold", "b")], {"F": ["a", "b"]})
    r.press("F", 200)                                                     # A's tap came and went long ago
    assert r.engine.injector.is_down(("bind", "b", 0)) and ev(r).count((D, 0)) == 1
    r.advance(2000)
    assert r.engine.injector.is_down(("bind", "b", 0)) and len(r.touches()) == 3


# ===================================================================== multi-action and delays inside participants
def test_each_participant_runs_all_of_its_own_actions_with_its_own_delays():
    a = b("F", 0.1, 0.1, "hold", "a", extras=[act("tap", 0.2, 0.2, 60)])
    c = b("F", 0.9, 0.9, "hold", "b", extras=[act("tap", 0.8, 0.8, 120), act("hold", 0.7, 0.7, 60)])
    r = shared([a, c], {"F": ["a", "b"]})
    r.press("F", 59)
    assert spots(r) == [px(0.1, 0.1), px(0.9, 0.9)]
    r.advance(1)
    assert spots(r)[2:] == [px(0.2, 0.2), px(0.7, 0.7)]                  # 60 ms: A's tap, then B's hold (list order)
    r.advance(60)
    assert spots(r)[4:] == [px(0.8, 0.8)]                                # 120 ms: B's tap
    r.release("F", 100)
    assert r.fingers() == 0


@pytest.mark.parametrize("order", ORDERS)
def test_shared_delayed_actions_with_equal_deadlines_start_in_binding_then_action_order(order):
    a = b("F", 0.1, 0.1, "tap", "a", extras=[act("tap", 0.2, 0.2, 100), act("tap", 0.3, 0.3, 100)])
    c = b("F", 0.9, 0.9, "tap", "b", extras=[act("tap", 0.8, 0.8, 100), act("tap", 0.7, 0.7, 100)])
    r = shared([a, c], {"F": ["a", "b"]}, rig=TieRig, order=order)
    r.press("F", 99)
    assert len(spots(r)) == 2
    r.advance(1)
    assert spots(r)[2:] == [px(0.2, 0.2), px(0.3, 0.3), px(0.8, 0.8), px(0.7, 0.7)]
    assert r.engine._starts == []


@pytest.mark.parametrize("order", ORDERS)
def test_cancelling_before_the_shared_deadline_stops_every_participants_pending_actions(order):
    a = b("F", 0.1, 0.1, "tap", "a", extras=[act("tap", 0.2, 0.2, 100)])
    c = b("F", 0.9, 0.9, "tap", "b", extras=[act("tap", 0.8, 0.8, 100)])
    r = shared([a, c], {"F": ["a", "b"]}, rig=TieRig, order=order)
    r.press("F", 50)
    r.engine.release_all()
    r.advance(1000)
    assert spots(r) == [px(0.1, 0.1), px(0.9, 0.9)] and r.fingers() == 0 and r.engine._starts == []


@pytest.mark.parametrize("order", ORDERS)
def test_a_binding_whose_pending_hold_was_cancelled_is_not_restarted_by_a_stale_timer_and_the_other_binding_is_unaffected(order):
    a = b("F", 0.1, 0.1, "tap", "a", extras=[act("hold", 0.2, 0.2, 100)])
    c = b("F", 0.9, 0.9, "tap", "b", extras=[act("tap", 0.8, 0.8, 100)])
    r = shared([a, c], {"F": ["a", "b"]}, rig=TieRig, order=order)
    r.press("F", 50)
    r.release("F", 1000)                                                  # A's pending hold is cancelled (nobody holds F); B's tap is not
    assert spots(r)[2:] == [px(0.8, 0.8)] and r.fingers() == 0 and r.engine._starts == []
    n = len(r.touches())
    r.advance(5000)
    assert len(r.touches()) == n                                          # no stale timer starts A's hold later
    r.press("F", 150)                                                     # a new activation works for both, once each
    assert spots(r)[3:] == [px(0.1, 0.1), px(0.9, 0.9), px(0.2, 0.2), px(0.8, 0.8)]
    r.release("F", 500)
    assert r.fingers() == 0


@pytest.mark.parametrize("order", ORDERS)
def test_release_all_in_between_leaves_no_timer_that_can_start_a_cancelled_participant(order):
    a = b("F", 0.1, 0.1, "tap", "a", extras=[act("hold", 0.2, 0.2, 100)])
    c = b("F", 0.9, 0.9, "tap", "b", extras=[act("tap", 0.8, 0.8, 100)])
    r = shared([a, c], {"F": ["a", "b"]}, rig=TieRig, order=order)
    r.press("F", 50)
    r.engine.release_all()
    r.release("F")
    r.press("F", 30)                                                      # a new activation at t = 80: its own deadlines are at 180
    r.advance(30)                                                         # t = 110: the old deadline (100) has passed
    assert px(0.2, 0.2) not in spots(r) and px(0.8, 0.8) not in spots(r)
    r.advance(100)
    assert spots(r).count(px(0.2, 0.2)) == 1 and spots(r).count(px(0.8, 0.8)) == 1
    r.release("F")
    assert r.fingers() == 0


# ===================================================================== failures and abort
def test_a_binding_that_cannot_start_does_not_stop_the_other_participants():
    fillers = [b(k, 0.05 + 0.09 * i, 0.95, id=f"f{i}") for i, k in enumerate("123456789")]            # nine pointers taken
    first = b("F", 0.1, 0.1, "pinch-out", id="a")                         # needs two fingers: only one is free
    second = b("F", 0.9, 0.9, "hold", id="b")
    r = shared([first, second] + fillers, {"F": ["a", "b"]})
    for k in "123456789":
        r.press(k)
    r.press("F")
    assert r.engine.action_failures == 1 and r.engine.recent_failures[-1][0] == "F"
    assert r.engine.injector.is_down(("bind", "b", 0)) and not r.engine.injector.is_down(("bind", "a", 0, "a"))
    assert r.fingers() == 10
    r.release("F")
    assert r.fingers() == 9


def test_pointer_exhaustion_fails_the_later_participants_in_list_order_and_leaves_the_earlier_ones_alone():
    fillers = [b(k, 0.05 + 0.09 * i, 0.95, id=f"f{i}") for i, k in enumerate("12345678")]            # eight pointers taken
    parts = [b("F", 0.1 * (i + 1), 0.2, id=f"p{i}") for i in range(4)]
    r = shared(parts + fillers, {"F": [p.id for p in parts]})
    for k in "12345678":
        r.press(k)
    r.press("F")
    assert [pid for pid, _ in downs(r)][8:] == [8, 9] and r.fingers() == 10
    assert r.engine.action_failures == 2
    assert [f[0] for f in r.engine.recent_failures] == ["F", "F"]
    r.release("F")
    assert r.fingers() == 8 and r.kinds().count(U) == 2


def test_abort_cancels_only_its_own_binding_and_only_its_own_fingers():
    fillers = [b(k, 0.05 + 0.09 * i, 0.95, id=f"f{i}") for i, k in enumerate("1234567")]            # seven pointers taken
    a = b("F", 0.1, 0.1, "hold", id="a")
    c = b("F", 0.5, 0.5, "hold", id="b", extras=[act("hold", 0.6, 0.6), act("hold", 0.7, 0.7)], on_failure="abort")
    d = b("F", 0.9, 0.9, "hold", id="c")
    r = shared([a, c, d] + fillers, {"F": ["a", "b", "c"]})
    for k in "1234567":
        r.press(k)
    r.press("F")
    inj = r.engine.injector
    assert inj.is_down(("bind", "a", 0))                                   # A took pointer 7 and keeps it
    assert not inj.is_down(("bind", "b", 0)) and not inj.is_down(("bind", "b", 1))       # B's two fingers (8, 9) were lifted by its own abort
    assert inj.is_down(("bind", "c", 0))                                   # C, after B in the list, simply got a freed pointer
    assert r.engine.action_failures == 1
    assert all(inj.is_down(("bind", f"f{i}", 0)) for i in range(7))
    assert ev(r)[7:] == [(D, 7), (D, 8), (D, 9), (U, 8), (U, 9), (D, 8)]


def test_continue_in_one_binding_and_abort_in_another_are_independent():
    fillers = [b(k, 0.05 + 0.09 * i, 0.95, id=f"f{i}") for i, k in enumerate("12345678")]            # eight pointers taken
    a = b("F", 0.1, 0.1, "hold", id="a", extras=[act("hold", 0.2, 0.2), act("hold", 0.3, 0.3)], on_failure="continue")
    c = b("F", 0.9, 0.9, "hold", id="b", on_failure="abort")
    r = shared([a, c] + fillers, {"F": ["a", "b"]})
    for k in "12345678":
        r.press(k)
    r.press("F")
    inj = r.engine.injector
    assert inj.is_down(("bind", "a", 0)) and inj.is_down(("bind", "a", 1))        # A's first two actions fit; its third failed and A carried on
    assert r.engine.action_failures >= 1
    assert r.engine.recent_failures[0] == ("F", 2, "no free touch pointer")


def test_a_disconnected_channel_fails_every_participant_and_they_recover_when_it_is_back():
    r = shared([b("F", 0.1, 0.1, id="a"), b("F", 0.9, 0.9, id="b", on_failure="abort")], {"F": ["a", "b"]})
    live = r.control
    r.control = None
    assert r.press("F", 100) is True
    assert r.fingers() == 0 and live.touches() == [] and r.engine.action_failures == 2
    r.control = live
    r.release("F")
    r.press("F")
    assert spots(r) == [px(0.1, 0.1), px(0.9, 0.9)]
    r.release("F")
    assert r.fingers() == 0


# ===================================================================== cleanup
@pytest.mark.parametrize("how", ["release_all", "mapping_off", "screen_changed", "disconnect"])
def test_every_cleanup_path_lifts_every_participant_once_and_old_key_ups_change_nothing(how):
    parts = [b("F", 0.1, 0.1, id="a", extras=[act("toggle", 0.2, 0.2)]), b("F", 0.9, 0.9, "repeat", "b"),
             b("F", 0.5, 0.5, "drag", "c", x2=0.8, y2=0.5)]
    r = shared(parts, {"F": ["a", "b", "c"]})
    r.press("F", 100)
    live = r.control
    if how == "disconnect":
        r.control = None
    {"release_all": r.engine.release_all, "mapping_off": lambda: r.engine.set_mapping_active(False),
     "screen_changed": lambda: (setattr(r, "size", (1080, 2400)), r.engine.screen_changed()), "disconnect": r.engine.release_all}[how]()
    assert r.fingers() == 0
    r.control = live
    r.engine.set_mapping_active(True)
    sent = len(r.touches())
    r.release("F", 3000)
    assert len(r.touches()) == sent and r.fingers() == 0 and r.timers == []
    r.press("F")                                                          # a fresh press starts all three again, cleanly
    assert r.fingers() >= 3
    r.engine.release_all()
    assert r.fingers() == 0


def test_a_rotation_lifts_each_participants_fingers_at_the_new_size():
    r = shared([b("F", 0.1, 0.1, id="a", also=[(0.2, 0.2)]), b("F", 0.9, 0.9, id="b")], {"F": ["a", "b"]})
    r.press("F")
    r.size = (1080, 2400)
    r.engine.screen_changed()
    ups = [t for t in r.touches() if t[0] == U]
    assert sorted(t[1] for t in ups) == [0, 1, 2] and all(t[4:] == (1080, 2400) for t in ups)


# ===================================================================== which keys can be shared
def test_a_shared_modifier_combination_runs_both_and_the_plain_key_is_a_different_trigger():
    r = shared([b("Shift+F", 0.1, 0.1, id="a"), b("Shift+F", 0.9, 0.9, id="b"), b("F", 0.5, 0.5, id="c")], {"Shift+F": ["a", "b"]})
    r.press("F", 100)
    r.release("F")
    assert spots(r) == [px(0.5, 0.5)]                                    # no Shift: the plain binding only
    r.press("Shift")
    r.press("F")
    assert spots(r)[1:] == [px(0.1, 0.1), px(0.9, 0.9)]                  # Shift+F: both sharing bindings, not the plain one
    r.release("Shift")
    assert r.fingers() == 0                                              # releasing a needed modifier ends both


def test_a_group_on_a_combination_given_with_modifiers_in_another_order_is_the_same_trigger():
    p = Profile("p", bindings=[b("Ctrl+Shift+F", 0.1, 0.1, id="a"), b("Ctrl+Shift+F", 0.9, 0.9, id="b")])
    p.share_trigger("Shift+Ctrl+F", ["a", "b"])
    assert p.is_shared("Ctrl+Shift+F") and list(p.shared_triggers) == ["Ctrl+Shift+F"]


def test_mapped_mouse_buttons_can_be_shared_and_the_release_reaches_both():
    r = shared([b("LMB", 0.1, 0.1, id="a"), b("E", 0.9, 0.9, id="b", triggers=["LMB"])], {"LMB": ["a", "b"]})
    assert mouse(r, "LMB", True) is True
    assert spots(r) == [px(0.1, 0.1), px(0.9, 0.9)] and r.fingers() == 2
    assert mouse(r, "LMB", False) is True
    assert r.fingers() == 0 and r.kinds().count(U) == 2


def test_the_wheel_stays_an_impulse_for_every_participant():
    r = shared([b("WheelUp", 0.1, 0.1, "hold", "a"), b("WheelUp", 0.9, 0.9, "repeat", "b"), b("WheelUp", 0.5, 0.5, "drag", "c", x2=0.7, y2=0.5)],
               {"WheelUp": ["a", "b", "c"]})
    assert r.engine.has_wheel_binding() is True and r.engine.wheel_binding(+1) is True
    r.advance(1000)
    assert r.fingers() == 0 and r.timers == []
    assert r.kinds().count(D) == r.kinds().count(U) == 3                    # hold->tap, repeat->tap, drag->swipe
    assert r.engine.wheel_binding(-1) is False


def test_a_stick_key_still_belongs_to_the_stick_even_if_a_group_names_it():
    p = Profile("p", bindings=[b("R", 0.1, 0.1, id="a", triggers=["W"]), b("T", 0.9, 0.9, id="b", triggers=["W"])])
    p.shared_triggers = {"W": ["a", "b"]}                                 # (hand-edited: share_trigger refuses a stick key)
    p.sticks.append(Stick(0.15, 0.7, ramp_ms=0))
    r = Rig()
    r.engine.set_profile(p)
    r.press("W")
    assert r.fingers() == 1 and r.kinds() == [D, M]                         # only the stick moves: the bindings never start
    q = Profile("q", bindings=[b("R", 0.1, 0.1, id="a", triggers=["W"]), b("T", 0.9, 0.9, id="b", triggers=["W"])])
    q.sticks.append(Stick(0.15, 0.7, ramp_ms=0))
    from app.mapping.profile import ProfileError
    with pytest.raises(ProfileError, match="stick key"):
        q.share_trigger("W", ["a", "b"])


def test_a_binding_appears_once_even_if_the_group_lists_it_twice_or_it_lists_the_trigger_in_two_ways():
    a = b("Shift+F", 0.1, 0.1, id="a", triggers=["F"])
    c = b("F", 0.9, 0.9, id="b")
    p = Profile("p", bindings=[a, c])
    p.share_trigger("F", ["a", "a", "b", "b"])
    r = Rig()
    r.engine.set_profile(p)
    r.press("F", 50)
    assert spots(r) == [px(0.1, 0.1), px(0.9, 0.9)]                       # A once (through its extra trigger F), B once
    r.release("F")
    assert r.fingers() == 0


def test_precedence_a_combination_still_beats_the_plain_key_and_the_group_decides_only_its_own_trigger():
    r = shared([b("F", 0.1, 0.1, id="a"), b("F", 0.2, 0.2, id="b"), b("Shift+F", 0.9, 0.9, id="c")], {"F": ["a", "b"]})
    r.press("Shift")
    r.press("F", 50)
    assert spots(r) == [px(0.9, 0.9)]                                     # Shift+F exists: only its binding runs
    r.release("F")
    r.release("Shift")
    r.press("F", 50)
    assert spots(r)[1:] == [px(0.1, 0.1), px(0.2, 0.2)]


# ===================================================================== partial authorization and legacy
def test_a_binding_that_uses_the_trigger_without_being_authorized_never_runs_even_if_it_is_later_in_the_list():
    r = shared([b("F", 0.1, 0.1, id="a"), b("F", 0.2, 0.2, id="b"), b("F", 0.9, 0.9, id="c")], {"F": ["a", "b"]})
    r.press("F", 50)
    assert spots(r) == [px(0.1, 0.1), px(0.2, 0.2)]


def test_a_group_on_extra_triggers_beats_a_first_input_that_is_not_part_of_it():
    r = shared([b("E", 0.9, 0.9, id="x"), b("R", 0.1, 0.1, id="a", triggers=["E"]), b("T", 0.2, 0.2, id="b", triggers=["E"])], {"E": ["a", "b"]})
    r.press("E", 50)
    assert spots(r) == [px(0.1, 0.1), px(0.2, 0.2)]


def test_a_single_binding_match_never_runs_unrelated_bindings_that_merely_exist_in_a_group_elsewhere():
    r = shared([b("F", 0.1, 0.1, id="a"), b("F", 0.2, 0.2, id="b"), b("G", 0.9, 0.9, id="c")], {"F": ["a", "b"]})
    r.press("G", 50)
    assert spots(r) == [px(0.9, 0.9)]


def test_an_authorization_naming_a_binding_that_does_not_use_the_trigger_is_ignored_at_run_time():
    p = Profile("p", bindings=[b("F", 0.1, 0.1, id="a"), b("G", 0.2, 0.2, id="b")])
    p.shared_triggers = {"F": ["a", "b"]}                                  # b never uses F: the group has one real participant
    r = Rig()
    r.engine.set_profile(p)
    r.press("F", 50)
    assert spots(r) == [px(0.1, 0.1)] and not p.sharing_groups()


def test_a_version_1_profile_with_repeated_inputs_keeps_running_only_the_winner(tmp_path):
    from app.mapping.profile import ProfileStore
    (tmp_path / "old.json").write_text(
        '{"version": 1, "name": "old", "bindings": [{"input": "R", "x": 0.1, "y": 0.1, "mode": "hold"},'
        ' {"input": "R", "x": 0.9, "y": 0.9, "mode": "hold"}]}', encoding="utf-8")
    store = ProfileStore(tmp_path)
    p = store.load("old")
    assert len(p.bindings) == 2 and p.notes and p.shared_triggers == {}
    r = Rig()
    r.engine.set_profile(p)
    r.press("R", 50)
    assert spots(r) == [px(0.9, 0.9)]
    store.save(p)                                                           # converting it to version 2 must not authorize anything
    q = store.load("old")
    assert q.shared_triggers == {} and q.schema_version == 2
    r2 = Rig()
    r2.engine.set_profile(q)
    r2.press("R", 50)
    assert spots(r2) == [px(0.9, 0.9)]


# ===================================================================== analyzer and runtime agree
def agree_cases():
    def mk(*specs, groups=None):
        p = Profile("p")
        p.bindings = [Binding(k, 0.1 * (i + 1), 0.5, "hold", id=f"b{i}", extra_triggers=list(extra)) for i, (k, extra) in enumerate(specs)]
        for t, ids in (groups or {}).items():
            p.share_trigger(t, ids)
        return p
    return [
        mk(("F", ()), ("F", ())), mk(("F", ()), ("F", ()), groups={"F": ["b0", "b1"]}),
        mk(("F", ()), ("F", ()), ("F", ()), groups={"F": ["b0", "b1"]}), mk(("F", ()), ("F", ()), ("F", ()), groups={"F": ["b0", "b1", "b2"]}),
        mk(("E", ("F",)), ("F", ())), mk(("E", ("F",)), ("F", ()), groups={"F": ["b0", "b1"]}),
        mk(("E", ("F", "G")), ("R", ("F", "G")), ("T", ("G",)), groups={"G": ["b1", "b2"]}),
        mk(("Shift+F", ()), ("Shift+F", ()), ("F", ()), groups={"Shift+F": ["b0", "b1"]}),
        mk(("LMB", ()), ("E", ("LMB",)), ("LMB", ()), groups={"LMB": ["b0", "b1", "b2"]}),
    ]


@pytest.mark.parametrize("index", range(9))
def test_the_analyzer_and_the_engine_agree_on_which_bindings_run_for_every_trigger(index):
    p = agree_cases()[index]
    issues = p.analyze()
    for trigger, runners in p.runners().items():
        r = Rig()
        r.engine.set_profile(p)
        mods, _, base = trigger.rpartition("+")
        for m in filter(None, mods.split("+")):
            r.press(m)
        if base in ("LMB", "RMB", "MMB"):
            mouse(r, base, True)
        else:
            r.press(base)
        ran = {b.id for b in p.bindings if any(t[2:4] == px(b.x, b.y) and t[0] == D for t in r.touches())}
        assert ran == {b.id for b in runners}, trigger
        users = [b for b in p.bindings if trigger in {make for make in [t for t in b.triggers]}]
        not_running = {b.id for b in users} - ran
        flagged = {i.binding_ids[0] for i in issues if i.trigger == trigger and i.code in
                   ("shadowed_by_later_binding", "shared_trigger_not_executed")}
        flagged |= {i.binding_ids[-1] for i in issues if i.trigger == trigger and i.code == "shared_trigger_partially_authorized"}
        assert flagged == not_running, (trigger, flagged, not_running)
        if len(users) > 1 and not_running == set() and len(ran) > 1:
            assert any(i.code == "shared_trigger" and i.trigger == trigger for i in issues)


# ===================================================================== v2 file with sharing, run end to end
def test_a_version_2_file_with_a_sharing_list_runs_both_bindings():
    p = Profile.from_dict({"version": 2, "name": "v2", "bindings": [
        {"type": "binding", "id": "fire", "triggers": ["F"], "on_failure": "continue", "actions": [{"mode": "hold", "x": 0.1, "y": 0.1}]},
        {"type": "binding", "id": "use", "triggers": ["F"], "on_failure": "continue", "actions": [{"mode": "tap", "x": 0.9, "y": 0.9}]}],
        "shared_triggers": [{"trigger": "F", "bindings": ["fire", "use"]}]})
    r = Rig()
    r.engine.set_profile(p)
    r.press("F", 100)
    assert spots(r) == [px(0.1, 0.1), px(0.9, 0.9)]
    r.release("F")
    assert r.fingers() == 0
