# Testing Mobile Key One safely

Automated tests must never reach a physical phone and never touch your data. This is enforced by code, not by habit.

## Run the checks

```
.venv\Scripts\python -m pytest tests                      # unit tests (about 2,450)
.venv\Scripts\python tools\run_isolated.py --all          # every GUI suite, on the simulated phone
.venv\Scripts\python tools\run_isolated.py editor_test    # one GUI suite
.venv\Scripts\python tools\run_isolated.py --list         # what each tool is
```

Running a simulated tool directly (`python tools\editor_test.py`) is isolated too, because importing `tools/fake_phone.py` isolates the
process. There is no switch, option or environment variable that turns this off.

## What is enforced (tools/device_isolation.py)

* **Device guard.** Before anything starts, these are refused with `RealDeviceAccessBlocked`: any process whose program is `adb` or `scrcpy`
  (or that mentions scrcpy's server or `app_process`), through `subprocess`, `os.system`, `os.spawn*`, `os.exec*` and `os.startfile`; and any
  socket connection to the ADB server port (5037, or `ANDROID_ADB_SERVER_PORT`) or to a non-local address. The socket part matters:
  `app/device/tracker.py` lists phones over a raw socket to the ADB server and starts no process.
* **Private data folder.** `LOCALAPPDATA` points at a fresh temporary folder for the run, so profiles, backups, `settings.ini`, logs and Qt
  caches are private. The environment is restored exactly afterwards. Only that folder is removed, and only if it is directly inside the temp
  folder, has our name prefix and marker file, is not a link or junction, and is not your real data folder.
* **Simulated phone only.** `connect_window()` always attaches the fake phone; the window's `adb` becomes an object that refuses everything and
  its device tracker does nothing. If the simulation cannot be set up the test stops with an error; nothing else is tried.
* **Visible failures.** The pytest suite fails any test that leaves a blocked attempt behind, even if the code under test swallowed the error.
  The runner does the same for GUI tools, and also fails if your real data folder changed (compared read-only before and after).

## The app's own command line

`python -m app` (and `run.bat`) is the real application: it starts the ADB server and lists phones at launch, and connects only when you press
Connect. It has no option that connects by itself. (`--selftest` and `--screenshot` did, with no confirmation; they were removed. The
simulated GUI suites test the window, and `tools/stream_test.py` is the live equivalent behind the opt-in below.) Tests prove this: unknown or
old options are refused before any window exists, and only the Connect button, 'Apply now' / reconnect after a loss and the retry timer start a
session, the latter two only after a connection you started.

## Generating profiles (tools/make_cod_profile.py, make_pubg_profile.py)

They write nothing unless you choose where: `--output-dir DIR` (a full path to a new, empty or profile-only folder), or
`--write-my-real-profiles` for your real profile folder, on purpose. Neither or both is refused. An existing profile of the same name is only
replaced with `--overwrite`, other profiles linked to the same game are only unlinked with `--allow-unlink` (or skip linking with `--no-link`),
and whatever is replaced is first copied to `<folder>/backup-before-generate/`. Your real data folder, links, drive roots, your home or temp
folder and folders with other files are refused as `--output-dir`. Try `--dry-run` first. (Before this, they wrote your real folder directly and
`link_package` rewrote every other profile linked to the same game.)

## Tools that need a real phone

`tools/cod_*`, `game_*`, `pubg_*`, `control_test`, `look_test`, `probe_device`, `stream_test` talk to a real phone by design. Each refuses to
start unless you give the flag `--really-use-real-phone`, run it in an interactive terminal, and type `USE MY PHONE`. They use your real data
folder and phone: never run them while you are playing, and never as part of a test run. The isolated runner refuses them.
Every script in `tools/` is classified in `tools/tool_registry.py`; a test fails if one is missing or does not behave as classified.

## What the guard cannot see

A process started natively (ctypes `CreateProcess`, `QProcess`) or by another interpreter that a test launches. The application uses neither
(a test scans its sources to keep it that way), and tests only launch Python.

## Supervised real-device verification

The staged plan for connecting the phone yourself, with what to expect from delayed and cancelled actions, is in
`docs/REAL_DEVICE_VERIFICATION.md`.

## Optional manual checks of a phone that was reached by accident (later, by you)

Nothing here was done for you, and none of it is needed for the app to work. If you want to confirm the phone is in the state you expect
(each step is something *you* run or look at, with the phone attached when you choose):

1. **Stay awake.** Phone: Settings > Developer options > "Stay awake". The app starts scrcpy with `stay_awake=true` and `cleanup=true`; the
   server restores the old value when it shuts down cleanly. If the phone no longer sleeps while charging and you did not intend that, switch it.
2. **Screen timeout** under Settings > Display: confirm it is what you set.
3. **Leftover server file.** `/data/local/tmp/scrcpy-server.jar` exists after any normal use of the app; it is harmless.
4. **Leftover port forwards.** `vendor\adb\adb.exe forward --list` should be empty when the app is closed.
5. **Stuck setting.** `vendor\adb\adb.exe shell settings get global stay_on_while_plugged_in` normally prints `0`; `7` means the server did not
   restore it.
6. **The game.** If you were in a match at the time (the log shows Call of Duty Mobile in the foreground during two sessions), confirm it
   behaved normally. The tests swallowed every touch, so none should have arrived.
