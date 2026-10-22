"""hard_exit ends a process that still has a busy background thread, with the requested exit code and no crash."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

CODE = """
import sys, threading, time
sys.path.insert(0, {root!r})
from app.process import hard_exit
def busy():
    while True:
        time.sleep(0.01)          # a thread inside Python code: what crashed os._exit on Windows
threading.Thread(target=busy).start()                  # not a daemon: a normal exit would wait for it forever
time.sleep(0.2)
print("exiting", flush=True)
hard_exit({code})
"""


@pytest.mark.parametrize("code", [0, 7])
def test_hard_exit_returns_the_exit_code_even_with_a_busy_thread(code):
    for _ in range(3):                                  # the old crash was intermittent
        result = subprocess.run([sys.executable, "-c", CODE.format(root=str(ROOT), code=code)],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == code, (result.returncode, result.stderr)
        assert "exiting" in result.stdout
