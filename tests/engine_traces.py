"""Scenario library and trace recorder for the input engine (Phase 2, step 4).

A scenario is plain data: a profile (bindings, stick, look) and a list of steps. Running it drives the engine through its
public API with a manual clock and records EVERYTHING that can be observed from outside:
  * every control message sent to the phone, as hex bytes, with the time it was sent,
  * what each step returned, how many fingers were down afterwards, the scope flag,
  * every change of the "pressed" set that the overlay highlights.
tests/golden/engine_traces.json holds the traces that the engine produced BEFORE its bookkeeping was refactored; the test in
test_engine_traces.py replays every scenario and requires the identical trace, byte for byte. Regenerate the golden file only
when engine behaviour is meant to change (the multi-trigger / multi-action step), and say so in the commit.

Run this module directly to (re)write the golden file:  python tests/engine_traces.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.mapping.profile import Action, Binding, Look, Profile, Stick  # noqa: E402
from test_characterization_engine import Rig, SPECS  # noqa: E402
from test_mapping import FakeControl  # noqa: E402

GOLDEN = Path(__file__).parent / "golden" / "engine_traces.json"
LAND, PORT = [2400, 1080], [1080, 2400]


class RecordingControl(FakeControl):
    def __init__(self, rig):
        super().__init__()
        self.rig = rig
        self.log = []

    def send(self, data):
        self.log.append([round(self.rig.now, 3), bytes(data).hex()])
        return super().send(data)


def b(key, mode="hold", x=0.5, y=0.5, **kw):
    return {"input": key, "x": x, "y": y, "mode": mode, **SPECS.get(mode, {}), **kw}


def build_profile(spec):
    profile = Profile("trace")
    for raw in spec.get("bindings", []):
        raw = dict(raw)
        raw["steps"] = [tuple(s) for s in raw.get("steps", [])]
        raw["also"] = [tuple(s) for s in raw.get("also", [])]
        raw["extra_actions"] = [Action(**a) for a in raw.get("extra_actions", [])]
        binding = Binding(**raw)
        (profile.bindings.append if spec.get("raw_bindings") else profile.add)(binding)
    for group in spec.get("share", []):                      # explicit sharing: {"trigger": "F", "ids": ["a", "b"]}
        profile.share_trigger(group["trigger"], group["ids"])
    if spec.get("same_id"):                                  # bindings that (wrongly) share one runtime id: a hand-edited list
        for binding in profile.bindings[1:]:
            binding.id = profile.bindings[0].id
    if spec.get("stick"):
        profile.set_stick(Stick(**spec["stick"]))
    for look in spec.get("looks", []):
        profile.looks.append(Look(**look))
    return profile


def run(spec):
    profile = build_profile(spec)
    rig = Rig(size=tuple(spec.get("size", LAND)))
    rig.control = RecordingControl(rig)
    rig.profile = profile
    rig.engine.set_profile(profile)
    rig.engine.set_mapping_active(True)
    rig.engine.direct_touch = spec.get("direct_touch", True)
    rig.pressed_log.clear()
    returns, after = [], []
    live = rig.control
    for step in spec["steps"]:
        op, args = step[0], step[1:]
        result = None
        if op == "press":
            result = rig.press(args[0], args[1] if len(args) > 1 else 0)
        elif op == "release":
            result = rig.release(args[0], args[1] if len(args) > 1 else 0)
        elif op == "advance":
            rig.advance(args[0])
        elif op == "mouse":
            result = rig.engine.mouse_button(args[0], args[1], tuple(args[2]) if args[2] else None)
            rig.advance(args[3] if len(args) > 3 else 0)
        elif op == "move":
            rig.engine.mouse_move(tuple(args[0]))
        elif op == "wheel":
            result = rig.engine.wheel_binding(args[0])
            rig.advance(args[1] if len(args) > 1 else 0)
        elif op == "scroll":
            result = rig.engine.wheel(tuple(args[0]), args[1], args[2])
        elif op == "look":
            result = rig.engine.look_delta(args[0], args[1])
            rig.advance(args[2] if len(args) > 2 else 4)
        elif op == "capture":
            rig.engine.set_look_captured(args[0])
        elif op == "release_all":
            rig.engine.release_all()
        elif op == "screen_changed":
            rig.size = tuple(args[0])
            rig.engine.screen_changed()
        elif op == "mapping":
            rig.engine.set_mapping_active(args[0])
        elif op == "refresh":
            rig.engine.refresh_profile()
        elif op == "profile_none":
            rig.engine.set_profile(None)
        elif op == "control":
            rig.control = live if args[0] == "live" else None
        else:
            raise ValueError(op)
        returns.append(result)
        after.append([rig.fingers(), rig.engine.scope_active, rig.engine.look_captured])
    return {"messages": live.log, "returns": returns, "after": after,
            "highlights": [sorted(h) for h in rig.pressed_log]}


# ===================================================================== the scenarios
def scenarios():
    s = {}
    modes = list(SPECS)

    for mode in modes:
        k = [b("K", mode)]
        s[f"{mode}/held-then-released"] = {"bindings": k, "steps": [["press", "K", 500], ["release", "K", 3000]]}
        s[f"{mode}/released-at-once"] = {"bindings": k, "steps": [["press", "K"], ["release", "K", 3000]]}
        s[f"{mode}/released-after-40ms"] = {"bindings": k, "steps": [["press", "K", 40], ["release", "K", 3000]]}
        s[f"{mode}/release_all-after-20ms"] = {"bindings": k, "steps": [["press", "K", 20], ["release_all"], ["advance", 3000]]}
        s[f"{mode}/pressed-three-times"] = {"bindings": k, "steps": [["press", "K"], ["press", "K", 30], ["press", "K", 30],
                                                                      ["release", "K", 3000], ["release", "K", 100]]}
        s[f"{mode}/pressed-again-after-it-ended"] = {"bindings": k, "steps": [["press", "K", 400], ["release", "K", 2000],
                                                                              ["press", "K", 400], ["release", "K", 2000]]}
        s[f"{mode}/on-the-wheel"] = {"bindings": [b("WheelUp", mode), b("WheelDown", mode, x=0.3, y=0.7)],
                                     "steps": [["wheel", 1, 10], ["wheel", 1, 10], ["wheel", -1, 500], ["wheel", 1, 3000],
                                               ["wheel", 0], ["wheel", -1, 20], ["release_all"], ["advance", 500]]}
        s[f"{mode}/scope-button"] = {"bindings": [b("RMB", mode, scope=True)], "looks": [dict(x0=0.5, y0=0.2, x1=0.9, y1=0.8, ads_scale=0.5)],
                                     "steps": [["mouse", "RMB", True, None, 300], ["capture", True], ["look", 20, 0, 200],
                                               ["mouse", "RMB", False, None, 300], ["mouse", "RMB", True, None, 300],
                                               ["mouse", "RMB", False, None, 3000], ["look", 20, 0, 300], ["release_all"]]}
        s[f"{mode}/mapped-mouse-button"] = {"bindings": [b("LMB", mode, x=0.7, y=0.6)],
                                            "steps": [["mouse", "LMB", True, [0.2, 0.2], 200], ["move", [0.3, 0.3]],
                                                      ["mouse", "LMB", False, [0.3, 0.3], 3000]]}
        s[f"{mode}/with-extra-spots"] = {"bindings": [b("K", mode, also=[[0.2, 0.2], [0.8, 0.8]])],
                                         "steps": [["press", "K", 300], ["release", "K", 1500], ["press", "K", 20],
                                                   ["press", "K", 400], ["release_all"], ["advance", 800]]}
        s[f"{mode}/modifier-combination"] = {"bindings": [b("K", mode, x=0.3), b("Shift+K", mode, x=0.7, y=0.8)],
                                             "steps": [["press", "Shift"], ["press", "K", 300], ["release", "Shift", 100],
                                                       ["release", "K", 2000], ["press", "K", 300], ["press", "Shift", 100],
                                                       ["release", "K", 1500], ["release", "Shift", 100]]}
        s[f"{mode}/rotation"] = {"bindings": [b("K", mode, also=[[0.2, 0.2]])],
                                 "steps": [["press", "K", 30], ["screen_changed", PORT], ["advance", 2000], ["release", "K", 100],
                                           ["press", "K", 300], ["release", "K", 2000]]}
        s[f"{mode}/mapping-off"] = {"bindings": [b("K", mode)],
                                    "steps": [["press", "K", 30], ["mapping", False], ["advance", 2000], ["press", "K", 100],
                                              ["release", "K"], ["mapping", True], ["press", "K", 300], ["release", "K", 1000]]}
        s[f"{mode}/dead-connection"] = {"bindings": [b("K", mode)],
                                        "steps": [["press", "K", 30], ["control", "none"], ["release_all"], ["advance", 1000],
                                                  ["press", "K", 100], ["control", "live"], ["release", "K", 100], ["press", "K", 300],
                                                  ["release", "K", 1500]]}

    # combination precedence ---------------------------------------------------------------------
    combos = [b("W", "hold", x=0.1), b("Shift+W", "hold", x=0.2), b("Ctrl+W", "tap", x=0.3), b("Ctrl+Shift+W", "tap", x=0.4),
              b("Shift", "hold", x=0.5, y=0.9), b("Alt+LMB", "tap", x=0.6), b("LMB", "hold", x=0.7), b("Ctrl+WheelUp", "tap", x=0.8),
              b("WheelUp", "tap", x=0.9)]
    s["combos/every-modifier-order"] = {"bindings": combos, "steps": [
        ["press", "W", 100], ["release", "W", 100], ["press", "Shift", 100], ["press", "W", 100], ["release", "W", 100],
        ["press", "Ctrl", 100], ["press", "W", 100], ["release", "W", 100], ["release", "Shift", 100], ["press", "W", 100],
        ["release", "W", 100], ["release", "Ctrl", 100], ["press", "Alt", 10], ["mouse", "LMB", True, [0.5, 0.5], 100],
        ["mouse", "LMB", False, [0.5, 0.5], 100], ["release", "Alt", 100], ["mouse", "LMB", True, [0.5, 0.5], 100],
        ["mouse", "LMB", False, [0.5, 0.5], 100], ["press", "Ctrl", 0], ["wheel", 1, 200], ["release", "Ctrl", 0], ["wheel", 1, 200]]}
    s["combos/modifier-changes-while-a-button-is-held"] = {"bindings": combos, "steps": [
        ["mouse", "LMB", True, [0.5, 0.5], 100], ["press", "Alt", 100], ["mouse", "LMB", False, [0.5, 0.5], 100],
        ["release", "Alt", 100], ["press", "W", 100], ["press", "Shift", 100], ["release", "Shift", 100], ["release", "W", 100]]}
    s["combos/lone-modifier-binding"] = {"bindings": [b("Shift", "hold", x=0.1), b("Shift+E", "tap", x=0.9)], "steps": [
        ["press", "Shift", 100], ["press", "E", 100], ["release", "E", 100], ["release", "Shift", 100]]}
    s["combos/exact-modifier-set"] = {"bindings": [b("Ctrl+Shift+Q", "tap", x=0.2), b("Q", "tap", x=0.8)], "steps": [
        ["press", "Ctrl"], ["press", "Q", 100], ["release", "Q", 100], ["press", "Shift"], ["press", "Q", 100], ["release", "Q", 100],
        ["press", "Alt"], ["press", "Q", 100], ["release", "Q", 100]]}

    # stick with modifiers, sprint, walk, auto-run ---------------------------------------------------
    stick = dict(x=0.15, y=0.7, radius=0.1, ramp_ms=50, sprint_key="Shift", sprint_scale=1.5, walk_key="Ctrl", walk_scale=0.5,
                 autorun_key="Q")
    s["stick/wasd-sprint-walk-autorun"] = {"stick": stick, "bindings": [b("Shift+W", "hold", x=0.8, y=0.3), b("R", "tap", x=0.7, y=0.9)], "steps": [
        ["press", "W", 200], ["press", "D", 200], ["press", "Shift", 200], ["release", "W", 100], ["press", "W", 200],
        ["release", "Shift", 200], ["press", "Ctrl", 200], ["release", "Ctrl", 100], ["release", "D", 100], ["release", "W", 100],
        ["press", "Q", 100], ["release", "Q", 200], ["press", "A", 200], ["release", "A", 100], ["press", "S", 100], ["release", "S", 300],
        ["press", "Shift", 10], ["press", "W", 100], ["press", "R", 100], ["release", "R", 100], ["release_all"], ["advance", 500]]}
    s["stick/no-ramp-and-opposites"] = {"stick": dict(x=0.15, y=0.7, radius=0.1, ramp_ms=0), "steps": [
        ["press", "W"], ["press", "S"], ["release", "W"], ["press", "A"], ["release", "S"], ["release", "A"]]}
    s["stick/rotation-and-focus-loss"] = {"stick": stick, "steps": [
        ["press", "W", 20], ["screen_changed", PORT], ["advance", 500], ["press", "W", 200], ["release_all"], ["advance", 500],
        ["press", "D", 300], ["mapping", False], ["advance", 500]]}
    s["stick/two-sticks"] = {"stick": stick, "steps": [["press", "W", 100], ["release", "W", 100]]}

    # mouse look ----------------------------------------------------------------------------------
    look = dict(x0=0.5, y0=0.2, x1=0.9, y1=0.8, sens_x=1.5, sens_y=1.0, accel=0.5, invert_y=True, ads_scale=0.5)
    s["look/flicks-idle-and-edges"] = {"looks": [look], "steps": [
        ["capture", True], ["look", 10, 5, 10], ["look", 3, 0, 4], ["look", 50, 0, 300], ["look", 400, 0, 10], ["look", 400, 0, 10],
        ["look", -4, 9, 200], ["advance", 500], ["look", 5, 5, 10], ["capture", False], ["advance", 500], ["look", 5, 5, 10]]}
    s["look/with-buttons-stick-and-scope"] = {"looks": [look], "stick": dict(x=0.15, y=0.7, ramp_ms=0), "bindings": [
        b("RMB", "toggle", scope=True), b("LMB", "hold", x=0.8, y=0.7), b("K", "tap")], "steps": [
        ["capture", True], ["look", 20, 0, 10], ["press", "W", 10], ["mouse", "LMB", True, None, 50], ["look", 20, 0, 10],
        ["mouse", "RMB", True, None, 10], ["mouse", "RMB", False, None, 10], ["look", 20, 0, 10], ["press", "K", 100],
        ["release", "K"], ["mouse", "LMB", False, None, 10], ["release_all"], ["advance", 1000], ["look", 5, 5, 10]]}
    s["look/rotation"] = {"looks": [look], "steps": [["capture", True], ["look", 10, 0, 10], ["screen_changed", PORT], ["advance", 500],
                                                    ["look", 10, 0, 10]]}
    s["look/second-zone-is-ignored"] = {"looks": [look, dict(x0=0.1, y0=0.2, x1=0.3, y1=0.8)], "steps": [
        ["capture", True], ["look", 10, 0, 10]]}
    s["look/capture-needs-mapping-and-a-zone"] = {"steps": [["capture", True], ["look", 5, 5, 10], ["mapping", False], ["mapping", True]]}
    s["look/mapping-off-ends-capture"] = {"looks": [look], "steps": [["capture", True], ["look", 5, 5, 10], ["mapping", False],
                                                                    ["look", 5, 5, 10], ["capture", True]]}

    # the direct (mouse-as-finger) path ----------------------------------------------------------------------
    s["direct/drag-click-scroll"] = {"bindings": [b("K", "tap")], "steps": [
        ["mouse", "LMB", True, [0.2, 0.3], 10], ["move", [0.4, 0.5]], ["move", [0.6, 0.7]], ["mouse", "LMB", False, [0.6, 0.7], 10],
        ["mouse", "LMB", True, None, 10], ["mouse", "RMB", True, [0.5, 0.5], 10], ["mouse", "RMB", False, [0.5, 0.5], 10],
        ["scroll", [0.5, 0.5], 0, -1], ["mapping", False], ["mouse", "RMB", True, [0.5, 0.5], 10], ["mouse", "RMB", False, [0.5, 0.5], 10],
        ["mouse", "MMB", True, [0.5, 0.5], 10], ["mouse", "MMB", False, [0.5, 0.5], 10], ["mouse", "LMB", True, [0.2, 0.3], 10],
        ["mapping", True], ["release_all"], ["advance", 100]]}
    s["direct/disabled-and-captured"] = {"direct_touch": False, "looks": [look], "steps": [
        ["mouse", "LMB", True, [0.2, 0.3], 10], ["mouse", "LMB", False, [0.2, 0.3], 10], ["capture", True],
        ["mouse", "LMB", True, [0.2, 0.3], 10], ["scroll", [0.5, 0.5], 1, 1]]}

    # many fingers ---------------------------------------------------------------------------------------
    keys = "ABCDEFGHIJ"
    full = [b(k, "hold", x=0.05 + 0.09 * i) for i, k in enumerate(keys)]
    s["fingers/ten-holds-then-an-eleventh"] = {"bindings": full + [b("L", "hold", x=0.9, y=0.9)], "steps": [
        *[["press", k, 5] for k in keys], ["press", "L", 5], ["release", "C", 5], ["press", "L", 5], ["release", "L", 5],
        *[["release", k, 5] for k in keys]]}
    s["fingers/partial-extra-spots"] = {"bindings": full[:8] + [b("L", "hold", x=0.9, y=0.9, also=[[0.1, 0.1], [0.2, 0.2], [0.3, 0.3]])], "steps": [
        *[["press", k, 5] for k in keys[:8]], ["press", "L", 5], ["release", "L", 5], ["release", "A", 5], ["press", "L", 5], ["release_all"]]}
    s["fingers/pinch-with-no-room"] = {"bindings": full[:9] + [b("Z", "pinch-out")], "steps": [
        *[["press", k, 5] for k in keys[:9]], ["press", "Z", 500], ["release", "Z", 100], ["release_all"]]}
    s["fingers/reuse-of-lowest-pointer-id"] = {"bindings": full[:4], "steps": [
        ["press", "A", 1], ["press", "B", 1], ["press", "C", 1], ["release", "A", 1], ["press", "D", 1], ["release", "B", 1],
        ["press", "A", 1], ["release_all"]]}

    # cleanup in a busy session ------------------------------------------------------------------------------
    busy = {"bindings": [b("G", "hold", x=0.1, y=0.9, also=[[0.2, 0.9]]), b("T", "toggle", x=0.3, y=0.9), b("F", "repeat", x=0.5, y=0.9),
                         b("V", "drag", x=0.2, y=0.4, x2=0.8, y2=0.4, duration_ms=400), b("N", "sequence", steps=[[0.1, 0.1, 0], [0.2, 0.2, 300]]),
                         b("RMB", "toggle", x=0.9, y=0.5, scope=True)],
            "stick": dict(x=0.15, y=0.7, radius=0.1, ramp_ms=100), "looks": [look]}
    setup = [["press", "G"], ["press", "T"], ["release", "T"], ["press", "W", 30], ["mouse", "LMB", True, [0.9, 0.1], 10], ["capture", True],
             ["look", 10, 0, 10], ["mouse", "RMB", True, None, 10], ["mouse", "RMB", False, None, 10], ["press", "V", 100], ["press", "N", 100],
             ["press", "F"]]
    for name, tail in (("release_all", [["release_all"], ["advance", 3000]]), ("mapping-off", [["mapping", False], ["advance", 3000]]),
                       ("rotation", [["screen_changed", PORT], ["advance", 3000]]), ("disconnect", [["control", "none"], ["release_all"],
                                                                                                  ["control", "live"], ["advance", 3000]]),
                       ("refresh", [["refresh"], ["advance", 3000]]), ("profile-removed", [["profile_none"], ["advance", 3000]])):
        s[f"busy/{name}"] = {**busy, "steps": setup + tail + [["press", "G", 50], ["release", "G", 500]]}
    s["busy/keys-released-normally"] = {**busy, "steps": setup + [["release", k] for k in "GVNF"] + [["release", "W", 300], ["advance", 3000]]}

    # shared and extra things the engine must keep ignoring in this step ---------------------------------------------
    s["shared/same-first-input-the-last-one-wins"] = {"raw_bindings": True, "bindings": [b("R", "hold", x=0.1), b("R", "tap", x=0.9)], "steps": [
        ["press", "R", 300], ["release", "R", 300]]}
    s["unbound/keys-and-buttons-and-no-profile"] = {"steps": [["press", "Z"], ["release", "Z"], ["wheel", 1], ["profile_none"], ["press", "Q"],
                                                             ["mouse", "LMB", True, [0.5, 0.5], 10], ["mouse", "LMB", False, [0.5, 0.5], 10]]}
    shared_id = [b("K", "hold", x=0.2, also=[[0.1, 0.1]]), b("L", "hold", x=0.8), b("Z", "pinch-out"), b("WheelUp", "tap", x=0.4, y=0.4)]
    s["ids/bindings-that-share-a-runtime-id-still-run-independently"] = {"same_id": True, "bindings": shared_id, "steps": [
        ["press", "K", 10], ["press", "L", 10], ["press", "Z", 100], ["wheel", 1, 10], ["release", "K", 10], ["release", "L", 10],
        ["press", "K", 10], ["release", "Z", 500], ["release", "K", 10], ["press", "L", 10], ["release_all"], ["advance", 500]]}
    s["ids/bindings-that-share-a-runtime-id-and-the-same-release-all"] = {"same_id": True, "bindings": shared_id[:3], "steps": [
        ["press", "K"], ["press", "L"], ["press", "Z", 50], ["release_all"], ["advance", 1000], ["press", "L", 50], ["release", "L", 50]]}
    s["refresh/while-holding-then-hold-again"] = {"bindings": [b("K", "hold", also=[[0.2, 0.2]])], "steps": [
        ["press", "K", 100], ["refresh"], ["release", "K", 100], ["press", "K", 100], ["release", "K", 100]]}
    s.update(multi_trigger_scenarios())
    s.update(multi_action_scenarios())
    s.update(shared_binding_scenarios())
    return s


def multi_trigger_scenarios():
    """Scenarios for a binding with several triggers (Step 5). Their traces were recorded from the multi-trigger engine and
    each behaviour is asserted explicitly in test_multi_trigger.py; the traces only guard against later drift."""
    s = {}
    for mode in SPECS:
        e = b("E", mode, extra_triggers=["F"])
        s[f"multi-trigger/{mode}/second-key-joins-first-released-first"] = {"bindings": [e], "steps": [
            ["press", "E", 30], ["press", "F", 100], ["release", "E", 300], ["release", "F", 3000]]}
        s[f"multi-trigger/{mode}/second-key-joins-last-released-first"] = {"bindings": [e], "steps": [
            ["press", "E", 30], ["press", "F", 100], ["release", "F", 300], ["release", "E", 3000]]}
        s[f"multi-trigger/{mode}/started-by-the-second-key"] = {"bindings": [e], "steps": [
            ["press", "F", 30], ["release", "F", 3000], ["press", "E", 30], ["release", "E", 3000]]}
        s[f"multi-trigger/{mode}/release_all-with-two-keys-down"] = {"bindings": [e], "steps": [
            ["press", "E", 30], ["press", "F", 30], ["release_all"], ["advance", 3000], ["release", "E", 100], ["release", "F", 100]]}
    three = [b("E", "hold", x=0.3, y=0.4, extra_triggers=["F", "MMB", "Shift+G"])]
    s["multi-trigger/hold-three-triggers-key-mouse-and-combination"] = {"bindings": three, "steps": [
        ["press", "E", 50], ["mouse", "MMB", True, [0.5, 0.5], 50], ["press", "Shift"], ["press", "G", 50], ["release", "E", 50],
        ["release", "Shift", 50], ["mouse", "MMB", False, [0.5, 0.5], 50], ["press", "F", 50], ["release", "G", 50], ["release", "F", 500]]}
    s["multi-trigger/combination-precedence-and-plain-fallback"] = {"bindings": [
        b("E", "tap", x=0.2, extra_triggers=["Shift+F"]), b("F", "tap", x=0.8), b("Shift+E", "tap", x=0.5)], "steps": [
        ["press", "F", 100], ["release", "F", 100], ["press", "Shift"], ["press", "F", 100], ["release", "F", 100],
        ["press", "E", 100], ["release", "E", 100], ["release", "Shift", 100], ["press", "E", 100], ["release", "E", 100]]}
    s["multi-trigger/repeated-key-downs-never-add-a-second-finger"] = {"bindings": [b("E", "hold", extra_triggers=["F"]),
                                                                                  b("R", "repeat", rate_hz=10, extra_triggers=["T"])], "steps": [
        ["press", "E"], ["press", "E"], ["press", "F"], ["press", "F"], ["press", "E"], ["release", "E", 50], ["press", "F"], ["release", "F", 50],
        ["press", "R", 250], ["press", "R", 50], ["press", "T", 250], ["press", "T", 50], ["release", "R", 300], ["release", "T", 300]]}
    s["multi-trigger/tap-joins-while-running-and-fires-again-when-finished"] = {"bindings": [b("E", "tap", extra_triggers=["F"])], "steps": [
        ["press", "E", 10], ["press", "F", 10], ["release", "E", 10], ["release", "F", 100], ["press", "F", 41], ["press", "E", 1],
        ["release", "F"], ["release", "E", 200], ["press", "E", 40], ["press", "F", 100]]}
    s["multi-trigger/toggle-over-several-triggers"] = {"bindings": [b("E", "toggle", extra_triggers=["F", "Shift+G"])], "steps": [
        ["press", "E", 10], ["press", "F", 10], ["release", "E", 10], ["release", "F", 100], ["press", "F", 10], ["release", "F", 100],
        ["press", "E", 10], ["release", "E", 100], ["press", "Shift"], ["press", "G", 10], ["press", "F", 10], ["release", "G", 10],
        ["release", "F", 10], ["release", "Shift", 100], ["press", "F", 10], ["release", "F", 10], ["release_all"]]}
    s["multi-trigger/toggle-pressed-while-another-key-sustains-it"] = {"bindings": [b("E", "toggle", extra_triggers=["F"])], "steps": [
        ["press", "E", 10], ["press", "F", 10], ["press", "E", 10], ["release", "E"], ["release", "F"], ["press", "E"], ["release", "E"]]}
    s["multi-trigger/wheel-is-an-impulse-even-when-keys-share-the-binding"] = {"bindings": [
        b("E", "hold", extra_triggers=["WheelUp"]), b("R", "drag", extra_triggers=["WheelDown"], x2=0.9, y2=0.5),
        b("T", "toggle", extra_triggers=["Ctrl+WheelUp"])], "steps": [
        ["wheel", 1, 10], ["wheel", 1, 100], ["wheel", -1, 500], ["press", "E", 100], ["wheel", 1, 100], ["release", "E", 100],
        ["wheel", -1, 20], ["press", "R", 100], ["wheel", -1, 100], ["release", "R", 100], ["press", "Ctrl"], ["wheel", 1, 10],
        ["wheel", 1, 10], ["release", "Ctrl", 10], ["press", "T", 10], ["wheel", 1, 10], ["release", "T", 10], ["release_all"], ["advance", 500]]}
    s["multi-trigger/mapped-mouse-buttons-share-the-run"] = {"bindings": [b("LMB", "hold", extra_triggers=["MMB", "RMB"])], "steps": [
        ["mouse", "LMB", True, [0.5, 0.5], 50], ["mouse", "RMB", True, [0.5, 0.5], 50], ["mouse", "LMB", False, [0.5, 0.5], 50],
        ["mouse", "MMB", True, [0.5, 0.5], 50], ["mouse", "RMB", False, [0.5, 0.5], 50], ["mouse", "MMB", False, [0.5, 0.5], 200]]}
    s["multi-trigger/scope-button-with-two-triggers"] = {"looks": [dict(x0=0.5, y0=0.2, x1=0.9, y1=0.8, ads_scale=0.5)], "bindings": [
        b("RMB", "hold", x=0.9, scope=True, extra_triggers=["Q"])], "steps": [
        ["capture", True], ["mouse", "RMB", True, None, 50], ["look", 20, 0, 100], ["press", "Q", 50], ["mouse", "RMB", False, None, 50],
        ["look", 20, 0, 100], ["release", "Q", 50], ["look", 20, 0, 100], ["release_all"]]}
    s["multi-trigger/the-stick-keeps-its-keys-and-combinations-still-work"] = {
        "stick": dict(x=0.15, y=0.7, radius=0.1, ramp_ms=0, sprint_key="Shift"), "bindings": [
            b("R", "hold", x=0.8, y=0.3, extra_triggers=["Shift+W", "Z"])], "steps": [
            ["press", "W", 10], ["press", "R", 10], ["press", "Shift", 10], ["press", "W", 10], ["release", "R", 10], ["release", "W", 10],
            ["press", "W", 10], ["press", "Z", 10], ["release", "Shift", 10], ["release", "W", 10], ["release", "Z", 10]]}
    s["multi-trigger/an-extra-trigger-on-a-stick-key-is-ignored"] = {"stick": dict(x=0.15, y=0.7, ramp_ms=0), "bindings": [
        b("R", "hold", extra_triggers=["W"])], "steps": [["press", "W", 10], ["press", "R", 10], ["release", "W", 10], ["release", "R", 10]]}
    s["multi-trigger/shared-extra-triggers-across-bindings-one-wins"] = {"bindings": [
        b("E", "hold", x=0.1, extra_triggers=["F", "G"]), b("F", "hold", x=0.2), b("R", "hold", x=0.3, extra_triggers=["G", "H"]),
        b("T", "hold", x=0.4, extra_triggers=["H"])], "steps": [
        ["press", "F", 20], ["release", "F", 20], ["press", "G", 20], ["release", "G", 20], ["press", "H", 20], ["release", "H", 20],
        ["press", "E", 20], ["release", "E", 20], ["press", "R", 20], ["release", "R", 20], ["press", "T", 20], ["release", "T", 20]]}
    s["multi-trigger/extra-triggers-and-extra-actions-together"] = {"bindings": [
        b("E", "hold", x=0.3, extra_triggers=["F", "MMB"], extra_actions=[dict(mode="tap", x=0.8, y=0.8, delay_ms=60)])], "steps": [
        ["press", "F", 100], ["release", "F", 100], ["mouse", "MMB", True, [0.5, 0.5], 100], ["mouse", "MMB", False, [0.5, 0.5], 100],
        ["press", "E", 500], ["release", "E", 500]]}
    s["multi-trigger/former-inert-scenario-with-primary-only-bindings-still-identical"] = {"bindings": [b("E", "hold", x=0.3)], "steps": [
        ["press", "F", 100], ["release", "F", 100], ["mouse", "MMB", True, [0.5, 0.5], 100], ["mouse", "MMB", False, [0.5, 0.5], 100],
        ["press", "E", 500], ["release", "E", 500]]}
    s["multi-trigger/busy-session-cleanup"] = {"bindings": [
        b("G", "hold", x=0.1, y=0.9, also=[[0.2, 0.9]], extra_triggers=["H"]), b("T", "toggle", x=0.3, y=0.9, extra_triggers=["Y"]),
        b("F", "repeat", x=0.5, y=0.9, extra_triggers=["J"])], "stick": dict(x=0.15, y=0.7, ramp_ms=100),
        "looks": [dict(x0=0.5, y0=0.2, x1=0.9, y1=0.8)], "steps": [
        ["press", "G"], ["press", "H"], ["press", "T"], ["press", "Y"], ["release", "T"], ["press", "W", 30], ["capture", True],
        ["look", 10, 0, 10], ["press", "F", 100], ["press", "J", 100], ["screen_changed", PORT], ["advance", 2000], ["press", "H"],
        ["press", "Y"], ["press", "J", 100], ["mapping", False], ["advance", 2000], ["mapping", True], ["press", "G", 30],
        ["control", "none"], ["release_all"], ["control", "live"], ["advance", 1000], ["press", "J", 100], ["release", "J", 100]]}
    return s


def multi_action_scenarios():
    """Scenarios for a binding with several actions (Step 6). Their traces were recorded from the multi-action engine and each
    behaviour is asserted explicitly in test_multi_action.py; the traces only guard against later drift."""
    s = {}

    def extra(mode, x=0.5, y=0.5, delay_ms=0, **kw):
        return {"mode": mode, "x": x, "y": y, "delay_ms": delay_ms, **SPECS.get(mode, {}), **kw}

    for mode in SPECS:
        s[f"multi-action/{mode}-as-the-second-action-next-to-a-hold"] = {"bindings": [
            b("E", "hold", x=0.1, y=0.9, extra_actions=[extra(mode)])], "steps": [
            ["press", "E", 300], ["release", "E", 3000], ["press", "E", 20], ["release_all"], ["advance", 800]]}
        s[f"multi-action/{mode}-as-a-delayed-action-released-early"] = {"bindings": [
            b("E", "hold", x=0.1, y=0.9, extra_actions=[extra(mode, delay_ms=100)])], "steps": [
            ["press", "E", 20], ["release", "E", 3000], ["press", "E", 150], ["release", "E", 3000]]}
        s[f"multi-action/{mode}-as-the-first-action-with-a-tap-after-60ms"] = {"bindings": [
            b("E", mode, x=0.1, y=0.9, extra_actions=[extra("tap", 0.5, 0.5, delay_ms=60)])], "steps": [
            ["press", "E", 30], ["release", "E", 3000], ["press", "E", 1000], ["release", "E", 1000]]}
    s["multi-action/hold-and-a-zero-delay-tap"] = {"bindings": [b("L", "hold", x=0.8, y=0.7, extra_actions=[extra("tap", 0.5, 0.2)])], "steps": [
        ["press", "L"], ["advance", 41], ["release", "L", 100]]}
    s["multi-action/three-actions-at-0-60-and-120-ms"] = {"bindings": [
        b("F", "hold", x=0.1, y=0.9, extra_actions=[extra("tap", 0.5, 0.5, delay_ms=60),
                                                   extra("swipe", 0.2, 0.3, delay_ms=120, x2=0.8, y2=0.3, duration_ms=80)])], "steps": [
        ["press", "F", 59], ["advance", 1], ["advance", 59], ["advance", 1], ["advance", 300], ["release", "F", 100]]}
    s["multi-action/delays-are-counted-from-the-activation-not-the-previous-action"] = {"bindings": [
        b("F", "sequence", x=0.1, y=0.1, steps=[[0.1, 0.1, 0], [0.2, 0.2, 400]], extra_actions=[extra("tap", 0.5, 0.5, delay_ms=100)])],
        "steps": [["press", "F", 99], ["advance", 1], ["advance", 2000]]}
    mixed = [b("F", "hold", x=0.1, y=0.9, extra_actions=[extra("toggle", 0.5, 0.9), extra("tap", 0.7, 0.9, delay_ms=60)])]
    s["multi-action/mixed-hold-toggle-delayed-tap-and-reactivations"] = {"bindings": mixed, "steps": [
        ["press", "F", 20], ["release", "F", 500], ["press", "F", 59], ["advance", 1], ["release", "F", 500],
        ["press", "F", 20], ["release", "F", 500], ["press", "F", 5], ["press", "F", 5], ["release", "F", 1000]]}
    s["multi-action/a-pending-action-is-not-started-twice-by-a-new-activation"] = {"bindings": [
        b("F", "hold", x=0.1, y=0.9, extra_actions=[extra("tap", 0.5, 0.5, delay_ms=100), extra("toggle", 0.6, 0.6)])], "steps": [
        ["press", "F", 20], ["release", "F", 10], ["press", "F", 10], ["release", "F", 1000]]}
    s["multi-action/a-repeat-restarted-before-its-old-timer-fires"] = {"bindings": [
        b("F", "tap", x=0.1, y=0.9, extra_actions=[extra("repeat", 0.5, 0.5, rate_hz=10)])], "steps": [
        ["press", "F", 30], ["release", "F", 20], ["press", "F", 970], ["release", "F", 500]]}
    s["multi-action/a-delayed-hold-cancelled-by-releasing-before-its-start"] = {"bindings": [
        b("F", "tap", x=0.1, y=0.9, extra_actions=[extra("hold", 0.5, 0.5, delay_ms=100), extra("drag", 0.3, 0.3, delay_ms=100, x2=0.6, y2=0.3),
                                                  extra("sequence", 0.7, 0.7, delay_ms=100)])], "steps": [
        ["press", "F", 50], ["release", "F", 1000]]}
    s["multi-action/repeat-and-sequence-together"] = {"bindings": [
        b("L", "repeat", x=0.1, y=0.9, rate_hz=10, extra_actions=[extra("sequence", 0.5, 0.5, steps=[[0.2, 0.2, 0], [0.3, 0.3, 300], [0.4, 0.4, 100]])])],
        "steps": [["press", "L", 200], ["release", "L", 3000]]}
    s["multi-action/extra-spots-and-pinch-on-extra-actions"] = {"bindings": [
        b("L", "hold", x=0.1, y=0.5, also=[[0.11, 0.5]], extra_actions=[extra("hold", 0.3, 0.3, also=[[0.31, 0.3]]), extra("pinch-out", 0.6, 0.6)])],
        "steps": [["press", "L", 10], ["release", "L", 500]]}
    s["multi-action/several-triggers-and-several-actions"] = {"bindings": [
        b("E", "hold", x=0.1, y=0.1, extra_triggers=["F", "MMB"], extra_actions=[extra("hold", 0.2, 0.2), extra("tap", 0.3, 0.3, delay_ms=50)])], "steps": [
        ["press", "F", 100], ["press", "E"], ["mouse", "MMB", True, [0.5, 0.5], 10], ["release", "F"], ["mouse", "MMB", False, [0.5, 0.5], 10],
        ["release", "E", 200]]}
    s["multi-action/on-the-wheel-every-action-is-a-one-shot"] = {"bindings": [
        b("E", "hold", x=0.1, y=0.1, extra_triggers=["WheelUp"], extra_actions=[extra("repeat", 0.2, 0.2), extra("drag", 0.3, 0.3, x2=0.6, y2=0.3, duration_ms=40),
                                                                               extra("toggle", 0.8, 0.8), extra("tap", 0.5, 0.5, delay_ms=60)])], "steps": [
        ["wheel", 1, 1000], ["wheel", 1, 1000], ["release_all"], ["advance", 200]]}
    s["multi-action/two-bindings-share-a-first-input-the-last-one-wins-with-its-actions"] = {"raw_bindings": True, "bindings": [
        b("R", "hold", x=0.1, y=0.1, extra_actions=[extra("tap", 0.2, 0.2)]), b("R", "hold", x=0.9, y=0.9, extra_actions=[extra("tap", 0.8, 0.8)])],
        "steps": [["press", "R", 100], ["release", "R", 100]]}
    for name, tail in (("release_all", [["release_all"]]), ("mapping-off", [["mapping", False]]), ("rotation", [["screen_changed", PORT]]),
                       ("disconnect", [["control", "none"], ["release_all"], ["control", "live"]])):
        s[f"multi-action/cleanup-{name}-with-pending-and-active-actions"] = {"bindings": [
            b("F", "hold", x=0.1, y=0.9, extra_actions=[extra("tap", 0.5, 0.5, delay_ms=200), extra("repeat", 0.6, 0.6, delay_ms=300, rate_hz=10),
                                                       extra("toggle", 0.7, 0.7), extra("swipe", 0.2, 0.2, x2=0.9, y2=0.2, delay_ms=400),
                                                       extra("hold", 0.4, 0.4, also=[[0.41, 0.4]])])], "steps": [
            ["press", "F", 100], *tail, ["advance", 5000], ["release", "F", 100], ["press", "F", 100], ["release", "F", 1000]]}
    fill8 = [b(k, "hold", x=0.05 + 0.09 * i, y=0.95) for i, k in enumerate("ABCDEFGH")]
    fill9 = fill8 + [b("I", "hold", x=0.9, y=0.95)]
    for policy in ("continue", "abort"):
        s[f"multi-action/pointer-exhaustion-on-failure-{policy}"] = {"bindings": fill8 + [
            b("L", "hold", x=0.8, y=0.7, on_failure=policy, extra_actions=[extra("hold", 0.5, 0.2), extra("hold", 0.3, 0.2), extra("tap", 0.1, 0.1, delay_ms=100)])],
            "steps": [*[["press", k, 5] for k in "ABCDEFGH"], ["press", "L", 300], ["release", "L", 100], ["release", "A", 5], ["press", "L", 300],
                      ["release", "L", 100]]}
        s[f"multi-action/a-delayed-action-fails-on-failure-{policy}"] = {"bindings": fill9 + [
            b("L", "hold", x=0.8, y=0.7, on_failure=policy, extra_actions=[extra("hold", 0.5, 0.2, delay_ms=100)])],
            "steps": [["press", "L", 10], *[["press", k, 5] for k in "ABCDEFGHI"], ["advance", 200], ["release", "L", 100]]}
        s[f"multi-action/disconnected-control-on-failure-{policy}"] = {"bindings": [
            b("L", "hold", x=0.8, y=0.7, on_failure=policy, extra_actions=[extra("hold", 0.5, 0.2), extra("tap", 0.3, 0.2, delay_ms=50)])],
            "steps": [["control", "none"], ["press", "L", 200], ["control", "live"], ["release", "L"], ["press", "L", 100], ["release", "L", 100]]}
    s["multi-action/abort-lifts-a-running-repeat-and-a-swipe-in-flight"] = {"bindings": fill9[:9] + [
        b("L", "repeat", x=0.5, y=0.5, rate_hz=10, on_failure="abort", extra_actions=[extra("hold", 0.3, 0.3, delay_ms=100)])], "steps": [
        *[["press", k, 5] for k in "ABCDEFGHI"], ["press", "L", 250], ["advance", 3000], ["release", "L", 100]]}
    s["multi-action/all-actions-fail-then-the-binding-works-again"] = {"bindings": [b(k, "hold", x=0.05 + 0.09 * i, y=0.95) for i, k in enumerate("ABCDEFGHIJ")] + [
        b("L", "hold", x=0.8, y=0.7, extra_actions=[extra("hold", 0.5, 0.2), extra("tap", 0.3, 0.2)])], "steps": [
        *[["press", k, 5] for k in "ABCDEFGHIJ"], ["press", "L", 100], ["release", "A", 5], ["press", "L", 100], ["release", "L", 100]]}
    scope = dict(x0=0.5, y0=0.2, x1=0.9, y1=0.8, ads_scale=0.5)
    s["multi-action/two-scope-holds-released-in-both-orders"] = {"looks": [scope], "bindings": [
        b("RMB", "hold", x=0.9, y=0.5, scope=True), b("Z", "hold", x=0.8, y=0.4, scope=True)], "steps": [
        ["capture", True], ["look", 20, 0, 300], ["mouse", "RMB", True, None, 10], ["press", "Z", 10], ["look", 20, 0, 300], ["release", "Z", 10],
        ["look", 20, 0, 300], ["mouse", "RMB", False, None, 10], ["look", 20, 0, 300],
        ["press", "Z", 10], ["mouse", "RMB", True, None, 10], ["mouse", "RMB", False, None, 10], ["look", 20, 0, 300], ["release", "Z", 10], ["look", 20, 0, 300]]}
    s["multi-action/toggle-scope-and-hold-scope-and-a-tap-latch"] = {"looks": [scope], "bindings": [
        b("T", "toggle", x=0.9, y=0.5, scope=True), b("Z", "hold", x=0.8, y=0.4, scope=True), b("Q", "tap", x=0.7, y=0.4, scope=True)], "steps": [
        ["capture", True], ["press", "T", 10], ["release", "T", 10], ["press", "Z", 10], ["release", "Z", 10], ["look", 20, 0, 300],
        ["press", "Q", 100], ["release", "Q", 10], ["look", 20, 0, 300], ["press", "T", 10], ["release", "T", 10], ["look", 20, 0, 300],
        ["press", "Z", 10], ["press", "Q", 100], ["release", "Q", 10], ["look", 20, 0, 300], ["release", "Z", 10], ["look", 20, 0, 300], ["release_all"]]}
    s["multi-action/scope-on-extra-actions-follows-each-action"] = {"looks": [scope], "bindings": [
        b("L", "hold", x=0.1, y=0.9, extra_actions=[extra("hold", 0.5, 0.5, scope=True), extra("toggle", 0.6, 0.6, scope=True)])], "steps": [
        ["capture", True], ["press", "L", 10], ["look", 20, 0, 300], ["release", "L", 10], ["look", 20, 0, 300], ["press", "L", 10], ["look", 20, 0, 300],
        ["release", "L", 10], ["look", 20, 0, 300]]}
    s["multi-action/a-failed-scope-repeat-never-leaves-scope-on"] = {"looks": [scope], "bindings": [
        b(k, "hold", x=0.05 + 0.09 * i, y=0.95) for i, k in enumerate("ABCDEFGHIJ")] + [b("Z", "repeat", x=0.8, y=0.4, scope=True)], "steps": [
        ["capture", True], *[["press", k, 5] for k in "ABCDEFGHIJ"], ["press", "Z", 100], ["look", 20, 0, 300]]}
    return s


def shared_binding_scenarios():
    """Scenarios for SEPARATE bindings that share a trigger (Step 7). Their traces were recorded from the engine that runs authorized
    sharing groups and each behaviour is asserted explicitly in test_shared_runtime.py; the traces guard against later drift.
    Bindings are listed in binding-list order and given ids so a group can name them."""
    s = {}

    def extra(mode, x=0.5, y=0.5, delay_ms=0, **kw):
        return {"mode": mode, "x": x, "y": y, "delay_ms": delay_ms, **SPECS.get(mode, {}), **kw}

    def shared(bindings, trigger="F", ids=None, **more):
        return {"raw_bindings": True, "bindings": bindings, "share": [{"trigger": trigger, "ids": ids or [x["id"] for x in bindings]}], **more}

    for mode in SPECS:
        pair = [b("F", mode, x=0.1, y=0.9, id="a"), b("F", "hold", x=0.9, y=0.9, id="b")]
        s[f"shared-binding/{mode}-next-to-a-hold/held-then-released"] = shared(pair, steps=[["press", "F", 300], ["release", "F", 3000]])
        s[f"shared-binding/{mode}-next-to-a-hold/released-at-once"] = shared(pair, steps=[["press", "F"], ["release", "F", 3000], ["press", "F", 400], ["release", "F", 1000]])
        s[f"shared-binding/{mode}-next-to-a-hold/release_all-after-20ms"] = shared(pair, steps=[["press", "F", 20], ["release_all"], ["advance", 3000], ["release", "F", 100]])
    trio = [b("F", "hold", x=0.1, y=0.1, id="a"), b("F", "tap", x=0.5, y=0.5, id="b"), b("F", "toggle", x=0.9, y=0.9, id="c")]
    s["shared-binding/three-bindings-hold-tap-toggle"] = shared(trio, steps=[
        ["press", "F", 100], ["release", "F", 100], ["press", "F", 100], ["release", "F", 100], ["press", "F", 100], ["release", "F", 100]])
    s["shared-binding/three-bindings-group-written-in-another-order"] = shared(trio, ids=["c", "a", "b"], steps=[["press", "F", 100], ["release", "F", 500]])
    ef = [b("E", "hold", x=0.1, y=0.5, id="a", extra_triggers=["F"]), b("F", "hold", x=0.9, y=0.5, id="b")]
    for name, steps in (("e-then-f-released-f-first", [["press", "E"], ["press", "F"], ["release", "F"], ["release", "E", 100]]),
                        ("e-then-f-released-e-first", [["press", "E"], ["press", "F"], ["release", "E"], ["release", "F", 100]]),
                        ("f-then-e-released-e-first", [["press", "F"], ["press", "E"], ["release", "E"], ["release", "F", 100]]),
                        ("f-then-e-released-f-first", [["press", "F"], ["press", "E"], ["release", "F"], ["release", "E", 100]]),
                        ("repeated-key-downs", [["press", "E"], ["press", "F"], ["press", "F"], ["press", "E"], ["release", "F"], ["release", "E", 100]])):
        s[f"shared-binding/a-with-e-and-f-b-with-f/{name}"] = shared(ef, steps=steps)
    s["shared-binding/combination-group-next-to-a-plain-key"] = shared(
        [b("Shift+F", "hold", x=0.1, y=0.1, id="a"), b("Shift+F", "tap", x=0.9, y=0.9, id="b"), b("F", "hold", x=0.5, y=0.5, id="c")],
        trigger="Shift+F", ids=["a", "b"], steps=[["press", "F", 100], ["release", "F", 100], ["press", "Shift"], ["press", "F", 100],
                                                 ["release", "Shift", 100], ["release", "F", 100], ["press", "Shift"], ["press", "F", 100], ["release", "F", 100], ["release", "Shift"]])
    s["shared-binding/mapped-mouse-buttons"] = shared(
        [b("LMB", "hold", x=0.1, y=0.1, id="a"), b("E", "hold", x=0.9, y=0.9, id="b", extra_triggers=["LMB"])], trigger="LMB",
        steps=[["mouse", "LMB", True, [0.5, 0.5], 100], ["mouse", "LMB", False, [0.5, 0.5], 100], ["mouse", "LMB", True, [0.5, 0.5], 10], ["release_all"], ["advance", 100]])
    s["shared-binding/wheel-impulses"] = shared(
        [b("WheelUp", "hold", x=0.1, y=0.1, id="a"), b("WheelUp", "repeat", x=0.9, y=0.9, id="b"), b("WheelUp", "drag", x=0.5, y=0.5, id="c", x2=0.8, y2=0.5)],
        trigger="WheelUp", steps=[["wheel", 1, 1000], ["wheel", 1, 10], ["wheel", 1, 1000], ["wheel", -1, 100]])
    delayed = [b("F", "tap", x=0.1, y=0.1, id="a", extra_actions=[extra("tap", 0.2, 0.2, 100), extra("tap", 0.3, 0.3, 100)]),
               b("F", "tap", x=0.9, y=0.9, id="b", extra_actions=[extra("tap", 0.8, 0.8, 100), extra("tap", 0.7, 0.7, 60)])]
    s["shared-binding/delayed-actions-with-equal-deadlines"] = shared(delayed, steps=[["press", "F", 300], ["release", "F", 300]])
    s["shared-binding/delayed-actions-cancelled-before-the-deadline"] = shared(delayed, steps=[["press", "F", 50], ["release_all"], ["advance", 1000], ["release", "F", 100]])
    s["shared-binding/delayed-sustained-action-cancelled-by-release-while-the-other-binding-keeps-its-tap"] = shared(
        [b("F", "tap", x=0.1, y=0.1, id="a", extra_actions=[extra("hold", 0.2, 0.2, 100)]),
         b("F", "tap", x=0.9, y=0.9, id="b", extra_actions=[extra("tap", 0.8, 0.8, 100)])],
        steps=[["press", "F", 50], ["release", "F", 1000], ["press", "F", 150], ["release", "F", 500]])
    fill9 = [b(k, "hold", x=0.05 + 0.09 * i, y=0.95, id=f"f{i}") for i, k in enumerate("123456789")]
    fill8 = fill9[:8]
    s["shared-binding/a-binding-that-cannot-start-does-not-stop-the-others"] = {
        "raw_bindings": True, "bindings": [b("F", "pinch-out", x=0.1, y=0.1, id="a"), b("F", "hold", x=0.9, y=0.9, id="b")] + fill9,
        "share": [{"trigger": "F", "ids": ["a", "b"]}], "steps": [*[["press", k, 5] for k in "123456789"], ["press", "F", 200], ["release", "F", 100]]}
    s["shared-binding/pointer-exhaustion-later-participants-fail-in-order"] = {
        "raw_bindings": True, "bindings": [b("F", "hold", x=0.1 * (i + 1), y=0.2, id=f"p{i}") for i in range(4)] + fill8,
        "share": [{"trigger": "F", "ids": [f"p{i}" for i in range(4)]}], "steps": [*[["press", k, 5] for k in "12345678"], ["press", "F", 100], ["release", "F", 100]]}
    s["shared-binding/abort-in-one-binding-only"] = {
        "raw_bindings": True, "bindings": [b("F", "hold", x=0.1, y=0.1, id="a"),
                                           b("F", "hold", x=0.5, y=0.5, id="b", on_failure="abort", extra_actions=[extra("hold", 0.6, 0.6), extra("hold", 0.7, 0.7)]),
                                           b("F", "hold", x=0.9, y=0.9, id="c")] + fill9[:7],
        "share": [{"trigger": "F", "ids": ["a", "b", "c"]}], "steps": [*[["press", k, 5] for k in "1234567"], ["press", "F", 100], ["release", "F", 100]]}
    s["shared-binding/continue-in-one-binding-abort-in-another"] = {
        "raw_bindings": True, "bindings": [b("F", "hold", x=0.1, y=0.1, id="a", extra_actions=[extra("hold", 0.2, 0.2), extra("hold", 0.3, 0.3)]),
                                           b("F", "hold", x=0.9, y=0.9, id="b", on_failure="abort")] + fill8,
        "share": [{"trigger": "F", "ids": ["a", "b"]}], "steps": [*[["press", k, 5] for k in "12345678"], ["press", "F", 100], ["release", "F", 100]]}
    s["shared-binding/disconnected-control-fails-every-participant-then-recovers"] = shared(
        [b("F", "hold", x=0.1, y=0.1, id="a"), b("F", "hold", x=0.9, y=0.9, id="b", on_failure="abort")],
        steps=[["control", "none"], ["press", "F", 100], ["control", "live"], ["release", "F"], ["press", "F", 100], ["release", "F", 100]])
    cleanup = [b("F", "hold", x=0.1, y=0.9, id="a", extra_actions=[extra("toggle", 0.5, 0.9), extra("tap", 0.7, 0.9, delay_ms=200)]),
               b("F", "repeat", x=0.9, y=0.9, id="b"), b("F", "drag", x=0.4, y=0.4, id="c", x2=0.8, y2=0.4, duration_ms=400)]
    for name, tail in (("release_all", [["release_all"]]), ("mapping-off", [["mapping", False]]), ("rotation", [["screen_changed", PORT]]),
                       ("disconnect", [["control", "none"], ["release_all"], ["control", "live"]])):
        s[f"shared-binding/cleanup-{name}"] = shared(cleanup, steps=[["press", "F", 100], *tail, ["advance", 3000], ["release", "F", 100], ["press", "F", 100], ["release", "F", 1000]])
    s["shared-binding/scope-from-two-shared-bindings"] = shared(
        [b("F", "hold", x=0.1, y=0.1, id="a", scope=True), b("F", "toggle", x=0.9, y=0.9, id="b", scope=True)], looks=[dict(x0=0.5, y0=0.2, x1=0.9, y1=0.8, ads_scale=0.5)],
        steps=[["capture", True], ["press", "F", 10], ["look", 20, 0, 300], ["release", "F", 10], ["look", 20, 0, 300], ["press", "F", 10], ["look", 20, 0, 300], ["release", "F", 10], ["look", 20, 0, 300]])
    # NOT authorized: the old rule, unchanged
    dup = [b("F", "hold", x=0.1, y=0.1, id="a"), b("F", "hold", x=0.9, y=0.9, id="b")]
    s["shared-binding/unauthorized-duplicates-keep-the-winner"] = {"raw_bindings": True, "bindings": dup, "steps": [["press", "F", 100], ["release", "F", 100]]}
    s["shared-binding/partially-authorized-the-third-binding-never-runs"] = shared(
        [b("F", "hold", x=0.1, y=0.1, id="a"), b("F", "hold", x=0.2, y=0.2, id="b"), b("F", "hold", x=0.9, y=0.9, id="c")], ids=["a", "b"],
        steps=[["press", "F", 100], ["release", "F", 100]])
    s["shared-binding/a-group-names-a-binding-that-does-not-use-the-trigger"] = {
        "raw_bindings": True, "bindings": [b("F", "hold", x=0.1, y=0.1, id="a"), b("G", "hold", x=0.9, y=0.9, id="b")], "steps": [["press", "F", 100], ["release", "F", 100]]}
    return s


def record_all():
    return {name: run(spec) for name, spec in scenarios().items()}


if __name__ == "__main__":
    traces = record_all()
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(json.dumps(traces, indent=0, sort_keys=True), encoding="utf-8")
    total = sum(len(t["messages"]) for t in traces.values())
    print(f"wrote {len(traces)} scenarios, {total} control messages to {GOLDEN}")
