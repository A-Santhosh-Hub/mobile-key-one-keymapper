"""Test-infrastructure safety (Phase 2, step 9): tests cannot reach a physical phone and cannot touch the user's data.

EVERY provocation here uses a FAKE `adb.bat` / `scrcpy.bat` in a temporary folder (they only write a sentinel file) and loopback or
reserved addresses, so even if a guard failed, nothing would reach a real device. No test here starts the real adb, connects to the
real ADB server, or needs a phone."""
import ast
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import device_isolation as iso
import tool_registry
from app import paths

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
APP = ROOT / "app"
blocked = pytest.mark.expects_device_block


@pytest.fixture
def fake_programs(tmp_path):
    """Stand-ins named like the device programs. They only write a sentinel; they are never expected to run."""
    folder = tmp_path / "bin"
    folder.mkdir()
    sentinel = tmp_path / "ran.txt"
    made = {}
    for name in ("adb", "scrcpy"):
        exe = folder / f"{name}.bat"
        exe.write_text(f'@echo ran> "{sentinel}"\r\n', encoding="ascii")
        made[name] = str(exe)
    made["sentinel"] = sentinel
    return made


def ran(fake) -> bool:
    return fake["sentinel"].exists()


def real_tree():
    """The user's real data folder, read-only, without the log (the app itself may be writing that while a test runs)."""
    tree = iso.fingerprint_tree(iso.REAL_DATA_DIR) or {}
    return {k: v for k, v in tree.items() if not k.startswith("logs")}


# ===================================================================== the session itself is isolated
def test_the_unit_suite_runs_inside_a_private_data_folder_with_the_guard_installed():
    session = iso.active()
    assert session is not None and iso.guard_installed()
    assert iso.is_inside(paths.data_dir(), session.root) and not iso.is_inside(paths.data_dir(), iso.REAL_DATA_DIR)
    assert iso.REAL_DATA_DIR is not None and iso.REAL_DATA_DIR.name == "MobileKeyOne"


# ===================================================================== what counts as a device command
@pytest.mark.parametrize("command,shell", [
    (["adb", "devices"], False), (["adb.exe", "-s", "X", "shell", "ls"], False), ([r"C:\x\ADB.EXE", "version"], False),
    (["D:/a b/vendor/adb/adb.exe", "start-server"], False), (["scrcpy"], False), (["scrcpy.exe", "--serial", "X"], False),
    ("adb devices", True), ('"C:\\Program Files\\adb.exe" devices', False), ("cmd /c adb devices", False),
    (["cmd", "/c", "adb", "devices"], False), (["powershell", "-c", "adb devices"], False), (["sh", "-c", "echo hi; adb devices"], False),
    (["python", "run.py", "scrcpy-server-v5.0.1"], False), (["tool", "CLASSPATH=/x", "app_process", "/"], False),
])
def test_every_form_of_a_device_command_is_recognised(command, shell):
    assert iso.classify_command(command, shell)


@pytest.mark.parametrize("command", [
    [sys.executable, "-c", "print('adb')"], ["python", "script.py"], ["git", "status"], "dir", ["cmd", "/c", "echo hello"],
    [r"C:\tools\adb-notes.txt"], ["adbfs"], ["notscrcpy"],
])
def test_ordinary_commands_are_not_mistaken_for_device_commands(command):
    assert iso.classify_command(command) == ""


def test_ordinary_processes_still_run():
    done = subprocess.run([sys.executable, "-c", "print('fine')"], capture_output=True, text=True)
    assert done.stdout.strip() == "fine" and not iso.ATTEMPTS


# ===================================================================== blocked BEFORE anything starts
@blocked
def test_every_subprocess_entry_point_refuses_a_device_program_and_starts_nothing(fake_programs):
    adb = fake_programs["adb"]
    calls = [lambda: subprocess.run([adb, "devices"]), lambda: subprocess.Popen([adb]), lambda: subprocess.check_output([adb, "version"]),
             lambda: subprocess.check_call([adb]), lambda: subprocess.call([adb]),
             lambda: subprocess.run(f'"{adb}" devices', shell=True), lambda: subprocess.run([adb], shell=True),
             lambda: subprocess.Popen(["whatever"], executable=adb), lambda: subprocess.run([fake_programs["scrcpy"], "--version"])]
    for call in calls:
        with pytest.raises(iso.RealDeviceAccessBlocked):
            call()
    assert not ran(fake_programs) and len(iso.ATTEMPTS) == len(calls)


@blocked
def test_the_os_level_ways_to_start_a_program_are_blocked_too(fake_programs):
    adb = fake_programs["adb"]
    with pytest.raises(iso.RealDeviceAccessBlocked):
        os.system(f'"{adb}" devices')
    with pytest.raises(iso.RealDeviceAccessBlocked):
        os.spawnv(os.P_NOWAIT, adb, [adb])
    with pytest.raises(iso.RealDeviceAccessBlocked):
        os.spawnl(os.P_NOWAIT, adb, adb)
    if hasattr(os, "startfile"):
        with pytest.raises(iso.RealDeviceAccessBlocked):
            os.startfile(adb)
    assert not ran(fake_programs)


@blocked
def test_the_apps_own_adb_wrapper_cannot_start_adb_even_when_given_a_path(fake_programs):
    from app.device.adb import Adb
    wrapper = Adb(path=fake_programs["adb"])
    operations = [lambda: wrapper.run("devices"), wrapper.devices, wrapper.start_server, wrapper.version,
                  lambda: wrapper.push("S", "a", "/b"), lambda: wrapper.forward("S", "localabstract:x"),
                  lambda: wrapper.getprop("S", "ro.x"), lambda: wrapper.screencap("S"), lambda: wrapper.popen_shell("S", "ls"),
                  lambda: wrapper.forward_remove("S", 1234)]
    for operation in operations:
        with pytest.raises(iso.RealDeviceAccessBlocked):
            operation()
    assert not ran(fake_programs)


@blocked
def test_a_scrcpy_session_cannot_push_or_start_the_server(fake_programs):
    from app.device.adb import Adb
    from app.stream.session import ScrcpySession, StreamConfig
    session = ScrcpySession(Adb(path=fake_programs["adb"]), "SERIAL", StreamConfig())
    with pytest.raises(iso.RealDeviceAccessBlocked):
        session.start()
    assert not ran(fake_programs) and any("push" in a for a in iso.ATTEMPTS)


@blocked
def test_the_adb_server_socket_cannot_be_opened_which_is_how_the_tracker_lists_phones(monkeypatch):
    with pytest.raises(iso.RealDeviceAccessBlocked):
        socket.create_connection(("127.0.0.1", 5037), timeout=0.2)
    sock = socket.socket()
    with pytest.raises(iso.RealDeviceAccessBlocked):
        sock.connect(("localhost", 5037))
    with pytest.raises(iso.RealDeviceAccessBlocked):
        sock.connect_ex(("127.0.0.1", 5037))
    sock.close()
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "15555")                           # a relocated ADB server is protected too
    with pytest.raises(iso.RealDeviceAccessBlocked):
        socket.create_connection(("127.0.0.1", 15555), timeout=0.2)
    assert len(iso.ATTEMPTS) == 4


@blocked
def test_the_device_tracker_cannot_listen_for_phones():
    from app.device.adb import Adb
    from app.device.tracker import DeviceTracker
    with pytest.raises(iso.RealDeviceAccessBlocked):
        DeviceTracker(Adb(path="Z:/none/tool.exe"))._track()


@blocked
def test_non_local_addresses_are_refused():
    for host in ("192.0.2.10", "198.51.100.7", "203.0.113.9"):                 # (literal addresses: no name lookup is ever made)
        with pytest.raises(iso.RealDeviceAccessBlocked):
            socket.create_connection((host, 80), timeout=0.2)


def test_a_loopback_connection_to_the_tests_own_listener_still_works():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname(), timeout=2)
    client.close()
    listener.close()
    assert not iso.ATTEMPTS


def test_installing_and_removing_the_guard_balances_exactly():
    before = (subprocess.Popen.__init__, socket.socket.connect, os.system)
    iso.install_device_guard()
    iso.install_device_guard()
    iso.uninstall_device_guard()
    assert (subprocess.Popen.__init__, socket.socket.connect, os.system) == before          # still the session's guard, unchanged
    iso.uninstall_device_guard()
    assert iso.guard_installed()                                                           # the session's own guard is still there


# ===================================================================== the private data folder
def test_isolation_gives_a_private_folder_and_restores_the_environment_exactly():
    before = dict(os.environ)
    with iso.Isolation(prefix="mko-t-priv-") as inner:
        assert os.environ["LOCALAPPDATA"] == str(inner.root) and iso.is_inside(paths.data_dir(), inner.root)
        assert paths.data_dir() == inner.data_dir and not iso.is_inside(paths.data_dir(), iso.REAL_DATA_DIR)
        root = inner.root
    assert dict(os.environ) == before and not root.exists()


def test_the_environment_is_restored_when_the_block_fails_and_stopping_twice_is_harmless():
    before = dict(os.environ)
    try:
        with iso.Isolation(prefix="mko-t-fail-") as inner:
            root = inner.root
            raise RuntimeError("the test body failed")
    except RuntimeError:
        pass
    assert dict(os.environ) == before and not root.exists()
    assert inner.stop() is False


def test_a_missing_localappdata_is_restored_as_missing(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with iso.Isolation(prefix="mko-t-none-"):
        assert "LOCALAPPDATA" in os.environ
    assert "LOCALAPPDATA" not in os.environ


def test_profiles_settings_backups_and_logs_stay_inside_the_private_folder():
    from PySide6.QtCore import QSettings
    from app.mapping.profile import Binding, Profile, ProfileStore
    real_before = real_tree()
    with iso.Isolation(prefix="mko-t-ops-") as inner:
        store = ProfileStore(paths.data_dir() / "profiles")
        profile = Profile("p", bindings=[Binding("F", 0.5, 0.5, "tap", id="a"), Binding("F", 0.2, 0.2, "tap", id="b")])   # a duplicate: converts, with a backup
        store.save(Profile("p", bindings=[Binding("F", 0.5, 0.5)]))
        store.save(profile)
        store.save_checkpoint(profile)
        settings = QSettings(str(paths.data_dir() / "settings.ini"), QSettings.Format.IniFormat)
        settings.setValue("profile", "p")
        settings.sync()
        (paths.log_dir() / "app.log").write_text("x", encoding="utf-8")
        written = [p for p in inner.root.rglob("*") if p.is_file() and p.name != iso.MARKER_FILE]
        assert {"p.json", "settings.ini", "app.log"} <= {p.name for p in written}
        assert all(iso.is_inside(p, inner.root) for p in written)
    assert real_tree() == real_before                                                       # the real data folder did not change


def test_the_tracker_is_neutralised_only_when_asked_and_restored_afterwards():
    from app.device import tracker
    original = tracker.DeviceTracker.start
    with iso.Isolation(prefix="mko-t-trk-"):
        assert tracker.DeviceTracker.start is original                                      # the unit tests that test the tracker keep it
    inner = iso.Isolation(prefix="mko-t-trk2-").start(neutralize_tracker=True)
    try:
        assert tracker.DeviceTracker.start is not original
        t = tracker.DeviceTracker(object())
        t.start()
        assert not t.isRunning()
    finally:
        inner.stop()
    assert tracker.DeviceTracker.start is original


# ===================================================================== clean-up never removes anything else
def _junction(link: Path, target: Path) -> bool:
    if os.name != "nt":
        link.symlink_to(target, target_is_directory=True)
        return True
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    return done.returncode == 0


def make_root(name: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix=name))
    (root / iso.MARKER_FILE).write_text("x", encoding="utf-8")
    return root


def test_cleanup_removes_exactly_one_private_folder_and_nothing_next_to_it():
    mine, neighbour = make_root("mko-t-clean-"), make_root("mko-t-neighbour-")
    (mine / "data.txt").write_text("x", encoding="utf-8")
    try:
        assert iso.remove_private_root(mine, "mko-t-clean-") is True
        assert not mine.exists() and neighbour.exists()
    finally:
        iso.remove_private_root(neighbour, "mko-t-neighbour-")


def test_cleanup_refuses_everything_that_is_not_a_private_test_folder(tmp_path):
    decoy_no_marker = Path(tempfile.mkdtemp(prefix="mko-t-nomarker-"))
    wrong_prefix = make_root("other-")
    outside_temp = tmp_path / "mko-t-outside-"
    outside_temp.mkdir()
    (outside_temp / iso.MARKER_FILE).write_text("x", encoding="utf-8")
    real_like = make_root("mko-t-real-")
    try:
        (decoy_no_marker / "keep.txt").write_text("x", encoding="utf-8")
        assert iso.remove_private_root(decoy_no_marker, "mko-t-nomarker-") is False and (decoy_no_marker / "keep.txt").exists()
        assert iso.remove_private_root(wrong_prefix, "mko-t-") is False and wrong_prefix.exists()
        assert iso.remove_private_root(outside_temp, "mko-t-outside-") is False and outside_temp.exists()
        assert iso.remove_private_root(real_like, "mko-t-real-", real_data_dir=real_like) is False and real_like.exists()
        assert iso.remove_private_root(real_like, "mko-t-real-", real_data_dir=real_like.parent) is False and real_like.exists()
        # the dangerous targets are only ASKED about (a pure question): nothing in a test ever tries to delete them
        for dangerous in (Path(tempfile.gettempdir()), Path("/"), Path(paths.data_dir()), iso.REAL_DATA_DIR, iso.REAL_DATA_DIR.parent, ROOT, Path.home()):
            assert iso.is_removable_private_root(dangerous, "") is False, dangerous
    finally:
        for p in (decoy_no_marker, wrong_prefix, real_like):
            iso.shutil.rmtree(p, ignore_errors=True)


def test_cleanup_refuses_a_link_that_points_at_a_folder_and_leaves_that_folder_alone():
    target = make_root("mko-t-target-")
    (target / "precious.txt").write_text("keep", encoding="utf-8")
    link = Path(tempfile.gettempdir()) / "mko-t-link-for-test"
    if link.exists() or link.is_symlink():
        iso.shutil.rmtree(link, ignore_errors=True)
    if not _junction(link, target):
        pytest.skip("this account cannot create a directory link")
    try:
        assert iso.remove_private_root(link, "mko-t-link-") is False
        assert (target / "precious.txt").read_text(encoding="utf-8-sig") == "keep"
    finally:
        try:
            os.rmdir(link)                                                              # removes the link only
        except OSError:
            pass
        iso.remove_private_root(target, "mko-t-target-")


def test_a_link_inside_the_private_folder_is_removed_as_a_link_and_never_followed():
    outside = Path(tempfile.mkdtemp(prefix="mko-t-outside-data-"))
    (outside / "precious.txt").write_text("keep", encoding="utf-8")
    inner = iso.Isolation(prefix="mko-t-linkin-").start()
    link = inner.root / "points-outside"
    if not _junction(link, outside):
        inner.stop()
        iso.shutil.rmtree(outside, ignore_errors=True)
        pytest.skip("this account cannot create a directory link")
    root = inner.root
    inner.stop()
    try:
        assert not root.exists()
        assert (outside / "precious.txt").read_text(encoding="utf-8-sig") == "keep"
    finally:
        iso.shutil.rmtree(outside, ignore_errors=True)


# ===================================================================== the simulated phone is the only phone
class _Settings:
    def fileName(self):
        return str(iso.active().data_dir / "settings.ini")


class _Video:
    def __init__(self):
        self.frames = []

    def set_frame(self, frame, stamp):
        self.frames.append(frame)


class StubWindow:
    """Enough of MainWindow for the helper: a settings file, a video, a connect hook that must never be used."""

    def __init__(self, devices=()):
        self.settings = _Settings()
        self.video = _Video()
        self.worker = None
        self.tracker = None
        self.devices = list(devices)
        self.toggled = 0
        self.connected = []

    def _set_connected(self, value):
        self.connected.append(value)

    def _toggle_connection(self):
        self.toggled += 1
        raise AssertionError("a test tried to connect to a real phone")


def pump(_seconds):
    pass


def test_a_normal_gui_test_gets_the_simulated_phone():
    import fake_phone
    window = StubWindow()
    assert fake_phone.connect_window(window, pump) == "fake"
    assert isinstance(window.worker, fake_phone.FakeWorker) and isinstance(window.adb, fake_phone.SimulatedAdb)
    assert window.connected == [True] and window.video.frames and window.toggled == 0


def test_a_phone_that_is_plugged_in_cannot_override_the_simulation():
    import fake_phone
    from app.device.adb import DeviceInfo
    window = StubWindow(devices=[DeviceInfo("REAL123", "device", "I2302")])               # a usable physical device is listed
    assert fake_phone.connect_window(window, pump, wait_for_phone=30) == "fake"
    assert window.toggled == 0 and isinstance(window.worker, fake_phone.FakeWorker)


@pytest.mark.parametrize("name,value", [("MKO_ALLOW_REAL_PHONE", "1"), ("MKO_ALLOW_REAL_PHONE", "true"), ("MKO_REAL_PHONE", "1"),
                                        ("MKO_LIVE", "yes"), ("ANDROID_SERIAL", "REAL123"), ("ADB_VENDOR_KEYS", "x")])
def test_inherited_configuration_cannot_switch_a_test_to_a_real_phone(monkeypatch, name, value):
    import fake_phone
    monkeypatch.setenv(name, value)
    window = StubWindow()
    assert fake_phone.connect_window(window, pump) == "fake" and window.toggled == 0 and not iso.live_allowed()


def test_no_source_reads_an_environment_variable_to_enable_a_real_phone():
    offenders = []
    for path in [*TOOLS.glob("*.py"), *APP.rglob("*.py")]:
        text = path.read_text(encoding="utf-8-sig")
        if re.search(r"MKO_ALLOW_REAL_PHONE|MKO_REAL|MKO_LIVE", text):
            offenders.append(path.name)
    assert offenders == [], offenders
    for module in ("fake_phone", "live_device", "run_isolated", "device_isolation"):
        tree = ast.parse((TOOLS / f"{module}.py").read_text(encoding="utf-8-sig"))
        reads = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("MKO_")]
        assert reads == [], module
    env_reads = [m for m in re.findall(r"environ(?:\.get)?\(?\[?\"([A-Z_]+)\"", (TOOLS / "device_isolation.py").read_text(encoding="utf-8-sig"))]
    assert set(env_reads) <= {"LOCALAPPDATA", "ANDROID_ADB_SERVER_PORT"}                    # the only variables it looks at


def test_a_failed_simulation_stops_the_test_and_tries_nothing_else(monkeypatch):
    import fake_phone
    import live_device
    from app.device.adb import Adb

    def broken(window, size=fake_phone.FAKE_SIZE):
        raise RuntimeError("the simulation could not be set up")
    spies = []
    monkeypatch.setattr(fake_phone, "attach_fake_phone", broken)
    monkeypatch.setattr(live_device, "connect_real_window", lambda *a, **k: spies.append("real window"))
    monkeypatch.setattr(Adb, "run", lambda *a, **k: spies.append("adb"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spies.append("process"))
    window = StubWindow()
    with pytest.raises(RuntimeError, match="could not be set up"):
        fake_phone.connect_window(window, pump)
    assert spies == [] and window.toggled == 0 and not iso.ATTEMPTS


def test_the_simulation_refuses_to_start_when_the_process_is_not_isolated(monkeypatch):
    import fake_phone
    monkeypatch.setattr(iso, "_ACTIVE", None)
    with pytest.raises(RuntimeError, match="Not isolated"):
        fake_phone.attach_fake_phone(StubWindow())


def test_the_simulation_refuses_a_window_whose_settings_are_outside_the_private_folder():
    import fake_phone

    class Leaky(StubWindow):
        def __init__(self):
            super().__init__()
            self.settings = type("S", (), {"fileName": staticmethod(lambda: str(iso.REAL_DATA_DIR / "settings.ini"))})()
    with pytest.raises(RuntimeError, match="outside the private test folder"):
        fake_phone.attach_fake_phone(Leaky())


def test_the_simulation_will_not_replace_a_real_worker_or_run_beside_a_listening_tracker():
    import fake_phone
    window = StubWindow()
    window.worker = object()
    with pytest.raises(RuntimeError, match="real worker"):
        fake_phone.attach_fake_phone(window)
    window = StubWindow()
    window.tracker = type("T", (), {"isRunning": staticmethod(lambda: True)})()
    with pytest.raises(RuntimeError, match="tracker is running"):
        fake_phone.attach_fake_phone(window)


def test_the_simulated_adb_refuses_everything_and_starts_nothing():
    import fake_phone
    from app.device.adb import AdbError
    adb = fake_phone.SimulatedAdb()
    for call in (lambda: adb.run("devices"), adb.devices, adb.version, adb.start_server, lambda: adb.push("S", "a", "/b"),
                 lambda: adb.forward("S", "x"), lambda: adb.getprop("S", "p"), lambda: adb.device_details("S"), lambda: adb.screencap("S"),
                 lambda: adb.popen_shell("S", "ls"), lambda: adb.forward_remove("S", 1)):
        with pytest.raises(AdbError):
            call()
    assert not iso.ATTEMPTS                                                                # refused before any command was even built


def test_the_fake_phone_swallows_touches_and_keys():
    import fake_phone
    control = fake_phone.FakeWorker().control
    assert control.send(b"x") is True and control.messages == [b"x"] and control.key(4) is None


# ===================================================================== a fresh process isolates itself on import
CHILD = """
import json, os, sys
sys.path.insert(0, {tools!r}); sys.path.insert(0, {root!r})
import subprocess, socket
import fake_phone, device_isolation as iso
from app import paths
root = iso.active().root
print(json.dumps({{"local": os.environ["LOCALAPPDATA"], "root": str(root), "inside": iso.is_inside(paths.data_dir(), root),
                  "popen": subprocess.Popen.__init__.__name__, "connect": socket.socket.connect.__name__,
                  "guard": iso.guard_installed(), "tracker_neutral": __import__("app.device.tracker", fromlist=["x"]).DeviceTracker.start.__name__}}))
"""


def test_importing_the_simulated_phone_isolates_a_fresh_process_and_cleans_up_when_it_exits(tmp_path):
    base = tmp_path / "pretend-local-appdata"
    data = base / "MobileKeyOne"
    data.mkdir(parents=True)
    (data / "profile.json").write_text("{}", encoding="utf-8")
    before = iso.fingerprint_tree(base)
    env = dict(os.environ, LOCALAPPDATA=str(base))
    done = subprocess.run([sys.executable, "-c", CHILD.format(tools=str(TOOLS), root=str(ROOT))], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr[-800:]
    info = json.loads(done.stdout.strip().splitlines()[-1])
    assert info["local"] != str(base) and info["inside"] and info["guard"]
    assert info["popen"] == "guarded_init" and info["connect"] == "guarded_connect" and info["tracker_neutral"] == "<lambda>"
    assert not Path(info["root"]).exists()                                                 # removed when the process ended
    assert iso.fingerprint_tree(base) == before                                            # the pretend real folder was not touched


# ===================================================================== the isolated runner
def run_runner(args, base, tools_dir=None):
    command = [sys.executable, str(TOOLS / "run_isolated.py")]
    if tools_dir is not None:
        command += ["--tools-dir", str(tools_dir)]
    done = subprocess.run(command + args, capture_output=True, text=True, env=dict(os.environ, LOCALAPPDATA=str(base), PYTHONIOENCODING="utf-8"),
                          timeout=180)
    return done


def private_roots():
    return {p.name for p in Path(tempfile.gettempdir()).glob("mko-isolated-*")}


@pytest.fixture
def pretend_user_data(tmp_path):
    base = tmp_path / "pretend-local-appdata"
    (base / "MobileKeyOne" / "profiles").mkdir(parents=True)
    (base / "MobileKeyOne" / "profiles" / "mine.json").write_text('{"keep": true}', encoding="utf-8")
    (base / "MobileKeyOne" / "settings.ini").write_text("[General]\nprofile=mine\n", encoding="utf-8")
    return base


@pytest.fixture
def dummy_tools(tmp_path, fake_programs):
    folder = tmp_path / "dummy-tools"
    folder.mkdir()
    header = f"import sys\nsys.path.insert(0, {str(ROOT)!r})\nsys.path.insert(0, {str(TOOLS)!r})\nimport device_isolation\nfrom app import paths\n"
    (folder / "ok.py").write_text(header + "(paths.data_dir() / 'profiles').mkdir(parents=True, exist_ok=True)\n"
                                  "(paths.data_dir() / 'profiles' / 'x.json').write_text('{}')\nprint('1/1 checks passed')\n", encoding="utf-8")
    (folder / "boom.py").write_text(header + "(paths.data_dir() / 'scratch.txt').write_text('x')\nraise RuntimeError('the tool crashed')\n", encoding="utf-8")
    (folder / "fails.py").write_text(header + "raise SystemExit(1)\n", encoding="utf-8")
    (folder / "swallows_adb.py").write_text(header + f"import subprocess\ntry:\n    subprocess.run([{fake_programs['adb']!r}, 'devices'])\nexcept Exception:\n    pass\nprint('1/1 checks passed')\n", encoding="utf-8")
    (folder / "reaches_for_adb.py").write_text(header + f"import subprocess\nsubprocess.run([{fake_programs['adb']!r}, 'devices'])\n", encoding="utf-8")
    return folder


def test_the_runner_runs_a_tool_inside_a_private_folder_and_removes_it(pretend_user_data, dummy_tools):
    before, roots = iso.fingerprint_tree(pretend_user_data), private_roots()
    done = run_runner(["ok"], pretend_user_data, dummy_tools)
    assert done.returncode == 0 and "1/1 checks passed" in done.stdout
    assert iso.fingerprint_tree(pretend_user_data) == before and private_roots() == roots


def test_a_crashing_tool_fails_cleanly_without_touching_the_users_data_or_leaving_its_folder(pretend_user_data, dummy_tools):
    before, roots = iso.fingerprint_tree(pretend_user_data), private_roots()
    done = run_runner(["boom"], pretend_user_data, dummy_tools)
    assert done.returncode == 1 and "the tool crashed" in done.stderr
    assert iso.fingerprint_tree(pretend_user_data) == before and private_roots() == roots
    done = run_runner(["fails"], pretend_user_data, dummy_tools)
    assert done.returncode == 1 and iso.fingerprint_tree(pretend_user_data) == before and private_roots() == roots


def test_a_tool_that_reaches_for_adb_is_stopped_by_the_runner(pretend_user_data, dummy_tools, fake_programs):
    done = run_runner(["reaches_for_adb"], pretend_user_data, dummy_tools)
    assert done.returncode == 1 and "Blocked" in done.stderr and not ran(fake_programs)


def test_a_tool_that_tried_to_reach_a_device_and_hid_the_refusal_still_fails(pretend_user_data, dummy_tools, fake_programs):
    done = run_runner(["swallows_adb"], pretend_user_data, dummy_tools)
    assert done.returncode == 4 and "tried to reach a real device" in done.stderr and not ran(fake_programs)


def test_the_runner_reports_a_leak_into_the_real_data_folder_as_a_failure(pretend_user_data, tmp_path):
    folder = tmp_path / "leaky-tools"
    folder.mkdir()
    target = pretend_user_data / "MobileKeyOne" / "profiles" / "leaked.json"
    (folder / "leaky.py").write_text(f"import sys\nsys.path.insert(0, {str(TOOLS)!r})\nimport device_isolation\n"
                                     f"open({str(target)!r}, 'w').write('{{}}')\n", encoding="utf-8")
    done = run_runner(["leaky"], pretend_user_data, folder)
    assert done.returncode == 3 and "real data folder changed" in done.stderr


def test_keep_leaves_the_private_folder_for_inspection_and_nothing_else(pretend_user_data, dummy_tools):
    before, roots = iso.fingerprint_tree(pretend_user_data), private_roots()
    done = run_runner(["ok", "keep"], pretend_user_data, dummy_tools)
    kept = private_roots() - roots
    try:
        assert done.returncode == 0 and len(kept) == 1
        assert iso.fingerprint_tree(pretend_user_data) == before
    finally:
        for name in kept:
            iso.remove_private_root(Path(tempfile.gettempdir()) / name, "mko-isolated-")


def test_the_runner_refuses_to_run_a_live_device_tool_and_never_imports_it(pretend_user_data):
    for tool in ("control_test", "pubg_stop", "stream_test"):
        done = run_runner([tool], pretend_user_data)
        assert done.returncode == 2 and "real phone" in done.stderr


def test_the_runner_lists_every_class_of_tool(pretend_user_data):
    done = run_runner(["--list"], pretend_user_data)
    assert done.returncode == 0 and all(label in done.stdout for label in ("SIMULATED_TESTS", "LIVE_DEVICE", "LOCAL_ONLY"))


# ===================================================================== the way to a real phone is separate and explicit
class _Terminal:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


def gate(argv, monkeypatch, tty=False, typed=""):
    import live_device
    monkeypatch.setattr(sys, "argv", ["tool.py", *argv])
    monkeypatch.setattr(sys, "stdin", _Terminal(tty))
    monkeypatch.setattr(live_device, "_has_console", lambda: tty)
    monkeypatch.setattr("builtins.input", lambda prompt="": typed)
    monkeypatch.setattr(iso, "_live_allowed", False)
    return live_device


def test_a_live_tool_refuses_without_the_flag(monkeypatch):
    live = gate([], monkeypatch, tty=True, typed="USE MY PHONE")
    with pytest.raises(SystemExit) as stopped:
        live.require_live_opt_in("tool.py")
    assert stopped.value.code == 2 and not iso.live_allowed()


def test_a_live_tool_refuses_when_it_is_not_in_an_interactive_terminal(monkeypatch):
    live = gate(["--really-use-real-phone"], monkeypatch, tty=False, typed="USE MY PHONE")
    with pytest.raises(SystemExit):
        live.require_live_opt_in("tool.py")
    assert not iso.live_allowed()


def test_a_live_tool_refuses_without_the_typed_confirmation(monkeypatch):
    live = gate(["--really-use-real-phone"], monkeypatch, tty=True, typed="yes")
    with pytest.raises(SystemExit):
        live.require_live_opt_in("tool.py")
    assert not iso.live_allowed()


def test_the_opt_in_needs_all_three_things_and_nothing_in_the_environment(monkeypatch):
    monkeypatch.setenv("MKO_ALLOW_REAL_PHONE", "1")
    live = gate(["--really-use-real-phone"], monkeypatch, tty=True, typed="USE MY PHONE")
    live.require_live_opt_in("tool.py")
    assert iso.live_allowed() and "--really-use-real-phone" not in sys.argv                # (the flag is consumed)
    monkeypatch.setattr(iso, "_live_allowed", False)


def test_the_live_connect_helper_refuses_until_the_opt_in_was_given(monkeypatch):
    import live_device
    monkeypatch.setattr(iso, "_live_allowed", False)
    with pytest.raises(RuntimeError, match="needs live_device.require_live_opt_in"):
        live_device.connect_real_window(StubWindow(), pump)


def test_the_gate_script_stops_a_process_without_the_flag(tmp_path):
    script = tmp_path / "gate_probe.py"
    script.write_text(f"import sys\nsys.path.insert(0, {str(TOOLS)!r})\nimport live_device\nlive_device.require_live_opt_in(__file__)\nprint('STARTED')\n",
                      encoding="utf-8")
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert done.returncode == 2 and "STARTED" not in done.stdout and "REAL phone" in done.stderr
    done = subprocess.run([sys.executable, str(script), "--really-use-real-phone"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert done.returncode == 2 and "STARTED" not in done.stdout


# ===================================================================== the tools are classified and behave as classified
def _module_imports(tree):
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append((node.module or "", node.lineno))
    return names


def test_every_tool_is_classified_and_nothing_stale_is_listed():
    on_disk = {p.stem for p in TOOLS.glob("*.py")}
    assert on_disk == set(tool_registry.ALL), (sorted(on_disk - set(tool_registry.ALL)), sorted(set(tool_registry.ALL) - on_disk))
    assert len(tool_registry.ALL) == len(set(tool_registry.ALL))


def test_every_simulated_tool_uses_the_simulated_phone_and_nothing_real():
    forbidden = ("live_device", "connect_real_window", "adb_path", "vendor", "MKO_ALLOW_REAL_PHONE", "allow_live_devices", "--really-use-real-phone")
    for name in (*tool_registry.SIMULATED_TESTS, *tool_registry.SIMULATED_UTILITIES):
        text = (TOOLS / f"{name}.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(text)
        modules = [m for m, _ in _module_imports(tree)]
        assert "fake_phone" in modules, f"{name} does not use the simulated phone"
        assert not [w for w in forbidden if w in text], (name, [w for w in forbidden if w in text])
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not calls & {"Adb", "MirrorWorker", "ScrcpySession"}, name
        for node in ast.walk(tree):                                                         # no adb / scrcpy string handed to a process call
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip().lower() in ("adb", "adb.exe", "scrcpy"):
                pytest.fail(f"{name} mentions the program {node.value!r}")
        assert "connect_window" not in text or "from fake_phone import" in text


def test_every_live_device_tool_asks_for_the_explicit_opt_in_before_it_touches_anything():
    for name in tool_registry.LIVE_DEVICE:
        text = (TOOLS / f"{name}.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(text)
        gate_lines = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr == "require_live_opt_in"]
        assert len(gate_lines) == 1, name
        early = [(m, line) for m, line in _module_imports(tree) if line < gate_lines[0]]
        assert not [m for m, _ in early if m.split(".")[0] in ("app", "fake_phone", "device_isolation", "PySide6")], (name, early)
        assert "fake_phone" not in text and "device_isolation" not in text, name            # (a live tool must not get isolated by accident)
        top_level = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom, ast.Expr, ast.Assign, ast.FunctionDef, ast.ClassDef, ast.If))]
        assert not [n for n in top_level if n.lineno < gate_lines[0]], name


def test_the_local_only_and_user_data_tools_do_not_reach_a_phone():
    for name in (*tool_registry.LOCAL_ONLY, *tool_registry.USER_DATA_UTILITIES):
        text = (TOOLS / f"{name}.py").read_text(encoding="utf-8-sig")
        assert not re.search(r"\bAdb\b|MirrorWorker|ScrcpySession|DeviceTracker|subprocess|MainWindow|connect_window", text), name


def test_the_application_has_no_way_to_start_a_process_other_than_the_adb_wrapper():
    """The guard sees Python-level process creation; a native one (QProcess, ctypes CreateProcess) it could not see. Keep the app free of them."""
    allowed_subprocess = {"adb.py"}
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        assert not re.search(r"QProcess|CreateProcess|ShellExecute|os\.system|os\.spawn|os\.exec|os\.startfile|shell\s*=\s*True|asyncio\.create_subprocess", text), path.name
        if re.search(r"^\s*(import|from)\s+subprocess", text, re.M):
            assert path.name in allowed_subprocess, path.name
    for path in APP.rglob("*.py"):                                                          # sockets only where the device protocol lives
        if re.search(r"socket\.(create_connection|socket)\(", path.read_text(encoding="utf-8-sig")):
            assert path.name in {"tracker.py", "session.py"}, path.name


def test_no_test_or_tool_still_enables_a_real_phone_through_the_environment():
    this = Path(__file__).resolve()
    for path in [*TOOLS.glob("*.py"), *(ROOT / "tests").glob("*.py")]:
        if path.resolve() != this:
            assert "MKO_ALLOW_REAL_PHONE" not in path.read_text(encoding="utf-8-sig"), path.name


def test_the_null_device_is_not_an_interactive_terminal(monkeypatch):
    """On Windows os.isatty(NUL) is True; the gate must not be fooled by redirected input."""
    import live_device
    if os.name != "nt":
        pytest.skip("Windows console check")
    assert live_device._has_console() is False                  # pytest's own stdin is not a console either


def test_an_isolation_releases_the_guard_it_took():
    depth = iso._guard_depth
    with iso.Isolation(prefix="mko-t-depth-"):
        assert iso._guard_depth == depth + 1
    assert iso._guard_depth == depth
    inner = iso.Isolation(prefix="mko-t-depth2-").start()
    inner.stop()
    inner.stop()
    assert iso._guard_depth == depth


CASE_TESTS = '''
import subprocess, sys
import pytest
from pathlib import Path
FAKE = {fake!r}

def test_leaves_an_attempt_behind_and_swallows_the_error():
    try:
        subprocess.run([FAKE, "devices"])
    except Exception:
        pass                                      # the code under test hid the exception: the attempt is still recorded

@pytest.mark.expects_device_block
def test_declares_it_provokes_a_block():
    with pytest.raises(Exception):
        subprocess.run([FAKE, "devices"])

def test_ordinary_test_passes():
    assert 1 + 1 == 2
'''


def test_the_unit_suite_fails_a_test_that_leaves_a_blocked_attempt_even_if_it_swallowed_the_error(tmp_path, fake_programs):
    """Runs a tiny pytest session with a copy of this suite's conftest: only the test that tried (and hid it) fails."""
    case = tmp_path / "case"
    (case / "tests").mkdir(parents=True)
    (case / "tools").mkdir()
    (case / "tests" / "conftest.py").write_text((ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8")
    (case / "tools" / "device_isolation.py").write_text((TOOLS / "device_isolation.py").read_text(encoding="utf-8"), encoding="utf-8")
    (case / "tests" / "test_case.py").write_text(CASE_TESTS.format(fake=fake_programs["adb"]), encoding="utf-8")
    done = subprocess.run([sys.executable, "-m", "pytest", str(case / "tests"), "-q", "-p", "no:cacheprovider", "--no-header", "-rA"],
                          capture_output=True, text=True, timeout=120, cwd=case)
    out = done.stdout
    assert done.returncode == 1 and not ran(fake_programs)
    flat = out.replace("\\", "/")
    assert "ERROR tests/test_case.py::test_leaves_an_attempt_behind_and_swallows_the_error" in flat      # (a failure in the fixture's teardown is an ERROR)
    assert "tried to reach a real device" in out
    assert "PASSED tests/test_case.py::test_declares_it_provokes_a_block" in flat and "PASSED tests/test_case.py::test_ordinary_test_passes" in flat
    assert "3 passed, 1 error" in out


# ===================================================================== a process that ends itself abruptly cannot clean up: the next start does
def test_the_liveness_check_never_touches_the_process_it_asks_about():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert iso.pid_alive(child.pid) is True and iso.pid_alive(os.getpid()) is True
        assert child.poll() is None                                                           # still running: asking did not terminate it
    finally:
        child.kill()
        child.wait()
    assert iso.pid_alive(child.pid) is False and iso.pid_alive(0) is False and iso.pid_alive(-5) is False


def test_stale_folders_of_dead_processes_are_swept_and_everything_else_is_left_alone():
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    stale = make_root("mko-isolated-stale-")
    (stale / iso.MARKER_FILE).write_text(f"x\n{dead.pid}\n", encoding="utf-8")
    running = make_root("mko-isolated-running-")
    (running / iso.MARKER_FILE).write_text(f"x\n{os.getpid()}\n", encoding="utf-8")
    no_pid = make_root("mko-isolated-nopid-")                                                  # (marker without a process id)
    unmarked = Path(tempfile.mkdtemp(prefix="mko-isolated-unmarked-"))
    (unmarked / "keep.txt").write_text("keep", encoding="utf-8")
    other = Path(tempfile.mkdtemp(prefix="someone-elses-"))
    try:
        removed = iso.sweep_stale_roots()
        assert stale.name in removed and not stale.exists()
        assert running.exists() and no_pid.exists() and (unmarked / "keep.txt").exists() and other.exists()
        assert running.name not in removed and no_pid.name not in removed
    finally:
        for p in (stale, running, no_pid, unmarked, other):
            iso.shutil.rmtree(p, ignore_errors=True)


def test_a_tool_that_ends_itself_with_terminateprocess_leaves_a_folder_that_the_next_start_removes():
    code = ("import sys, os; sys.path.insert(0, %r); import device_isolation as iso\n"
            "i = iso.Isolation(prefix='mko-isolated-').start(); print(i.root, flush=True); os._exit(0)\n") % str(TOOLS)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    left = Path(done.stdout.strip().splitlines()[-1])
    assert done.returncode == 0 and left.exists()                                              # it could not clean up after itself
    with iso.Isolation(prefix="mko-t-sweeper-"):                                               # ...the next isolation start does
        pass
    assert not left.exists()


def test_a_single_run_that_ends_abruptly_leaves_its_folder_for_the_sweep_to_remove(pretend_user_data, tmp_path):
    folder = tmp_path / "abrupt-tools"
    folder.mkdir()
    (folder / "abrupt.py").write_text(f"import sys, os\nsys.path.insert(0, {str(TOOLS)!r})\nprint('1/1 checks passed', flush=True)\nos._exit(0)\n",
                                      encoding="utf-8")
    roots = private_roots()
    done = run_runner(["abrupt"], pretend_user_data, folder)
    assert done.returncode == 0
    for leftover in private_roots() - roots:                                                   # (a direct runner call cannot clean up either: the sweep does)
        assert (Path(tempfile.gettempdir()) / leftover).exists()
        iso.remove_private_root(Path(tempfile.gettempdir()) / leftover, "mko-isolated-")
