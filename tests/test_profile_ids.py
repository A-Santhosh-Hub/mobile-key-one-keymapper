"""Profile identity: no two profiles can share a file, old files migrate without loss, nothing is overwritten silently."""
import json

import pytest

from app.mapping.profile import BACKUP_FOLDER, Binding, Profile, ProfileError, ProfileStore


def make(name, key="R", x=0.5, **kw):
    return Profile(name, bindings=[Binding(key, x, 0.5, "tap")], **kw)


def write_legacy(folder, filename, name, key="R", package="", with_checkpoint=False):
    """A profile file as the old version wrote it: no id."""
    data = make(name, key, package=package).to_dict()
    (folder / filename).write_text(json.dumps(data, indent=2), encoding="utf-8")
    if with_checkpoint:
        (folder / "saved").mkdir(exist_ok=True)
        (folder / "saved" / filename).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


# ===================================================================== the collision bug itself
@pytest.mark.parametrize("a,b", [("COD: BR", "COD? BR"), ("A/B", "A_B"), ("Zz", "zz"), ("PUBG*", "PUBG_")])
def test_names_that_map_to_one_file_name_no_longer_overwrite_each_other(tmp_path, a, b):
    assert ProfileStore.filename(a).lower() == ProfileStore.filename(b).lower()      # the cause, still true
    store = ProfileStore(tmp_path)
    first, second = make(a, "Q", 0.1), make(b, "W", 0.9)
    store.save(first)
    if a.lower() == b.lower():
        # the SAME name in another letter case is the same profile name: an object without an id updates it ...
        store.save(second)
        assert len(store.names()) == 1 and len(list(tmp_path.glob("*.json"))) == 1
        # ... but a profile with its own, different identity is refused instead of overwriting it
        stranger = make(b, "E", 0.5)
        stranger.id = "someone-else"
        with pytest.raises(ProfileError):
            store.save(stranger)
        assert store.load(a).find("W") is not None
        return
    store.save(second)
    assert sorted(store.names(), key=str.lower) == sorted([a, b], key=str.lower)
    assert store.load(a).find("Q") is not None and store.load(b).find("W") is not None
    assert len({f.name.lower() for f in tmp_path.glob("*.json")}) == 2


def test_a_second_profile_gets_a_suffixed_file_but_the_first_keeps_its_plain_name(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(make("COD: BR"))
    store.save(make("COD? BR"))
    names = {f.name for f in tmp_path.glob("*.json")}
    assert "COD_ BR.json" in names and len(names) == 2 and any(n.startswith("COD_ BR-") for n in names)


def test_saving_a_different_profile_under_a_taken_name_is_refused_and_changes_nothing(tmp_path):
    store = ProfileStore(tmp_path)
    original = make("Mine", "Q", 0.1)
    store.save(original)
    before = (tmp_path / "Mine.json").read_text(encoding="utf-8")
    impostor = make("mine", "W", 0.9)
    impostor.id = "another-identity"
    with pytest.raises(ProfileError, match="nothing was overwritten"):
        store.save(impostor)
    assert (tmp_path / "Mine.json").read_text(encoding="utf-8") == before


def test_an_object_without_an_id_saved_under_an_existing_name_updates_that_profile(tmp_path):
    """The old behaviour that scripts and tests rely on: same name = same profile."""
    store = ProfileStore(tmp_path)
    first = make("G", "Q")
    store.save(first)
    second = make("G", "W")                                      # a fresh object, no id
    store.save(second)
    assert second.id == first.id and store.names() == ["G"]
    assert store.load("G").find("W") is not None and store.load("G").find("Q") is None
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_an_unreadable_file_is_never_overwritten(tmp_path):
    (tmp_path / "Broken.json").write_text("{not json", encoding="utf-8")
    store = ProfileStore(tmp_path)
    store.save(make("Broken"))
    assert (tmp_path / "Broken.json").read_text(encoding="utf-8") == "{not json"
    assert store.load("Broken").find("R") is not None


# ===================================================================== identity is permanent
def test_the_id_is_written_and_stays_the_same(tmp_path):
    store = ProfileStore(tmp_path)
    p = make("G")
    store.save(p)
    assert p.id and json.loads((tmp_path / "G.json").read_text(encoding="utf-8"))["id"] == p.id
    again = store.load("G")
    assert again.id == p.id
    store.save(again)
    assert store.load("G").id == p.id


def test_a_copy_made_with_to_dict_is_a_new_profile(tmp_path):
    store = ProfileStore(tmp_path)
    p = make("Original")
    store.save(p)
    copy = Profile.from_dict(p.to_dict())
    copy.name = "Copy"
    assert copy.id == ""
    store.save(copy)
    assert copy.id and copy.id != p.id and sorted(store.names()) == ["Copy", "Original"]


def test_renaming_keeps_the_identity_and_the_file(tmp_path):
    store = ProfileStore(tmp_path)
    p = make("Old")
    store.save(p)
    file_before = store.path("Old")
    p.name = "New"
    store.save(p)
    assert store.names() == ["New"] and store.path("New") == file_before and len(list(tmp_path.glob("*.json"))) == 1


def test_renaming_onto_another_profiles_name_is_refused(tmp_path):
    store = ProfileStore(tmp_path)
    a, b = make("A"), make("B")
    store.save(a)
    store.save(b)
    b.name = "a"
    with pytest.raises(ProfileError):
        store.save(b)
    assert sorted(store.names()) == ["A", "B"]


def test_deleting_one_profile_leaves_a_colliding_neighbour_alone(tmp_path):
    store = ProfileStore(tmp_path)
    for name in ("COD: BR", "COD? BR"):
        p = make(name)
        store.save(p)
        store.save_checkpoint(p)
    store.delete("COD? BR")
    assert store.names() == ["COD: BR"] and store.has_checkpoint("COD: BR") and not store.has_checkpoint("COD? BR")
    assert len(list((tmp_path / "saved").glob("*.json"))) == 1


def test_restore_points_follow_their_own_profile_when_files_are_suffixed(tmp_path):
    store = ProfileStore(tmp_path)
    a, b = make("COD: BR", "Q"), make("COD? BR", "W")
    for p in (a, b):
        store.save(p)
        store.save_checkpoint(p)
    a.add(Binding("T", 0.2, 0.2))
    store.save(a)
    assert store.has_unsaved_changes(a) is True and store.has_unsaved_changes(b) is False
    assert store.load_checkpoint("COD: BR").find("T") is None and store.load_checkpoint("COD? BR").find("W") is not None


def test_linking_a_package_follows_ids_even_for_colliding_names(tmp_path):
    store = ProfileStore(tmp_path)
    a, b = make("COD: BR", package="com.game"), make("COD? BR")
    store.save(a)
    store.save(b)
    assert store.link_package(b, "com.game") == ["COD: BR"]
    assert store.find_by_package("com.game") == "COD? BR" and store.load("COD: BR").package == ""


# ===================================================================== migration of existing files
def test_old_files_get_ids_in_place_and_their_content_is_unchanged(tmp_path):
    original = write_legacy(tmp_path, "PUBG Battle Royale.json", "PUBG Battle Royale", "Q", package="com.pubg.imobile")
    store = ProfileStore(tmp_path)
    data = json.loads((tmp_path / "PUBG Battle Royale.json").read_text(encoding="utf-8"))
    assert data.pop("id")
    assert data == original                                      # nothing but the id was added
    assert store.names() == ["PUBG Battle Royale"] and store.find_by_package("com.pubg.imobile") == "PUBG Battle Royale"
    assert store.load("PUBG Battle Royale").to_dict() == original


def test_the_original_is_backed_up_before_it_is_changed(tmp_path):
    original = write_legacy(tmp_path, "G.json", "G")
    raw = (tmp_path / "G.json").read_text(encoding="utf-8")
    ProfileStore(tmp_path)
    backups = list((tmp_path / BACKUP_FOLDER).glob("*.json"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == raw
    assert json.loads(backups[0].read_text(encoding="utf-8")) == original


def test_migration_runs_once_and_keeps_the_ids(tmp_path):
    write_legacy(tmp_path, "G.json", "G")
    first = ProfileStore(tmp_path).load("G").id
    second_store = ProfileStore(tmp_path)
    assert second_store.load("G").id == first
    assert second_store.migrate_ids() == []
    assert len(list((tmp_path / BACKUP_FOLDER).glob("*.json"))) == 1       # no second backup of an already migrated file


def test_restore_points_are_migrated_with_the_same_id_and_still_pair_up(tmp_path):
    write_legacy(tmp_path, "G.json", "G", with_checkpoint=True)
    store = ProfileStore(tmp_path)
    profile = store.load("G")
    restore = json.loads((tmp_path / "saved" / "G.json").read_text(encoding="utf-8"))
    assert restore["id"] == profile.id
    assert store.has_checkpoint("G") and store.has_unsaved_changes(profile) is False
    assert len(list((tmp_path / BACKUP_FOLDER / "saved").glob("*.json"))) == 1


def test_a_copied_file_with_the_same_id_gets_its_own_id(tmp_path):
    a = ProfileStore(tmp_path)
    p = make("Orig")
    a.save(p)
    raw = json.loads((tmp_path / "Orig.json").read_text(encoding="utf-8"))
    raw["name"] = "Copy of Orig"
    (tmp_path / "Copy of Orig.json").write_text(json.dumps(raw), encoding="utf-8")        # user copied the file
    store = ProfileStore(tmp_path)
    ids = {store.load(n).id for n in store.names()}
    assert len(ids) == 2 and p.id in ids


def test_files_from_a_newer_version_are_left_exactly_as_they_are(tmp_path):
    raw = json.dumps({"version": 99, "name": "future", "bindings": []})
    (tmp_path / "f.json").write_text(raw, encoding="utf-8")
    store = ProfileStore(tmp_path)
    assert (tmp_path / "f.json").read_text(encoding="utf-8") == raw and not (tmp_path / BACKUP_FOLDER).exists()
    assert store.names() == ["future"]


def test_if_the_backup_cannot_be_made_the_file_is_not_touched(tmp_path, monkeypatch):
    write_legacy(tmp_path, "G.json", "G")
    raw = (tmp_path / "G.json").read_text(encoding="utf-8")
    import app.mapping.profile as profile_module
    monkeypatch.setattr(profile_module.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    store = ProfileStore(tmp_path)
    assert (tmp_path / "G.json").read_text(encoding="utf-8") == raw
    assert store.load("G").find("R") is not None                 # still works, with a stable in-memory id
    assert store.load("G").id == store.load("G").id


def test_a_profile_without_a_persisted_id_can_still_be_saved_and_keeps_its_file(tmp_path, monkeypatch):
    write_legacy(tmp_path, "G.json", "G")
    import app.mapping.profile as profile_module
    monkeypatch.setattr(profile_module.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    store = ProfileStore(tmp_path)
    p = store.load("G")
    p.add(Binding("T", 0.1, 0.1))
    store.save(p)
    assert len(list(tmp_path.glob("*.json"))) == 1 and store.load("G").find("T") is not None


def test_two_old_files_that_were_already_one_overwritten_profile_stay_one(tmp_path):
    """What the old version could have left behind: one file, one profile. Nothing to recover, nothing to lose."""
    write_legacy(tmp_path, "COD_ BR.json", "COD: BR")
    store = ProfileStore(tmp_path)
    store.save(make("COD? BR", "W"))                             # the colliding name now gets its own file
    assert sorted(store.names()) == ["COD: BR", "COD? BR"]


def test_a_full_old_style_folder_round_trips(tmp_path):
    write_legacy(tmp_path, "Default.json", "Default")
    write_legacy(tmp_path, "COD.json", "COD", "Q", package="com.cod", with_checkpoint=True)
    write_legacy(tmp_path, "PUBG.json", "PUBG", "W", package="com.pubg")
    store = ProfileStore(tmp_path)
    assert store.names() == ["COD", "Default", "PUBG"]
    assert {n: store.load(n).package for n in store.names()} == {"COD": "com.cod", "Default": "", "PUBG": "com.pubg"}
    for name in store.names():                                   # edit and save everything: still three files
        p = store.load(name)
        p.add(Binding("Z", 0.3, 0.3))
        store.save(p)
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_asking_about_a_deleted_profile_never_reaches_a_neighbours_restore_point(tmp_path):
    store = ProfileStore(tmp_path)
    a, b = make("COD: BR"), make("COD? BR")
    for p in (a, b):
        store.save(p)
        store.save_checkpoint(p)
    store.delete("COD? BR")
    assert not store.has_checkpoint("COD? BR") and store.checkpoint_time("COD? BR") is None
    with pytest.raises(ProfileError):
        store.load_checkpoint("COD? BR")
    assert store.has_checkpoint("COD: BR")


def test_a_restore_point_for_an_unsaved_profile_saves_it_first_into_a_free_file(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(make("COD: BR", "Q"))
    store.save_checkpoint(store.load("COD: BR"))
    newcomer = make("COD? BR", "W")
    store.save_checkpoint(newcomer)                              # never saved before
    assert store.load("COD: BR").find("Q") is not None and store.load("COD? BR").find("W") is not None
    assert store.load_checkpoint("COD: BR").find("Q") is not None and store.load_checkpoint("COD? BR").find("W") is not None
