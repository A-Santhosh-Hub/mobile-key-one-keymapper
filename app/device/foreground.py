"""Watches which Android app is in the foreground, so the matching game profile can load by itself."""
from __future__ import annotations

import logging
import re
import time

from PySide6.QtCore import QThread, Signal

from .adb import Adb, AdbError

log = logging.getLogger(__name__)

_TOP_ACTIVITY = re.compile(r"\bu\d+\s+([A-Za-z0-9_.]+)/")


def parse_foreground_package(text: str) -> str:
    """Extract the app id from a `topResumedActivity=ActivityRecord{... u0 com.pkg/.Activity t12 d0}` line."""
    match = _TOP_ACTIVITY.search(text)
    return match.group(1) if match else ""


class ForegroundWatcher(QThread):
    packageChanged = Signal(str)

    def __init__(self, adb: Adb, serial: str, interval: float = 1.2, parent=None) -> None:
        super().__init__(parent)
        self._adb = adb
        self._serial = serial
        self._interval = interval
        self._stop = False
        self._process = None                       # the adb call in flight, so request_stop() can cancel it

    def request_stop(self) -> None:
        """Ask the thread to end. Returns at once; `finished` is emitted when it has (the loop checks every 50 ms, but it
        may be inside an adb call that can take a few seconds)."""
        self._stop = True
        process = self._process
        if process is not None:
            try:
                process.kill()                     # the call returns at once and the loop ends
            except OSError:
                pass

    def stop(self, timeout_ms: int = 4000) -> None:
        """request_stop() and then WAIT. Blocks: never call this from the GUI thread."""
        self.request_stop()
        self.wait(timeout_ms)

    def _remember(self, process) -> None:
        self._process = process
        if self._stop:                             # stop was requested between the check and the call: cancel it now
            self.request_stop()

    def run(self) -> None:
        last = None
        while not self._stop:
            try:
                out = self._adb.run("shell", "dumpsys activity activities | grep -m1 topResumedActivity",
                                    serial=self._serial, check=False, timeout=5,
                                    process_hook=self._remember).stdout
                package = parse_foreground_package(out)
                if package and package != last:
                    last = package
                    self.packageChanged.emit(package)
            except AdbError as exc:
                log.debug("Foreground check failed: %s", exc)
            end = time.time() + self._interval
            while time.time() < end and not self._stop:
                time.sleep(0.05)
