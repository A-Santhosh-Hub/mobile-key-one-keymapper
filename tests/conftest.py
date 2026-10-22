"""pytest setup: the whole unit suite runs FAIL-CLOSED (see tools/device_isolation.py).

* the device guard is installed for the entire session: a test that tries to start adb or scrcpy, to open the ADB server's socket or to
  reach a non-local address is stopped before anything happens;
* LOCALAPPDATA points at a private temporary folder for the session, so nothing a test does with app.paths can reach the user's data;
* a blocked attempt that a test leaves behind FAILS that test, even if the code under test swallowed the exception. A test that
  deliberately provokes one says so with @pytest.mark.expects_device_block.
There is no environment variable or option that turns this off."""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))

import device_isolation  # noqa: E402

_SESSION = device_isolation.Isolation(prefix="mko-pytest-").start(neutralize_tracker=False)


def pytest_configure(config):
    config.addinivalue_line("markers", "expects_device_block: the test deliberately provokes a blocked real-device attempt")


def pytest_unconfigure(config):
    _SESSION.stop()


@pytest.fixture(autouse=True)
def _no_real_device_attempts(request):
    device_isolation.ATTEMPTS.clear()
    yield
    attempts = list(device_isolation.ATTEMPTS)
    device_isolation.ATTEMPTS.clear()
    if attempts and "expects_device_block" not in request.keywords:
        pytest.fail("a test tried to reach a real device (blocked, but it must not try): " + "; ".join(attempts))
