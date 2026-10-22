"""Every script in tools/ and what it may touch. tests/test_device_isolation.py fails if a tool is missing here, or does not behave as its class says.

  SIMULATED_TESTS      GUI tests that run against the simulated phone (tools/fake_phone.py). `run_isolated.py --all` runs exactly these.
  SIMULATED_UTILITIES  helpers that also use only the simulated phone but are not checks (they render pictures).
  LIVE_DEVICE          tools that talk to a REAL phone by design. Each refuses to start without the explicit opt-in of tools/live_device.py.
  LOCAL_ONLY           touch no phone and no adb (OpenGL / OS-input self-tests).
  USER_DATA_UTILITIES  touch no phone; they write profiles ONLY into a destination chosen on purpose (--output-dir, or --write-my-real-profiles for
                       the real folder, see tools/profile_tools.py); never part of any test run.
  SUPPORT_MODULES      importable helpers, not tools."""
from __future__ import annotations

SIMULATED_TESTS = (
    "advanced_editor_ui_test", "advanced_ui_test", "binding_delete_ui_test", "editor_test", "intercept_test", "multi_trigger_ui_test",
    "opacity_ui_test", "picture_ui_test", "profile_ui_test", "reliability_ui_test", "smart_ui_test", "tilde_ui_test",
)
SIMULATED_UTILITIES = ("ui_shots",)
LIVE_DEVICE = (
    "cod_explore", "cod_play", "cod_probe", "control_test", "game_keytest", "game_looktest", "game_play", "game_sticktest",
    "game_wheeltest", "look_test", "probe_device", "pubg_livetest", "pubg_sprintlook_test", "pubg_stop", "pubg_verify", "stream_test",
)
LOCAL_ONLY = ("gl_selftest", "look_gl_test", "rawmouse_test", "smooth_gl_test")
USER_DATA_UTILITIES = ("make_cod_profile", "make_pubg_profile")
SUPPORT_MODULES = ("fake_phone", "live_device", "run_isolated", "device_isolation", "tool_registry", "profile_tools")

ALL = (*SIMULATED_TESTS, *SIMULATED_UTILITIES, *LIVE_DEVICE, *LOCAL_ONLY, *USER_DATA_UTILITIES, *SUPPORT_MODULES)
