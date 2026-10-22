"""The ONLY door to a real phone, and it is outside normal test execution.

The tools in tool_registry.LIVE_DEVICE talk to a physical phone by design (mirroring, touches, screenshots, adb). Each starts with
`require_live_opt_in(__file__)`, which refuses to continue unless ALL of these hold:
  * the command line contains the flag  --really-use-real-phone  (an environment variable can be inherited by accident; a flag cannot);
  * the process is attached to an interactive terminal (a test runner, a scheduler or a pipe is not);
  * the person types the phrase  USE MY PHONE  when asked.
Only then is the device guard lifted, for that process. Nothing here is imported by the simulated tools or by pytest.

These tools use the real data folder and the real phone: never run them while you are playing, and never as part of a test run."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import device_isolation

FLAG = "--really-use-real-phone"
PHRASE = "USE MY PHONE"


def _refuse(tool: str, why: str) -> None:
    print(f"\n{Path(tool).name} talks to a REAL phone and was not started: {why}.\n"
          f"To run it on purpose: python tools/{Path(tool).name} {FLAG}   (in a terminal, then type {PHRASE}).\n"
          "For automated checks use the simulated tools: python tools/run_isolated.py --all", file=sys.stderr)
    raise SystemExit(2)


def _has_console() -> bool:
    """Is standard input a real console? On Windows isatty() is also True for the NUL device, so ask the console API itself."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        mode = wintypes.DWORD()
        return bool(kernel32.GetConsoleMode(kernel32.GetStdHandle(-10), ctypes.byref(mode)))
    except Exception:  # noqa: BLE001 - if it cannot be established, it is not a console
        return False


def require_live_opt_in(tool: str) -> None:
    if FLAG not in sys.argv:
        _refuse(tool, f"the flag {FLAG} is missing")
    sys.argv.remove(FLAG)
    if not (sys.stdin and sys.stdin.isatty() and _has_console()):
        _refuse(tool, "it is not attached to an interactive terminal")
    try:
        typed = input(f"This will use your REAL phone and your real profiles. Type {PHRASE} to continue: ")
    except EOFError:
        typed = ""
    if typed.strip() != PHRASE:
        _refuse(tool, "the confirmation was not typed")
    device_isolation.allow_live_devices()


def connect_real_window(window, pump, wait_for_phone: float = 4.0) -> str:
    """The old behaviour of fake_phone.connect_window for the live tools: connect to the first usable phone, else use nothing.
    Refuses unless require_live_opt_in() has lifted the guard in this process."""
    if not device_isolation.live_allowed():
        raise RuntimeError("connect_real_window needs live_device.require_live_opt_in() first")
    end = time.time() + wait_for_phone
    while not any(d.usable for d in window.devices) and time.time() < end:
        pump(0.2)
    if not any(d.usable for d in window.devices):
        raise RuntimeError("No usable phone found (a live tool never falls back to a simulation)")
    window._toggle_connection()
    end = time.time() + 15
    while window.video.frame_size() == (0, 0) and time.time() < end:
        pump(0.1)
    if window.video.frame_size() == (0, 0):
        raise RuntimeError("The phone did not deliver a picture")
    return "real"
