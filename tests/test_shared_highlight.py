"""The marker highlight of controls that SHARE a key. Controls on one authorized trigger are all named after it, so their markers share
one name; the overlay must keep that marker lit for as long as ANY of them is still showing, and put it out only when the last one has
finished. (Found while testing the editor on the simulated phone: a quick tap sharing a key with a hold switched the name off and the held
control's marker went dark while its finger was down.) Only the highlight is concerned; no control message changes."""
import pytest

from app.mapping.profile import Action, Binding, Profile
from test_characterization_engine import SPECS, Rig


def shared(*bindings, group=None):
    r = Rig()
    p = Profile("p")
    p.bindings = list(bindings)
    ids = group or [b.id for b in bindings]
    p.share_trigger("F", ids)
    r.engine.set_profile(p)
    r.profile = p
    return r


def b(mode, x, y, id, **kw):
    return Binding("F", x, y, mode, id=id, **{**SPECS.get(mode, {}), **kw})


def lit(r):
    return r.pressed_log[-1] if r.pressed_log else frozenset()


def test_a_hold_stays_lit_after_a_tap_on_the_same_key_has_finished():
    r = shared(b("hold", 0.1, 0.1, "fire"), b("tap", 0.9, 0.9, "interact"))
    r.press("F", 400)                                  # the tap is long over, the key is still down
    assert r.fingers() == 1 and lit(r) == frozenset({"F"})


def test_it_goes_out_when_the_last_one_finishes_and_not_before():
    r = shared(b("hold", 0.1, 0.1, "fire"), b("hold", 0.9, 0.9, "other"), b("tap", 0.5, 0.5, "tap"))
    r.press("F", 300)
    assert lit(r) == frozenset({"F"})
    r.release("F", 10)
    assert lit(r) == frozenset() and r.fingers() == 0


def test_the_name_stays_lit_while_a_toggle_of_the_group_is_on():
    r = shared(b("tap", 0.1, 0.1, "tap"), b("toggle", 0.9, 0.9, "toggle"))
    r.press("F", 300)
    r.release("F", 300)
    assert r.fingers() == 1 and lit(r) == frozenset({"F"})          # the toggle is on: its marker stays lit though the tap ended
    r.press("F", 50)
    r.release("F", 300)
    assert r.fingers() == 0 and lit(r) == frozenset()


def test_a_binding_that_aborts_does_not_switch_off_the_marker_of_the_others():
    fillers = [Binding(str(i), 0.05 + 0.09 * i, 0.95, "hold", id=f"f{i}") for i in range(1, 8)]
    r = Rig()
    p = Profile("p")
    p.bindings = [b("hold", 0.1, 0.1, "ok"), Binding("F", 0.5, 0.5, "hold", id="bad", on_failure="abort",
                                                     extra_actions=[Action("hold", 0.6, 0.6), Action("hold", 0.7, 0.7)]), *fillers]
    p.share_trigger("F", ["ok", "bad"])
    r.engine.set_profile(p)
    for i in range(1, 8):
        r.press(str(i), 5)
    r.press("F", 100)
    assert r.engine.action_failures == 1
    assert r.engine.injector.is_down(("bind", "ok", 0)) and not r.engine.injector.is_down(("bind", "bad", 0))
    assert "F" in lit(r) and r.fingers() == 8                     # "bad" was cancelled, "ok" still holds its finger AND its marker
    r.release("F", 10)
    assert "F" not in lit(r) and r.fingers() == 7


def test_release_all_clears_every_marker_of_a_shared_key():
    r = shared(b("hold", 0.1, 0.1, "fire"), b("tap", 0.9, 0.9, "interact"))
    r.press("F", 400)
    r.engine.release_all()
    assert lit(r) == frozenset() and r.fingers() == 0


def test_controls_on_different_keys_are_unchanged():
    r = Rig([Binding("A", 0.1, 0.1, "hold"), Binding("B", 0.9, 0.9, "tap")])
    r.press("A", 10)
    r.press("B", 400)
    assert lit(r) == frozenset({"A"})
    r.release("A", 10)
    assert lit(r) == frozenset()


def test_unauthorized_duplicates_still_light_only_the_winner():
    r = Rig()
    p = Profile("p")
    p.bindings = [b("hold", 0.1, 0.1, "a"), b("tap", 0.9, 0.9, "b")]
    r.engine.set_profile(p)
    r.press("F", 400)
    assert r.fingers() == 0 and r.pressed_log == [frozenset({"F"}), frozenset()]          # only the winner (the later one, a tap) ran, and is over
    r.release("F", 10)
    assert lit(r) == frozenset()


def test_the_number_of_notifications_is_what_it_always_was():
    r = shared(b("hold", 0.1, 0.1, "fire"), b("tap", 0.9, 0.9, "interact"))
    r.press("F", 400)
    r.release("F", 10)
    assert len(r.pressed_log) >= 4 and r.pressed_log[0] == frozenset({"F"})
