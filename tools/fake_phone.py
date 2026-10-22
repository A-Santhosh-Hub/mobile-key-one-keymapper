"""A simulated phone for GUI tests: lets the app be tested without hardware, and WITHOUT any way to reach hardware.

Importing this module isolates the process (tools/device_isolation.py): the device guard blocks adb, scrcpy and the ADB server socket, the
window's DeviceTracker does nothing, and the app's data folder is a private temporary one. `connect_window()` then attaches a fake phone
that supplies a synthetic landscape picture and swallows every touch. It has no real-phone branch at all: a connected phone, an
inherited environment variable or a failed simulation can never turn a test into a real-device test. If the simulation cannot be set
up, the test stops with an error. (Real-phone tools live in tools/live_device.py and need an explicit flag and a typed confirmation.)
"""
from __future__ import annotations

import time

import numpy as np

import device_isolation

device_isolation.ensure_active()                 # first: nothing below may run before the guard and the private data folder exist

from app.device.adb import Adb, AdbError  # noqa: E402
from app.video.frame import YuvFrame  # noqa: E402

FAKE_SIZE = (1920, 864)


class SimulatedAdbError(AdbError, device_isolation.RealDeviceAccessBlocked):
    """Raised by the simulated adb: there is no adb in a simulated test (an AdbError, so the app's own error handling copes with it)."""


class SimulatedAdb(Adb):
    """Stands in for window.adb. Every method builds its command line through _cmd, which refuses: nothing can start adb through it."""

    def __init__(self) -> None:
        self.path = "<simulated: there is no adb in a simulated test>"

    def _cmd(self, args, serial):
        raise SimulatedAdbError("adb is not available in a simulated test")


class FakeControl:
    """Stands in for the control channel: records messages, sends nothing anywhere."""

    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.closed = False

    def send(self, data: bytes) -> bool:
        self.messages.append(data)
        return True

    def key(self, keycode: int) -> None:
        pass


class FakeWorker:
    serial = "FAKE-PHONE"

    def __init__(self, size: tuple[int, int] = FAKE_SIZE) -> None:
        self.video_size = size
        self.control = FakeControl()

    def stop(self) -> None:
        pass

    def isRunning(self) -> bool:
        return False

    def wait(self, *_args) -> bool:
        return True

    def take_frame(self):
        return None, 0.0

    def snapshot(self):
        from app.stream.worker import StatsSnapshot
        return StatsSnapshot()


def synthetic_frame(size: tuple[int, int] = FAKE_SIZE) -> YuvFrame:
    w, h = size
    y = np.tile(np.linspace(40, 200, w, dtype=np.uint8), (h, 1))
    y[h // 2 - 2:h // 2 + 2, :] = 235                       # a horizon line, so the picture is not a flat colour
    u = np.full(((h + 1) // 2, (w + 1) // 2), 128, np.uint8)
    return YuvFrame(w, h, y.tobytes(), u.tobytes(), u.tobytes())


def frame_from_gray(gray: np.ndarray) -> YuvFrame:
    """A grey picture (rows x columns, uint8) as a frame, for tests that need a specific picture on the 'phone'."""
    h, w = gray.shape
    u = np.full(((h + 1) // 2, (w + 1) // 2), 128, np.uint8)
    return YuvFrame(w, h, np.ascontiguousarray(gray).tobytes(), u.tobytes(), u.tobytes())


def assert_window_is_private(window) -> None:
    """The window's settings file must live in the private folder (the app derives it from LOCALAPPDATA when the window is created)."""
    iso = device_isolation.require_active()
    settings_file = getattr(getattr(window, "settings", None), "fileName", lambda: "")()
    if settings_file and not device_isolation.is_inside(settings_file, iso.root):
        raise RuntimeError(f"Refusing to continue: the window's settings file {settings_file} is outside the private test folder")


def attach_fake_phone(window, size: tuple[int, int] = FAKE_SIZE) -> FakeWorker:
    device_isolation.require_active()
    assert_window_is_private(window)
    worker = getattr(window, "worker", None)
    if worker is not None and not isinstance(worker, FakeWorker):
        raise RuntimeError("Refusing to attach a simulated phone over a real worker")
    tracker = getattr(window, "tracker", None)
    if tracker is not None and tracker.isRunning():
        raise RuntimeError("The window's device tracker is running; a simulated test must not listen to the ADB server")
    window.adb = SimulatedAdb()                              # nothing in the window can reach adb any more
    if tracker is not None:
        tracker._adb = window.adb
    worker = FakeWorker(size)
    window.worker = worker
    window._set_connected(True)
    window.video.set_frame(synthetic_frame(size), time.perf_counter())
    return worker


def connect_window(window, pump, wait_for_phone: float = 4.0) -> str:
    """Always the simulated phone; returns 'fake'. `wait_for_phone` is accepted for old callers and ignored. A failure of the simulation
    is raised, never answered by trying anything else."""
    attach_fake_phone(window)
    pump(0.3)
    return "fake"
