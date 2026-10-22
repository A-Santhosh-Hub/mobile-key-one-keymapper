"""Ending the process without running any cleanup."""
from __future__ import annotations

import os
import sys


def hard_exit(code: int = 0) -> None:
    """End the process at once and return `code` to the caller. For use only when everything is already saved and a
    background thread is stuck inside a system call. os._exit() is not safe for that on Windows: while another thread is
    inside Python code the DLL teardown it triggers can crash (access violation). TerminateProcess skips that teardown."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code & 0xFFFFFFFF)
    os._exit(code)                                       # other platforms (or if TerminateProcess returned)
