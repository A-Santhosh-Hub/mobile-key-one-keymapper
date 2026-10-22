"""Creates the 'Call of Duty Mobile' profile, linked to the game so it loads automatically.

Nothing is written unless you choose where (see tools/profile_tools.py): --output-dir DIR, or --write-my-real-profiles to write your real
profile folder on purpose. An existing profile of that name, or another profile linked to the game, is only changed with --overwrite /
--allow-unlink, and a copy is kept. Try --dry-run first.

No jump button exists in this HUD layout, so Space is unbound.
Button positions are fractions of the 2400x1080 landscape screen, read from a screenshot of the default HUD.
Edit them in the app (Controls tab) if your HUD layout differs.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import profile_tools  # noqa: E402
from app.mapping.profile import Binding, Look, Profile, Stick  # noqa: E402

COD_PACKAGE = "com.activision.callofduty.shooter"
NAME = "Call of Duty Mobile"


def build() -> Profile:
    profile = Profile(NAME, "landscape", package=COD_PACKAGE)
    profile.set_stick(Stick(0.1365, 0.667, radius=0.10, ramp_ms=50))
    profile.looks.append(Look(0.36, 0.12, 0.66, 0.52, sens_x=1.0, sens_y=1.0))
    for binding in (
        Binding("LMB", 0.7515, 0.674, "hold", "Fire"),
        Binding("F", 0.2575, 0.167, "hold", "Fire (left)"),
        Binding("RMB", 0.9085, 0.500, "tap", "Scope"),
        Binding("R", 0.7325, 0.883, "tap", "Reload"),
        Binding("C", 0.641, 0.747, "tap", "Crouch"),
        Binding("X", 0.9075, 0.681, "tap", "Stance (stand/prone)"),
        Binding("Shift", 0.816, 0.317, "tap", "Sprint lock"),
        Binding("1", 0.444, 0.883, "tap", "Weapon 1"),
        Binding("2", 0.5325, 0.883, "tap", "Weapon 2"),
        Binding("WheelDown", 0.5325, 0.883, "tap", "Next weapon (wheel down)"),
        Binding("WheelUp", 0.444, 0.883, "tap", "Previous weapon (wheel up)"),
    ):
        profile.add(binding)
    return profile


def main(argv=None) -> int:
    return profile_tools.run(argv, build=build, name=NAME, package=COD_PACKAGE, description=__doc__.strip().splitlines()[0])


if __name__ == "__main__":
    sys.exit(main())
