"""Adb.run keeps its behaviour and can now be cancelled from another thread. Uses the Python interpreter as a stand-in
for adb.exe (Adb only builds a command line and runs it), so no phone or adb server is involved. The real adb is never started by a
test: the device guard refuses it (tests/test_device_isolation.py)."""
import sys
import threading
import time

import pytest

from app.device.adb import Adb, AdbError


@pytest.fixture
def fake_adb():
    return Adb(path=sys.executable)


def test_run_returns_the_output_and_exit_code(fake_adb):
    result = fake_adb.run("-c", "print('hello')")
    assert result.returncode == 0 and result.stdout.strip() == "hello"


def test_a_failing_command_raises_with_its_message_unless_check_is_off(fake_adb):
    with pytest.raises(AdbError, match="boom"):
        fake_adb.run("-c", "import sys; sys.stderr.write('boom'); sys.exit(3)")
    result = fake_adb.run("-c", "import sys; sys.exit(3)", check=False)
    assert result.returncode == 3


def test_a_missing_program_is_reported_as_an_adb_error():
    with pytest.raises(AdbError, match="not found"):
        Adb(path="Z:/definitely/not/a-tool.exe").run("version")       # (not named like adb: a test never starts the real one)


def test_a_command_that_takes_too_long_is_killed_and_reported(fake_adb):
    started = time.perf_counter()
    with pytest.raises(AdbError, match="timed out"):
        fake_adb.run("-c", "import time; time.sleep(30)", timeout=0.5)
    assert time.perf_counter() - started < 5


def test_another_thread_can_cancel_a_call_in_flight(fake_adb):
    holder = {}
    ready = threading.Event()

    def hook(process):
        holder["process"] = process
        ready.set()

    results = {}

    def work():
        started = time.perf_counter()
        results["value"] = fake_adb.run("-c", "import time; time.sleep(30)", check=False, process_hook=hook)
        results["elapsed"] = time.perf_counter() - started

    thread = threading.Thread(target=work)
    thread.start()
    assert ready.wait(5)
    holder["process"].kill()
    thread.join(10)
    assert not thread.is_alive() and results["elapsed"] < 5 and results["value"].returncode != 0
