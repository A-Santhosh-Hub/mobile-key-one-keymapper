"""Test isolation that FAILS CLOSED: a test process cannot reach a physical Android phone, and cannot touch the user's data.

Two independent mechanisms, because the code has more than one way to reach a device:

  1. DEVICE GUARD (install_device_guard). Process creation and sockets are intercepted where they really happen:
       * subprocess.Popen (everything in `subprocess` goes through it, also asyncio's), os.system, os.spawn*, os.exec*, os.startfile:
         a command whose program is `adb` or `scrcpy` (or that mentions scrcpy's server / app_process) is refused BEFORE it is started;
       * socket.connect / connect_ex: a connection to the ADB server port (5037, or ANDROID_ADB_SERVER_PORT) or to any non-loopback
         host is refused. This matters: app/device/tracker.py talks to the ADB server over a raw socket and spawns no process, so a
         process-only guard would not stop it from listing the connected phone.
     A refusal raises RealDeviceAccessBlocked and is recorded in ATTEMPTS (the pytest suite fails any test that leaves one behind).
  2. PRIVATE DATA FOLDER (Isolation). LOCALAPPDATA points at a fresh temporary folder for the life of the test, so every path the app
     derives from it (profiles, backups, settings.ini, logs, Qt caches) is private. The real folder is never created, written or
     deleted; the environment is put back exactly as it was; and the private folder is removed only if it passes remove_private_root's
     checks (inside the temp folder, our prefix, our marker file, not a link, never the real data folder).

There is deliberately NO environment variable or setting that switches any of this off. The only way to lift the guard is
allow_live_devices(), which tools/live_device.py calls after an explicit command-line flag AND a typed confirmation in an interactive
terminal; a test runner never does that.

What this guard cannot see: a native call that creates a process without going through Python (ctypes CreateProcess, QProcess). The
application uses none; tests/test_device_isolation.py scans the sources to keep it that way."""
from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

MARKER_FILE = ".mko-isolated-root"
DEFAULT_PREFIX = "mko-isolated-"
ADB_SERVER_PORT = 5037
DEVICE_PROGRAMS = {"adb", "scrcpy"}
DEVICE_MARKERS = ("scrcpy-server", "app_process")
SHELL_PROGRAMS = {"cmd", "powershell", "pwsh", "sh", "bash", "wsl", "start", "env", "call"}
LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


class RealDeviceAccessBlocked(RuntimeError):
    """A test tried to reach a physical device (adb, scrcpy, the ADB server, the network). It was stopped before anything happened."""


class UnsafeCleanup(RuntimeError):
    pass


ATTEMPTS: list[str] = []                    # every blocked attempt, newest last
_state_lock = threading.RLock()
_guard_depth = 0
_originals: dict = {}
_live_allowed = False
REAL_DATA_DIR: Path | None = None           # the user's real data folder, captured (never created) when isolation first starts


# ===================================================================== the device guard
def live_allowed() -> bool:
    return _live_allowed


def allow_live_devices() -> None:
    """Lift the guard for THIS process. Only tools/live_device.py calls it, after a flag and a typed confirmation."""
    global _live_allowed
    _live_allowed = True


def _normalise(token) -> str:
    name = os.path.basename(str(token).strip().strip("\"'")).lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _tokens(args) -> list[str]:
    if isinstance(args, (str, bytes, os.PathLike)):
        return re.findall(r"\"[^\"]*\"|'[^']*'|\S+", os.fsdecode(args))
    return [os.fsdecode(a) if isinstance(a, (str, bytes, os.PathLike)) else str(a) for a in args]


def classify_command(args, shell: bool = False) -> str:
    """Why this command would reach a device ('' if it would not). Looks at the program, and where a shell may hide one, at every word."""
    tokens = _tokens(args)
    if not tokens:
        return ""
    first = _normalise(tokens[0])
    if first in DEVICE_PROGRAMS:
        return f"the program '{first}'"
    scan_all = shell or isinstance(args, (str, bytes, os.PathLike)) or first in SHELL_PROGRAMS
    if scan_all:                                              # a shell can hide the program inside one quoted word: look at every word
        for word in re.split(r"[\s;&|()<>\"']+", " ".join(tokens)):
            if word and _normalise(word) in DEVICE_PROGRAMS:
                return f"'{_normalise(word)}' inside a shell command"
    for token in tokens:
        lowered = token.lower()
        if any(marker in lowered for marker in DEVICE_MARKERS):
            return "the scrcpy server on a device"
    return ""


def _block(kind: str, detail: str) -> None:
    message = (f"Blocked: a test tried to reach a real device ({kind}: {detail}). Tests must use the simulated phone; "
               "see docs/TESTING.md. Nothing was started.")
    with _state_lock:
        ATTEMPTS.append(f"{kind}: {detail}")
    if not _live_allowed:
        raise RealDeviceAccessBlocked(message)


def _check_command(args, shell: bool = False, kind: str = "process") -> None:
    reason = classify_command(args, shell)
    if reason:
        _block(kind, f"{reason}: {' '.join(_tokens(args))[:160]}")


def _adb_ports() -> set[int]:
    ports = {ADB_SERVER_PORT}
    extra = os.environ.get("ANDROID_ADB_SERVER_PORT", "")
    if extra.isdigit():
        ports.add(int(extra))
    return ports


def _check_address(address) -> None:
    if not (isinstance(address, tuple) and len(address) >= 2):
        return
    host, port = str(address[0]), address[1]
    if isinstance(port, int) and port in _adb_ports():
        _block("socket", f"the ADB server port {host}:{port}")
    if host not in LOOPBACK and not host.startswith("127."):
        _block("socket", f"a non-local address {host}:{port}")


def install_device_guard() -> None:
    """Install the guard (idempotent; reference counted, so nested users are fine)."""
    global _guard_depth
    with _state_lock:
        _guard_depth += 1
        if _guard_depth > 1:
            return
        original_init = subprocess.Popen.__init__
        original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
        _originals["Popen.__init__"] = original_init
        _originals["connect"], _originals["connect_ex"] = original_connect, original_connect_ex

        def guarded_init(self, args, *a, **k):
            executable = k.get("executable") or (a[1] if len(a) > 1 else None)     # (the program can also be named here)
            if executable:
                _check_command([executable])
            _check_command(args, bool(k.get("shell")) or (len(a) >= 8 and bool(a[7])))
            return original_init(self, args, *a, **k)

        def guarded_connect(self, address):
            _check_address(address)
            return original_connect(self, address)

        def guarded_connect_ex(self, address):
            _check_address(address)
            return original_connect_ex(self, address)
        subprocess.Popen.__init__ = guarded_init
        socket.socket.connect, socket.socket.connect_ex = guarded_connect, guarded_connect_ex

        def wrap(name: str, picker):
            original = getattr(os, name, None)
            if original is None:
                return
            _originals[f"os.{name}"] = original

            def wrapper(*a, **k):
                target = picker(a, k)
                if target is not None:
                    _check_command(target, shell=(name == "system"), kind=f"os.{name}")
                return original(*a, **k)
            setattr(os, name, wrapper)
        wrap("system", lambda a, k: a[0] if a else k.get("command"))
        wrap("startfile", lambda a, k: [a[0]] if a else None)
        for name in ("spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"):
            wrap(name, lambda a, k: [a[1]] if len(a) > 1 else None)
        for name in ("execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe"):
            wrap(name, lambda a, k: [a[0]] if a else None)


def uninstall_device_guard() -> None:
    global _guard_depth
    with _state_lock:
        if _guard_depth == 0:
            return
        _guard_depth -= 1
        if _guard_depth:
            return
        subprocess.Popen.__init__ = _originals.pop("Popen.__init__")
        socket.socket.connect, socket.socket.connect_ex = _originals.pop("connect"), _originals.pop("connect_ex")
        for key in [k for k in _originals if k.startswith("os.")]:
            setattr(os, key[3:], _originals.pop(key))


def guard_installed() -> bool:
    return _guard_depth > 0


# ===================================================================== the private data folder
def fingerprint_tree(path: Path | None) -> dict | None:
    """{relative path: (size, mtime_ns)} of a folder, read-only; None if it does not exist. For 'did anything change in there?'."""
    if path is None or not Path(path).exists():
        return None
    out = {}
    for item in sorted(Path(path).rglob("*")):
        try:
            info = item.stat()
        except OSError:
            continue
        out[str(item.relative_to(path))] = (info.st_size if item.is_file() else -1, info.st_mtime_ns)
    return out


def _compute_real_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "MobileKeyOne"                           # (computed only: the folder is never created here)


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or os.path.realpath(path) != os.path.abspath(path)
    except OSError:
        return True


def is_removable_private_root(root: Path, prefix: str = DEFAULT_PREFIX, real_data_dir: Path | None = None) -> bool:
    """Would remove_private_root delete this folder? Pure: it looks and decides, it never deletes. True only if the folder is directly inside the
    system temp folder, has our name prefix and our marker file, is not a link or junction, and is neither the user's data folder nor contains it
    (or is contained by it)."""
    root = Path(root)
    try:
        temp = Path(tempfile.gettempdir()).resolve()
        if root.name == "" or not root.name.startswith(prefix) or not root.is_dir():
            return False
        if root.parent.resolve() != temp or _is_link(root) or not (root / MARKER_FILE).is_file():
            return False
        real = (real_data_dir or REAL_DATA_DIR)
        if real is not None:
            real_resolved = Path(real).resolve()
            if root.resolve() == real_resolved or real_resolved in root.resolve().parents or root.resolve() in real_resolved.parents:
                return False
    except OSError:
        return False
    return True


def _detach_links(root: Path) -> None:
    """Remove every link / junction inside `root` AS A LINK (never following it), so deleting the folder cannot reach what they point at."""
    for dirpath, dirnames, _files in os.walk(root, topdown=True, followlinks=False):
        for name in list(dirnames):
            candidate = Path(dirpath) / name
            if _is_link(candidate):
                dirnames.remove(name)
                try:
                    os.rmdir(candidate)                       # a directory link / junction goes without touching its target
                except OSError:
                    try:
                        os.unlink(candidate)
                    except OSError:
                        pass


def remove_private_root(root: Path, prefix: str = DEFAULT_PREFIX, real_data_dir: Path | None = None) -> bool:
    """Delete a private test folder, and ONLY one (see is_removable_private_root). Returns False and deletes nothing if the folder does not
    qualify. Links inside it are detached first, so what they point at is never followed."""
    root = Path(root)
    if not is_removable_private_root(root, prefix, real_data_dir):
        return False
    _detach_links(root)
    for handler in list(logging.getLogger().handlers):           # a log file inside the folder must be closed before it can go
        name = getattr(handler, "baseFilename", "")
        if name and Path(name).resolve().is_relative_to(root.resolve()):
            logging.getLogger().removeHandler(handler)
            handler.close()
    for attempt in range(8):                                     # (Windows may still hold a file for a moment after a window closed)
        try:
            shutil.rmtree(root)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.15 * (attempt + 1))
    shutil.rmtree(root, ignore_errors=True)
    if root.exists():
        print(f"[isolation] WARNING: the private test folder could not be removed completely: {root} (it holds only test data)", file=sys.stderr)
        return False
    return True


def pid_alive(pid: int) -> bool:
    """Is a process with this id running? (Never os.kill(pid, 0): on Windows that TERMINATES the process.)"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)           # PROCESS_QUERY_LIMITED_INFORMATION: ask only, change nothing
        if not handle:
            return kernel32.GetLastError() == 5                     # access denied: it exists, we may not look at it
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == 259    # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)                                             # (POSIX: signal 0 only checks)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _owner_pid(root: Path) -> int:
    try:
        lines = (Path(root) / MARKER_FILE).read_text(encoding="utf-8").split()
        return int(lines[1]) if len(lines) > 1 else 0
    except (OSError, ValueError):
        return 0


def sweep_stale_roots(prefixes: tuple = (DEFAULT_PREFIX, "mko-pytest-")) -> list[str]:
    """Remove private test folders whose owning process is gone (a tool that ends itself with TerminateProcess, as the stuck-thread test
    does, cannot clean up). Only folders that pass is_removable_private_root and record a DEAD process id are removed; a folder of a running
    process, one without a recorded id, or anything else is left alone. Returns the names removed."""
    removed = []
    temp = Path(tempfile.gettempdir())
    for prefix in prefixes:
        for candidate in sorted(temp.glob(prefix + "*")):
            pid = _owner_pid(candidate)
            if pid and pid != os.getpid() and not pid_alive(pid) and remove_private_root(candidate, prefix):
                removed.append(candidate.name)
    return removed


_ACTIVE: "Isolation | None" = None


class Isolation:
    """A private LOCALAPPDATA for the life of a test (see the module docstring). Use as a context manager or start() / stop()."""

    def __init__(self, prefix: str = DEFAULT_PREFIX) -> None:
        self.prefix = prefix
        self.root: Path | None = None
        self._saved_env: dict[str, str | None] = {}
        self._tracker_original = None
        self._started = False
        self._registered = False

    @property
    def data_dir(self) -> Path:
        return self.root / "MobileKeyOne"

    def start(self, neutralize_tracker: bool = False) -> "Isolation":
        """Create the folder, point LOCALAPPDATA at it and install the device guard. `neutralize_tracker` additionally turns the window's
        DeviceTracker (which listens to the ADB server) into a no-op; GUI tools want it, the unit tests that test the tracker do not."""
        global REAL_DATA_DIR, _ACTIVE
        if self._started:
            raise RuntimeError("this isolation is already started")
        if REAL_DATA_DIR is None:
            REAL_DATA_DIR = _compute_real_data_dir()
        sweep_stale_roots()                                       # (folders that a process which ended itself abruptly could not remove)
        self.root = Path(tempfile.mkdtemp(prefix=self.prefix))
        (self.root / MARKER_FILE).write_text(f"{uuid.uuid4().hex}\n{os.getpid()}\n", encoding="utf-8")
        self._saved_env = {"LOCALAPPDATA": os.environ.get("LOCALAPPDATA")}
        os.environ["LOCALAPPDATA"] = str(self.root)
        install_device_guard()
        if neutralize_tracker:
            try:
                from app.device import tracker
            except ImportError:                                  # (no Qt in this interpreter: nothing to neutralize)
                pass
            else:
                self._tracker_original = tracker.DeviceTracker.start
                tracker.DeviceTracker.start = lambda self: None
        self._started = True
        if _ACTIVE is None:
            _ACTIVE = self
            self._registered = True
        return self

    def stop(self, keep: bool = False) -> bool:
        """Put the environment back exactly as it was and remove the private folder (if it passes the safety checks). Idempotent."""
        global _ACTIVE
        if not self._started:
            return False
        self._started = False
        if self._tracker_original is not None:
            from app.device import tracker
            tracker.DeviceTracker.start = self._tracker_original
            self._tracker_original = None
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        uninstall_device_guard()
        if self._registered:
            _ACTIVE = None
            self._registered = False
        return True if keep else remove_private_root(self.root, self.prefix)

    def __enter__(self) -> "Isolation":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def active() -> Isolation | None:
    return _ACTIVE


def ensure_active(neutralize_tracker: bool = True) -> Isolation:
    """The process-wide isolation, started now if nothing started one yet. Cleaned up (and the environment restored) when the process exits."""
    global _ACTIVE
    if _ACTIVE is None:
        iso = Isolation().start(neutralize_tracker=neutralize_tracker)
        atexit.register(iso.stop)
    return _ACTIVE


def require_active() -> Isolation:
    """For the simulated-phone helpers: refuse to go on unless the process is isolated (fail closed)."""
    if _ACTIVE is None or not guard_installed():
        raise RuntimeError("Not isolated: the device guard and the private data folder must be active before a simulated phone is used. "
                           "Import fake_phone first, or run the tool with tools/run_isolated.py.")
    return _ACTIVE


def is_inside(path, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False
