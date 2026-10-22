"""How intentional sharing is recorded, kept and cleaned up (Phase 2, step 7).

The representation: an optional top-level list in a VERSION-2 profile file,
    "shared_triggers": [{"trigger": "F", "bindings": ["fire", "interact"]}]
(in memory: Profile.shared_triggers = {"F": ["fire", "interact"]}). It names the bindings that may run together on a trigger, by
the binding ids that version 2 stores anyway. Nothing else ever makes bindings share a trigger: repeated inputs alone do not, so
a profile without the list behaves exactly as before and needs no migration (no version bump). Only groups that still describe
something are written; stale entries are dropped. Version-1 files cannot carry it (a `shared_triggers` key there is just an
unknown field, kept as such)."""
import copy
import json
from pathlib import Path

import pytest

from app.mapping import layout_edit as le
from app.mapping.profile import (BACKUP_FOLDER, REPAIR_BACKUP_FOLDER, UPGRADE_BACKUP_FOLDER, Action, Binding, Profile, ProfileError,
                                 ProfileStore, Stick, effective_sharing, trigger_owners, trigger_runners)

GOLDEN = Path(__file__).parent / "golden" / "profile_v2_shared.json"


def golden_profile():
    p = Profile("Shared Golden", orientation="landscape", id="fedcbafedcba")
    p.bindings = [
        Binding("F", 0.8, 0.7, "hold", "Fire", id="fire", extra_triggers=["MMB"]),
        Binding("F", 0.5, 0.5, "tap", "Interact", id="interact", extra_actions=[Action("tap", 0.2, 0.2, delay_ms=60)]),
        Binding("R", 0.7, 0.9, "tap", "Reload", id="reload"),
        Binding("R", 0.3, 0.9, "tap", "Accidental twin of Reload", id="twin"),
    ]
    p.share_trigger("F", ["fire", "interact"])
    return p


def three(trigger="F"):
    p = Profile("p")
    p.bindings = [Binding(trigger, 0.1, 0.1, id="a"), Binding(trigger, 0.2, 0.2, id="b"), Binding(trigger, 0.3, 0.3, id="c"),
                  Binding("Z", 0.9, 0.9, id="z")]
    return p


def text(path):
    return path.read_text(encoding="utf-8")


def groups(p):
    return {t: [b.id for b in members] for t, members in p.sharing_groups().items()}


# ===================================================================== the file format
def test_the_sharing_list_is_written_exactly_like_the_golden_file(tmp_path):
    ProfileStore(tmp_path).save(golden_profile())
    assert text(next(tmp_path.glob("*.json"))) == text(GOLDEN)


def test_the_golden_file_has_the_list_after_looks_and_names_every_participant_by_id():
    data = json.loads(text(GOLDEN))
    assert data["version"] == 2 and list(data)[-1] == "shared_triggers"
    assert data["shared_triggers"] == [{"trigger": "F", "bindings": ["fire", "interact"]}]
    assert "twin" in [b["id"] for b in data["bindings"]]                        # the unauthorized duplicate is kept, and not in the list


def test_loading_and_saving_the_golden_file_changes_nothing(tmp_path):
    (tmp_path / "g.json").write_text(text(GOLDEN), encoding="utf-8")
    store = ProfileStore(tmp_path)
    store.save(store.load("Shared Golden"))
    assert text(tmp_path / "g.json") == text(GOLDEN) and not (tmp_path / UPGRADE_BACKUP_FOLDER).exists()


def test_a_profile_without_sharing_gets_no_list_and_its_file_is_unchanged():
    p = golden_profile()
    p.unshare_trigger("F")
    assert "shared_triggers" not in p.to_dict()


def test_a_profile_with_sharing_always_needs_version_2():
    p = Profile("p")
    p.bindings = [Binding("F", 0.1, 0.1, id="a", extra_triggers=["G"]), Binding("G", 0.2, 0.2, id="b")]
    assert p.required_version == 2
    p.share_trigger("G", ["a", "b"])
    assert p.to_dict()["version"] == 2 and p.to_dict()["shared_triggers"] == [{"trigger": "G", "bindings": ["a", "b"]}]


def test_the_group_is_written_in_binding_list_order_whatever_order_it_was_made_in():
    p = three()
    p.share_trigger("F", ["c", "a", "b"])
    assert p.to_dict()["shared_triggers"][0]["bindings"] == ["a", "b", "c"]


# ===================================================================== it survives everything that touches a profile file
def test_sharing_survives_save_load_and_the_restore_point(tmp_path):
    store = ProfileStore(tmp_path)
    original = golden_profile()
    store.save(original)
    store.save_checkpoint(original)
    for loaded in (store.load("Shared Golden"), store.load_checkpoint("Shared Golden")):
        assert groups(loaded) == {"F": ["fire", "interact"]} and loaded.id == "fedcbafedcba" and loaded.shared_triggers == {"F": ["fire", "interact"]}


def test_sharing_survives_a_copy_made_with_to_dict_and_from_dict():
    copy_ = Profile.from_dict(golden_profile().to_dict())
    assert groups(copy_) == {"F": ["fire", "interact"]} and copy_.id == ""


def test_sharing_survives_renaming_the_profile_and_reordering_its_bindings(tmp_path):
    store = ProfileStore(tmp_path)
    p = golden_profile()
    store.save(p)
    p.name = "Renamed"
    p.bindings.reverse()                                                         # twin, reload, interact, fire
    store.save(p)
    loaded = store.load("Renamed")
    assert [b.id for b in loaded.bindings] == ["twin", "reload", "interact", "fire"]
    assert groups(loaded) == {"F": ["interact", "fire"]} and loaded.id == "fedcbafedcba"       # the run order follows the list now
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_the_permanent_profile_id_and_binding_ids_are_untouched_by_sharing(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(golden_profile())
    raw = json.loads(text(next(tmp_path.glob("*.json"))))
    assert raw["id"] == "fedcbafedcba" and [b["id"] for b in raw["bindings"]] == ["fire", "interact", "reload", "twin"]


def test_unknown_fields_in_the_sharing_list_and_in_the_profile_are_kept(tmp_path):
    raw = json.loads(text(GOLDEN))
    raw["shared_triggers"][0]["from_the_future"] = {"x": [1]}
    raw["profile_future"] = "kept"
    (tmp_path / "g.json").write_text(json.dumps(raw), encoding="utf-8")
    store = ProfileStore(tmp_path)
    store.save(store.load("Shared Golden"))
    saved = json.loads(text(tmp_path / "g.json"))
    assert saved["shared_triggers"][0]["from_the_future"] == {"x": [1]} and saved["profile_future"] == "kept"


def test_two_entries_for_one_trigger_are_merged_and_the_order_of_their_ids_does_not_matter():
    raw = json.loads(text(GOLDEN))
    raw["shared_triggers"] = [{"trigger": "F", "bindings": ["interact"]}, {"trigger": "F", "bindings": ["fire", "interact"]}]
    assert groups(Profile.from_dict(raw)) == {"F": ["fire", "interact"]}


def test_a_trigger_written_with_modifiers_in_another_order_is_the_same_trigger():
    raw = {"version": 2, "name": "n", "bindings": [
        {"type": "binding", "id": "a", "triggers": ["Ctrl+Shift+F"], "actions": [{"mode": "tap", "x": 0.1, "y": 0.1}], "on_failure": "continue"},
        {"type": "binding", "id": "b", "triggers": ["Ctrl+Shift+F"], "actions": [{"mode": "tap", "x": 0.2, "y": 0.2}], "on_failure": "continue"}],
        "shared_triggers": [{"trigger": "Shift+Ctrl+F", "bindings": ["a", "b"]}]}
    p = Profile.from_dict(raw)
    assert groups(p) == {"Ctrl+Shift+F": ["a", "b"]} and p.to_dict()["shared_triggers"][0]["trigger"] == "Ctrl+Shift+F"


# ===================================================================== repeated inputs alone never authorize anything
def test_a_version_1_file_with_repeated_inputs_converted_to_version_2_still_has_no_sharing(tmp_path):
    (tmp_path / "old.json").write_text(json.dumps({"id": "ddddeeeeffff", "version": 1, "name": "old", "bindings": [
        {"type": "tap", "input": "R", "x": 0.1, "y": 0.1, "mode": "hold"}, {"type": "tap", "input": "R", "x": 0.9, "y": 0.9, "mode": "hold"}]}),
        encoding="utf-8")
    store = ProfileStore(tmp_path)
    p = store.load("old")
    assert p.shared_triggers == {} and p.notes
    store.save(p)
    saved = json.loads(text(tmp_path / "old.json"))
    assert saved["version"] == 2 and "shared_triggers" not in saved and (tmp_path / UPGRADE_BACKUP_FOLDER / "old.json").exists()
    assert store.load("old").shared_triggers == {}


def test_saving_a_profile_with_unauthorized_duplicates_never_adds_an_authorization(tmp_path):
    p = Profile("p")
    p.bindings = [Binding("R", 0.1, 0.1, id="a"), Binding("R", 0.9, 0.9, id="b")]
    store = ProfileStore(tmp_path)
    store.save(p)
    for _ in range(3):
        store.save(store.load("p"))
    assert "shared_triggers" not in json.loads(text(next(tmp_path.glob("*.json")))) and runners_of(store.load("p"))["R"] == ["b"]


def test_a_version_1_file_carrying_a_sharing_list_keeps_it_as_an_unknown_field_and_does_not_interpret_it(tmp_path):
    raw = {"version": 1, "name": "v1", "bindings": [{"type": "tap", "input": "R", "x": 0.1, "y": 0.1, "mode": "hold"}],
           "shared_triggers": [{"trigger": "R", "bindings": ["x", "y"]}]}
    (tmp_path / "v1.json").write_text(json.dumps(raw), encoding="utf-8")
    store = ProfileStore(tmp_path)
    p = store.load("v1")
    assert p.shared_triggers == {} and p.unknown == {"shared_triggers": raw["shared_triggers"]}
    store.save(p)
    assert json.loads(text(tmp_path / "v1.json"))["shared_triggers"] == raw["shared_triggers"] and json.loads(text(tmp_path / "v1.json"))["version"] == 1


def runners_of(p):
    return {t: [b.id for b in bs] for t, bs in p.runners().items()}


# ===================================================================== stale or malformed entries
def test_entries_that_no_longer_describe_anything_are_dropped_on_load_with_a_note_and_never_enable_sharing():
    raw = json.loads(text(GOLDEN))
    raw["shared_triggers"] = [{"trigger": "F", "bindings": ["fire", "ghost"]}, {"trigger": "R", "bindings": ["reload", "fire"]},
                              {"trigger": "MMB", "bindings": ["fire"]}]
    p = Profile.from_dict(raw)
    assert p.shared_triggers == {} and p.sharing_groups() == {}
    assert len(p.repairs) == 3 and all("Sharing of" in note for note in p.repairs)
    assert runners_of(p)["F"] == ["interact"] and runners_of(p)["R"] == ["twin"]


def test_a_group_that_loses_one_stale_participant_keeps_the_others():
    raw = json.loads(text(GOLDEN))
    raw["bindings"].append({"type": "binding", "id": "third", "triggers": ["F"], "actions": [{"mode": "tap", "x": 0.4, "y": 0.4}], "on_failure": "continue"})
    raw["shared_triggers"] = [{"trigger": "F", "bindings": ["fire", "interact", "third", "ghost"]}]
    p = Profile.from_dict(raw)
    assert groups(p) == {"F": ["fire", "interact", "third"]} and any("lost 1" in n for n in p.repairs)


def test_a_binding_id_that_names_two_bindings_is_not_a_participant():
    p = three()
    p.share_trigger("F", ["a", "b", "c"])
    p.bindings[2].id = "a"                                                       # now 'a' names two bindings (hand-made damage)
    assert groups(p) == {}                                                        # only 'b' is unambiguous: one participant is no group
    assert p.runners()["F"] == (p.bindings[2],)                                   # so the old single-winner rule applies again (the later one)
    p.bindings[2].id = "c2"                                                       # the damage is repaired by giving it its own id ...
    assert groups(p) == {"F": ["a", "b"]}                                         # ... a and b are valid participants again, c2 never was one
    assert p.prune_sharing() and p.shared_triggers == {"F": ["a", "b"]}           # and the stale 'c' entry is pruned, c2 is NOT added


@pytest.mark.parametrize("bad", ["F", {"trigger": "F"}, [{"trigger": "F"}], [{"bindings": ["a"]}], [{"trigger": "", "bindings": ["a", "b"]}],
                                 [{"trigger": "F", "bindings": "ab"}], [{"trigger": "F", "bindings": ["a", 5]}], [None]])
def test_malformed_sharing_lists_are_refused_strictly(bad):
    raw = json.loads(text(GOLDEN))
    raw["shared_triggers"] = bad
    with pytest.raises(ProfileError):
        Profile.from_dict(raw)


def test_in_repair_mode_malformed_entries_are_skipped_and_the_good_ones_kept():
    raw = json.loads(text(GOLDEN))
    raw["shared_triggers"] = [{"trigger": "F"}, {"trigger": "F", "bindings": ["fire", "interact"]}, "junk"]
    p = Profile.from_dict(raw, repair=True)
    assert groups(p) == {"F": ["fire", "interact"]} and len(p.repairs) == 2
    q = Profile.from_dict({**raw, "shared_triggers": "oops"}, repair=True)
    assert q.shared_triggers == {} and q.repairs


def test_the_store_repair_backs_up_the_original_before_dropping_stale_sharing(tmp_path):
    raw = json.loads(text(GOLDEN))
    raw["id"] = "fedcbafedcba"
    raw["shared_triggers"].append({"trigger": "R", "bindings": ["reload", "missing"]})
    (tmp_path / "g.json").write_text(json.dumps(raw), encoding="utf-8")
    store = ProfileStore(tmp_path)
    repaired = store.repair("Shared Golden")
    assert groups(repaired) == {"F": ["fire", "interact"]}
    assert json.loads(text(tmp_path / REPAIR_BACKUP_FOLDER / "g.json")) == raw
    assert json.loads(text(tmp_path / "g.json"))["shared_triggers"] == [{"trigger": "F", "bindings": ["fire", "interact"]}]


def test_analysis_reports_stale_entries_that_were_made_in_memory():
    p = three()
    p.shared_triggers = {"F": ["a", "ghost"], "Z": ["z", "a"]}
    stale = [i for i in p.analyze() if i.code == "sharing_metadata_invalid"]
    assert {i.trigger for i in stale} == {"F", "Z"}
    saved = p.to_dict()
    assert "shared_triggers" not in saved                                          # and nothing stale is ever written


def test_a_future_version_with_sharing_is_refused_and_left_alone(tmp_path):
    raw = json.loads(text(GOLDEN))
    raw["version"] = 3
    (tmp_path / "f.json").write_text(json.dumps(raw), encoding="utf-8")
    store = ProfileStore(tmp_path)
    with pytest.raises(ProfileError, match="newer version"):
        store.load("Shared Golden")
    store.save(Profile("other"))
    assert json.loads(text(tmp_path / "f.json")) == raw and not (tmp_path / BACKUP_FOLDER).exists()


# ===================================================================== the explicit operations
def test_share_trigger_authorizes_exactly_the_named_bindings():
    p = three()
    assert p.share_trigger("F", ["a", "c"]) == ["a", "c"]
    assert groups(p) == {"F": ["a", "c"]} and runners_of(p)["F"] == ["a", "c"]
    assert p.share_trigger("F", ["b", "c"]) == ["a", "b", "c"]                      # an existing authorization stays
    assert p.is_shared("F") and not p.is_shared("Z")


@pytest.mark.parametrize("trigger, ids, why", [
    ("F", ["a"], "at least two"), ("F", ["a", "ghost"], "no single control"), ("F", ["a", "z"], "does not use"), ("Q", ["a", "b"], "does not use"),
    ("", ["a", "b"], "input"), ("Ctrl+Shift", ["a", "b"], None)])
def test_share_trigger_refuses_what_it_cannot_make_unambiguous_and_changes_nothing(trigger, ids, why):
    p = three()
    before = copy.deepcopy(p.shared_triggers), p.to_dict()
    with pytest.raises(ProfileError, match=why):
        p.share_trigger(trigger, ids)
    assert (p.shared_triggers, p.to_dict()) == before


def test_a_stick_key_cannot_be_shared():
    p = Profile("p", bindings=[Binding("R", 0.1, 0.1, id="a", extra_triggers=["W"]), Binding("T", 0.2, 0.2, id="b", extra_triggers=["W"])])
    p.sticks.append(Stick(0.15, 0.7))
    with pytest.raises(ProfileError, match="stick"):
        p.share_trigger("W", ["a", "b"])
    assert p.shared_triggers == {}


def test_a_binding_with_two_ids_in_a_row_cannot_be_authorized():
    p = three()
    p.bindings[1].id = "a"
    with pytest.raises(ProfileError, match="no single control"):
        p.share_trigger("F", ["a", "c"])


def test_unshare_withdraws_one_binding_or_the_whole_group_and_never_touches_the_bindings():
    p = three()
    p.share_trigger("F", ["a", "b", "c"])
    assert p.unshare_trigger("F", "b") is True and groups(p) == {"F": ["a", "c"]}
    assert p.unshare_trigger("F", "ghost") is False and p.unshare_trigger("Q") is False
    assert p.unshare_trigger("F", "a") is True and p.shared_triggers == {}               # fewer than two left: the group is gone
    assert [b.id for b in p.bindings] == ["a", "b", "c", "z"]
    p.share_trigger("F", ["a", "b"])
    assert p.unshare_trigger("F") is True and p.shared_triggers == {}


def test_add_shared_does_not_authorize_unless_asked_and_authorizes_every_common_input_when_asked():
    p = Profile("p", bindings=[Binding("F", 0.1, 0.1, id="a", extra_triggers=["G"])])
    p.add_shared(Binding("F", 0.2, 0.2, id="b"))
    assert p.shared_triggers == {} and runners_of(p)["F"] == ["b"]
    q = Profile("q", bindings=[Binding("F", 0.1, 0.1, id="a", extra_triggers=["G"])])
    q.add_shared(Binding("F", 0.2, 0.2, id="b", extra_triggers=["G"]), authorize=True)
    assert groups(q) == {"F": ["a", "b"], "G": ["a", "b"]}


def test_add_trigger_with_shared_authorizes_every_control_that_uses_it_and_without_it_is_refused():
    p = three("F")
    p.add_trigger("z", "H")
    p.add_trigger("b", "H", shared=True)
    assert groups(p) == {"H": ["b", "z"]}
    with pytest.raises(ProfileError, match="shared=True"):
        p.add_trigger("c", "H")
    assert groups(p) == {"H": ["b", "z"]}


def test_removing_a_binding_removes_it_from_its_groups_and_ends_groups_that_fall_below_two():
    p = three()
    p.share_trigger("F", ["a", "b", "c"])
    p.remove_by_id("b")
    assert groups(p) == {"F": ["a", "c"]} and p.shared_triggers == {"F": ["a", "c"]}
    p.remove_by_id("a")
    assert p.shared_triggers == {} and [b.id for b in p.bindings] == ["c", "z"]             # the obsolete entry is gone, not left dangling


def test_removing_a_binding_leaves_other_groups_and_unrelated_bindings_alone():
    p = Profile("p")
    p.bindings = [Binding("F", 0.1, 0.1, id="a"), Binding("F", 0.2, 0.2, id="b"), Binding("G", 0.3, 0.3, id="c"), Binding("G", 0.4, 0.4, id="d"),
                  Binding("H", 0.5, 0.5, id="e")]
    p.share_trigger("F", ["a", "b"])
    p.share_trigger("G", ["c", "d"])
    p.remove_by_id("a")
    assert groups(p) == {"G": ["c", "d"]} and [b.id for b in p.bindings] == ["b", "c", "d", "e"]


def test_removing_a_trigger_from_one_participant_takes_it_out_of_that_group_only():
    p = Profile("p")
    p.bindings = [Binding("E", 0.1, 0.1, id="a", extra_triggers=["F", "G"]), Binding("F", 0.2, 0.2, id="b"), Binding("G", 0.3, 0.3, id="c", extra_triggers=["F"])]
    p.share_trigger("F", ["a", "b", "c"])
    p.share_trigger("G", ["a", "c"])
    assert p.remove_trigger("a", "F") == "removed"
    assert groups(p) == {"F": ["b", "c"], "G": ["a", "c"]}                                  # a keeps G
    assert p.remove_trigger("c", "G") == "promoted"                                          # G was c's first input: F took its place
    assert groups(p) == {"F": ["b", "c"]} and "G" not in p.shared_triggers


def test_promoting_a_trigger_does_not_change_which_groups_a_binding_belongs_to():
    p = Profile("p")
    p.bindings = [Binding("E", 0.1, 0.1, id="a", extra_triggers=["F"]), Binding("F", 0.2, 0.2, id="b")]
    p.share_trigger("F", ["a", "b"])
    assert p.remove_trigger("a", "E") == "promoted" and p.find_by_id("a").input == "F"
    assert groups(p) == {"F": ["a", "b"]}                                                    # F is now a's first input; still shared


def test_moving_a_trigger_neither_keeps_the_source_in_the_group_nor_authorizes_the_destination():
    p = Profile("p")
    p.bindings = [Binding("E", 0.1, 0.1, id="a", extra_triggers=["F"]), Binding("F", 0.2, 0.2, id="b"), Binding("R", 0.3, 0.3, id="c"),
                  Binding("T", 0.4, 0.4, id="d")]
    p.share_trigger("F", ["a", "b"])
    p.move_trigger("F", "a", "c", shared=True)                      # c now uses F too (b already did); a no longer does
    assert p.shared_triggers == {} and "c" not in p.shared_triggers.get("F", [])
    assert runners_of(p)["F"] == ["b"]                                      # unauthorized again: b's FIRST input F beats c's additional F
    assert [b.id for b in p.bindings] == ["a", "b", "c", "d"]


def test_a_refused_operation_leaves_the_bindings_and_the_sharing_exactly_as_they_were():
    p = three()
    p.share_trigger("F", ["a", "b"])
    before = (copy.deepcopy(p.shared_triggers), p.to_dict())
    for call in (lambda: p.remove_trigger("a", "F"), lambda: p.remove_trigger("ghost", "F"), lambda: p.move_trigger("F", "a", "a"),
                 lambda: p.move_trigger("F", "a", "z"), lambda: p.add_trigger("z", "F"), lambda: p.share_trigger("F", ["a"])):
        with pytest.raises(ProfileError):
            call()
        assert (p.shared_triggers, p.to_dict()) == before


def test_removing_by_input_replacing_a_binding_and_a_new_stick_all_keep_the_metadata_consistent():
    p = three()
    p.share_trigger("F", ["a", "b", "c"])
    p.add(Binding("F", 0.7, 0.7, id="new"))                         # replaces every binding on F
    assert p.shared_triggers == {} and [b.id for b in p.bindings] == ["z", "new"]
    q = three()
    q.share_trigger("F", ["a", "b"])
    q.remove("F")
    assert q.shared_triggers == {}
    r = three("W")
    r.share_trigger("W", ["a", "b"])
    r.set_stick(Stick(0.15, 0.7))
    assert r.shared_triggers == {} and [b.id for b in r.bindings] == ["z"]


def test_the_editors_deletion_paths_keep_the_metadata_consistent():
    p = three()
    p.share_trigger("F", ["a", "b", "c"])
    assert le.remove_binding(p, "b") is not None and groups(p) == {"F": ["a", "c"]}
    hit = le.pin(p, le.Hit("tap", 0))
    assert le.delete(p, hit) is True and p.shared_triggers == {}


def test_prune_sharing_only_ever_removes():
    p = three()
    p.share_trigger("F", ["a", "b"])
    before = copy.deepcopy(p.shared_triggers)
    assert p.prune_sharing() == [] and p.shared_triggers == before
    p.shared_triggers["Z"] = ["z"]
    p.shared_triggers["F"] = ["a", "b", "ghost"]
    notes = p.prune_sharing()
    assert len(notes) == 2 and p.shared_triggers == {"F": ["a", "b"]}


# ===================================================================== the pure resolution
def test_effective_sharing_needs_exactly_one_binding_per_id_that_uses_the_trigger_and_two_participants():
    bindings = [Binding("F", 0.1, 0.1, id="a"), Binding("F", 0.2, 0.2, id="b"), Binding("G", 0.3, 0.3, id="c", extra_triggers=["F"])]
    assert [b.id for b in effective_sharing(bindings, {"F": ["a", "c"]})["F"]] == ["a", "c"]
    assert effective_sharing(bindings, {"F": ["a"]}) == {} and effective_sharing(bindings, {"F": ["a", "ghost"]}) == {}
    assert effective_sharing(bindings, {"G": ["a", "c"]}) == {} and effective_sharing(bindings, None) == {}


def test_trigger_runners_is_the_legacy_winner_everywhere_nothing_is_authorized():
    bindings = [Binding("E", 0.1, 0.1, id="a", extra_triggers=["F"]), Binding("F", 0.2, 0.2, id="b"), Binding("R", 0.3, 0.3, id="c", extra_triggers=["F", "G"]),
                Binding("T", 0.4, 0.4, id="d", extra_triggers=["G"])]
    owners = trigger_owners(bindings)
    runners = trigger_runners(bindings)
    assert {t: (b,) for t, b in owners.items()} == runners
    shared = trigger_runners(bindings, {"F": ["a", "b", "c"]})
    assert [b.id for b in shared["F"]] == ["a", "b", "c"] and shared["G"] == runners["G"] and shared["E"] == runners["E"]


def test_a_version_1_sharing_list_is_never_interpreted_even_if_it_happens_to_name_real_ids(monkeypatch):
    # Ids of a version-1 file are made up when it is loaded; here they are fixed so that the list WOULD match if it were read.
    from app.mapping import profile as profile_module
    counter = iter(f"x{i}" for i in range(10))
    monkeypatch.setattr(profile_module, "new_binding_id", lambda: next(counter))
    raw = {"version": 1, "name": "v1", "bindings": [{"type": "tap", "input": "R", "x": 0.1, "y": 0.1, "mode": "hold"},
                                                    {"type": "tap", "input": "R", "x": 0.9, "y": 0.9, "mode": "hold"}],
           "shared_triggers": [{"trigger": "R", "bindings": ["x0", "x1"]}]}
    p = Profile.from_dict(raw)
    assert [b.id for b in p.bindings] == ["x0", "x1"] and p.shared_triggers == {} and p.sharing_groups() == {}
    assert [b.id for b in p.runners()["R"]] == ["x1"]                                # still only the winner
    assert p.unknown["shared_triggers"] == raw["shared_triggers"]                       # and the list is kept, untouched, as data
