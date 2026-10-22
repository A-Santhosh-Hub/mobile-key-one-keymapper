"""Characterization tests: how profiles are stored TODAY (version 1), plus duplicate-key handling and profile ids.

Phase 2 must keep every simple profile byte-compatible with this format; `golden/profile_v1.json` is a file written by
the current code and is the reference. The tests never touch the user's real profile folder: every store lives in tmp_path.
Tests marked AMBIGUOUS pin questionable behaviour (see test_characterization_engine.py for the convention).
"""
import json
from pathlib import Path

import pytest

from app.mapping.profile import (BACKUP_FOLDER, LATEST_PROFILE_VERSION, PROFILE_VERSION, Binding, Look, Profile, ProfileError, ProfileStore,
                                 Stick)

GOLDEN = Path(__file__).parent / "golden" / "profile_v1.json"


def golden_profile() -> Profile:
    p = Profile("Golden", orientation="landscape", package="com.x", id="abc123abc123")
    p.add(Binding("G", 0.5, 0.25, "hold", "Forward"))
    p.add(Binding("R", 0.7, 0.9, "tap", "Reload", scope=True, also=[(0.1, 0.2)]))
    p.add(Binding("Q", 0.2, 0.5, "swipe", x2=0.6, y2=0.3, duration_ms=200))
    p.add(Binding("F", 0.5, 0.5, "repeat", rate_hz=12))
    p.add(Binding("Z", 0.5, 0.5, "pinch-out", span=0.2, duration_ms=300))
    p.add(Binding("X", 0.5, 0.5, "sequence", steps=[(0.1, 0.1, 0), (0.2, 0.2, 300)]))
    p.add(Binding("Shift+C", 0.4, 0.4, "toggle"))
    p.add(Binding("WheelUp", 0.6, 0.6, "double"))
    p.add(Binding("V", 0.3, 0.3, "drag", x2=0.5, y2=0.5, duration_ms=100))
    p.set_stick(Stick(0.15, 0.7))
    p.looks.append(Look(0.5, 0.2, 0.9, 0.8))
    return p


# ===================================================================== the version-1 file format
def test_a_saved_version_1_profile_matches_the_golden_file_text_for_text(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(golden_profile())
    written = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    assert written == GOLDEN.read_text(encoding="utf-8")


def test_loading_the_golden_file_and_saving_it_again_changes_nothing(tmp_path):
    (tmp_path / "Golden.json").write_text(GOLDEN.read_text(encoding="utf-8"), encoding="utf-8")
    store = ProfileStore(tmp_path)
    store.save(store.load("Golden"))
    assert (tmp_path / "Golden.json").read_text(encoding="utf-8") == GOLDEN.read_text(encoding="utf-8")
    assert not (tmp_path / BACKUP_FOLDER).exists()                     # nothing needed migrating


def test_the_profile_header_is_version_1_with_the_id_first_and_only_the_known_keys():
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert PROFILE_VERSION == 1 and data["version"] == 1
    assert list(data) == ["id", "version", "name", "orientation", "package", "bindings", "sticks", "looks"]


def test_every_binding_is_a_flat_record_with_one_input_and_one_primary_action():
    for raw in json.loads(GOLDEN.read_text(encoding="utf-8"))["bindings"]:
        assert raw["type"] == "tap" and isinstance(raw["input"], str)
        assert not {"triggers", "actions", "id", "extra_triggers"} & set(raw)


BASE_KEYS = ["type", "input", "x", "y", "mode", "label"]


@pytest.mark.parametrize("binding, extra_keys", [
    (Binding("K", 0.5, 0.5, "hold"), []),
    (Binding("K", 0.5, 0.5, "tap"), []),
    (Binding("K", 0.5, 0.5, "toggle"), []),
    (Binding("K", 0.5, 0.5, "double"), []),
    (Binding("K", 0.5, 0.5, "repeat"), ["rate_hz"]),
    (Binding("K", 0.5, 0.5, "swipe"), ["x2", "y2", "duration_ms"]),
    (Binding("K", 0.5, 0.5, "drag"), ["x2", "y2", "duration_ms"]),
    (Binding("K", 0.5, 0.5, "pinch-out"), ["span", "duration_ms"]),
    (Binding("K", 0.5, 0.5, "pinch-in"), ["span", "duration_ms"]),
    (Binding("K", 0.5, 0.5, "sequence"), ["steps"]),
    (Binding("K", 0.5, 0.5, "hold", scope=True), ["scope"]),
    (Binding("K", 0.5, 0.5, "hold", also=[(0.1, 0.1)]), ["also"]),
])
def test_each_mode_writes_only_the_fields_it_uses_in_a_fixed_order(binding, extra_keys):
    keys = list(binding.to_dict())
    assert keys[:6] == BASE_KEYS
    assert sorted(keys[6:]) == sorted(extra_keys)


def test_profile_to_dict_never_contains_the_id_only_the_store_adds_it():
    p = golden_profile()
    assert "id" not in p.to_dict()
    assert Profile.from_dict({"name": "n", "id": "feedfeedfeed"}).id == "feedfeedfeed"


def test_bindings_keep_their_list_order_through_save_and_load(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(golden_profile())
    assert [b.input for b in store.load("Golden").bindings] == [b.input for b in golden_profile().bindings]


def test_a_minimal_hand_written_binding_gets_the_documented_defaults():
    p = Profile.from_dict({"name": "n", "bindings": [{"input": "K", "x": 0.2, "y": 0.3}]})
    b = p.bindings[0]
    assert (b.mode, b.label, b.scope, b.also, b.rate_hz, b.duration_ms, b.span) == ("hold", "", False, [], 10.0, 160, 0.10)
    assert Profile.from_dict({"name": "n"}).orientation == "any"      # and a file with no version is read as version 1


def test_a_combination_is_written_with_its_modifiers_in_the_fixed_order():
    p = Profile.from_dict({"name": "n", "bindings": [{"input": "Shift+Ctrl+W", "x": 0.2, "y": 0.3}]})
    assert p.bindings[0].input == "Ctrl+Shift+W"


def test_fields_a_reader_does_not_know_are_kept_when_a_version_1_profile_is_saved_again(tmp_path):
    # CHANGED in Phase 2 step 2 (it used to pin that they were dropped): unknown fields now survive a load and save.
    raw = {"version": 1, "name": "u", "future_profile_field": {"a": 1},
           "bindings": [{"type": "tap", "input": "K", "x": 0.2, "y": 0.3, "mode": "hold", "label": "", "future": [1, 2]}]}
    (tmp_path / "u.json").write_text(json.dumps(raw), encoding="utf-8")
    store = ProfileStore(tmp_path)
    store.save(store.load("u"))
    saved = json.loads((tmp_path / "u.json").read_text(encoding="utf-8"))
    assert saved["future_profile_field"] == {"a": 1} and saved["bindings"][0]["future"] == [1, 2]
    assert saved["version"] == 1


def test_a_profile_from_a_newer_version_is_refused_and_its_file_is_left_alone(tmp_path):
    original = json.dumps({"version": LATEST_PROFILE_VERSION + 1, "name": "new", "bindings": [{"x": "unknown shape"}]})
    (tmp_path / "new.json").write_text(original, encoding="utf-8")
    store = ProfileStore(tmp_path)
    with pytest.raises(ProfileError, match="newer version"):
        store.load("new")
    assert store.names() == ["new"]
    store.migrate_ids()
    assert (tmp_path / "new.json").read_text(encoding="utf-8") == original and not (tmp_path / BACKUP_FOLDER).exists()


def test_a_restore_point_has_the_same_format_and_id_as_the_profile_file(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(golden_profile())
    store.save_checkpoint(golden_profile())
    checkpoint = next((tmp_path / "saved").glob("*.json"))
    assert checkpoint.read_text(encoding="utf-8") == GOLDEN.read_text(encoding="utf-8")
    assert [b.input for b in store.load_checkpoint("Golden").bindings] == [b.input for b in golden_profile().bindings]
    assert store.load_checkpoint("Golden").id == "abc123abc123"


def test_a_profile_without_the_new_fields_loads_exactly_like_before_after_the_id_migration(tmp_path):
    raw = json.loads(GOLDEN.read_text(encoding="utf-8"))
    del raw["id"]
    (tmp_path / "Golden.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
    store = ProfileStore(tmp_path)
    store.migrate_ids()
    migrated = json.loads((tmp_path / "Golden.json").read_text(encoding="utf-8"))
    assert migrated.pop("id") and migrated == raw                     # only the id was added; the bindings are unchanged
    assert (tmp_path / BACKUP_FOLDER / "Golden.json").exists()


# ===================================================================== duplicate inputs and replacement
def test_adding_a_binding_for_a_taken_input_replaces_the_old_one_and_moves_it_to_the_end():
    # AMBIGUOUS: the replaced binding silently changes position, which matters for any "list order" rule.
    p = Profile("p")
    p.add(Binding("A", 0.1, 0.1))
    p.add(Binding("B", 0.2, 0.2))
    p.add(Binding("A", 0.3, 0.3, "tap"))
    assert [(b.input, b.x, b.mode) for b in p.bindings] == [("B", 0.2, "hold"), ("A", 0.3, "tap")]


def test_replacing_is_by_exact_input_so_a_combination_and_its_plain_key_do_not_replace_each_other():
    p = Profile("p")
    p.add(Binding("W", 0.1, 0.1))
    p.add(Binding("Shift+W", 0.2, 0.2))
    p.add(Binding("Ctrl+W", 0.3, 0.3))
    assert [b.input for b in p.bindings] == ["W", "Shift+W", "Ctrl+W"]
    p.add(Binding("Shift+W", 0.9, 0.9))
    assert [b.input for b in p.bindings] == ["W", "Ctrl+W", "Shift+W"] and p.find("Shift+W").x == 0.9


def test_remove_deletes_by_exact_input_and_ignores_unknown_ones():
    p = Profile("p", bindings=[Binding("W", 0.1, 0.1), Binding("Shift+W", 0.2, 0.2)])
    p.remove("Q")
    p.remove("W")
    assert [b.input for b in p.bindings] == ["Shift+W"]


def test_key_owner_names_what_an_input_is_already_used_for():
    p = Profile("p", bindings=[Binding("R", 0.1, 0.1)])
    p.set_stick(Stick(0.15, 0.7))
    assert p.key_owner("R") == "another control"
    assert p.key_owner("W") == "the movement stick"
    assert p.key_owner("Z") is None
    assert p.key_owner("Shift+R") is None                             # a combination is a different input


def test_a_file_that_lists_the_same_input_twice_keeps_every_binding_and_says_so():
    # CHANGED in Phase 2 step 3 (it used to pin that the last one silently replaced the others). The full behaviour is in
    # test_shared_bindings.py; the engine still only runs the last one of them, as it always did.
    p = Profile.from_dict({"name": "d", "bindings": [
        {"input": "A", "x": 0.1, "y": 0.1, "mode": "hold"}, {"input": "B", "x": 0.2, "y": 0.2},
        {"input": "A", "x": 0.3, "y": 0.3, "mode": "tap"}]})
    assert [(b.input, b.x, b.mode) for b in p.bindings] == [("A", 0.1, "hold"), ("B", 0.2, "hold"), ("A", 0.3, "tap")]
    assert len(p.notes) == 1 and "1, 3" in p.notes[0]


def test_a_stick_key_used_by_a_binding_makes_the_whole_profile_unloadable():
    # AMBIGUOUS: one clash rejects the file instead of reporting it and loading the rest.
    with pytest.raises(ProfileError, match="stick key"):
        Profile.from_dict({"name": "c", "bindings": [{"input": "W", "x": 0.1, "y": 0.1}, {"input": "R", "x": 0.2, "y": 0.2}],
                           "sticks": [{"type": "stick", "x": 0.15, "y": 0.7}]})


def test_one_malformed_binding_makes_the_whole_profile_unloadable():
    # AMBIGUOUS: the other, valid bindings are lost to the caller too; there is no partial load today.
    good = {"input": "R", "x": 0.2, "y": 0.2}
    for bad in ({"input": "Q", "x": 0.1, "y": 0.1, "mode": "bogus"}, {"input": "Q", "y": 0.1}, {"input": "", "x": 0, "y": 0},
                {"input": "Shift", "x": 0.1, "y": 0.1, "mode": "hold", "also": [[1]]}):
        with pytest.raises(ProfileError):
            Profile.from_dict({"name": "m", "bindings": [good, bad]})


def test_a_binding_of_an_unknown_type_is_skipped_but_the_others_load():
    p = Profile.from_dict({"name": "s", "bindings": [{"type": "gamepad", "input": "Pad1"},
                                                     {"type": "tap", "input": "R", "x": 0.2, "y": 0.2}]})
    assert [b.input for b in p.bindings] == ["R"]


def test_stick_keys_cannot_be_given_to_a_binding_through_set_stick_they_are_removed_and_reported():
    p = Profile("p", bindings=[Binding("W", 0.1, 0.1), Binding("R", 0.2, 0.2)])
    assert p.set_stick(Stick(0.15, 0.7)) == ["W"]
    assert [b.input for b in p.bindings] == ["R"]


def test_extra_spots_are_limited_and_stored_in_the_binding_not_as_separate_bindings():
    b = Binding("L", 0.8, 0.7, "hold", also=[(0.1, 0.1)] * 9)
    assert len(b.also) == 4
    p = Profile("p", bindings=[b])
    assert len(p.bindings) == 1 and len(p.to_dict()["bindings"]) == 1


# ===================================================================== profile identity
def test_a_binding_has_a_runtime_id_that_a_version_1_file_does_not_store_and_list_position_still_addresses_it():
    # CHANGED in Phase 2 step 2: the binding used to have no id at all (see test_binding_model.py for the id rules).
    b = Binding("K", 0.5, 0.5)
    assert b.id and "id" not in b.to_dict()
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1), Binding("B", 0.2, 0.2)])
    assert p.bindings.index(p.find("B")) == 1
