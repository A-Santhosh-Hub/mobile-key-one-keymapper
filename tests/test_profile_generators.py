"""The profile-generator scripts (tools/make_cod_profile.py, make_pubg_profile.py) write ONLY where they are told to, and change nothing they were
not asked to. Nothing here touches the user's real profile folder: tests write into pytest temp folders, and the "real folder" cases resolve
to the PRIVATE data folder the whole test session runs in (tests/conftest.py), which is checked against the real folder's fingerprint."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import device_isolation as iso
import make_cod_profile
import make_pubg_profile
import profile_tools as pt
from app import paths
from app.mapping.profile import Binding, Profile, ProfileStore

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
GENERATORS = [(make_cod_profile, "Call of Duty Mobile", "com.activision.callofduty.shooter", 11),
              (make_pubg_profile, "PUBG Battle Royale", "com.pubg.imobile", 16)]
IDS = ["cod", "pubg"]


def real_tree():
    tree = iso.fingerprint_tree(iso.REAL_DATA_DIR) or {}
    return {k: v for k, v in tree.items() if not k.startswith("logs")}


@pytest.fixture(autouse=True)
def the_real_folder_is_never_touched():
    before = real_tree()
    yield
    assert real_tree() == before


def run(module, *args):
    return module.main(list(args))


def files(folder: Path):
    return sorted(p.name for p in folder.glob("*.json"))


def seed(folder: Path, name: str, package: str = "", bindings: int = 1, pid: str | None = None) -> Path:
    """An existing user profile (saved through the store, so it has a permanent id)."""
    store = ProfileStore(folder)
    profile = Profile(name, "landscape", package=package, bindings=[Binding("Q", 0.1 * (i + 1), 0.5, "tap", f"mine {i}") for i in range(bindings)])
    store.save(profile)
    return store.folder / ProfileStore.filename(name)


# ===================================================================== no destination, no write
@pytest.mark.parametrize("module,name,package,count", GENERATORS, ids=IDS)
def test_without_a_destination_nothing_is_written_anywhere(module, name, package, count, capsys, monkeypatch):
    monkeypatch.setattr(pt, "ProfileStore", lambda *a, **k: pytest.fail("a store was built although nothing was chosen"))
    assert run(module) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "--output-dir" in err and pt.REAL_FLAG in err
    assert not pt.real_profile_folder().exists()                      # not even created


@pytest.mark.parametrize("module,name,package,count", GENERATORS, ids=IDS)
def test_two_destinations_are_refused_rather_than_guessed(module, name, package, count, tmp_path, capsys):
    assert run(module, "--output-dir", str(tmp_path / "x"), pt.REAL_FLAG) == 2
    assert "ONE destination" in capsys.readouterr().err and not (tmp_path / "x").exists() and not pt.real_profile_folder().exists()


# ===================================================================== an explicit output folder
@pytest.mark.parametrize("module,name,package,count", GENERATORS, ids=IDS)
def test_an_explicit_folder_receives_exactly_the_profile_in_the_legacy_format(module, name, package, count, tmp_path, capsys):
    out = tmp_path / "out"
    assert run(module, "--output-dir", str(out)) == 0
    assert files(out) == [ProfileStore.filename(name)]
    data = json.loads((out / ProfileStore.filename(name)).read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["name"] == name and data["package"] == package and len(data["bindings"]) == count and data["id"]
    assert all("id" not in b for b in data["bindings"])               # (version 1: no binding ids)
    assert f"saved '{name}'" in capsys.readouterr().out
    assert not pt.real_profile_folder().exists()


@pytest.mark.parametrize("module,name,package,count", GENERATORS, ids=IDS)
def test_the_generated_profile_is_what_it_always_was(module, name, package, count):
    profile = module.build()
    assert (profile.name, profile.package, profile.orientation) == (name, package, "landscape")
    assert len(profile.bindings) == count and len(profile.sticks) == 1 and len(profile.looks) == 1


def test_an_existing_empty_or_profile_only_folder_is_accepted(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert pt.resolve_destination(str(empty), False) == empty.resolve()
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    (mixed / "a.json").write_text("{}", encoding="utf-8")
    (mixed / "saved").mkdir()
    (mixed / "backup-before-v2").mkdir()
    assert pt.resolve_destination(str(mixed), False) == mixed.resolve()
    assert pt.resolve_destination(str(tmp_path / "new"), False) == (tmp_path / "new").resolve()        # a new last folder is fine


# ===================================================================== unsafe or ambiguous destinations
def _refusals(tmp_path):
    data = iso.active().data_dir
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "notes.txt").write_text("x", encoding="utf-8")
    a_file = tmp_path / "a-file.txt"
    a_file.write_text("x", encoding="utf-8")
    return {
        "the real profile folder": str(pt.real_profile_folder()),
        "the real data folder": str(data),
        "inside the real data folder": str(data / "profiles" / "mine"),
        "a folder that contains the real data folder": str(data.parent),
        "a relative path": "relative-folder",
        "a drive root": str(Path(tmp_path.anchor)),
        "the home folder": str(Path.home()),
        "the temp folder": tempfile.gettempdir(),
        "the program folder": str(ROOT),
        "a file": str(a_file),
        "a missing parent": str(tmp_path / "no" / "such" / "parent"),
        "a folder with other files": str(stray),
    }


def test_every_unsafe_or_ambiguous_destination_is_refused_and_nothing_is_created(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "ProfileStore", lambda *a, **k: pytest.fail("a store was built for a refused destination"))
    for what, path in _refusals(tmp_path).items():
        with pytest.raises(pt.Refusal):
            pt.resolve_destination(path, False)
        for module, *_ in GENERATORS:
            assert run(module, "--output-dir", path) == 2, what
    assert not pt.real_profile_folder().exists() and not (tmp_path / "no").exists()


def test_a_link_or_junction_is_refused_and_its_target_is_left_alone(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    if os.name == "nt":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True).returncode == 0
    else:
        link.symlink_to(target, target_is_directory=True)
        made = True
    if not made:
        pytest.skip("this account cannot create a directory link")
    try:
        with pytest.raises(pt.Refusal, match="link"):
            pt.resolve_destination(str(link), False)
        assert list(target.iterdir()) == []
    finally:
        try:
            os.rmdir(link)
        except OSError:
            pass


def test_the_destination_check_never_creates_anything(tmp_path):
    before = sorted(p.name for p in tmp_path.iterdir())
    pt.resolve_destination(str(tmp_path / "later"), False)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# ===================================================================== existing files
@pytest.mark.parametrize("module,name,package,count", GENERATORS, ids=IDS)
def test_an_existing_profile_of_the_same_name_is_not_replaced_without_overwrite(module, name, package, count, tmp_path, capsys):
    out = tmp_path / "out"
    mine = seed(out, name, package="", bindings=3)
    before = mine.read_bytes()
    assert run(module, "--output-dir", str(out)) == 2
    assert "already exists" in capsys.readouterr().err and "--overwrite" in capsys.readouterr().err + "--overwrite"
    assert mine.read_bytes() == before and not (out / pt.BACKUP_FOLDER).exists()


@pytest.mark.parametrize("module,name,package,count", GENERATORS, ids=IDS)
def test_overwrite_replaces_it_after_keeping_a_copy_and_keeps_its_identity(module, name, package, count, tmp_path):
    out = tmp_path / "out"
    mine = seed(out, name, bindings=3)
    before = mine.read_bytes()
    old_id = json.loads(before)["id"]
    assert run(module, "--output-dir", str(out), "--overwrite") == 0
    new = json.loads(mine.read_text(encoding="utf-8"))
    assert new["id"] == old_id and len(new["bindings"]) == count and new["package"] == package
    copies = list((out / pt.BACKUP_FOLDER).rglob("*.json"))
    assert len(copies) == 1 and copies[0].read_bytes() == before


@pytest.mark.parametrize("module,name,package,count", GENERATORS, ids=IDS)
def test_another_profile_linked_to_the_game_is_not_unlinked_without_permission(module, name, package, count, tmp_path, capsys):
    out = tmp_path / "out"
    other = seed(out, "My own set", package=package, bindings=2)
    before = other.read_bytes()
    assert run(module, "--output-dir", str(out)) == 2
    err = capsys.readouterr().err
    assert "My own set" in err and "--allow-unlink" in err and "--no-link" in err
    assert other.read_bytes() == before and files(out) == [other.name]


@pytest.mark.parametrize("module,name,package,count", GENERATORS, ids=IDS)
def test_no_link_leaves_the_other_profile_alone_and_allow_unlink_keeps_a_copy(module, name, package, count, tmp_path):
    out = tmp_path / "out"
    other = seed(out, "My own set", package=package, bindings=2)
    before = other.read_bytes()
    assert run(module, "--output-dir", str(out), "--no-link") == 0
    assert other.read_bytes() == before and json.loads((out / ProfileStore.filename(name)).read_text(encoding="utf-8"))["package"] == ""
    (out / ProfileStore.filename(name)).unlink()
    assert run(module, "--output-dir", str(out), "--allow-unlink") == 0
    assert json.loads(other.read_text(encoding="utf-8"))["package"] == ""
    assert [c.read_bytes() for c in (out / pt.BACKUP_FOLDER).rglob("*.json")] == [before]


def test_a_folder_with_profile_files_that_have_no_id_is_refused_untouched(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    old = out / "old.json"
    old.write_text(json.dumps({"version": 1, "name": "Old", "bindings": []}), encoding="utf-8")
    assert run(make_cod_profile, "--output-dir", str(out)) == 2
    assert "without a permanent id" in capsys.readouterr().err and files(out) == ["old.json"]
    assert json.loads(old.read_text(encoding="utf-8")) == {"version": 1, "name": "Old", "bindings": []}


def test_if_the_copy_cannot_be_made_nothing_is_written(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    mine = seed(out, "Call of Duty Mobile", bindings=3)
    before = mine.read_bytes()

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(pt.shutil, "copy2", broken)
    assert run(make_cod_profile, "--output-dir", str(out), "--overwrite") == 2
    assert "Could not back up" in capsys.readouterr().err and mine.read_bytes() == before


def test_a_dry_run_says_what_would_happen_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "out"
    assert run(make_cod_profile, "--output-dir", str(out), "--dry-run") == 0
    text = capsys.readouterr().out
    assert "DRY RUN" in text and "new profile" in text and not out.exists()
    mine = seed(tmp_path / "there", "Call of Duty Mobile", bindings=2)
    before = mine.read_bytes()
    assert run(make_cod_profile, "--output-dir", str(tmp_path / "there"), "--overwrite", "--dry-run") == 0
    assert "REPLACES" in capsys.readouterr().out and mine.read_bytes() == before and not (tmp_path / "there" / pt.BACKUP_FOLDER).exists()


# ===================================================================== the deliberate way to write the real folder
def test_the_real_flag_writes_the_real_profile_folder_and_only_that(capsys):
    """(In this session 'real' is the private data folder: the same rule, a harmless place.)"""
    target = pt.real_profile_folder()
    assert target == paths.data_dir() / "profiles" and iso.is_inside(target, iso.active().root)
    assert run(make_pubg_profile, pt.REAL_FLAG) == 0
    assert files(target) == [ProfileStore.filename("PUBG Battle Royale")]
    assert iso.fingerprint_tree(iso.active().root) is not None
    iso.shutil.rmtree(target)


def test_the_real_flag_still_never_replaces_or_unlinks_without_permission(capsys):
    target = pt.real_profile_folder()
    try:
        mine = seed(target, "PUBG Battle Royale", package="com.pubg.imobile", bindings=4)
        other = seed(target, "My PUBG", package="com.pubg.imobile", bindings=2)
        before = (mine.read_bytes(), other.read_bytes())
        assert run(make_pubg_profile, pt.REAL_FLAG) == 2
        assert (mine.read_bytes(), other.read_bytes()) == before
        assert run(make_pubg_profile, pt.REAL_FLAG, "--overwrite") == 2                     # (unlinking 'My PUBG' still needs its own permission)
        assert (mine.read_bytes(), other.read_bytes()) == before
        assert run(make_pubg_profile, pt.REAL_FLAG, "--overwrite", "--allow-unlink") == 0
        assert len(list((target / pt.BACKUP_FOLDER).rglob("*.json"))) == 2
    finally:
        iso.shutil.rmtree(target, ignore_errors=True)


def test_the_real_folder_rule_is_the_apps_own_rule():
    assert pt.real_data_folder() == paths.data_dir() and pt.real_profile_folder() == paths.data_dir() / "profiles"


# ===================================================================== the scripts themselves
@pytest.mark.parametrize("script", ["make_cod_profile.py", "make_pubg_profile.py"])
def test_started_as_a_script_with_no_arguments_nothing_is_written(script, tmp_path):
    """Run as a real command against a PRETEND local-app-data folder, so even a bug could not reach a real one."""
    pretend = tmp_path / "pretend-local-appdata"
    pretend.mkdir()
    done = subprocess.run([sys.executable, str(TOOLS / script)], capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, LOCALAPPDATA=str(pretend)))
    assert done.returncode == 2 and "REFUSED" in done.stderr
    assert list(pretend.iterdir()) == []


@pytest.mark.parametrize("script", ["make_cod_profile.py", "make_pubg_profile.py"])
def test_started_as_a_script_with_an_output_folder_it_writes_only_there(script, tmp_path):
    pretend = tmp_path / "pretend-local-appdata"
    pretend.mkdir()
    out = tmp_path / "chosen"
    done = subprocess.run([sys.executable, str(TOOLS / script), "--output-dir", str(out)], capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, LOCALAPPDATA=str(pretend)))
    assert done.returncode == 0 and len(files(out)) == 1 and list(pretend.iterdir()) == []


def test_no_generator_names_a_real_location_itself():
    for name in ("make_cod_profile.py", "make_pubg_profile.py"):
        text = (TOOLS / name).read_text(encoding="utf-8-sig")
        assert "data_dir" not in text and "LOCALAPPDATA" not in text and "ProfileStore(" not in text, name
        assert "profile_tools.run(" in text


def test_special_folders_are_refused_even_when_they_look_empty(tmp_path, monkeypatch):
    """The 'other files' rule would already refuse the real ones; the named rule must hold on its own (an empty 'home', 'temp', 'program' folder)."""
    for what, attribute in (("home", "home"), ("temp", "temp"), ("program", "root")):
        folder = tmp_path / what
        folder.mkdir()
        with monkeypatch.context() as patch:
            if attribute == "home":
                patch.setattr(pt.Path, "home", staticmethod(lambda f=folder: f))
            elif attribute == "temp":
                patch.setattr(pt.tempfile, "gettempdir", lambda f=folder: str(f))
            else:
                patch.setattr(pt, "ROOT", folder)
            with pytest.raises(pt.Refusal, match=what):
                pt.resolve_destination(str(folder), False)
    anchor = Path(tmp_path.anchor)
    with pytest.raises(pt.Refusal):
        pt.resolve_destination(str(anchor), False)


def test_a_profile_of_the_same_name_in_another_letter_case_still_counts_as_existing(tmp_path, capsys):
    out = tmp_path / "out"
    mine = seed(out, "CALL OF DUTY MOBILE", bindings=2)
    before = mine.read_bytes()
    assert run(make_cod_profile, "--output-dir", str(out)) == 2
    assert "already exists" in capsys.readouterr().err and mine.read_bytes() == before and files(out) == [mine.name]
