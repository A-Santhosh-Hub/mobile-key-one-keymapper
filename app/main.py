from __future__ import annotations

import argparse
import logging
import sys

from PySide6.QtWidgets import QApplication

from .logging_setup import setup_logging
from .process import hard_exit
from .ui import theme
from .ui.main_window import MainWindow


def build_parser() -> argparse.ArgumentParser:
    """The command line of the app. It has NO option that connects to a phone by itself: a phone is only ever connected when the person
    presses Connect. (An old `--selftest` option did that on its own; it was removed. The simulated GUI suites test the window, and the
    live-device tools in tools/ need an explicit opt-in; see docs/TESTING.md.)"""
    return argparse.ArgumentParser(prog="mobile-key-one")


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(sys.argv[1:] if argv is None else argv)       # unknown options end the program here, before anything starts

    setup_logging()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Mobile Key One")
    app.setStyleSheet(theme.STYLE_SHEET)
    window = MainWindow()
    window.show()
    code = app.exec()
    if window.stuck_threads:
        # A thread that is still inside a system call cannot be stopped or destroyed safely. Everything has been saved,
        # so end the process directly; the system reclaims the thread.
        logging.shutdown()
        hard_exit(code)
    return code
