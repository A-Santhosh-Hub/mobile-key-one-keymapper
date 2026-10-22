"""The application's command line cannot connect to a phone by itself (Phase 10.1). `python -m app --selftest N` used to connect to the first
usable device with no confirmation; the option is gone. Everything here runs in-process under the device guard: if it ever did connect, the
attempt would be blocked and recorded, and the test would fail."""
import re
import sys
from pathlib import Path

import pytest

import app.main as app_main
import device_isolation as iso

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"


@pytest.fixture
def spies(monkeypatch):
    """Anything the real start-up would do before a window exists is replaced by something that records it."""
    calls = []
    monkeypatch.setattr(app_main, "setup_logging", lambda *a, **k: calls.append("logging"))
    monkeypatch.setattr(app_main, "QApplication", lambda *a, **k: calls.append("QApplication"))
    monkeypatch.setattr(app_main, "MainWindow", lambda *a, **k: calls.append("MainWindow"))
    return calls


@pytest.mark.parametrize("args", [["--selftest", "3"], ["--selftest", "0.5", "--screenshot", "x.png"], ["--screenshot", "x.png"],
                                  ["--serial", "ABC"], ["--connect"], ["--auto-connect"], ["--device", "ABC"], ["--selftest=3"]])
def test_every_option_that_would_connect_by_itself_is_refused_before_anything_starts(args, spies, capsys):
    with pytest.raises(SystemExit) as stopped:
        app_main.main(args)
    assert stopped.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err
    assert spies == [] and not iso.ATTEMPTS                                         # no logging, no QApplication, no window, no ADB


def test_help_is_answered_without_starting_anything(spies, capsys):
    with pytest.raises(SystemExit) as stopped:
        app_main.main(["--help"])
    assert stopped.value.code == 0 and "mobile-key-one" in capsys.readouterr().out and spies == []


def test_the_parser_defines_no_options_at_all():
    parser = app_main.build_parser()
    assert [a.option_strings for a in parser._actions if a.option_strings != ["-h", "--help"]] == []


def test_the_normal_start_still_builds_the_window_and_nothing_connects(monkeypatch):
    built = []

    class Window:
        stuck_threads = []

        def show(self):
            built.append("show")

    class App:
        def __init__(self, *a):
            built.append("app")

        def setApplicationName(self, name):
            pass

        def setStyleSheet(self, sheet):
            pass

        def exec(self):
            built.append("exec")
            return 0
    monkeypatch.setattr(app_main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(app_main, "QApplication", App)
    monkeypatch.setattr(app_main, "MainWindow", lambda: Window())
    assert app_main.main([]) == 0 and built == ["app", "show", "exec"] and not iso.ATTEMPTS


def test_no_application_code_starts_a_connection_except_through_the_connect_button():
    callers = []
    for path in APP.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"_toggle_connection\s*\(\s*\)", line) or re.search(r"_run_selftest|args\.selftest|add_argument", line):
                callers.append(f"{path.name}:{number}")
    assert callers == [], callers
    window = (APP / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert len(re.findall(r"connect_btn\.clicked\.connect\(self\._toggle_connection\)", window)) == 1
    import ast
    tree = ast.parse(window)
    starters = set()
    for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_start_worker":
                starters.add(function.name)
    assert starters == {"_toggle_connection", "_on_worker_finished", "_maybe_reconnect"}     # the button, 'Apply now' / reconnect after a loss, the retry timer


def test_the_application_does_not_depend_on_the_test_tools():
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+(device_isolation|live_device|fake_phone|tools)\b", text, re.M), path.name


def test_no_documentation_still_advertises_the_removed_option():
    for path in [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"--selftest", text):
            line = text[max(0, text.rfind("\n", 0, match.start())):text.find("\n", match.end())]
            assert "removed" in line.lower() or "no longer" in line.lower(), (path.name, line.strip())


def test_automatic_reconnecting_can_only_follow_a_connection_the_person_started():
    """_on_worker_finished / _maybe_reconnect act only for `_wanted_serial`; it is given a device in exactly one place: the Connect button."""
    import ast
    tree = ast.parse((APP / "ui" / "main_window.py").read_text(encoding="utf-8"))
    setters = set()
    for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "_wanted_serial" for t in node.targets):
                if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                    setters.add(function.name)
    assert setters == {"_toggle_connection"}
