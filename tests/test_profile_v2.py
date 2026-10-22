"""Profile serialization (Phase 2, step 2): the legacy version-1 format stays byte-for-byte, version 2 is written only when a
profile needs it, and everything in a profile round-trips: binding ids, triggers, actions, delays, failure policy, unknown
fields. Also: id handling, repair with backups, future versions and restore points.

`golden/profile_v1.json` and `golden/profile_v2.json` are files written by the code and reviewed by hand. Every store lives in
tmp_path: the user's real profile folder is never opened here."""
import json
from pathlib import Path

import pytest

from app.mapping.profile import (BACKUP_FOLDER, LATEST_PROFILE_VERSION, MAX_DELAY_MS, REPAIR_BACKUP_FOLDER, Action, Binding,
                                 Look, Profile, ProfileError, ProfileStore, Stick)
from app.stream import protocol
from test_characterization_engine import Rig

GOLDEN_V1 = Path(__file__).parent / "golden" / "profile_v1.json"
GOLDEN_V2 = Path(__file__).parent / "golden" / "profile_v2.json"
D, U = protocol.TOUCH_DOWN, protocol.TOUCH_UP


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def golden_v2_profile() -> Profile:
    p = Profile("Golden V2", orientation="landscape", package="com.x", id="def456def456")
    p.add(Binding("R", 0.7, 0.9, "tap", "Reload", id="reload"))
    p.add(Binding("E", 0.62, 0.55, "tap", "Interact", id="interact", extra_triggers=["F", "MMB"]))
    p.add(Binding("LMB", 0.8, 0.7, "hold", "Fire", id="fire", also=[(0.1, 0.4)], on_failure="abort",
                  extra_actions=[Action("tap", 0.5, 0.2, delay_ms=60),
                                 Action("swipe", 0.2, 0.5, x2=0.6, y2=0.3, duration_ms=200, delay_ms=120)]))
    p.add(Binding("Shift+C", 0.4, 0.4, "sequence", "Menu", id="menu", steps=[(0.1, 0.1, 0), (0.2, 0.2, 300)],
                  extra_triggers=["Ctrl+C"]))
    p.set_stick(Stick(0.15, 0.7))
    p.looks.append(Look(0.5, 0.2, 0.9, 0.8))
    return p


def v2_binding(id="b1", triggers=("E",), actions=None, **more):
    return {"type": "binding", "id": id, "label": "", "triggers": list(triggers),
            "actions": actions if actions is not None else [{"mode": "tap", "x": 0.5, "y": 0.5}],
            "on_failure": "continue", **more}


def v2_file(bindings, **more):
    return {"id": "abcabcabcabc", "version": 2, "name": "v2", "orientation": "any", "package": "", "bindings": bindings,
            "sticks": [], "looks": [], **more}


def store_with(tmp_path, raw, name="v2.json"):
    (tmp_path / name).write_text(json.dumps(raw), encoding="utf-8")
    return ProfileStore(tmp_path)


# ===================================================================== version 1 stays version 1
def test_a_version_1_profile_loaded_and_edited_in_every_ordinary_way_is_still_written_as_version_1(tmp_path):
    (tmp_path / "Golden.json").write_text(text(GOLDEN_V1), encoding="utf-8")
    store = ProfileStore(tmp_path)
    p = store.load("Golden")
    assert p.schema_version == 1 and p.required_version == 1
    p.find("G").x = 0.9
    p.find("G").label = "Walk"
    p.find("G").set_mode("tap")
    p.find("R").also.append((0.3, 0.3))
    p.add(Binding("T", 0.1, 0.1, "hold", also=[(0.2, 0.2)], scope=True))
    p.remove("Q")
    raw = p.to_dict()
    assert raw["version"] == 1
    assert all(set(b) <= {"type", "input", "x", "y", "mode", "label", "x2", "y2", "rate_hz", "duration_ms", "span", "scope",
                          "steps", "also"} and b["type"] == "tap" for b in raw["bindings"])
    store.save(p)
    assert json.loads(text(tmp_path / "Golden.json"))["version"] == 1


def test_version_1_bindings_get_runtime_ids_that_are_never_written_and_never_change_while_the_profile_lives(tmp_path):
    (tmp_path / "Golden.json").write_text(text(GOLDEN_V1), encoding="utf-8")
    store = ProfileStore(tmp_path)
    p = store.load("Golden")
    ids = [b.id for b in p.bindings]
    assert len(ids) == len(set(ids)) and all(ids)
    p.find("G").x = 0.9
    p.find("G").set_mode("tap")
    p.add(Binding("T", 0.1, 0.1))
    assert [b.id for b in p.bindings][:len(ids)] == ids and len({b.id for b in p.bindings}) == len(ids) + 1
    store.save(p)
    saved = text(tmp_path / "Golden.json")
    assert not any(i in saved for i in ids) and '"id": "' in saved and saved.count('"id"') == 1      # only the profile's own id
    again = store.load("Golden")
    assert [b.id for b in again.bindings if b.input == "G"] != [p.find("G").id]        # a new object: new runtime ids


def test_the_runtime_ids_of_a_loaded_version_1_profile_survive_the_save_it_triggers(tmp_path):
    (tmp_path / "Golden.json").write_text(text(GOLDEN_V1), encoding="utf-8")
    store = ProfileStore(tmp_path)
    p = store.load("Golden")
    before = {b.input: b.id for b in p.bindings}
    store.save(p)
    store.save_checkpoint(p)
    assert {b.input: b.id for b in p.bindings} == before


# ===================================================================== when version 2 is needed
@pytest.mark.parametrize("change", ["trigger", "action", "failure"])
def test_each_new_capability_on_its_own_makes_the_profile_version_2(change):
    p = Profile("p", bindings=[Binding("R", 0.1, 0.1, "tap"), Binding("E", 0.5, 0.5, "tap")])
    assert p.to_dict()["version"] == 1
    b = p.find("E")
    if change == "trigger":
        b.extra_triggers.append("F")
    elif change == "action":
        b.extra_actions.append(Action("tap", 0.2, 0.2))
    else:
        b.on_failure = "abort"
    assert p.required_version == 2 and p.to_dict()["version"] == 2


def test_a_profile_that_becomes_version_2_writes_every_binding_in_the_version_2_form_and_keeps_their_ids():
    p = Profile("p", bindings=[Binding("R", 0.1, 0.1, "tap", "Reload"), Binding("E", 0.5, 0.5, "tap")])
    ids = [b.id for b in p.bindings]
    p.find("E").extra_triggers.append("F")
    raw = p.to_dict()
    assert raw["version"] == 2
    assert [b["type"] for b in raw["bindings"]] == ["binding", "binding"]
    assert [b["id"] for b in raw["bindings"]] == ids                  # the ids it had as version 1 are the ones persisted now
    assert raw["bindings"][0] == {"type": "binding", "id": ids[0], "label": "Reload", "triggers": ["R"],
                                  "actions": [{"mode": "tap", "x": 0.1, "y": 0.1}], "on_failure": "continue"}
    assert raw["bindings"][1]["triggers"] == ["E", "F"]


def test_everything_except_the_bindings_is_written_the_same_in_both_versions():
    p = golden_v2_profile()
    v2 = p.to_dict()
    for b in p.bindings:
        b.extra_triggers, b.extra_actions, b.on_failure = [], [], "continue"
    v1 = p.to_dict()
    assert v1["version"] == 1 and v2["version"] == 2
    assert {k: v for k, v in v1.items() if k not in ("version", "bindings")} == \
           {k: v for k, v in v2.items() if k not in ("version", "bindings")}


def test_a_profile_built_in_memory_goes_back_to_version_1_when_the_capability_is_removed_again():
    p = Profile("p", bindings=[Binding("E", 0.5, 0.5, "tap", extra_triggers=["F"])])
    assert p.to_dict()["version"] == 2
    p.find("E").extra_triggers.clear()
    assert p.to_dict()["version"] == 1                                # nothing version-2-only is left to protect


def test_a_profile_loaded_from_a_version_2_file_stays_version_2_so_its_ids_are_not_lost(tmp_path):
    store = store_with(tmp_path, v2_file([v2_binding("keep-me", ["E"])]))
    p = store.load("v2")
    assert p.schema_version == 2 and p.required_version == 2 and not p.bindings[0].needs_v2
    store.save(p)
    saved = json.loads(text(tmp_path / "v2.json"))
    assert saved["version"] == 2 and saved["bindings"][0]["id"] == "keep-me"


# ===================================================================== version 2 round trips
def test_a_version_2_profile_is_written_exactly_like_the_golden_file(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(golden_v2_profile())
    assert text(next(tmp_path.glob("*.json"))) == text(GOLDEN_V2)


def test_loading_the_golden_version_2_file_and_saving_it_again_changes_nothing(tmp_path):
    (tmp_path / "Golden V2.json").write_text(text(GOLDEN_V2), encoding="utf-8")
    store = ProfileStore(tmp_path)
    store.save(store.load("Golden V2"))
    assert text(tmp_path / "Golden V2.json") == text(GOLDEN_V2) and not (tmp_path / BACKUP_FOLDER).exists()


def test_every_binding_property_survives_a_save_and_a_load(tmp_path):
    store = ProfileStore(tmp_path)
    original = golden_v2_profile()
    store.save(original)
    loaded = store.load("Golden V2")
    assert loaded.id == original.id == "def456def456"
    assert len(loaded.bindings) == len(original.bindings)
    for mine, theirs in zip(original.bindings, loaded.bindings):
        assert mine == theirs and mine.id == theirs.id                 # equality covers every field except the id
        assert mine.triggers == theirs.triggers and mine.actions == theirs.actions
    fire = loaded.find("LMB")
    assert fire.on_failure == "abort" and fire.also == [(0.1, 0.4)]
    assert [(a.mode, a.delay_ms) for a in fire.extra_actions] == [("tap", 60), ("swipe", 120)]
    assert (fire.extra_actions[1].x2, fire.extra_actions[1].y2, fire.extra_actions[1].duration_ms) == (0.6, 0.3, 200)
    assert loaded.find("Shift+C").steps == [(0.1, 0.1, 0), (0.2, 0.2, 300)] and loaded.find("Shift+C").extra_triggers == ["Ctrl+C"]
    assert loaded.sticks == original.sticks and loaded.looks == original.looks


def test_saving_a_loaded_version_2_profile_twice_gives_the_same_file(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(golden_v2_profile())
    first = text(next(tmp_path.glob("*.json")))
    store.save(store.load("Golden V2"))
    store.save(store.load("Golden V2"))
    assert text(next(tmp_path.glob("*.json"))) == first


def test_the_first_action_never_carries_a_delay_and_every_additional_one_always_does():
    raw = golden_v2_profile().to_dict()["bindings"][2]
    assert "delay_ms" not in raw["actions"][0]
    assert [a["delay_ms"] for a in raw["actions"][1:]] == [60, 120]
    assert raw["triggers"] == ["LMB"] and raw["on_failure"] == "abort"


def test_a_version_2_file_is_read_as_written_nothing_is_replaced_or_reordered(tmp_path):
    store = store_with(tmp_path, v2_file([v2_binding("a", ["E"]), v2_binding("b", ["E"]), v2_binding("c", ["Shift+E"])]))
    p = store.load("v2")
    assert [(b.id, b.input) for b in p.bindings] == [("a", "E"), ("b", "E"), ("c", "Shift+E")]    # sharing a key is not resolved here


def test_binding_ids_follow_their_bindings_when_the_list_is_reordered(tmp_path):
    store = ProfileStore(tmp_path)
    p = golden_v2_profile()
    p.bindings.reverse()
    store.save(p)
    assert [b.id for b in store.load("Golden V2").bindings] == ["menu", "fire", "interact", "reload"]


def test_version_2_records_that_are_not_bindings_are_skipped_like_unknown_types_always_were():
    p = Profile.from_dict(v2_file([{"type": "macro", "id": "m"}, v2_binding("a", ["E"])]))
    assert [b.id for b in p.bindings] == ["a"]


def test_a_version_2_binding_must_use_the_version_2_form():
    with pytest.raises(ProfileError):
        Profile.from_dict(v2_file([{"type": "binding", "input": "E", "x": 0.5, "y": 0.5}]))
    with pytest.raises(ProfileError):
        Profile.from_dict({"version": 1, "name": "n", "bindings": [{**v2_binding("a"), "type": "tap"}]})     # and version 1 the flat one


def test_a_binding_record_that_is_not_an_object_is_a_clear_error_in_both_versions():
    for version in (1, 2):
        with pytest.raises(ProfileError):
            Profile.from_dict({"version": version, "name": "n", "bindings": ["E"]})


# ===================================================================== the permanent profile id
def test_the_profile_id_is_written_first_kept_by_every_save_and_not_confused_with_binding_ids(tmp_path):
    store = ProfileStore(tmp_path)
    p = golden_v2_profile()
    store.save(p)
    store.save(p)
    raw = json.loads(text(next(tmp_path.glob("*.json"))))
    assert list(raw)[0] == "id" and raw["id"] == "def456def456"
    assert {b["id"] for b in raw["bindings"]} == {"reload", "interact", "fire", "menu"}
    assert store.load("Golden V2").id == "def456def456"


def test_a_version_2_file_without_a_profile_id_gets_one_in_place_after_a_backup_and_nothing_else_changes(tmp_path):
    raw = v2_file([v2_binding("a", ["E", "F"])], future_field=[1])
    del raw["id"]
    (tmp_path / "v2.json").write_text(json.dumps(raw), encoding="utf-8")
    store = ProfileStore(tmp_path)
    after = json.loads(text(tmp_path / "v2.json"))
    assert after.pop("id") and after == raw                           # supported now, so it is migrated like any other
    assert json.loads(text(tmp_path / BACKUP_FOLDER / "v2.json")) == raw
    assert store.load("v2").bindings[0].id == "a"


def test_a_copy_made_with_to_dict_is_a_new_profile_but_keeps_the_binding_ids_of_a_version_2_profile():
    original = golden_v2_profile()
    copy = Profile.from_dict(original.to_dict())
    assert copy.id == "" and [b.id for b in copy.bindings] == [b.id for b in original.bindings]


# ===================================================================== unknown fields
V1_UNKNOWN = {"version": 1, "name": "u", "profile_extra": {"deep": [1, {"x": 2}]}, "bindings": [
    {"type": "tap", "input": "K", "x": 0.2, "y": 0.3, "mode": "hold", "label": "", "binding_extra": {"layer": "ads"}}]}


def test_unknown_fields_of_a_version_1_profile_survive_and_the_known_ones_keep_their_place():
    p = Profile.from_dict(V1_UNKNOWN)
    raw = p.to_dict()
    assert raw["profile_extra"] == {"deep": [1, {"x": 2}]} and raw["bindings"][0]["binding_extra"] == {"layer": "ads"}
    assert list(raw)[:5] == ["version", "name", "orientation", "package", "bindings"]
    assert list(raw["bindings"][0])[:6] == ["type", "input", "x", "y", "mode", "label"] and raw["version"] == 1


def test_unknown_fields_move_into_the_version_2_record_when_a_profile_is_upgraded():
    p = Profile.from_dict(V1_UNKNOWN)
    p.find("K").extra_triggers.append("L")
    raw = p.to_dict()
    assert raw["version"] == 2 and raw["profile_extra"] == {"deep": [1, {"x": 2}]}
    assert raw["bindings"][0]["binding_extra"] == {"layer": "ads"} and raw["bindings"][0]["triggers"] == ["K", "L"]


def test_unknown_fields_at_every_level_of_a_version_2_profile_survive_a_save_and_a_load(tmp_path):
    raw = v2_file([v2_binding("a", ["E", "F"], actions=[{"mode": "hold", "x": 0.5, "y": 0.5, "primary_extra": 1},
                                                        {"mode": "tap", "x": 0.2, "y": 0.2, "delay_ms": 30, "extra_extra": [2]}],
                              binding_extra={"k": "v"})], profile_extra="p")
    store = store_with(tmp_path, raw)
    store.save(store.load("v2"))
    saved = json.loads(text(tmp_path / "v2.json"))
    binding = saved["bindings"][0]
    assert saved["profile_extra"] == "p" and binding["binding_extra"] == {"k": "v"}
    assert binding["actions"][0]["primary_extra"] == 1 and binding["actions"][1]["extra_extra"] == [2]
    assert saved == raw


def test_an_unknown_field_can_never_replace_a_field_this_version_writes():
    b = Binding("K", 0.5, 0.5, "hold", unknown={"mode": "tap", "id": "evil", "triggers": ["X"], "x": 0.9})
    flat = b.to_dict_v1()
    assert flat["mode"] == "hold" and flat["x"] == 0.5 and flat["id"] == "evil"          # version 1 has no id: kept as data
    rich = b.to_dict_v2()
    assert rich["id"] == b.id and rich["triggers"] == ["K"] and rich["x"] == 0.9         # a field it does not write: kept


# ===================================================================== invalid values
def with_action(**fields):
    return v2_binding("a", ["E", "F"], actions=[{"mode": "hold", "x": 0.5, "y": 0.5},
                                               {"mode": "tap", "x": 0.2, "y": 0.2, **fields}])


@pytest.mark.parametrize("delay", [-1, 1.5, 60.0, "60", None, True, [60], MAX_DELAY_MS + 1])
def test_a_file_with_an_invalid_delay_is_refused(delay):
    with pytest.raises(ProfileError):
        Profile.from_dict(v2_file([with_action(delay_ms=delay)]))


def test_a_delay_on_the_first_action_is_refused_instead_of_silently_dropped():
    with pytest.raises(ProfileError, match="first action"):
        Profile.from_dict(v2_file([v2_binding("a", actions=[{"mode": "tap", "x": 0.5, "y": 0.5, "delay_ms": 10}])]))
    Profile.from_dict(v2_file([v2_binding("a", actions=[{"mode": "tap", "x": 0.5, "y": 0.5, "delay_ms": 0}])]))


@pytest.mark.parametrize("bad", [
    v2_binding(on_failure="explode"), v2_binding(on_failure=None), v2_binding(on_failure=["abort"]),
    v2_binding(triggers=[]), v2_binding() | {"triggers": "E"}, v2_binding(triggers=["E", 5]), v2_binding(triggers=["E", "E"]),
    v2_binding(triggers=["E", "Ctrl+Shift"]),
    v2_binding(actions=[]), v2_binding(actions={"mode": "tap"}), v2_binding(actions=["tap"]),
    v2_binding(actions=[{"mode": "fly", "x": 0.5, "y": 0.5}]), v2_binding(actions=[{"x": 0.5, "y": 0.5}]),
    v2_binding(actions=[{"mode": "tap", "x": "left", "y": 0.5}]),
    v2_binding(actions=[{"mode": "tap", "x": 0.5, "y": 0.5, "also": [[1]]}]),
    {"type": "binding", "id": "a", "label": "", "on_failure": "continue"},
])
def test_malformed_version_2_bindings_are_refused_with_a_profile_error(bad):
    with pytest.raises(ProfileError):
        Profile.from_dict(v2_file([bad]))


def test_a_version_2_stick_key_clash_is_refused_like_in_version_1():
    with pytest.raises(ProfileError, match="stick key"):
        Profile.from_dict(v2_file([v2_binding("a", ["R", "W"])], sticks=[{"type": "stick", "x": 0.15, "y": 0.7}]))


# ===================================================================== missing and duplicated binding ids
def test_missing_unusable_and_repeated_ids_are_replaced_deterministically_and_no_binding_is_lost(tmp_path):
    raw = v2_file([v2_binding("dup", ["A"]), v2_binding("dup", ["B"]), v2_binding(None, ["C"]),
                   v2_binding("not valid!", ["D"]), v2_binding("dup", ["E"]), v2_binding("ok", ["F"]), v2_binding(7, ["G"])])
    del raw["bindings"][2]["id"]
    store = store_with(tmp_path, raw)
    p = store.load("v2")
    assert [b.input for b in p.bindings] == list("ABCDEFG")                      # nothing dropped, nothing reordered
    ids = [b.id for b in p.bindings]
    assert len(set(ids)) == 7 and ids[0] == "dup" and ids[5] == "ok"             # the first of a repeated id and the good one stay
    assert all(i not in ("dup", "not valid!", 7) for i in (ids[1], ids[3], ids[4], ids[6]))
    assert len(p.repairs) == 5 and any("repeated the id 'dup'" in note for note in p.repairs) and \
           any("had no usable id" in note for note in p.repairs)


def test_loading_never_rewrites_the_file_and_the_ids_are_fixed_in_the_file_only_when_the_user_saves(tmp_path):
    raw = v2_file([v2_binding("dup", ["A"]), v2_binding("dup", ["B"])])
    store = store_with(tmp_path, raw)
    before = text(tmp_path / "v2.json")
    p = store.load("v2")
    assert text(tmp_path / "v2.json") == before and not (tmp_path / REPAIR_BACKUP_FOLDER).exists()
    store.save(p)
    saved = json.loads(text(tmp_path / "v2.json"))
    assert saved["bindings"][0]["id"] == "dup" and saved["bindings"][1]["id"] != "dup"
    assert [b["triggers"] for b in saved["bindings"]] == [["A"], ["B"]]


def test_a_valid_set_of_ids_needs_no_repair():
    assert Profile.from_dict(v2_file([v2_binding("a"), v2_binding("b", ["F"])])).repairs == []


# ===================================================================== malformed bindings and repair backups
MIXED = {"version": 1, "name": "m", "bindings": [
    {"type": "tap", "input": "R", "x": 0.2, "y": 0.2, "mode": "tap"},
    {"type": "tap", "input": "Q", "x": 0.1, "y": 0.1, "mode": "bogus"},
    {"type": "tap", "input": "T", "x": 0.3, "y": 0.3}]}


def test_repair_mode_skips_a_malformed_binding_and_says_so_while_the_default_stays_strict():
    with pytest.raises(ProfileError):
        Profile.from_dict(MIXED)
    p = Profile.from_dict(MIXED, repair=True)
    assert [b.input for b in p.bindings] == ["R", "T"] and len(p.repairs) == 1 and "Skipped binding 2" in p.repairs[0]


def test_repair_backs_up_the_original_first_then_replaces_it_with_a_valid_profile(tmp_path):
    original = json.dumps({"id": "aaaabbbbcccc", **MIXED})
    (tmp_path / "m.json").write_text(original, encoding="utf-8")
    store = ProfileStore(tmp_path)
    (tmp_path / "saved").mkdir()
    (tmp_path / "saved" / "m.json").write_text(original, encoding="utf-8")
    with pytest.raises(ProfileError):
        store.load("m")
    repaired = store.repair("m")
    assert [b.input for b in repaired.bindings] == ["R", "T"] and repaired.id == "aaaabbbbcccc"
    assert text(tmp_path / REPAIR_BACKUP_FOLDER / "m.json") == original                       # the original, byte for byte
    assert [b.input for b in store.load("m").bindings] == ["R", "T"]                           # now loads with no repair mode
    assert json.loads(text(tmp_path / "m.json"))["id"] == "aaaabbbbcccc"
    assert text(tmp_path / "saved" / "m.json") == original                                      # the restore point is left alone
    assert not (tmp_path / BACKUP_FOLDER).exists()


def test_repairing_a_file_that_needs_nothing_touches_nothing_and_makes_no_backup(tmp_path):
    (tmp_path / "Golden.json").write_text(text(GOLDEN_V1), encoding="utf-8")
    store = ProfileStore(tmp_path)
    p = store.repair("Golden")
    assert p.repairs == [] and text(tmp_path / "Golden.json") == text(GOLDEN_V1) and not (tmp_path / REPAIR_BACKUP_FOLDER).exists()


def test_a_second_repair_never_overwrites_the_first_backup(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps({"id": "aaaabbbbcccc", **MIXED}), encoding="utf-8")
    store = ProfileStore(tmp_path)
    store.repair("m")
    second = {"id": "aaaabbbbcccc", "version": 1, "name": "m", "bindings": [
        {"type": "tap", "input": "Z", "x": 0.1, "y": 0.1}, {"type": "tap", "input": "Y"}]}
    (tmp_path / "m.json").write_text(json.dumps(second), encoding="utf-8")
    store.repair("m")
    backups = sorted((tmp_path / REPAIR_BACKUP_FOLDER).glob("m*.json"))
    assert len(backups) == 2 and json.loads(text(backups[0])) != json.loads(text(backups[1]))
    assert {json.loads(text(b))["bindings"][0]["input"] for b in backups} == {"R", "Z"}


def test_if_the_backup_cannot_be_made_the_file_is_not_repaired_or_changed(tmp_path, monkeypatch):
    original = json.dumps({"id": "aaaabbbbcccc", **MIXED})
    (tmp_path / "m.json").write_text(original, encoding="utf-8")
    store = ProfileStore(tmp_path)
    monkeypatch.setattr(ProfileStore, "_backup", lambda self, file, folder=BACKUP_FOLDER: False)
    with pytest.raises(ProfileError, match="nothing was changed"):
        store.repair("m")
    assert text(tmp_path / "m.json") == original


def test_repair_drops_a_binding_on_a_stick_key_and_only_the_clashing_trigger_of_a_version_2_binding(tmp_path):
    v1 = {"id": "aaaabbbbcccc", "version": 1, "name": "s", "sticks": [{"type": "stick", "x": 0.15, "y": 0.7}],
          "bindings": [{"input": "W", "x": 0.1, "y": 0.1}, {"input": "R", "x": 0.2, "y": 0.2}]}
    (tmp_path / "s.json").write_text(json.dumps(v1), encoding="utf-8")
    v2 = v2_file([v2_binding("a", ["R", "A", "F"]), v2_binding("b", ["W"])], name="s2", id="ddddeeeeffff",
                 sticks=[{"type": "stick", "x": 0.15, "y": 0.7}])
    (tmp_path / "s2.json").write_text(json.dumps(v2), encoding="utf-8")
    store = ProfileStore(tmp_path)
    assert [b.input for b in store.repair("s").bindings] == ["R"]
    fixed = store.repair("s2")
    assert [(b.id, b.triggers) for b in fixed.bindings] == [("a", ("R", "F"))]
    assert len(list((tmp_path / REPAIR_BACKUP_FOLDER).glob("*.json"))) == 2


def test_repair_also_fixes_binding_ids_in_the_file_after_a_backup(tmp_path):
    raw = v2_file([v2_binding("dup", ["A"]), v2_binding("dup", ["B"])])
    store = store_with(tmp_path, raw)
    p = store.repair("v2")
    assert len(p.repairs) == 1 and json.loads(text(tmp_path / REPAIR_BACKUP_FOLDER / "v2.json")) == raw
    assert len({b["id"] for b in json.loads(text(tmp_path / "v2.json"))["bindings"]}) == 2


def test_repair_refuses_what_it_cannot_read_or_does_not_know_and_leaves_the_file_alone(tmp_path):
    future = json.dumps({"id": "aaaabbbbcccc", "version": LATEST_PROFILE_VERSION + 1, "name": "f", "bindings": [{}]})
    (tmp_path / "f.json").write_text(future, encoding="utf-8")
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    store = ProfileStore(tmp_path)
    with pytest.raises(ProfileError, match="newer version"):
        store.repair("f")
    with pytest.raises(ProfileError):
        store.repair("nothing at all")
    assert text(tmp_path / "f.json") == future and not (tmp_path / REPAIR_BACKUP_FOLDER).exists()


# ===================================================================== future versions
def test_a_future_version_is_refused_by_the_reader_the_store_and_every_maintenance_step_and_the_file_never_changes(tmp_path):
    future = json.dumps({"version": LATEST_PROFILE_VERSION + 1, "name": "future", "bindings": [{"x": "unknown"}]})
    (tmp_path / "future.json").write_text(future, encoding="utf-8")
    with pytest.raises(ProfileError, match="newer version"):
        Profile.from_dict(json.loads(future))
    store = ProfileStore(tmp_path)
    store.migrate_ids()
    with pytest.raises(ProfileError, match="newer version"):
        store.load("future")
    other = Profile("other", bindings=[Binding("R", 0.1, 0.1)])
    store.save(other)
    store.save_checkpoint(other)
    assert text(tmp_path / "future.json") == future and not (tmp_path / BACKUP_FOLDER).exists()


def test_version_2_is_the_newest_this_version_reads_and_version_1_files_are_still_accepted():
    assert LATEST_PROFILE_VERSION == 2
    assert Profile.from_dict({"version": 1, "name": "a"}).schema_version == 1
    assert Profile.from_dict({"version": 2, "name": "a"}).schema_version == 2
    assert Profile.from_dict({"name": "a"}).schema_version == 1


# ===================================================================== restore points
def test_a_version_2_restore_point_has_the_same_format_and_ids_as_the_profile_file(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(golden_v2_profile())
    store.save_checkpoint(golden_v2_profile())
    checkpoint = next((tmp_path / "saved").glob("*.json"))
    assert text(checkpoint) == text(GOLDEN_V2)
    restored = store.load_checkpoint("Golden V2")
    assert restored.id == "def456def456" and [b.id for b in restored.bindings] == ["reload", "interact", "fire", "menu"]
    assert restored.find("LMB") == golden_v2_profile().find("LMB")


def test_a_profile_and_its_restore_point_may_be_different_versions_and_each_loads_by_its_own_rules(tmp_path):
    store = ProfileStore(tmp_path)
    p = Profile("p", bindings=[Binding("R", 0.1, 0.1, "tap")])
    store.save(p)
    store.save_checkpoint(p)                                                       # version 1
    p.find("R").extra_triggers.append("F")
    store.save(p)                                                                  # the profile file is version 2 now
    assert json.loads(text(tmp_path / "p.json"))["version"] == 2
    assert json.loads(text(tmp_path / "saved" / "p.json"))["version"] == 1
    assert store.load_checkpoint("p").bindings[0].triggers == ("R",)
    assert store.load("p").bindings[0].triggers == ("R", "F")


def test_a_version_2_restore_point_without_an_id_is_migrated_with_its_profile(tmp_path):
    raw = v2_file([v2_binding("a", ["E", "F"])])
    del raw["id"]
    (tmp_path / "saved").mkdir()
    (tmp_path / "v2.json").write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "saved" / "v2.json").write_text(json.dumps(raw), encoding="utf-8")
    store = ProfileStore(tmp_path)
    pid = store.load("v2").id
    restore = json.loads(text(tmp_path / "saved" / "v2.json"))
    assert restore["id"] == pid and restore["bindings"] == raw["bindings"]
    assert json.loads(text(tmp_path / BACKUP_FOLDER / "saved" / "v2.json")) == raw


# ===================================================================== the engine is untouched in this step
def test_extra_triggers_and_additional_actions_are_both_executed():
    # (This was "additional actions are stored but not executed yet"; the multi-action step changed it on purpose.)
    r = Rig([Binding("E", 0.5, 0.5, "hold", extra_triggers=["F"], extra_actions=[Action("tap", 0.2, 0.2)])])
    assert r.press("F") is True and [t[:2] for t in r.touches()] == [(D, 0), (D, 1)]     # the extra trigger starts the first action AND the tap
    r.release("F", 100)
    assert [t[:2] for t in r.touches()] == [(D, 0), (D, 1), (U, 0), (U, 1)]
