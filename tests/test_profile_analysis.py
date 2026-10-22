"""Profile.analyze() (Phase 2, step 3): structured, deterministic, read-only findings about a profile.

Each issue code has a scenario below; `test_every_code_the_source_can_emit_has_a_scenario` reads the source so a new code
cannot be added without a test. Constructors reject most bad values, so several scenarios damage a valid object afterwards
(as an editor bug, a script or a hand edit could): analyze() must cope with such objects, not only with valid ones."""
import copy
import json
import re
from pathlib import Path

import pytest

from app.hotkeys import DEFAULT_HOTKEYS, HOTKEY_LABELS
from app.mapping import profile as profile_module
from app.mapping.profile import (MAX_TOUCH_POINTERS, SEVERITIES, Action, Binding, Issue, Look, Profile, Stick)
from app.stream import protocol

RESERVED = {key: HOTKEY_LABELS[action] for action, key in DEFAULT_HOTKEYS.items()}
GOLDEN_V1 = Path(__file__).parent / "golden" / "profile_v1.json"


def codes(profile, **kw):
    return [i.code for i in profile.analyze(**kw)]


def issue(profile, code, **kw):
    found = [i for i in profile.analyze(**kw) if i.code == code]
    assert found, f"no {code} in {codes(profile, **kw)}"
    return found[0]


def two_looks_profile():
    p = Profile("p")
    p.looks += [Look(0.5, 0.2, 0.9, 0.8), Look(0.1, 0.2, 0.3, 0.8)]
    return p


# ---- one scenario per code: (code, severity, builder, analyze kwargs) ----------------------------------------------
def s_missing_id():
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1)])
    p.bindings[0].id = ""
    return p


def s_invalid_id():
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1)])
    p.bindings[0].id = "has space"
    return p


def s_duplicate_id():
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1), Binding("B", 0.2, 0.2)])
    p.bindings[1].id = p.bindings[0].id
    return p


def s_invalid_trigger():
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1)])
    p.bindings[0].extra_triggers.append("Ctrl+Shift")
    return p


def s_duplicate_trigger():
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1, extra_triggers=["F"])])
    p.bindings[0].extra_triggers.append("A")
    return p


def s_failure_policy():
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1)])
    p.bindings[0].on_failure = "explode"
    return p


def s_extra_triggers():
    return Profile("p", bindings=[Binding("A", 0.1, 0.1, extra_triggers=["F"])])


def s_extra_trigger_shared():
    return Profile("p", bindings=[Binding("E", 0.1, 0.1), Binding("R", 0.9, 0.9, extra_triggers=["E"])])


def s_extra_actions():
    return Profile("p", bindings=[Binding("A", 0.1, 0.1, extra_actions=[Action("tap", 0.5, 0.5)])])


def s_same_spot_actions():
    return Profile("p", bindings=[Binding("A", 0.5, 0.5, extra_actions=[Action("tap", 0.502, 0.5)])])


def s_shared():
    """Two bindings on LMB with the sharing AUTHORIZED: both run."""
    p = Profile("p", bindings=[Binding("LMB", 0.8, 0.7, "hold")])
    p.add_shared(Binding("LMB", 0.5, 0.2, "tap"), authorize=True)
    return p


def s_shared_same_spot():
    p = Profile("p", bindings=[Binding("LMB", 0.8, 0.7, "hold")])
    p.add_shared(Binding("LMB", 0.8, 0.7, "tap"), authorize=True)
    return p


def s_shared_two_scopes():
    p = Profile("p", bindings=[Binding("RMB", 0.9, 0.5, "hold", scope=True)])
    p.add_shared(Binding("RMB", 0.1, 0.1, "tap", scope=True), authorize=True)
    return p


def s_trigger_pointers():
    p = Profile("p")
    for i in range(3):
        p.add_shared(Binding("LMB", 0.1 * (i + 1), 0.5, "hold", also=[(0.1 * (i + 1), 0.6), (0.1 * (i + 1), 0.7), (0.1 * (i + 1), 0.8)]),
                     authorize=True)
    return p


def s_unshared():
    """The SAME two bindings on LMB, but sharing was never authorized: only the later one runs."""
    p = Profile("p", bindings=[Binding("LMB", 0.8, 0.7, "hold")])
    p.add_shared(Binding("LMB", 0.5, 0.2, "tap"))
    return p


def s_shadowed():
    return s_unshared()


def s_partial():
    """A and B share E on purpose; C also uses E but was never authorized."""
    p = Profile("p", bindings=[Binding("E", 0.1, 0.1, id="a"), Binding("E", 0.2, 0.2, id="b")])
    p.add_shared(Binding("E", 0.3, 0.3, id="c"))
    p.share_trigger("E", ["a", "b"])
    return p


def s_stale_sharing():
    p = Profile("p", bindings=[Binding("E", 0.1, 0.1, id="a"), Binding("E", 0.2, 0.2, id="b")])
    p.shared_triggers = {"E": ["a", "ghost"]}
    return p


def s_sharing_unsupported():
    return s_shared()


def s_smart_loot_invalid():
    return Profile("p", smart_loot={"key": "F"})


def s_smart_loot_key_conflict():
    return Profile("p", bindings=[Binding("F", 0.1, 0.1, id="a")],
                   smart_loot={"key": "F", "toggle": [0.74, 0.25], "row": [0.69, 0.35], "count": 3})


def s_stick_invalid():
    p = Profile("p")
    p.set_stick(Stick(0.15, 0.7))
    del p.sticks[0].keys["up"]
    return p


def s_stick_duplicate():
    p = Profile("p")
    p.set_stick(Stick(0.15, 0.7))
    p.sticks[0].keys["left"] = "W"
    return p


def s_stick_shared():
    p = Profile("p")
    p.set_stick(Stick(0.15, 0.7))
    p.sticks.append(Stick(0.8, 0.7, keys={"up": "W", "left": "J", "down": "K", "right": "L"}))
    return p


def s_stick_is_hotkey():
    p = Profile("p")
    p.set_stick(Stick(0.15, 0.7, sprint_key="F9"))
    return p


def s_trigger_is_stick_key():
    p = Profile("p")
    p.set_stick(Stick(0.15, 0.7))
    p.bindings.append(Binding("W", 0.5, 0.5))                  # Profile.set_stick would have removed it; this bypasses it
    return p


def s_trigger_is_hotkey():
    return Profile("p", bindings=[Binding("F9", 0.5, 0.5), Binding("Esc", 0.4, 0.4)])


def s_combination():
    return Profile("p", bindings=[Binding("W", 0.5, 0.5), Binding("Shift+W", 0.4, 0.4)])


def s_look_zones():
    return two_looks_profile()


def s_binding_pointers():
    return Profile("p", bindings=[Binding("A", 0.1, 0.1, "hold", also=[(0.2, 0.2)] * 4, extra_actions=[
        Action("pinch-out", 0.5, 0.5), Action("hold", 0.3, 0.3, also=[(0.4, 0.4)] * 4)])])


def s_budget():
    p = Profile("p")
    for i in range(6):
        p.add(Binding(chr(65 + i), 0.1 * (i + 1) - 0.05, 0.5, "hold", also=[(0.1 * (i + 1) - 0.05, 0.6), (0.1 * (i + 1) - 0.05, 0.7)]))
    return p


def s_repaired():
    return Profile.from_dict({"name": "n", "bindings": [{"input": "Q", "x": 0.1, "y": 0.1, "mode": "bogus"},
                                                        {"input": "R", "x": 0.1, "y": 0.1}]}, repair=True)


def s_note():
    return Profile.from_dict({"name": "n", "bindings": [{"input": "Q", "x": 0.1, "y": 0.1}, {"input": "Q", "x": 0.2, "y": 0.2}]})


def damaged(change):
    def build():
        p = Profile("p", bindings=[Binding("Q", 0.3, 0.3, "swipe", x2=0.6, y2=0.3, extra_actions=[Action("tap", 0.8, 0.8)])])
        change(p.bindings[0])
        return p
    return build


def set_mode(b):
    b.mode = "fly"


def set_delay(b):
    b.extra_actions[0].delay_ms = -5


def set_end(b):
    b.x2 = None


def set_position(b):
    b.x = 1.5


def set_rate(b):
    b.rate_hz = 99


def set_too_many(b):
    b.also = [(0.1, 0.1)] * 6


def set_ignored(b):
    b.also = [(0.1, 0.1)]


def set_not_action(b):
    b.extra_actions.append("tap")


def s_wheel():
    return Profile("p", bindings=[Binding("WheelUp", 0.5, 0.5, "hold")])


SCENARIOS = [
    ("missing_binding_id", "error", s_missing_id, {}),
    ("invalid_binding_id", "error", s_invalid_id, {}),
    ("duplicate_binding_id", "error", s_duplicate_id, {}),
    ("invalid_trigger", "error", s_invalid_trigger, {}),
    ("duplicate_trigger_in_binding", "error", s_duplicate_trigger, {}),
    ("invalid_failure_policy", "error", s_failure_policy, {}),
    ("extra_triggers_not_executed", "warning", s_extra_triggers, {"runtime_supports": frozenset()}),
    ("shared_trigger_not_executed", "warning", s_extra_trigger_shared, {}),
    ("extra_actions_not_executed", "warning", s_extra_actions, {"runtime_supports": frozenset({"extra_triggers"})}),
    ("actions_same_spot", "warning", s_same_spot_actions, {}),
    ("shared_trigger", "info", s_shared, {}),
    ("duplicate_trigger_not_shared", "warning", s_unshared, {}),
    ("shared_trigger_partially_authorized", "warning", s_partial, {}),
    ("sharing_metadata_invalid", "warning", s_stale_sharing, {}),
    ("sharing_not_executed", "warning", s_sharing_unsupported, {"runtime_supports": frozenset({"extra_triggers", "extra_actions"})}),
    ("shared_trigger_conflict", "warning", s_shared_same_spot, {}),
    ("shared_trigger_conflict", "warning", s_shared_two_scopes, {}),
    ("trigger_exceeds_pointers", "warning", s_trigger_pointers, {}),
    ("shadowed_by_later_binding", "warning", s_shadowed, {}),
    ("smart_loot_invalid", "error", s_smart_loot_invalid, {}),
    ("smart_loot_key_conflict", "error", s_smart_loot_key_conflict, {}),
    ("stick_keys_invalid", "error", s_stick_invalid, {}),
    ("stick_key_duplicate", "error", s_stick_duplicate, {}),
    ("stick_key_shared", "error", s_stick_shared, {}),
    ("stick_key_is_app_hotkey", "error", s_stick_is_hotkey, {"reserved": RESERVED}),
    ("trigger_is_stick_key", "error", s_trigger_is_stick_key, {}),
    ("trigger_is_app_hotkey", "error", s_trigger_is_hotkey, {"reserved": RESERVED}),
    ("combination_overrides_plain_key", "info", s_combination, {}),
    ("extra_look_zones_ignored", "warning", s_look_zones, {}),
    ("binding_exceeds_pointers", "error", s_binding_pointers, {}),
    ("pointer_budget_exceeded_if_all_held", "info", s_budget, {}),
    ("repaired_on_load", "warning", s_repaired, {}),
    ("load_note", "info", s_note, {}),
    ("invalid_mode", "error", damaged(set_mode), {}),
    ("invalid_action_delay", "error", damaged(set_delay), {}),
    ("missing_end_point", "error", damaged(set_end), {}),
    ("coordinate_out_of_range", "error", damaged(set_position), {}),
    ("setting_out_of_range", "error", damaged(set_rate), {}),
    ("too_many_extra_spots", "error", damaged(set_too_many), {}),
    ("extra_spots_ignored", "warning", damaged(set_ignored), {}),
    ("unsupported_action", "error", damaged(set_not_action), {}),
    ("wheel_runs_as_one_shot", "info", s_wheel, {}),
]


@pytest.mark.parametrize("code, severity, build, kw", SCENARIOS, ids=[f"{c}-{b.__name__}" for c, _s, b, _k in SCENARIOS])
def test_each_problem_is_reported_with_its_code_and_severity(code, severity, build, kw):
    found = [i for i in build().analyze(**kw) if i.code == code]
    assert found, codes(build(), **kw)
    assert {i.severity for i in found} == {severity}
    assert all(i.message.strip() for i in found)


def test_every_code_the_source_can_emit_has_a_scenario():
    source = Path(profile_module.__file__).read_text(encoding="utf-8")
    emitted = set(re.findall(r'add\(\s*"([a-z_]+)"', source))
    assert emitted and emitted == {code for code, *_ in SCENARIOS}


def test_every_severity_the_scenarios_use_is_a_known_one_and_the_order_is_errors_first():
    assert SEVERITIES == ("error", "warning", "info")
    assert {severity for _c, severity, *_ in SCENARIOS} <= set(SEVERITIES)


# ===================================================================== intentional sharing is not a conflict
def test_two_bindings_that_share_an_input_without_clashing_are_info_not_a_problem():
    p = s_shared()
    found = p.analyze(runtime_supports={"shared_triggers"})
    assert [(i.code, i.severity) for i in found] == [("shared_trigger", "info")]
    shared = found[0]
    assert shared.trigger == "LMB" and shared.binding_ids == tuple(b.id for b in p.bindings) and shared.positions == (0, 1)


def test_sharing_becomes_a_warning_only_when_the_actions_really_clash():
    assert "shared_trigger_conflict" not in codes(s_shared())
    same_spot = issue(s_shared_same_spot(), "shared_trigger_conflict")
    assert "same spot" in same_spot.message and same_spot.trigger == "LMB" and len(same_spot.binding_ids) == 2
    assert "scope" in issue(s_shared_two_scopes(), "shared_trigger_conflict").message


def test_a_trigger_shared_through_an_additional_trigger_counts_as_shared_too():
    p = Profile("p", bindings=[Binding("E", 0.1, 0.1, id="a", extra_triggers=["F"]), Binding("F", 0.9, 0.9, id="b")])
    p.share_trigger("F", ["a", "b"])
    found = issue(p, "shared_trigger")
    assert found.trigger == "F" and found.binding_ids == ("a", "b")


def test_three_bindings_on_one_input_give_one_shared_issue_and_a_conflict_per_clashing_pair():
    p = Profile("p")
    for x in (0.2, 0.2, 0.7):
        p.add_shared(Binding("LMB", x, 0.5, "tap"), authorize=True)
    found = p.analyze()
    assert [i.code for i in found].count("shared_trigger") == 1
    assert [i.positions for i in found if i.code == "shared_trigger_conflict"] == [(0, 1)]


def test_the_engine_gaps_disappear_when_the_engine_supports_them():
    p = Profile("p", bindings=[Binding("LMB", 0.8, 0.7, "hold", extra_triggers=["F"], extra_actions=[Action("tap", 0.1, 0.1)])])
    p.add_shared(Binding("LMB", 0.5, 0.2, "tap"))                                  # NOT authorized: the later one wins, as ever
    now = set(codes(p))
    assert {"shadowed_by_later_binding", "duplicate_trigger_not_shared"} <= now     # unauthorized duplicates are still reported
    assert not now & {"extra_triggers_not_executed", "extra_actions_not_executed"}  # the engine runs additional triggers and actions
    nothing = set(codes(p, runtime_supports=frozenset()))
    assert {"extra_triggers_not_executed", "extra_actions_not_executed", "shadowed_by_later_binding"} <= nothing
    p.share_trigger("LMB", [b.id for b in p.bindings])                              # now it is explicit
    authorized = set(codes(p))
    assert "shared_trigger" in authorized and not authorized & {"shadowed_by_later_binding", "duplicate_trigger_not_shared"}
    unsupported = set(codes(p, runtime_supports={"extra_triggers", "extra_actions"}))
    assert {"sharing_not_executed", "shadowed_by_later_binding"} <= unsupported     # an engine without sharing would run only one
    assert profile_module.RUNTIME_SUPPORTS == frozenset({"extra_triggers", "extra_actions", "shared_triggers"})


def test_an_additional_trigger_that_loses_to_another_binding_is_reported_with_the_reason():
    first_wins = Profile("p", bindings=[Binding("E", 0.1, 0.1, id="a"), Binding("R", 0.9, 0.9, id="b", extra_triggers=["E"])])
    found = issue(first_wins, "shared_trigger_not_executed")
    assert found.trigger == "E" and found.binding_ids == ("b", "a") and "first key beats" in found.message
    later_wins = Profile("p", bindings=[Binding("A", 0.1, 0.1, id="a", extra_triggers=["F"]),
                                        Binding("B", 0.9, 0.9, id="b", extra_triggers=["F"])])
    found = issue(later_wins, "shared_trigger_not_executed")
    assert found.binding_ids == ("a", "b") and "later control" in found.message
    first_wins.share_trigger("E", ["a", "b"])                                  # authorized: both run, nothing is shadowed
    assert "shared_trigger_not_executed" not in codes(first_wins)


def test_the_analyzer_and_the_engine_agree_on_which_binding_runs_for_a_shared_trigger():
    from app.mapping.profile import trigger_owners
    p = Profile("p", bindings=[Binding("E", 0.1, 0.1, id="a", extra_triggers=["F"]), Binding("F", 0.2, 0.2, id="b"),
                               Binding("R", 0.3, 0.3, id="c", extra_triggers=["F", "G"]), Binding("T", 0.4, 0.4, id="d", extra_triggers=["G"])])
    owners = trigger_owners(p.bindings)
    losers = {(i.trigger, i.binding_ids[0]) for i in p.analyze() if i.code == "shared_trigger_not_executed"}
    expected = {(t, b.id) for b in p.bindings for t in b.extra_triggers if owners[t] is not b}
    assert losers == expected == {("F", "a"), ("F", "c"), ("G", "c")}


def test_a_shadowed_binding_names_the_one_that_wins():
    p = s_unshared()
    found = issue(p, "shadowed_by_later_binding")
    assert found.positions == (0, 1) and found.binding_ids[0] == p.bindings[0].id and found.trigger == "LMB"


# ===================================================================== details of individual checks
def test_application_hotkeys_include_esc_and_whatever_the_caller_passes():
    p = Profile("p", bindings=[Binding("F9", 0.5, 0.5), Binding("Esc", 0.4, 0.4), Binding("K", 0.3, 0.3)])
    assert codes(p) == ["trigger_is_app_hotkey"] and issue(p, "trigger_is_app_hotkey").trigger == "Esc"     # F9 unknown without a list
    both = [(i.code, i.trigger) for i in p.analyze(reserved=RESERVED)]
    assert both == [("trigger_is_app_hotkey", "F9"), ("trigger_is_app_hotkey", "Esc")]
    assert "Key mapping on / off" in p.analyze(reserved=RESERVED)[0].message


def test_an_additional_trigger_can_clash_with_a_hotkey_or_a_stick_key_too():
    p = Profile("p", bindings=[Binding("E", 0.5, 0.5, extra_triggers=["F8", "W"])])
    p.sticks.append(Stick(0.15, 0.7))
    found = {(i.code, i.trigger) for i in p.analyze(reserved=RESERVED)}
    assert {("trigger_is_app_hotkey", "F8"), ("trigger_is_stick_key", "W")} <= found


def test_a_combination_on_a_stick_key_is_allowed_and_only_noted():
    p = Profile("p", bindings=[Binding("Shift+W", 0.5, 0.5)])
    p.sticks.append(Stick(0.15, 0.7))
    assert [(i.code, i.severity) for i in p.analyze()] == [("combination_overrides_plain_key", "info")]


def test_pointer_counts_use_the_worst_case_of_every_action_together():
    p = s_binding_pointers()
    assert "needs 12 fingers" in issue(p, "binding_exceeds_pointers").message and MAX_TOUCH_POINTERS == protocol.MAX_POINTERS
    ok = Profile("p", bindings=[Binding("A", 0.1, 0.1, "hold", also=[(0.2, 0.2)] * 4, extra_actions=[Action("pinch-out", 0.5, 0.5)])])
    assert "binding_exceeds_pointers" not in codes(ok)                             # 5 + 2 = 7


def test_the_all_held_estimate_counts_the_stick_and_look_but_not_quick_one_shots():
    p = Profile("p")
    for i in range(8):
        p.add(Binding(chr(65 + i), 0.1, 0.1 + 0.1 * i, "tap" if i < 3 else "hold"))
    p.sticks.append(Stick(0.15, 0.7))
    p.looks.append(Look(0.5, 0.2, 0.9, 0.8))
    assert "pointer_budget_exceeded_if_all_held" not in codes(p)                   # 1 stick + 1 look + 5 holds = 7
    for i in range(4):
        p.add(Binding(chr(97 + i), 0.9, 0.1 + 0.1 * i, "hold"))
    assert "would need 11 fingers" in issue(p, "pointer_budget_exceeded_if_all_held").message               # 1 + 1 + 9 holds


def test_load_problems_are_listed_with_their_original_wording():
    p = s_repaired()
    assert [i.message for i in p.analyze() if i.code == "repaired_on_load"] == p.repairs
    q = s_note()
    assert [i.message for i in q.analyze() if i.code == "load_note"] == q.notes


def test_binding_ids_that_were_replaced_at_load_are_reported():
    raw = {"version": 2, "name": "n", "bindings": [
        {"type": "binding", "id": "x", "triggers": ["A"], "actions": [{"mode": "tap", "x": 0.1, "y": 0.1}]},
        {"type": "binding", "id": "x", "triggers": ["B"], "actions": [{"mode": "tap", "x": 0.1, "y": 0.1}]}]}
    p = Profile.from_dict(raw)
    assert "repaired_on_load" in codes(p) and "duplicate_binding_id" not in codes(p)       # fixed in memory: no longer duplicated


# ===================================================================== purity, determinism, shape
def snapshot(p):
    """Everything about the profile, including damage that to_dict() could not even write."""
    return (p.name, [id(b) for b in p.bindings], [repr(b) for b in p.bindings], [b.unknown for b in p.bindings],
            [b.primary_unknown for b in p.bindings], [repr(b.extra_actions) for b in p.bindings], repr(p.sticks), repr(p.looks),
            list(p.repairs), list(p.notes), p.schema_version, p.unknown)


@pytest.mark.parametrize("build", [b for _c, _s, b, _k in SCENARIOS], ids=[f"{c}-{b.__name__}" for c, _s, b, _k in SCENARIOS])
def test_analysis_never_changes_the_profile(build):
    p = build()
    before = snapshot(p)
    p.analyze(reserved=RESERVED)
    assert snapshot(p) == before


def test_analysis_does_not_change_a_damaged_binding_either():
    p = damaged(set_position)()
    p.analyze()
    assert p.bindings[0].x == 1.5                                                  # not clamped, not repaired: only reported


def test_analysis_is_deterministic_and_does_not_depend_on_the_object_identity():
    built = [s_shared_same_spot(), s_trigger_pointers(), s_budget()]
    for p in built:
        first = p.analyze(reserved=RESERVED)
        assert first == p.analyze(reserved=RESERVED) == copy.deepcopy(p).analyze(reserved=RESERVED)


def test_issues_are_ordered_by_severity_and_then_by_the_fixed_order_of_the_checks():
    p = Profile("p", bindings=[Binding("F9", 0.5, 0.5, extra_triggers=["Q"]), Binding("Shift+Q", 0.4, 0.4)])
    p.add_shared(Binding("Q", 0.5, 0.5, "tap"))
    p.looks += two_looks_profile().looks
    found = p.analyze(reserved=RESERVED)
    ranks = [SEVERITIES.index(i.severity) for i in found]
    assert ranks == sorted(ranks) and found[0].severity == "error" and found[-1].severity == "info"
    assert [i.code for i in found] == [i.code for i in p.analyze(reserved=RESERVED)]


def test_an_issue_is_plain_frozen_data_that_a_ui_can_show_or_turn_into_json():
    found = s_shared_same_spot().analyze()
    assert all(isinstance(i, Issue) for i in found)
    as_json = json.dumps([i.to_dict() for i in found])
    assert set(json.loads(as_json)[0]) == {"code", "severity", "message", "binding_ids", "positions", "trigger"}
    with pytest.raises(AttributeError):
        found[0].code = "other"                                                    # frozen


def test_issues_point_at_the_bindings_by_id_and_by_position():
    p = s_duplicate_id()
    found = issue(p, "duplicate_binding_id")
    assert found.positions == (0, 1) and found.binding_ids == (p.bindings[0].id, p.bindings[0].id)


# ===================================================================== clean profiles stay clean
def test_a_clean_profile_has_no_findings():
    assert Profile("empty").analyze() == []
    assert Profile.from_dict(json.loads(GOLDEN_V1.read_text(encoding="utf-8"))).analyze(reserved=RESERVED) == []


def test_the_ready_made_templates_have_no_errors_or_warnings():
    from app.mapping import templates
    for name in (templates.STANDARD, templates.BATTLE_ROYALE):
        p = templates.build(name, "t", "landscape")
        bad = [i for i in p.analyze(reserved=RESERVED) if i.severity != "info"]
        assert bad == [], (name, bad)
