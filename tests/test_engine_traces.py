"""The input engine must produce exactly the control messages it always produced (Phase 2, step 4).

`golden/engine_traces.json` was recorded from the engine as it was BEFORE its running-action bookkeeping was refactored (engine.py
checksum 32366b2e568c87a6a97c9702acae1053). Every scenario in engine_traces.py is replayed here and must give the identical
trace: the same control messages as bytes, at the same instants, the same return values, finger counts, scope flag and overlay
highlights after every step. A pointer id, a timing, an ordering or a missing release that changes anywhere makes a test fail.

Five golden files exist; never regenerate any but the last, they are the references it is compared with:
  engine_traces_before_multi_trigger.json   the BASELINE: exactly what was recorded from the legacy engine (173 scenarios).
  engine_traces_before_multi_action.json    the golden file as it was after the multi-trigger step (228 scenarios).
  engine_traces_before_shared_triggers.json the golden file after the multi-action step and its hardening (285 scenarios).
  engine_traces_before_shared_highlight_fix.json  the file of the shared-trigger step (341 scenarios, the shared-binding ones included).
  engine_traces.json                        the CURRENT file (the shared-binding scenarios are the only addition to the one above). Against the baseline: one scenario removed (additional triggers used to
                                            be inert) and the multi-trigger / multi-action scenarios added. Against the multi-trigger
                                            file: one scenario removed and one changed (additional actions used to be inert) and the
                                            multi-action scenarios added. Everything else is identical, which is tested here.
When engine BEHAVIOUR is meant to change again, regenerate engine_traces.json with `python tests/engine_traces.py`, compare it
with both references (the tests below list what differs), and say which scenarios changed and why."""
import json

import pytest

from app.mapping.profile import MODES
from app.stream import protocol
import engine_traces as traces

GOLDEN = json.loads(traces.GOLDEN.read_text(encoding="utf-8"))
BASELINE = json.loads((traces.GOLDEN.parent / "engine_traces_before_multi_trigger.json").read_text(encoding="utf-8"))
STEP5 = json.loads((traces.GOLDEN.parent / "engine_traces_before_multi_action.json").read_text(encoding="utf-8"))
STEP6 = json.loads((traces.GOLDEN.parent / "engine_traces_before_shared_triggers.json").read_text(encoding="utf-8"))
STEP7 = json.loads((traces.GOLDEN.parent / "engine_traces_before_shared_highlight_fix.json").read_text(encoding="utf-8"))
SCENARIOS = traces.scenarios()

# The ONLY intentional differences between the BASELINE (legacy engine) and the current golden file:
REMOVED = {"inert/extra-triggers-and-actions-are-ignored":
           "pressed F and MMB, extra triggers of E, and expected nothing to happen; they run E's action since the multi-trigger step "
           "(see multi-trigger/extra-triggers-and-extra-actions-together, same bindings and steps)"}
ADDED_PREFIXES = ("multi-trigger/", "multi-action/", "shared-binding/")

# The ONLY intentional differences between the golden file of the multi-trigger step (STEP5) and the current one (multi-action step):
REMOVED_IN_STEP6 = {"inert/extra-actions-are-ignored":
                    "a hold with additional actions (a tap after 60 ms, a hold) expected only the first action; the additional actions "
                    "run now (replaced by the multi-action/ scenarios)"}
CHANGED_IN_STEP6 = {"multi-trigger/extra-triggers-and-extra-actions-together":
                    "E (hold) with additional triggers F and MMB and an additional tap after 60 ms: the trace used to contain only the hold, "
                    "it now also contains the delayed tap each time the binding is activated"}
ADDED_IN_STEP6 = ("multi-action/", "shared-binding/")


# The ONLY intentional difference between the file of the shared-trigger step (STEP7) and the current one (editor step): the
# `highlights` of 36 shared-binding scenarios. Controls that share a key are all named after it; when a quick one (a tap) ended, the
# old engine switched the shared marker name off although a longer one (a hold) was still showing, so the held control's marker went
# dark while its finger was down. Now the name stays lit until the LAST control showing it has finished. No control message, return
# value, finger count or scope flag changed in any scenario, and nothing outside shared-binding/ changed.
HIGHLIGHT_FIX = "marker of a shared key stays lit while any control on it still shows"


def normalised(trace):
    return json.loads(json.dumps(trace))


def test_the_golden_file_and_the_scenario_library_describe_the_same_scenarios():
    assert set(GOLDEN) == set(SCENARIOS)


def test_the_baseline_is_untouched_and_the_current_golden_differs_from_it_only_as_documented():
    assert len(BASELINE) == 173 and sum(len(t["messages"]) for t in BASELINE.values()) == 3934
    assert set(BASELINE) - set(GOLDEN) == set(REMOVED)
    added = set(GOLDEN) - set(BASELINE)
    assert added and all(n.startswith(ADDED_PREFIXES) for n in added), sorted(added)


def test_the_multi_trigger_golden_file_is_untouched_and_the_current_one_differs_from_it_only_as_documented():
    assert len(STEP5) == 228 and sum(len(t["messages"]) for t in STEP5.values()) == 4776
    assert set(STEP5) - set(GOLDEN) == set(REMOVED_IN_STEP6)
    added = set(GOLDEN) - set(STEP5)
    assert added and all(n.startswith(ADDED_IN_STEP6) for n in added), sorted(added)
    changed = {n for n in set(STEP5) & set(GOLDEN) if STEP5[n] != GOLDEN[n]}
    assert changed == set(CHANGED_IN_STEP6), sorted(changed ^ set(CHANGED_IN_STEP6))


def test_the_shared_trigger_step_changed_no_existing_trace_and_only_added_shared_binding_scenarios():
    assert len(STEP6) == 285 and sum(len(t["messages"]) for t in STEP6.values()) == 6128
    assert set(STEP6) <= set(GOLDEN)                                             # nothing removed
    assert [n for n in STEP6 if STEP6[n] != GOLDEN[n]] == []                     # nothing changed
    added = set(GOLDEN) - set(STEP6)
    assert added and all(n.startswith("shared-binding/") for n in added), sorted(added)


def test_the_editor_step_changed_only_the_highlights_of_shared_binding_scenarios():
    assert len(STEP7) == 341 and sum(len(t["messages"]) for t in STEP7.values()) == 7054
    assert set(STEP7) == set(GOLDEN)
    changed = sorted(n for n in STEP7 if STEP7[n] != GOLDEN[n])
    assert changed and len(changed) == 36 and all(n.startswith("shared-binding/") for n in changed)
    for name in changed:
        assert [key for key in STEP7[name] if STEP7[name][key] != GOLDEN[name][key]] == ["highlights"], name
        assert len(STEP7[name]["highlights"]) == len(GOLDEN[name]["highlights"]), name          # (the same notifications, other content)
        for old, new in zip(STEP7[name]["highlights"], GOLDEN[name]["highlights"]):
            assert set(old) <= set(new), name                                                   # a name is only ever kept lit, never lost
    assert all(STEP7[n] == GOLDEN[n] for n in STEP7 if not n.startswith("shared-binding/"))


@pytest.mark.parametrize("name", sorted(STEP6))
def test_the_engine_still_produces_every_trace_of_the_multi_action_step_unchanged(name):
    trace = normalised(traces.run(SCENARIOS[name]))
    ref = STEP6[name]
    assert trace["messages"] == ref["messages"], f"{name}: control messages differ from the multi-action engine"
    assert (trace["returns"], trace["after"], trace["highlights"]) == (ref["returns"], ref["after"], ref["highlights"])


@pytest.mark.parametrize("name", sorted(set(STEP5) - set(REMOVED_IN_STEP6) - set(CHANGED_IN_STEP6)))
def test_the_engine_still_produces_every_multi_trigger_trace_unchanged(name):
    trace = normalised(traces.run(SCENARIOS[name]))
    ref = STEP5[name]
    assert trace["messages"] == ref["messages"], f"{name}: control messages differ from the multi-trigger engine"
    assert (trace["returns"], trace["after"], trace["highlights"]) == (ref["returns"], ref["after"], ref["highlights"])


def test_every_legacy_trace_is_identical_to_the_baseline():
    legacy = set(BASELINE) - set(REMOVED)
    changed = sorted(n for n in legacy if GOLDEN[n] != BASELINE[n])
    assert changed == [], f"traces that differ from the baseline: {changed}"
    assert len(legacy) == 172


@pytest.mark.parametrize("name", sorted(set(BASELINE) - set(REMOVED)))
def test_the_engine_still_produces_every_legacy_trace_of_the_baseline(name):
    trace = normalised(traces.run(SCENARIOS[name]))
    base = BASELINE[name]
    assert trace["messages"] == base["messages"], f"{name}: control messages differ from the legacy engine"
    assert (trace["returns"], trace["after"], trace["highlights"]) == (base["returns"], base["after"], base["highlights"])


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_engine_still_produces_the_recorded_trace(name):
    trace = normalised(traces.run(SCENARIOS[name]))
    golden = GOLDEN[name]
    assert trace["messages"] == golden["messages"], f"{name}: control messages differ"
    assert trace["returns"] == golden["returns"] and trace["after"] == golden["after"]
    assert trace["highlights"] == golden["highlights"]


def test_replaying_twice_gives_the_same_trace():
    name = "busy/release_all"
    assert normalised(traces.run(SCENARIOS[name])) == normalised(traces.run(SCENARIOS[name]))


def test_the_library_covers_every_mode_in_every_situation_and_the_cleanup_paths():
    for mode in MODES:
        for situation in ("held-then-released", "released-at-once", "release_all-after-20ms", "on-the-wheel", "scope-button",
                          "mapped-mouse-button", "with-extra-spots", "modifier-combination", "rotation", "mapping-off", "dead-connection"):
            assert f"{mode}/{situation}" in SCENARIOS
    for name in ("busy/release_all", "busy/mapping-off", "busy/rotation", "busy/disconnect", "busy/refresh",
                 "stick/wasd-sprint-walk-autorun", "look/flicks-idle-and-edges", "combos/every-modifier-order",
                 "fingers/ten-holds-then-an-eleventh", "shared/same-first-input-the-last-one-wins",
                 "multi-trigger/hold/second-key-joins-first-released-first",
                 "multi-trigger/toggle-over-several-triggers", "multi-trigger/busy-session-cleanup",
                 "multi-action/mixed-hold-toggle-delayed-tap-and-reactivations", "multi-action/two-scope-holds-released-in-both-orders",
                 "multi-action/pointer-exhaustion-on-failure-abort", "multi-action/cleanup-rotation-with-pending-and-active-actions",
                 "shared-binding/a-with-e-and-f-b-with-f/e-then-f-released-f-first", "shared-binding/unauthorized-duplicates-keep-the-winner",
                 "shared-binding/abort-in-one-binding-only", "shared-binding/cleanup-rotation"):
        assert name in SCENARIOS


def test_the_traces_are_substantial_not_empty_formalities():
    total = sum(len(t["messages"]) for t in GOLDEN.values())
    assert len(GOLDEN) >= 330 and total >= 7000
    kinds = {bytes.fromhex(message[1])[1] for t in GOLDEN.values() for message in t["messages"]
             if bytes.fromhex(message[1])[0] == protocol.TYPE_INJECT_TOUCH_EVENT}
    assert kinds == {protocol.TOUCH_DOWN, protocol.TOUCH_UP, protocol.TOUCH_MOVE}
    types = {bytes.fromhex(message[1])[0] for t in GOLDEN.values() for message in t["messages"]}
    assert protocol.TYPE_INJECT_TOUCH_EVENT in types and len(types) >= 2          # touches plus the back / home / scroll messages


def test_every_scenario_that_should_end_with_nothing_touching_does():
    # Scenarios named like this finish with every finger up; a toggle left on is the one deliberate exception.
    for name, trace in GOLDEN.items():
        if name.startswith(("busy/", "fingers/", "multi-action/", "shared-binding/")) or "toggle/" in name or "mapped-mouse" in name or name.startswith(("look/", "direct/")):
            continue                  # (multi-action scenarios end with a toggle on on purpose; test_multi_action.py asserts their cleanup)
        if any(token in name for token in ("held-then-released", "released-at-once", "release_all", "rotation", "mapping-off",
                                           "dead-connection", "pressed-three-times", "pressed-again")):
            assert trace["after"][-1][0] == 0, name
