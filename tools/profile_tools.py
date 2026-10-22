"""Safe destinations for the profile-generator scripts (make_cod_profile.py, make_pubg_profile.py).

The generators used to write straight into the user's real profile folder, and `ProfileStore.link_package` silently UNLINKED AND REWROTE
every other profile linked to the same game, while saving under an existing name replaced that profile. They now do nothing until a
destination is chosen on purpose, and they never change anything they were not asked to:

  --output-dir DIR           write into DIR (a full path). DIR must be new, empty, or hold only profile files; it must not be, contain or lie
                             inside your real data folder, a link, a drive root, your home folder or the temp folder.
  --write-my-real-profiles   write into your real profile folder (%LOCALAPPDATA%\\MobileKeyOne\\profiles). Deliberate, named, never a default.
  (neither / both)           refused: nothing is guessed.
  --overwrite                replace an existing profile of the same name (otherwise refused).
  --allow-unlink             let other profiles linked to the same game lose that link (otherwise refused, naming them).
  --no-link                  do not link the new profile to the game.
  --dry-run                  say what would happen and write nothing.
Anything that is replaced or unlinked is first copied to <folder>/backup-before-generate/<time>/; if a copy cannot be made nothing is
written. A folder that contains profile files without a permanent id is refused (open the app once to migrate it, or choose another)."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import paths  # noqa: E402
from app.mapping.profile import Profile, ProfileError, ProfileStore  # noqa: E402

REAL_FLAG = "--write-my-real-profiles"
BACKUP_FOLDER = "backup-before-generate"
KNOWN_SUBFOLDERS = {"saved", "backup-before-ids", "backup-before-repair", "backup-before-v2", BACKUP_FOLDER}


class Refusal(Exception):
    """The destination or the request is ambiguous or unsafe: nothing was written."""


def _local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "MobileKeyOne"       # (the same rule as app.paths.data_dir, without creating it)


def real_data_folder() -> Path:
    return _local_app_data()


def real_profile_folder() -> Path:
    return _local_app_data() / "profiles"


def _overlaps(a: Path, b: Path) -> bool:
    a, b = a.resolve(), b.resolve()
    return a == b or a in b.parents or b in a.parents


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or os.path.realpath(path) != os.path.abspath(path)
    except OSError:
        return True


def resolve_destination(output_dir: str | None, real: bool) -> Path:
    """The folder to write into, or Refusal. Never creates anything."""
    if real and output_dir:
        raise Refusal(f"Choose ONE destination: --output-dir or {REAL_FLAG}, not both.")
    if real:
        return real_profile_folder()
    if not output_dir:
        raise Refusal(f"No destination was chosen, so nothing was written. Give --output-dir DIR (a folder of your choice) or, to write "
                      f"into your real profile folder on purpose, {REAL_FLAG}.")
    path = Path(output_dir).expanduser()
    if not path.is_absolute():
        raise Refusal(f"'{output_dir}' is not a full path (it would depend on the folder the script was started from). Give a full path.")
    if _is_link(path):
        raise Refusal(f"'{output_dir}' is a link or junction; give the real folder.")
    resolved = path.resolve()
    if _overlaps(resolved, real_data_folder()):
        raise Refusal(f"'{output_dir}' is your real data folder (or contains or lies inside it). If you mean to write your real profiles, use "
                      f"{REAL_FLAG}.")
    for special, what in ((Path(resolved.anchor), "a drive root"), (Path.home(), "your home folder"),
                          (Path(tempfile.gettempdir()), "the temp folder"), (ROOT, "the program folder")):
        if resolved == special.resolve():
            raise Refusal(f"'{output_dir}' is {what}; choose a folder just for profiles.")
    if resolved.exists():
        if not resolved.is_dir():
            raise Refusal(f"'{output_dir}' is a file, not a folder.")
        stray = [e.name for e in resolved.iterdir()
                 if not ((e.is_file() and e.suffix.lower() == ".json") or (e.is_dir() and e.name in KNOWN_SUBFOLDERS))]
        if stray:
            raise Refusal(f"'{output_dir}' does not look like a profile folder (it holds {', '.join(stray[:4])}); choose a new or empty folder.")
    elif not resolved.parent.exists():
        raise Refusal(f"The parent of '{output_dir}' does not exist; create it first (only the last folder is created for you).")
    return resolved


@dataclass
class Plan:
    folder: Path
    name: str
    package: str
    link: bool
    existing: Path | None = None                   # a profile of this name is already there
    unlink: list = field(default_factory=list)     # [(profile name, file)] linked to the same game
    without_id: list = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"folder: {self.folder}" + ("" if self.folder.exists() else "  (will be created)")]
        out.append(f"'{self.name}': " + (f"REPLACES the existing profile ({self.existing.name})" if self.existing else "new profile"))
        if self.link:
            out.append(f"link to {self.package}: " + (", ".join(f"'{n}' loses its link" for n, _ in self.unlink) if self.unlink else "no other profile affected"))
        else:
            out.append("not linked to the game")
        return out


def make_plan(folder: Path, name: str, package: str, link: bool) -> Plan:
    """Look at the folder (read-only: no ProfileStore is built, so nothing is migrated or created) and say what saving would change."""
    plan = Plan(folder, name, package, link)
    if not folder.is_dir():
        return plan
    for file in sorted(folder.glob("*.json")):
        data = ProfileStore._read_json(file)
        if data is None:
            continue
        if not data.get("id"):
            plan.without_id.append(file.name)
        if str(data.get("name", "")).lower() == name.lower():
            plan.existing = file
        elif link and package and data.get("package") == package:
            plan.unlink.append((data.get("name", file.stem), file))
    return plan


def _backup(files: list[Path], folder: Path) -> Path | None:
    if not files:
        return None
    target = folder / BACKUP_FOLDER / time.strftime("%Y%m%d-%H%M%S")
    try:
        target.mkdir(parents=True, exist_ok=False)
        for file in files:
            shutil.copy2(file, target / file.name)
    except OSError as exc:
        raise Refusal(f"Could not back up what would change ({exc}); nothing was written.") from exc
    return target


def generate(build, name: str, package: str, folder: Path, *, link: bool = True, overwrite: bool = False, allow_unlink: bool = False,
             dry_run: bool = False) -> list[str]:
    """Write the generated profile into `folder` under the rules above. Returns what was (or would be) done, as lines. Raises Refusal."""
    plan = make_plan(folder, name, package, link)
    if plan.without_id:
        raise Refusal(f"The folder holds profile files without a permanent id ({', '.join(plan.without_id[:3])}); saving would rewrite them. "
                      "Open the app once to migrate them, or choose another folder.")
    if plan.existing and not overwrite:
        raise Refusal(f"A profile named '{name}' already exists in that folder ({plan.existing.name}); nothing was changed. "
                      "Use --overwrite to replace it (a copy is kept).")
    if plan.unlink and not allow_unlink:
        raise Refusal("Linking would take the game away from: " + ", ".join(f"'{n}'" for n, _ in plan.unlink)
                      + ". Nothing was changed. Use --allow-unlink to let that happen (a copy is kept), or --no-link.")
    lines = plan.lines()
    if dry_run:
        return ["DRY RUN (nothing written)"] + lines
    to_save = ([plan.existing] if plan.existing else []) + [f for _, f in plan.unlink]
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)             # (an explicit folder's parent was checked to exist)
    kept = _backup(to_save, folder)
    profile: Profile = build()
    if not link:
        profile.package = ""                                 # 'not linked' means not linked: the generated profile does not carry the game either
    try:
        store = ProfileStore(folder)
        if link:
            store.link_package(profile, package)
        else:
            store.save(profile)
    except ProfileError as exc:
        raise Refusal(f"The profile could not be saved: {exc}") from exc
    return lines + ([f"copies of what changed: {kept}"] if kept else []) + [
        f"saved '{name}' with {len(profile.bindings)} bindings, {len(profile.sticks)} stick, {len(profile.looks)} look zone"]


def run(argv: list[str] | None, *, build, name: str, package: str, description: str) -> int:
    """The command line of a generator script. Exit code 0 = written (or dry run), 2 = refused (nothing written)."""
    parser = argparse.ArgumentParser(description=description + "  Nothing is written unless a destination is chosen.", add_help=True)
    parser.add_argument("--output-dir", metavar="DIR", help="write into this (new, empty or profile-only) full-path folder")
    parser.add_argument(REAL_FLAG, action="store_true", dest="real", help="write into your real profile folder, on purpose")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing profile of the same name (a copy is kept)")
    parser.add_argument("--allow-unlink", action="store_true", help="let other profiles linked to this game lose the link (a copy is kept)")
    parser.add_argument("--no-link", action="store_true", help="do not link the profile to the game")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen; write nothing")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        folder = resolve_destination(args.output_dir, args.real)
        for line in generate(build, name, package, folder, link=not args.no_link, overwrite=args.overwrite, allow_unlink=args.allow_unlink,
                             dry_run=args.dry_run):
            print(line)
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0
