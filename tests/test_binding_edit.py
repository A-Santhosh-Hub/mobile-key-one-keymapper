"""The editing layer of the advanced editor (app/mapping/binding_edit.py): triggers, actions and sharing, by IDENTITY, as atomic
transactions. A refused or cancelled operation leaves the profile EXACTLY as it was; sharing is authorized only by an explicit choice;
the analyzer and the engine are only read, never duplicated."""
import copy

import pytest

from app.mapping import binding_edit as be
from app.mapping.binding_edit import CANCEL, CONTEXT_REBIND, PRIMARY, REPLACE, SHARE, SWAP, ProfileEditor
from app.mapping.profile import Action, Binding, Profile, Stick
from test_characterization_engine import Rig


def make(*bindings, sticks=()):
    p = Profile("p", orientation="landscape")
    p.bindings = list(bindings)
    p.sticks = list(sticks)
    return p


def fire_interact():
    """Fire on F, Interact on F (a repeated key, NOT authorized), a plain Reload, and a control with several keys and actions."""
    return make(Binding("F", 0.8, 0.7, "hold", "Fire", id="fire"),
                Binding("F", 0.3, 0.3, "tap", "Interact", id="interact"),
                Binding("R", 0.7, 0.9, "tap", "Reload", id="reload"),
                Binding("Q", 0.2, 0.8, "tap", "Lean", id="lean", extra_triggers=["E"],
                        extra_actions=[Action("tap", 0.25, 0.8, delay_ms=80)], on_failure="abort"))


def state(p: Profile):
    """Everything an operation could damage, as plain data."""
    return (p.to_dict(), [b.id for b in p.bindings], {t: list(i) for t, i in p.shared_triggers.items()})


def ids(p):
    return [b.id for b in p.bindings]


def editor(p, reserved=None):
    return ProfileEditor(p, reserved)


# ===================================================================== questions
def test_inspect_free_key_has_no_conflict():
    r = editor(fire_interact()).inspect("G", "reload")
    assert r.kind == "free" and r.users == ()


def test_inspect_sees_a_key_that_is_only_an_additional_trigger_of_another_control():
    r = editor(fire_interact()).inspect("E", "reload")
    assert r.kind == "taken" and r.users == ("lean",)                       # Profile.key_owner("E") would say: nobody


def test_inspect_lists_every_other_user_not_just_the_first():
    r = editor(fire_interact()).inspect("F", "reload")
    assert r.kind == "taken" and r.users == ("fire", "interact") and REPLACE in r.choices and SHARE in r.choices


def test_inspect_own_key_stick_key_reserved_key_and_garbage():
    p = fire_interact()
    p.set_stick(Stick(0.15, 0.7))
    ed = editor(p, {"F9": "the 'Free the mouse' shortcut"})
    assert ed.inspect("E", "lean").kind == "own"
    assert ed.inspect("W", "reload").kind == "stick"
    assert ed.inspect("F9", "reload").kind == "reserved" and "shortcut" in ed.inspect("F9", "reload").message
    assert ed.inspect("Esc", "reload").kind == "reserved"
    assert ed.inspect("Shift+", "reload").kind in ("invalid", "free")            # answered without an exception, whichever it is


def test_inspect_reads_combinations_in_canonical_form():
    p = make(Binding("Ctrl+Shift+E", 0.5, 0.5, id="a"), Binding("R", 0.1, 0.1, id="b"))
    r = editor(p).inspect("Shift+Ctrl+E", "b")
    assert r.kind == "taken" and r.trigger == "Ctrl+Shift+E" and r.users == ("a",)


def test_swap_is_offered_only_for_a_rebind_between_two_first_keys():
    p = make(Binding("G", 0.1, 0.1, id="a"), Binding("T", 0.5, 0.5, id="b"))
    ed = editor(p)
    assert SWAP in ed.inspect("T", "a", CONTEXT_REBIND, old="G").choices
    assert SWAP not in ed.inspect("T", "a").choices                                       # adding a key: nothing to swap


def test_swap_is_not_offered_when_two_controls_use_the_key_or_the_key_is_only_an_additional_one():
    p = make(Binding("G", 0.1, 0.1, id="a"), Binding("T", 0.5, 0.5, id="b"), Binding("Y", 0.9, 0.9, id="c", extra_triggers=["T"]))
    assert SWAP not in editor(p).inspect("T", "a", CONTEXT_REBIND, old="G").choices        # two users
    q = make(Binding("G", 0.1, 0.1, id="a"), Binding("Y", 0.9, 0.9, id="c", extra_triggers=["T"]))
    assert SWAP not in editor(q).inspect("T", "a", CONTEXT_REBIND, old="G").choices        # T is not c's first key
    r = make(Binding("G", 0.1, 0.1, id="a", extra_triggers=["H"]), Binding("T", 0.5, 0.5, id="b", extra_triggers=["G"]))
    assert SWAP not in editor(r).inspect("T", "a", CONTEXT_REBIND, old="G").choices        # b already has G: it would list it twice


# ===================================================================== adding a trigger
def test_adding_a_free_key_adds_a_trigger_and_no_action():
    p = fire_interact()
    r = editor(p).add_trigger("reload", "G")
    assert r.ok and p.find_by_id("reload").triggers == ("R", "G") and p.find_by_id("reload").extra_actions == []
    assert ids(p) == ["fire", "interact", "reload", "lean"]


def test_adding_a_key_another_control_uses_needs_a_choice_and_changes_nothing():
    p = fire_interact()
    before = state(p)
    r = editor(p).add_trigger("reload", "F")
    assert not r.ok and r.needs_choice and state(p) == before


def test_cancel_changes_nothing_and_reports_no_group_change():
    p = fire_interact()
    before = state(p)
    r = editor(p).add_trigger("reload", "F", CANCEL)
    assert not r.ok and not r.needs_choice and state(p) == before


def test_choosing_share_authorizes_every_user_of_the_key_and_persists_it():
    p = fire_interact()
    r = editor(p).add_trigger("reload", "F", SHARE)
    assert r.ok and p.shared_triggers == {"F": ["fire", "interact", "reload"]} and p.is_shared("F")
    assert [b.id for b in p.runners()["F"]] == ["fire", "interact", "reload"]
    back = Profile.from_dict(p.to_dict())
    assert back.shared_triggers == {"F": ["fire", "interact", "reload"]} and back.is_shared("F")
    assert p.to_dict()["shared_triggers"] == [{"trigger": "F", "bindings": ["fire", "interact", "reload"]}]


def test_choosing_replace_takes_the_key_from_the_others_and_never_shares():
    p = fire_interact()
    r = editor(p).add_trigger("lean", "F", REPLACE)
    assert r.ok and not p.shared_triggers
    assert ids(p) == ["reload", "lean"]                                    # Fire and Interact had only F: they went (and the result says so)
    assert [b.id for b in p.runners()["F"]] == ["lean"]


def test_replace_removes_a_control_only_when_the_key_was_its_only_key_and_says_so():
    p = fire_interact()
    r = editor(p).add_trigger("lean", "F", REPLACE)
    assert ids(p) == ["reload", "lean"] and any("only key" in n for n in r.notes)
    p2 = make(Binding("F", 0.8, 0.7, "hold", "Fire", id="fire", extra_triggers=["G"]), Binding("R", 0.7, 0.9, id="reload"))
    r2 = editor(p2).add_trigger("reload", "F", REPLACE)
    assert r2.ok and ids(p2) == ["fire", "reload"] and p2.find_by_id("fire").triggers == ("G",)    # the other key promoted: Fire keeps working
    assert any("keeps its other keys" in n for n in r2.notes)


def test_adding_a_stick_key_a_reserved_key_or_an_own_key_is_refused_untouched():
    p = fire_interact()
    p.set_stick(Stick(0.15, 0.7))
    ed = editor(p, {"F9": "a shortcut"})
    before = state(p)
    for key in ("W", "F9", "E"):
        r = ed.add_trigger("lean" if key == "E" else "reload", key)
        assert not r.ok and state(p) == before


def test_a_trigger_that_makes_the_binding_invalid_is_refused():
    p = make(Binding("R", 0.7, 0.9, id="reload"))
    before = state(p)
    assert not editor(p).add_trigger("reload", "R").ok and state(p) == before


# ===================================================================== changing a trigger
def test_changing_the_first_key_to_a_free_key_keeps_everything_else():
    p = fire_interact()
    r = editor(p).change_trigger("lean", "Q", "X")
    lean = p.find_by_id("lean")
    assert r.ok and lean.triggers == ("X", "E") and len(lean.extra_actions) == 1 and lean.on_failure == "abort" and lean.label == "Lean"


def test_changing_an_additional_key_keeps_its_position():
    p = fire_interact()
    assert editor(p).change_trigger("lean", "E", "Z").ok
    assert p.find_by_id("lean").triggers == ("Q", "Z")


def test_changing_to_a_taken_key_needs_a_choice_and_changing_to_itself_is_a_noop():
    p = fire_interact()
    before = state(p)
    assert editor(p).change_trigger("reload", "R", "F").needs_choice and state(p) == before
    r = editor(p).change_trigger("reload", "R", "R")
    assert r.ok and state(p) == before


def test_rebind_replace_and_rebind_share():
    p = fire_interact()
    assert editor(p).change_trigger("reload", "R", "F", SHARE).ok
    assert p.shared_triggers["F"] == ["fire", "interact", "reload"] and p.find_by_id("reload").triggers == ("F",)
    p2 = fire_interact()
    assert editor(p2).change_trigger("reload", "R", "F", REPLACE).ok
    assert ids(p2) == ["reload", "lean"] and not p2.shared_triggers


def test_rebind_swap_exchanges_the_two_first_keys_by_identity():
    p = make(Binding("G", 0.1, 0.1, label="Grenade", id="g"), Binding("T", 0.5, 0.5, label="Tab", id="t"))
    r = editor(p).change_trigger("g", "G", "T", SWAP)
    assert r.ok and p.find_by_id("g").input == "T" and p.find_by_id("t").input == "G"
    assert (p.find_by_id("g").x, p.find_by_id("t").x) == (0.1, 0.5) and ids(p) == ["g", "t"]


def test_rebind_never_authorizes_the_destination_and_leaves_the_old_group():
    p = make(Binding("F", 0.1, 0.1, id="a"), Binding("F", 0.2, 0.2, id="b"), Binding("G", 0.3, 0.3, id="c"))
    p.share_trigger("F", ["a", "b"])
    r = editor(p).change_trigger("b", "F", "H")
    assert r.ok and p.shared_triggers == {} and r.group_changes and "F" in r.group_changes[0]
    assert not p.is_shared("H") and p.find_by_id("b").triggers == ("H",)


def test_moving_a_key_onto_a_key_that_has_no_group_does_not_create_one():
    p = make(Binding("F", 0.1, 0.1, id="a"), Binding("G", 0.3, 0.3, id="c"))
    assert editor(p).change_trigger("c", "G", "F", REPLACE).ok
    assert p.shared_triggers == {}


def test_a_changed_group_asks_for_confirmation_and_a_no_changes_nothing():
    p = make(Binding("F", 0.1, 0.1, id="a"), Binding("F", 0.2, 0.2, id="b"), Binding("F", 0.3, 0.3, id="c"))
    p.share_trigger("F", ["a", "b", "c"])
    before = state(p)
    asked = []
    r = editor(p).change_trigger("c", "F", "H", confirm=lambda changes: (asked.append(changes), False)[1])
    assert not r.ok and asked and "no longer share" in asked[0][0] and state(p) == before
    r = editor(p).change_trigger("c", "F", "H", confirm=lambda changes: True)
    assert r.ok and p.shared_triggers == {"F": ["a", "b"]}


def test_no_confirmation_is_asked_when_no_group_changes():
    p = fire_interact()
    asked = []
    assert editor(p).change_trigger("reload", "R", "G", confirm=lambda c: (asked.append(c), True)[1]).ok and asked == []


# ===================================================================== removing triggers
def test_removing_an_additional_trigger_and_the_first_trigger_promotes():
    p = fire_interact()
    assert editor(p).remove_trigger("lean", "E").ok and p.find_by_id("lean").triggers == ("Q",)
    p = fire_interact()
    r = editor(p).remove_trigger("lean", "Q")
    lean = p.find_by_id("lean")
    assert r.ok and lean.triggers == ("E",) and lean.input == "E" and lean.extra_actions[0].delay_ms == 80 and "first key" in r.message


def test_removing_the_only_trigger_is_refused_and_changes_nothing():
    p = fire_interact()
    before = state(p)
    r = editor(p).remove_trigger("reload", "R")
    assert not r.ok and "only key" in r.message and state(p) == before


def test_removing_a_trigger_from_a_shared_group_updates_the_group_and_warns():
    p = make(Binding("F", 0.1, 0.1, id="a"), Binding("G", 0.2, 0.2, id="b", extra_triggers=["F"]), Binding("H", 0.3, 0.3, id="c"))
    p.share_trigger("F", ["a", "b"])
    asked = []
    r = editor(p).remove_trigger("b", "F", confirm=lambda c: (asked.append(c), True)[1])
    assert r.ok and p.shared_triggers == {} and asked and "Sharing of F ends" in asked[0][0] and ids(p) == ["a", "b", "c"]


def test_make_first_reorders_without_adding_or_losing_a_key():
    p = fire_interact()
    r = editor(p).make_first("lean", "E")
    lean = p.find_by_id("lean")
    assert r.ok and lean.triggers == ("E", "Q") and lean.input == "E"
    assert not editor(p).make_first("lean", "Z").ok and p.find_by_id("lean").triggers == ("E", "Q")


# ===================================================================== sharing
def test_share_and_unshare_are_explicit_and_reversible():
    p = fire_interact()
    assert not p.shared_triggers and not editor(p).sharing_info("fire")[0].state == "shared"
    assert editor(p).share("F", ["fire", "interact"]).ok and p.is_shared("F")
    info = editor(p).sharing_info("fire")[0]
    assert info.state == "shared" and [m[0] for m in info.members] == ["fire", "interact"] and info.runs
    assert editor(p).unshare("F").ok and not p.shared_triggers
    assert not editor(p).unshare("F").ok


def test_sharing_refuses_what_the_profile_refuses_and_changes_nothing():
    p = fire_interact()
    before = state(p)
    assert not editor(p).share("F", ["fire"]).ok and not editor(p).share("F", ["fire", "reload"]).ok and state(p) == before


def test_sharing_info_tells_unauthorized_duplicates_and_who_really_runs():
    p = fire_interact()
    fire, interact = editor(p).sharing_info("fire")[0], editor(p).sharing_info("interact")[0]
    assert fire.state == "unauthorized" and interact.state == "unauthorized"
    assert (fire.runs, interact.runs) == (False, True)                       # the later binding wins: the engine's own rule
    assert editor(p).sharing_info("reload")[0].state == "private"


def test_sharing_info_agrees_with_what_the_engine_runs():
    p = fire_interact()
    editor(p).add_trigger("reload", "F", SHARE)
    r = Rig()
    r.engine.set_profile(p)
    r.profile = p
    r.press("F", 50)
    started = {t[1] for t in r.touches() if t[0] == 0}
    assert len(started) == 3                                                  # all three share the key and run
    assert all(i.runs for b in ("fire", "interact", "reload") for i in editor(p).sharing_info(b) if i.trigger == "F")
    assert [b for b in p.runners()["F"]] == [p.find_by_id(x) for x in ("fire", "interact", "reload")]


def test_unauthorized_duplicates_keep_the_winner_only_rule_after_unrelated_edits():
    p = fire_interact()
    assert editor(p).change_trigger("reload", "R", "G").ok
    r = Rig()
    r.engine.set_profile(p)
    r.press("F", 50)
    assert len([t for t in r.touches() if t[0] == 0]) == 1                    # only the later of the two runs
    assert not p.shared_triggers


# ===================================================================== actions
def test_adding_an_action_gets_a_new_identity_a_delay_and_leaves_the_keys_alone():
    p = fire_interact()
    r = editor(p).add_action("reload")
    reload = p.find_by_id("reload")
    assert r.ok and r.ref and reload.triggers == ("R",) and len(reload.extra_actions) == 1
    new = reload.extra_actions[0]
    assert new.uid == r.ref and new.delay_ms == be.DEFAULT_NEW_DELAY_MS and new.mode == "tap"
    assert ProfileEditor.find_action(reload, r.ref) is new and ProfileEditor.find_action(reload, PRIMARY) is reload


def test_action_identities_are_unique_and_survive_other_edits():
    p = fire_interact()
    a = editor(p).add_action("lean").ref
    b = editor(p).add_action("lean").ref
    assert a != b
    editor(p).remove_action("lean", PRIMARY)
    assert {x.uid for x in p.find_by_id("lean").extra_actions} >= {a, b}


def test_action_uid_is_runtime_only_and_never_written_or_compared():
    p = fire_interact()
    ref = editor(p).add_action("reload").ref
    assert ref not in repr(p.to_dict())
    assert Action("tap", 0.1, 0.1).uid != Action("tap", 0.1, 0.1).uid and Action("tap", 0.1, 0.1) == Action("tap", 0.1, 0.1)
    back = Profile.from_dict(p.to_dict())
    assert back.find_by_id("reload").extra_actions[0].uid != ref and back.find_by_id("reload").extra_actions[0] == p.find_by_id("reload").extra_actions[0]


def test_duplicating_an_action_copies_its_settings_but_not_its_unknown_fields():
    p = fire_interact()
    lean = p.find_by_id("lean")
    lean.extra_actions[0].unknown = {"future": {"id": 7}}
    r = editor(p).duplicate_action("lean", lean.extra_actions[0].uid)
    copy_ = ProfileEditor.find_action(lean, r.ref)
    original = lean.extra_actions[0]
    assert r.ok and copy_ is not original and copy_.uid != original.uid and copy_.mode == original.mode
    assert copy_.delay_ms == 80 and copy_.x > original.x and copy_.unknown == {} and original.unknown == {"future": {"id": 7}}


def test_duplicating_the_first_action_makes_a_delayed_additional_action():
    p = fire_interact()
    r = editor(p).duplicate_action("fire", PRIMARY)
    fire = p.find_by_id("fire")
    assert r.ok and fire.mode == "hold" and fire.extra_actions[0].mode == "hold" and fire.extra_actions[0].delay_ms == be.DEFAULT_NEW_DELAY_MS
    assert fire.extra_actions[0].x > fire.x


def test_removing_an_additional_action_keeps_the_others_in_order():
    p = fire_interact()
    lean = p.find_by_id("lean")
    first = editor(p).add_action("lean").ref
    second = editor(p).add_action("lean").ref
    r = editor(p).remove_action("lean", lean.extra_actions[0].uid)
    assert r.ok and [a.uid for a in lean.extra_actions] == [first, second]


def test_removing_the_last_action_is_refused_and_changes_nothing():
    p = fire_interact()
    before = state(p)
    r = editor(p).remove_action("reload", PRIMARY)
    assert not r.ok and "only action" in r.message and state(p) == before


def test_removing_the_first_action_promotes_the_next_and_resets_its_delay():
    p = make(Binding("Q", 0.2, 0.8, "tap", "Lean", id="lean", primary_unknown={"p": 1},
                     extra_actions=[Action("swipe", 0.3, 0.3, x2=0.6, y2=0.5, delay_ms=120, unknown={"q": 2}), Action("hold", 0.5, 0.5, delay_ms=300)]))
    r = editor(p).remove_action("lean", PRIMARY)
    lean = p.find_by_id("lean")
    assert r.ok and lean.mode == "swipe" and (lean.x, lean.y, lean.x2, lean.y2) == (0.3, 0.3, 0.6, 0.5)
    assert [a.mode for a in lean.extra_actions] == ["hold"] and lean.extra_actions[0].delay_ms == 300
    assert lean.primary_unknown == {"q": 2} and any("120 ms" in n for n in r.notes)
    assert Profile.from_dict(p.to_dict()).find("Q").mode == "swipe"


def test_a_stale_action_reference_is_refused():
    p = fire_interact()
    before = state(p)
    assert not editor(p).remove_action("lean", "a-gone").ok and not editor(p).duplicate_action("lean", "a-gone").ok and state(p) == before


def test_actions_beyond_the_pointer_budget_are_refused():
    p = make(Binding("Q", 0.2, 0.8, "hold", id="q"))
    ed = editor(p)
    results = [ed.add_action("q", "hold", 0.1 + 0.05 * i, 0.5, delay_ms=0) for i in range(12)]
    assert results[0].ok and not results[-1].ok and len(p.find_by_id("q").extra_actions) <= 9
    assert "fingers" in results[-1].message


# ===================================================================== transactions
def test_adopt_keeps_the_identity_of_every_binding_that_did_not_change():
    p = fire_interact()
    objects = {b.id: b for b in p.bindings}
    editor(p).change_trigger("reload", "R", "G")
    for b in p.bindings:
        assert b is objects[b.id]


def test_a_removed_control_is_dropped_and_a_new_one_adopted_in_order():
    p = fire_interact()
    r = editor(p).delete_binding("interact")
    assert r.ok and ids(p) == ["fire", "reload", "lean"]
    assert not editor(p).delete_binding("interact").ok


def test_editing_a_version_1_profile_without_new_features_stays_version_1_and_gets_no_ids():
    p = make(Binding("R", 0.7, 0.9, "tap", "Reload"), Binding("G", 0.1, 0.1, "tap", "Grenade"))
    assert editor(p).change_trigger(p.bindings[0].id, "R", "T").ok
    data = p.to_dict()
    assert data["version"] == 1 and all("id" not in b for b in data["bindings"]) and "shared_triggers" not in data


def test_adding_an_additional_trigger_to_a_version_1_profile_needs_version_2():
    p = make(Binding("R", 0.7, 0.9, "tap", "Reload"))
    assert editor(p).add_trigger(p.bindings[0].id, "T").ok and p.required_version == 2


def test_apply_working_adopts_a_copy_edited_step_by_step_and_checks_it():
    p = fire_interact()
    working = editor(p).working_copy()
    editor(working).add_trigger("reload", "G")
    editor(working).add_action("reload")
    assert p.find_by_id("reload").triggers == ("R",)                        # nothing happened to the real profile yet
    r = editor(p).apply_working(working)
    assert r.ok and p.find_by_id("reload").triggers == ("R", "G") and len(p.find_by_id("reload").extra_actions) == 1


def test_apply_working_refuses_a_copy_that_is_invalid_and_changes_nothing():
    p = fire_interact()
    working = editor(p).working_copy()
    working.find_by_id("reload").extra_triggers = ["R"]                      # the same key twice
    before = state(p)
    r = editor(p).apply_working(working)
    assert not r.ok and state(p) == before


def test_apply_working_warns_when_a_group_changes_and_a_no_changes_nothing():
    p = make(Binding("F", 0.1, 0.1, id="a"), Binding("F", 0.2, 0.2, id="b"))
    p.share_trigger("F", ["a", "b"])
    working = editor(p).working_copy()
    editor(working).change_trigger("b", "F", "H")
    before = state(p)
    r = editor(p).apply_working(working, confirm=lambda c: False)
    assert not r.ok and r.group_changes and state(p) == before
    assert editor(p).apply_working(working, confirm=lambda c: True).ok and not p.shared_triggers


def test_a_failed_operation_leaves_the_object_graph_untouched():
    p = fire_interact()
    snapshot = copy.deepcopy(p)
    objects = [id(b) for b in p.bindings]
    assert not editor(p).add_trigger("reload", "F", "nonsense").ok
    editor(p).remove_trigger("reload", "R")
    assert p == snapshot and [id(b) for b in p.bindings] == objects and p.to_dict() == snapshot.to_dict()


# ===================================================================== the controls table
def test_row_infos_show_every_key_the_action_count_and_sharing():
    p = fire_interact()
    editor(p).add_trigger("reload", "F", SHARE)
    infos = editor(p).row_infos()
    assert infos["lean"].keys == "Q +1" and infos["lean"].actions == "2 actions" and "Keys: Q, E" in infos["lean"].keys_tip
    assert infos["reload"].shared and "shares a key with" in infos["reload"].info and "Fire" in infos["reload"].info
    assert infos["reload"].keys == "R +1" and "Keys: R, F" in infos["reload"].keys_tip
    assert not infos["lean"].shared


def test_row_infos_flag_an_unauthorized_duplicate_as_something_to_check():
    p = fire_interact()
    infos = editor(p).row_infos()
    assert infos["fire"].severity == "warning" and "to check" in infos["fire"].info and "F" in infos["fire"].tip
    assert infos["reload"].severity == "" and infos["reload"].info == ""


def test_issues_for_returns_only_what_concerns_one_control():
    p = fire_interact()
    found = be.issues_for(p, "fire")
    assert found and all("fire" in i.binding_ids for i in found)
    assert be.issues_for(p, "reload") == []


# ===================================================================== a NEW control that wants a taken key
def test_a_new_control_on_a_free_key_is_just_added_and_a_taken_key_needs_a_choice():
    p = fire_interact()
    assert editor(p).add_binding(Binding("G", 0.5, 0.5, "tap", "Grenade", id="gren")).ok and ids(p)[-1] == "gren"
    before = state(p)
    r = editor(p).add_binding(Binding("R", 0.5, 0.5, "tap", "Second reload", id="r2"))
    assert not r.ok and r.needs_choice and state(p) == before


def test_a_new_control_can_replace_or_share_and_never_does_so_silently():
    p = fire_interact()
    assert editor(p).add_binding(Binding("R", 0.5, 0.5, "tap", id="r2"), REPLACE).ok
    assert ids(p) == ["fire", "interact", "lean", "r2"] and not p.shared_triggers and [b.id for b in p.runners()["R"]] == ["r2"]
    q = fire_interact()
    r = editor(q).add_binding(Binding("R", 0.5, 0.5, "tap", id="r2"), SHARE)
    assert r.ok and r.ref == "r2" and q.shared_triggers == {"R": ["reload", "r2"]} and ids(q) == ["fire", "interact", "reload", "lean", "r2"]


def test_a_new_control_on_a_key_used_only_as_an_additional_trigger_is_seen_as_a_conflict():
    p = fire_interact()
    r = editor(p).add_binding(Binding("E", 0.5, 0.5, "tap", id="e2"))
    assert not r.ok and r.needs_choice
    assert editor(p).add_binding(Binding("E", 0.5, 0.5, "tap", id="e2"), SHARE).ok and p.shared_triggers == {"E": ["lean", "e2"]}


def test_a_dry_run_reports_what_would_happen_and_changes_nothing():
    p = fire_interact()
    before = state(p)
    r = editor(p).add_trigger("lean", "F", REPLACE, dry_run=True)
    assert r.ok and any("only key" in n for n in r.notes) and state(p) == before
    r = editor(p).change_trigger("reload", "R", "F", SHARE, dry_run=True)
    assert r.ok and r.group_changes and "F will be shared" in r.group_changes[0] and state(p) == before
    r = editor(p).add_binding(Binding("F", 0.5, 0.5, id="x"), SHARE, dry_run=True)
    assert r.ok and state(p) == before and "x" not in ids(p)


def test_an_operation_that_fails_half_way_leaves_no_trace():
    from app.mapping.profile import ProfileError
    p = fire_interact()
    before = state(p)
    objects = [id(b) for b in p.bindings]

    def half(w):
        w.remove_by_id("reload")                                  # it already changed the copy...
        w.find_by_id("lean").extra_triggers.append("Z")
        w.share_trigger("F", ["fire", "interact"])
        raise ProfileError("boom")                                # ...and then it failed
    r = editor(p).run(half)
    assert not r.ok and r.message == "boom" and state(p) == before and [id(b) for b in p.bindings] == objects
    assert p.find_by_id("lean").extra_triggers == ["E"] and not p.shared_triggers


def test_an_operation_that_makes_the_profile_invalid_after_changing_it_leaves_no_trace():
    p = fire_interact()
    before = state(p)
    r = editor(p).run(lambda w: w.find_by_id("reload").extra_triggers.append("R"))             # R listed twice: an analyzer ERROR
    assert not r.ok and "invalid" in r.message and state(p) == before
