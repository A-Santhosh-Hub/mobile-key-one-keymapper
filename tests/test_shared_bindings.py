"""Shared bindings survive saving and loading (Phase 2, step 3).

The hazard: two bindings that share an input, e.g. LMB -> fire and LMB -> another action. In the flat version-1 form a profile
file means "one binding per input", and the old loader replaced a repeated input, so one of the two could vanish on reload.
The rules proved here:
  * a profile with two bindings on the same first input is written as version 2, which keeps every binding, in order;
  * an ordinary profile is still written as version 1, byte for byte;
  * a version-1 FILE that already repeats an input loads with every binding kept and a note, is never rewritten by loading,
    and is copied to backup-before-v2/ before the first save converts it;
  * nothing is ever dropped silently, and ids, unknown fields, the profile id and future-version files are kept.
Every store lives in tmp_path."""
import json
from pathlib import Path

import pytest

from app.mapping.profile import (BACKUP_FOLDER, REPAIR_BACKUP_FOLDER, UPGRADE_BACKUP_FOLDER, Action, Binding, Profile,
                                 ProfileError, ProfileStore, Stick)

GOLDEN_V1 = Path(__file__).parent / "golden" / "profile_v1.json"


def text(path):
    return path.read_text(encoding="utf-8")


def shared_profile():
    p = Profile("Shared", orientation="landscape", id="aaaabbbbcccc")
    p.add(Binding("LMB", 0.8, 0.7, "hold", "Fire", id="fire", also=[(0.1, 0.4)]))
    p.add(Binding("R", 0.7, 0.9, "tap", "Reload", id="reload"))
    p.add_shared(Binding("LMB", 0.5, 0.2, "tap", "Peek", id="peek"))
    p.add_shared(Binding("LMB", 0.3, 0.3, "swipe", "Flick", id="flick", x2=0.6, y2=0.3, scope=True,
                         extra_triggers=["MMB"], extra_actions=[Action("tap", 0.4, 0.4, delay_ms=50)]))
    p.set_stick(Stick(0.15, 0.7))
    return p


def v1_file_with_duplicates(**more):
    return {"id": "ddddeeeeffff", "version": 1, "name": "dup", "orientation": "any", "package": "", "sticks": [], "looks": [],
            "bindings": [{"type": "tap", "input": "LMB", "x": 0.8, "y": 0.7, "mode": "hold", "label": "Fire", "tag": "keep-me"},
                         {"type": "tap", "input": "R", "x": 0.7, "y": 0.9, "mode": "tap", "label": "Reload"},
                         {"type": "tap", "input": "LMB", "x": 0.5, "y": 0.2, "mode": "tap", "label": "Peek"}], **more}


def summary(profile):
    return [(b.id, b.input, b.label, b.mode, b.triggers, [(a.mode, a.x, a.y, a.delay_ms) for a in b.actions]) for b in profile.bindings]


# ===================================================================== which version is written
def test_two_bindings_on_one_input_make_the_profile_version_2():
    p = Profile("p")
    p.add(Binding("LMB", 0.8, 0.7, "hold"))
    assert p.required_version == 1 and p.to_dict()["version"] == 1
    p.add_shared(Binding("LMB", 0.5, 0.2, "tap"))
    assert p.has_shared_inputs() and p.required_version == 2 and p.to_dict()["version"] == 2


def test_the_version_is_chosen_for_bindings_put_into_the_list_by_hand_too_so_nothing_can_be_lost():
    p = Profile("p")
    p.bindings = [Binding("A", 0.1, 0.1), Binding("A", 0.2, 0.2)]
    assert p.to_dict()["version"] == 2 and len(p.to_dict()["bindings"]) == 2


def test_an_ordinary_profile_is_still_version_1_and_byte_for_byte_what_it_was(tmp_path):
    golden = text(GOLDEN_V1)
    (tmp_path / "Golden.json").write_text(golden, encoding="utf-8")
    store = ProfileStore(tmp_path)
    p = store.load("Golden")
    assert not p.has_shared_inputs() and p.required_version == 1 and p.notes == []
    store.save(p)
    assert text(tmp_path / "Golden.json") == golden
    assert not (tmp_path / UPGRADE_BACKUP_FOLDER).exists() and not (tmp_path / BACKUP_FOLDER).exists()


def test_sharing_that_goes_away_again_lets_an_in_memory_profile_return_to_version_1():
    p = Profile("p")
    p.add(Binding("LMB", 0.8, 0.7, "hold", id="a"))
    p.add_shared(Binding("LMB", 0.5, 0.2, "tap", id="b"))
    p.remove_by_id("b")
    assert p.required_version == 1


# ===================================================================== round trips: every binding and its order survive
def test_two_bindings_sharing_an_input_both_survive_a_save_and_a_load_in_order(tmp_path):
    store = ProfileStore(tmp_path)
    original = shared_profile()
    store.save(original)
    loaded = store.load("Shared")
    assert json.loads(text(next(tmp_path.glob("*.json"))))["version"] == 2
    assert [b.id for b in loaded.bindings] == ["fire", "reload", "peek", "flick"]
    assert [b.input for b in loaded.bindings].count("LMB") == 3
    assert summary(loaded) == summary(original)


def test_every_property_of_every_shared_binding_survives_the_round_trip(tmp_path):
    store = ProfileStore(tmp_path)
    original = shared_profile()
    store.save(original)
    loaded = store.load("Shared")
    for mine, theirs in zip(original.bindings, loaded.bindings):
        assert mine == theirs and mine.id == theirs.id and mine.actions == theirs.actions
    flick = loaded.find_by_id("flick")
    assert flick.scope and flick.extra_triggers == ["MMB"] and flick.extra_actions[0].delay_ms == 50
    assert loaded.find_by_id("fire").also == [(0.1, 0.4)] and loaded.id == "aaaabbbbcccc"
    assert loaded.sticks == original.sticks


def test_saving_and_loading_again_and_again_never_changes_the_file_or_loses_a_binding(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(shared_profile())
    first = text(next(tmp_path.glob("*.json")))
    for _ in range(3):
        store.save(store.load("Shared"))
    assert text(next(tmp_path.glob("*.json"))) == first
    assert [b.id for b in store.load("Shared").bindings] == ["fire", "reload", "peek", "flick"]


def test_the_list_order_is_what_is_written_even_after_reordering(tmp_path):
    store = ProfileStore(tmp_path)
    p = shared_profile()
    p.bindings.reverse()
    store.save(p)
    assert [b.id for b in store.load("Shared").bindings] == ["flick", "peek", "reload", "fire"]


def test_a_copy_made_with_to_dict_keeps_all_shared_bindings():
    original = shared_profile()
    copy = Profile.from_dict(original.to_dict())
    assert summary(copy) == summary(original) and copy.id == ""


def test_the_restore_point_of_a_shared_profile_keeps_all_bindings_and_the_profile_id(tmp_path):
    store = ProfileStore(tmp_path)
    original = shared_profile()
    store.save(original)
    store.save_checkpoint(original)
    restored = store.load_checkpoint("Shared")
    assert summary(restored) == summary(original) and restored.id == "aaaabbbbcccc"
    assert json.loads(text(next((tmp_path / "saved").glob("*.json"))))["version"] == 2


def test_unknown_fields_on_shared_bindings_are_kept(tmp_path):
    p = shared_profile()
    p.find_by_id("peek").unknown = {"future": {"a": [1]}}
    p.find_by_id("peek").extra_actions.append(Action("tap", 0.2, 0.2, delay_ms=5, unknown={"act": 1}))
    store = ProfileStore(tmp_path)
    store.save(p)
    loaded = store.load("Shared")
    assert loaded.find_by_id("peek").unknown == {"future": {"a": [1]}}
    assert loaded.find_by_id("peek").extra_actions[0].unknown == {"act": 1}


def test_a_shared_profile_is_not_confused_with_a_replacing_one_in_the_same_store(tmp_path):
    store = ProfileStore(tmp_path)
    replacing = Profile("Replacing")
    replacing.add(Binding("LMB", 0.8, 0.7, "hold", id="a"))
    replacing.add(Binding("LMB", 0.5, 0.2, "tap", id="b"))                      # add REPLACES: one binding is meant
    store.save(replacing)
    store.save(shared_profile())
    assert [(b.input, b.x) for b in store.load("Replacing").bindings] == [("LMB", 0.5)]      # (version 1 stores no binding ids)
    assert len(store.load("Shared").bindings) == 4


# ===================================================================== a version-1 FILE that repeats an input
def test_loading_a_version_1_file_that_repeats_an_input_keeps_every_binding_in_order_and_reports_it(tmp_path):
    (tmp_path / "dup.json").write_text(json.dumps(v1_file_with_duplicates()), encoding="utf-8")
    p = ProfileStore(tmp_path).load("dup")
    assert [(b.input, b.label) for b in p.bindings] == [("LMB", "Fire"), ("R", "Reload"), ("LMB", "Peek")]
    assert p.schema_version == 1 and p.required_version == 2
    assert len(p.notes) == 1 and "1, 3" in p.notes[0] and "'LMB'" in p.notes[0] and "version-2" in p.notes[0]
    assert p.repairs == []                                                       # nothing was changed or skipped


def test_the_same_note_is_given_for_the_profile_reader_without_a_store():
    p = Profile.from_dict(v1_file_with_duplicates())
    assert [b.label for b in p.bindings] == ["Fire", "Reload", "Peek"] and len(p.notes) == 1


def test_loading_never_rewrites_that_file_or_makes_a_backup(tmp_path):
    original = json.dumps(v1_file_with_duplicates())
    (tmp_path / "dup.json").write_text(original, encoding="utf-8")
    store = ProfileStore(tmp_path)
    store.load("dup")
    store.repair("dup")                                                          # nothing to repair: still untouched
    assert text(tmp_path / "dup.json") == original
    assert not (tmp_path / UPGRADE_BACKUP_FOLDER).exists() and not (tmp_path / REPAIR_BACKUP_FOLDER).exists()


def test_the_first_save_converts_it_to_version_2_after_copying_the_original(tmp_path):
    original = json.dumps(v1_file_with_duplicates())
    (tmp_path / "dup.json").write_text(original, encoding="utf-8")
    store = ProfileStore(tmp_path)
    p = store.load("dup")
    runtime_ids = [b.id for b in p.bindings]
    store.save(p)
    assert text(tmp_path / UPGRADE_BACKUP_FOLDER / "dup.json") == original
    saved = json.loads(text(tmp_path / "dup.json"))
    assert saved["version"] == 2 and saved["id"] == "ddddeeeeffff"
    assert [b["id"] for b in saved["bindings"]] == runtime_ids                  # the ids it had in memory are the ones kept
    assert [b["label"] for b in saved["bindings"]] == ["Fire", "Reload", "Peek"]
    assert saved["bindings"][0]["tag"] == "keep-me"                              # an unknown field of the old file came along
    again = store.load("dup")
    assert summary(again) == summary(p) and again.notes == []                    # now it is just a version-2 profile


def test_later_saves_do_not_back_up_again_and_never_overwrite_the_first_backup(tmp_path):
    original = json.dumps(v1_file_with_duplicates())
    (tmp_path / "dup.json").write_text(original, encoding="utf-8")
    store = ProfileStore(tmp_path)
    p = store.load("dup")
    store.save(p)
    p.find("R").x = 0.6
    store.save(p)
    store.save(store.load("dup"))
    assert [f.name for f in (tmp_path / UPGRADE_BACKUP_FOLDER).iterdir()] == ["dup.json"]
    assert text(tmp_path / UPGRADE_BACKUP_FOLDER / "dup.json") == original


def test_if_the_original_cannot_be_copied_nothing_is_saved_in_the_new_format(tmp_path, monkeypatch):
    original = json.dumps(v1_file_with_duplicates())
    (tmp_path / "dup.json").write_text(original, encoding="utf-8")
    store = ProfileStore(tmp_path)
    p = store.load("dup")
    monkeypatch.setattr(ProfileStore, "_backup", lambda self, file, folder=BACKUP_FOLDER: False)
    with pytest.raises(ProfileError, match="nothing was changed"):
        store.save(p)
    assert text(tmp_path / "dup.json") == original


def test_a_profile_that_becomes_version_2_through_a_new_capability_is_also_backed_up_first(tmp_path):
    store = ProfileStore(tmp_path)
    p = Profile("p", bindings=[Binding("R", 0.1, 0.1, "tap", id="r")])
    store.save(p)
    version_1_text = text(next(tmp_path.glob("p.json")))
    p.find_by_id("r").add_trigger("F")
    store.save(p)
    assert text(tmp_path / UPGRADE_BACKUP_FOLDER / "p.json") == version_1_text and json.loads(text(tmp_path / "p.json"))["version"] == 2


def test_the_first_save_of_a_new_profile_has_nothing_to_back_up(tmp_path):
    ProfileStore(tmp_path).save(shared_profile())
    assert not (tmp_path / UPGRADE_BACKUP_FOLDER).exists()


def test_a_restore_point_saved_before_the_conversion_stays_version_1_and_still_loads(tmp_path):
    (tmp_path / "dup.json").write_text(json.dumps(v1_file_with_duplicates()), encoding="utf-8")
    (tmp_path / "saved").mkdir()
    (tmp_path / "saved" / "dup.json").write_text(json.dumps(v1_file_with_duplicates()), encoding="utf-8")
    store = ProfileStore(tmp_path)
    store.save(store.load("dup"))
    restore = store.load_checkpoint("dup")
    assert [b.label for b in restore.bindings] == ["Fire", "Reload", "Peek"]
    assert json.loads(text(tmp_path / "saved" / "dup.json"))["version"] == 1


# ===================================================================== version-2 files, malformed data, future versions
def v2_shared_file(*extra):
    def record(i, trigger, **more):
        return {"type": "binding", "id": i, "label": i, "triggers": [trigger], "actions": [{"mode": "tap", "x": 0.5, "y": 0.5}],
                "on_failure": "continue", **more}
    return {"id": "abcabcabcabc", "version": 2, "name": "v2", "orientation": "any", "package": "", "sticks": [], "looks": [],
            "bindings": [record("a", "LMB"), record("b", "LMB"), *extra]}


def test_a_version_2_file_with_a_shared_input_loads_as_written_with_no_note(tmp_path):
    (tmp_path / "v2.json").write_text(json.dumps(v2_shared_file()), encoding="utf-8")
    p = ProfileStore(tmp_path).load("v2")
    assert [b.id for b in p.bindings] == ["a", "b"] and p.notes == [] and p.repairs == []


def test_repair_keeps_both_shared_bindings_and_only_skips_the_malformed_one_after_a_backup(tmp_path):
    raw = v2_shared_file({"type": "binding", "id": "bad", "triggers": ["LMB"], "actions": [{"mode": "fly", "x": 0, "y": 0}]})
    (tmp_path / "v2.json").write_text(json.dumps(raw), encoding="utf-8")
    store = ProfileStore(tmp_path)
    with pytest.raises(ProfileError):
        store.load("v2")
    repaired = store.repair("v2")
    assert [b.id for b in repaired.bindings] == ["a", "b"]
    assert json.loads(text(tmp_path / REPAIR_BACKUP_FOLDER / "v2.json")) == raw
    assert [b["id"] for b in json.loads(text(tmp_path / "v2.json"))["bindings"]] == ["a", "b"]


def test_repairing_a_version_1_file_with_a_repeated_input_and_a_malformed_binding_keeps_the_repeats(tmp_path):
    raw = v1_file_with_duplicates()
    raw["bindings"].append({"type": "tap", "input": "Q", "x": 0.1, "y": 0.1, "mode": "bogus"})
    (tmp_path / "dup.json").write_text(json.dumps(raw), encoding="utf-8")
    store = ProfileStore(tmp_path)
    repaired = store.repair("dup")
    assert [b.label for b in repaired.bindings] == ["Fire", "Reload", "Peek"]
    assert json.loads(text(tmp_path / REPAIR_BACKUP_FOLDER / "dup.json")) == raw          # the original, with all four records
    assert json.loads(text(tmp_path / "dup.json"))["version"] == 2


def test_a_malformed_shared_binding_is_still_refused_by_the_strict_loader():
    raw = v1_file_with_duplicates()
    raw["bindings"][2]["mode"] = "bogus"
    with pytest.raises(ProfileError):
        Profile.from_dict(raw)


def test_a_future_version_with_shared_bindings_is_left_exactly_as_it_is(tmp_path):
    future = json.dumps({**v2_shared_file(), "version": 3})
    (tmp_path / "f.json").write_text(future, encoding="utf-8")
    store = ProfileStore(tmp_path)
    with pytest.raises(ProfileError, match="newer version"):
        store.load("v2")
    with pytest.raises(ProfileError, match="newer version"):
        store.repair("v2")
    store.save(shared_profile())                                                 # saving another profile changes nothing else
    assert text(tmp_path / "f.json") == future and not (tmp_path / REPAIR_BACKUP_FOLDER).exists()


def test_the_permanent_profile_id_survives_the_conversion(tmp_path):
    (tmp_path / "dup.json").write_text(json.dumps(v1_file_with_duplicates()), encoding="utf-8")
    store = ProfileStore(tmp_path)
    store.save(store.load("dup"))
    assert json.loads(text(tmp_path / "dup.json"))["id"] == "ddddeeeeffff" == store.load("dup").id
