"""The engine's running-action bookkeeping after the refactor (Phase 2, step 4).

What changed inside: running actions are keyed by the binding's runtime id instead of by the input that started them, and the
fingers of a binding are named ("bind", binding id, action index, ...). What did NOT change is anything visible from outside:
test_engine_traces.py proves that byte for byte. These tests pin the new internal contract the multi-trigger step builds on, and
that a version-2 profile (extra triggers, extra actions, shared inputs) is still executed exactly like its first trigger and
first action, as before."""
import json

import pytest

from app.input.engine import DIRECT_OWNER, PRIMARY, InputEngine
from app.mapping import profile as profile_module
from app.mapping.profile import Action, Binding, Profile, Stick
from test_characterization_engine import Rig, bound
import engine_traces as traces


def owner(binding, *more):
    return ("bind", binding.id, PRIMARY, *more)


# ===================================================================== fingers are named per binding and per action
def test_the_fingers_of_a_binding_are_named_after_the_binding_and_its_action_not_after_the_input():
    r = Rig([Binding("K", 0.5, 0.5, "hold", also=[(0.1, 0.1), (0.2, 0.2)]), bound("Z", "pinch-out")])
    k, z = r.profile.find("K"), r.profile.find("Z")
    r.press("K")
    r.press("Z", 20)
    inj = r.engine.injector
    assert inj.is_down(owner(k)) and inj.is_down(owner(k, "also", 0)) and inj.is_down(owner(k, "also", 1))
    assert inj.is_down(owner(z, "a")) and inj.is_down(owner(z, "b")) and not inj.is_down(owner(z))
    assert not inj.is_down(("input", "K")) and inj.active_count == 5


def test_every_kind_of_finger_has_its_own_name_that_can_never_equal_a_bindings():
    r = Rig([Binding("G", 0.1, 0.9, "hold")], stick=Stick(0.15, 0.7, ramp_ms=0), look=traces.Look(0.5, 0.2, 0.9, 0.8))
    r.press("G")
    r.press("W")
    r.engine.mouse_button("LMB", True, (0.9, 0.1))
    r.engine.set_look_captured(False)
    r.engine.set_look_captured(True)
    r.engine.look_delta(5, 0)
    r.advance(10)
    inj = r.engine.injector
    names = [owner(r.profile.find("G")), ("stick", 0), DIRECT_OWNER, ("look", 0)]
    assert all(inj.is_down(n) for n in names) and inj.active_count == 4
    assert all(n[0] != "bind" for n in names[1:])


def test_releasing_one_binding_lifts_only_its_own_fingers_by_name():
    r = Rig([Binding("A", 0.1, 0.1, "hold", also=[(0.2, 0.2)]), Binding("B", 0.3, 0.3, "hold")])
    a, b = r.profile.find("A"), r.profile.find("B")
    r.press("A")
    r.press("B")
    r.release("A")
    inj = r.engine.injector
    assert not inj.is_down(owner(a)) and not inj.is_down(owner(a, "also", 0)) and inj.is_down(owner(b))


def test_bindings_that_wrongly_share_a_runtime_id_never_share_fingers():
    # Only a hand-edited list can do this (Profile.add and loading make ids unique); the engine must not let it matter.
    p = Profile("p", bindings=[Binding("K", 0.2, 0.2, "hold"), Binding("L", 0.8, 0.8, "hold")])
    p.bindings[1].id = p.bindings[0].id
    r = Rig()
    r.profile = p
    r.engine.set_profile(p)
    r.press("K")
    r.press("L")
    assert r.fingers() == 2 and [t[:2] for t in r.touches()] == [(0, 0), (0, 1)]
    r.release("K")
    assert r.fingers() == 1
    r.release("L")
    assert r.fingers() == 0


def test_the_engine_never_changes_the_profile_it_runs():
    p = Profile("p", bindings=[Binding("K", 0.2, 0.2, "hold"), Binding("L", 0.8, 0.8, "tap")])
    p.bindings[1].id = p.bindings[0].id                                # even with duplicate ids it does not repair them
    before = ([b.id for b in p.bindings], json.dumps(p.bindings[0].__dict__, default=str))
    r = Rig()
    r.profile = p
    r.engine.set_profile(p)
    r.press("K", 100)
    r.press("L", 100)
    r.engine.refresh_profile()
    r.engine.release_all()
    assert ([b.id for b in p.bindings], json.dumps(p.bindings[0].__dict__, default=str)) == before


def test_a_running_binding_is_recognised_by_its_key_whatever_started_it():
    # The seam multi-trigger execution is built on: starting the SAME binding again from another trigger joins its run
    # instead of starting a second one (CHANGED in step 5: in step 4 the second start was simply ignored).
    r = Rig([Binding("K", 0.5, 0.5, "hold")])
    binding = r.profile.find("K")
    r.engine._start(binding, "K", {"K"}, impulse=False)
    r.engine._start(binding, "SomethingElse", {"SomethingElse"}, impulse=False)
    assert r.kinds() == [0] and r.fingers() == 1
    r.release("K")
    assert r.fingers() == 1                                              # "SomethingElse" still sustains it
    r.release("SomethingElse")
    assert r.fingers() == 0


def test_a_profile_refresh_gives_every_binding_a_key_and_lifts_what_was_running():
    r = Rig([Binding("K", 0.5, 0.5, "hold", also=[(0.1, 0.1)])])
    r.press("K")
    r.engine.refresh_profile()
    assert r.fingers() == 0 and r.kinds() == [0, 0, 1, 1]
    r.press("K")
    assert r.fingers() == 2


# ===================================================================== version-2 profiles: extra triggers run, extra actions do not
V2 = {"id": "abcabcabcabc", "version": 2, "name": "v2", "orientation": "any", "package": "", "sticks": [], "looks": [], "bindings": [
    {"type": "binding", "id": "interact", "label": "Interact", "triggers": ["E", "F", "MMB"],
     "actions": [{"mode": "hold", "x": 0.3, "y": 0.4}, {"mode": "tap", "x": 0.8, "y": 0.8, "delay_ms": 60},
                 {"mode": "repeat", "x": 0.6, "y": 0.6, "rate_hz": 10}], "on_failure": "abort"},
    {"type": "binding", "id": "reload", "label": "Reload", "triggers": ["R", "Shift+R"],
     "actions": [{"mode": "tap", "x": 0.7, "y": 0.9, "also": [[0.1, 0.1]]}], "on_failure": "continue"}]}

SCRIPT = [["press", "F", 100], ["release", "F", 100], ["mouse", "MMB", True, [0.5, 0.5], 100], ["mouse", "MMB", False, [0.5, 0.5], 100],
          ["press", "Shift"], ["press", "R", 100], ["release", "R", 100], ["release", "Shift", 100],
          ["press", "E", 500], ["release", "E", 500], ["press", "R", 100], ["release", "R", 500], ["release_all"], ["advance", 500]]


def trace_of(profile):
    rig_spec = {"bindings": [], "steps": SCRIPT}
    original = traces.build_profile
    try:
        traces.build_profile = lambda _spec: profile
        return json.loads(json.dumps(traces.run(rig_spec)))
    finally:
        traces.build_profile = original


def test_a_version_2_profile_runs_its_triggers_and_all_of_its_actions():
    rich = Profile.from_dict(V2)
    assert [b.needs_v2 for b in rich.bindings] == [True, True] and rich.bindings[0].triggers == ("E", "F", "MMB")
    without_actions = Profile.from_dict(V2)
    for b in without_actions.bindings:
        b.extra_actions.clear()                                          # same triggers, no additional actions
    got, expected = trace_of(rich), trace_of(without_actions)
    assert got["messages"] != expected["messages"]                       # (CHANGED in step 6: the additional actions run now)
    first_only = [m for m in got["messages"] if m in expected["messages"]]
    assert first_only == expected["messages"]                            # the first action's messages are all there, unchanged
    assert len(got["messages"]) > len(expected["messages"]) > 8


def test_two_bindings_sharing_an_input_in_a_version_2_file_still_run_only_the_last_one():
    raw = {**V2, "bindings": [
        {"type": "binding", "id": "a", "label": "", "triggers": ["LMB"], "actions": [{"mode": "hold", "x": 0.1, "y": 0.1}], "on_failure": "continue"},
        {"type": "binding", "id": "b", "label": "", "triggers": ["LMB"], "actions": [{"mode": "hold", "x": 0.9, "y": 0.9}], "on_failure": "continue"}]}
    p = Profile.from_dict(raw)
    r = Rig()
    r.profile = p
    r.engine.set_profile(p)
    r.engine.mouse_button("LMB", True, None)
    assert r.fingers() == 1 and r.touches()[0][2:4] == (2160, 972)
    r.engine.mouse_button("LMB", False, None)
    assert r.fingers() == 0


def test_the_analyzer_says_what_the_engine_does_and_does_not_execute():
    assert profile_module.RUNTIME_SUPPORTS == frozenset({"extra_triggers", "extra_actions", "shared_triggers"})   # what the engine really runs
    issues = {i.code for i in Profile.from_dict(V2).analyze()}
    assert "extra_actions_not_executed" not in issues and "extra_triggers_not_executed" not in issues
    nothing = {i.code for i in Profile.from_dict(V2).analyze(runtime_supports=frozenset())}
    assert {"extra_actions_not_executed", "extra_triggers_not_executed"} <= nothing
    raw = {**V2, "bindings": [{**V2["bindings"][1], "id": "a", "triggers": ["LMB"]}, {**V2["bindings"][1], "id": "b", "triggers": ["LMB"]}]}
    assert "shadowed_by_later_binding" in {i.code for i in Profile.from_dict(raw).analyze()}


# ===================================================================== no dead entries are left behind
@pytest.mark.parametrize("mode", list(__import__("test_characterization_engine").SPECS))
def test_nothing_is_left_in_the_running_table_once_an_action_has_ended_or_been_cancelled(mode):
    r = Rig([bound("K", mode)])
    r.press("K")
    r.release("K")
    if mode == "toggle":
        r.press("K")
        r.release("K")
    r.advance(3000)
    assert r.engine._runs == {} and r.fingers() == 0                # ended by itself or by its key
    r.press("K", 20)
    r.engine.release_all()
    assert r.engine._runs == {} and r.fingers() == 0                # cancelled
