"""Run the GUI tests with NO contact to any real phone and NO access to the user's data.

    python tools/run_isolated.py --all            run the whole simulated GUI regression suite (tool_registry.SIMULATED_TESTS)
    python tools/run_isolated.py <tool>           run one tool (name without .py); add `keep` to leave its private data folder
    python tools/run_isolated.py --list           list the tools and what each is

What it guarantees (all implemented in tools/device_isolation.py, none of it switchable by an environment variable):
  * the device guard refuses adb, scrcpy, the ADB server socket and non-local connections before anything starts;
  * LOCALAPPDATA points at a fresh temporary folder: profiles, backups, settings.ini, logs and Qt caches are private to the run;
  * the simulated phone is the only phone (tools/fake_phone.py), the window's device tracker does nothing;
  * afterwards the environment is exactly as it was, only that private folder is removed (and only if it passes the safety checks),
    and the user's real data folder is compared before / after (read-only): any change is reported as a failure. A tool that TRIED to reach
    a device (even if it swallowed the refusal) is a failure too.
A tool that crashes or fails still gets the same clean-up. Tools that need a real phone (tool_registry.LIVE_DEVICE) are refused here."""
from __future__ import annotations

import os
import runpy
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import device_isolation  # noqa: E402
import tool_registry  # noqa: E402

DEFAULT_TOOLS_DIR = HERE


def _real_dir_unchanged(before) -> bool:
    return before == device_isolation.fingerprint_tree(device_isolation.REAL_DATA_DIR)


def run_tool(tool: str, tools_dir: Path = DEFAULT_TOOLS_DIR, keep: bool = False) -> int:
    """Run one tool inside an isolation. Returns the tool's exit code (3 if the real data folder changed, 4 if it tried to reach a device, 2 if it was refused)."""
    script = Path(tools_dir) / f"{tool}.py"
    if Path(tools_dir).resolve() == HERE and tool in tool_registry.LIVE_DEVICE:
        print(f"{tool} talks to a real phone and is not run by the isolated runner (see tools/live_device.py).", file=sys.stderr)
        return 2
    if not script.is_file():
        print(f"No such tool: {script}", file=sys.stderr)
        return 2
    iso = device_isolation.ensure_active()
    before = device_isolation.fingerprint_tree(device_isolation.REAL_DATA_DIR)
    print(f"[isolated] private data folder: {iso.root}", flush=True)
    code = 0
    old_argv, old_path = list(sys.argv), list(sys.path)
    try:
        sys.argv = [str(script)]
        sys.path.insert(0, str(Path(tools_dir)))
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code in (None, 0) else 1)
    except BaseException:                                   # a crashing tool is a failed tool, and still gets its clean-up
        traceback.print_exc()
        code = 1
    finally:
        sys.argv, sys.path[:] = old_argv, old_path
        iso.stop(keep=keep)
    if keep:
        print(f"[isolated] kept: {iso.root}")
    if device_isolation.ATTEMPTS:                           # blocked, but it must not even try: report it even if the tool swallowed the error
        print(f"[isolated] FAILED: the tool tried to reach a real device {len(device_isolation.ATTEMPTS)} time(s) (all blocked): "
              + "; ".join(device_isolation.ATTEMPTS[:5]), file=sys.stderr)
        device_isolation.ATTEMPTS.clear()
        return code or 4
    if not _real_dir_unchanged(before):
        print("[isolated] FAILED: the user's real data folder changed during the run", file=sys.stderr)
        return 3
    return code


def run_all() -> int:
    device_isolation.install_device_guard()                   # (this parent only starts python; the guard is a backstop)
    before = device_isolation.fingerprint_tree(device_isolation.REAL_DATA_DIR or device_isolation._compute_real_data_dir())
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    results = []
    for tool in tool_registry.SIMULATED_TESTS:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), tool], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env)
        lines = (done.stdout + done.stderr).splitlines()
        for line in lines:                                  # a tool may end itself abruptly (the stuck-thread test does): remove its folder here
            if line.startswith("[isolated] private data folder: "):
                device_isolation.remove_private_root(Path(line.split(": ", 1)[1].strip()), device_isolation.DEFAULT_PREFIX)
        summary = next((ln.strip() for ln in reversed(lines) if "checks passed" in ln), "no result line")
        results.append((tool, done.returncode, summary))
        print(f"  {'PASS' if done.returncode == 0 else 'FAIL'}  {tool:<26} {summary}", flush=True)
    changed = before != device_isolation.fingerprint_tree(device_isolation.REAL_DATA_DIR or device_isolation._compute_real_data_dir())
    failed = [r for r in results if r[1] != 0]
    print(f"\n{len(results) - len(failed)}/{len(results)} GUI suites passed on the simulated phone"
          f"{'; the real data folder CHANGED (a failure)' if changed else '; the real data folder was not touched'}")
    return 1 if (failed or changed) else 0


def main(argv: list[str]) -> int:
    args = list(argv)
    tools_dir = DEFAULT_TOOLS_DIR
    if "--tools-dir" in args:
        i = args.index("--tools-dir")
        tools_dir = Path(args[i + 1])
        del args[i:i + 2]
    if args[:1] == ["--all"]:
        return run_all()
    if args[:1] == ["--list"]:
        for label in ("SIMULATED_TESTS", "SIMULATED_UTILITIES", "LIVE_DEVICE", "LOCAL_ONLY", "USER_DATA_UTILITIES"):
            print(f"{label}: {', '.join(getattr(tool_registry, label))}")
        return 0
    if not args:
        print(__doc__)
        return 2
    return run_tool(args[0], tools_dir, keep=args[1:2] == ["keep"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
