"""Qt-free logic of the drag-and-drop layout editor: what is under the pointer, how dragging changes it, and the
palette of ready-made controls (fire, scope, reload, ... and the WASD stick / mouse-look area)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from .binding_edit import users_of
from .profile import ALSO_MODES, DIRECTIONS, MAX_ALSO, TWO_POINT_MODES, Binding, Look, Profile, Stick

TAP_TOLERANCE_PX = 22
NODE_TOLERANCE_PX = 16
CORNER_TOLERANCE_PX = 14
MIN_STICK_RADIUS = 0.02
MAX_STICK_RADIUS = 0.40
STICK_NODE_ORDER = DIRECTIONS
DEFAULT_STEP_DELAY_MS = 250             # pause between the points of a multi-tap
PICKUP_STEP_DELAY_MS = 220              # pause between the taps of "pick up several items" (a loot list moves up after each pick-up)
PICKUP_MIN, PICKUP_MAX, PICKUP_DEFAULT = 2, 10, 3

# name -> (input, mode, label). Wheel inputs fire a quick tap per notch of the mouse wheel.
PRESETS: dict[str, tuple[str, str, str]] = {
    "fire": ("LMB", "hold", "Fire"),
    "scope": ("RMB", "tap", "Scope"),
    "reload": ("R", "tap", "Reload"),
    "crouch": ("C", "tap", "Crouch"),
    "jump": ("Space", "tap", "Jump"),
    "sprint": ("Shift", "tap", "Sprint"),
    "next weapon": ("WheelDown", "tap", "Next weapon"),
    "previous weapon": ("WheelUp", "tap", "Previous weapon"),
}


@dataclass(frozen=True)
class Hit:
    kind: str          # "tap" | "stick" | "look"
    index: int         # position in profile.bindings / sticks / looks WHEN the hit was made
    part: str = ""     # tap: "" | end | stepN | alsoN ; stick: center | up | left | down | right ; look: move | tl | tr | bl | br
                       # an ADDITIONAL action of a tap is named by its identity: act:<uid> | act:<uid>/end | act:<uid>/stepN | act:<uid>/alsoN
    binding_id: str = field(default="", compare=False)   # tap: the binding's runtime id, so the hit still means THAT binding
                                                         # after the list changed (see pin / tap_position); "" = go by position


def _dist_px(ax: float, ay: float, bx: float, by: float, tw: float, th: float) -> float:
    return math.hypot((ax - bx) * tw, (ay - by) * th)


def split_part(part: str) -> tuple[str | None, str]:
    """('uid' | None, what is meant inside that action): an `act:<uid>/alsoN` part is about an additional action, anything else is
    about the binding's own first action."""
    if part.startswith("act:"):
        uid, _, rest = part[4:].partition("/")
        return uid, rest
    return None, part


def action_part(uid: str, rest: str = "") -> str:
    return f"act:{uid}" + (f"/{rest}" if rest else "")


def step_index(part: str) -> int | None:
    part = split_part(part)[1]
    return int(part[4:]) if part.startswith("step") and part[4:].isdigit() else None


def also_index(part: str) -> int | None:
    part = split_part(part)[1]
    return int(part[4:]) if part.startswith("also") and part[4:].isdigit() else None


def _spec_part(spec, nx: float, ny: float, tw: float, th: float) -> str | None:
    """What of ONE action (the binding's own fields, or an Action) is at the pointer: "alsoN" | "end" | "stepN" | "" (its spot) | None."""
    if spec.mode in ALSO_MODES:
        for k in range(len(spec.also) - 1, -1, -1):
            ax, ay = spec.also[k]
            if _dist_px(nx, ny, ax, ay, tw, th) <= TAP_TOLERANCE_PX:
                return f"also{k}"
    if spec.mode in TWO_POINT_MODES and spec.x2 is not None and _dist_px(nx, ny, spec.x2, spec.y2, tw, th) <= TAP_TOLERANCE_PX:
        return "end"
    if spec.mode == "sequence":
        for k in range(len(spec.steps) - 1, 0, -1):
            sx, sy, _ = spec.steps[k]
            if _dist_px(nx, ny, sx, sy, tw, th) <= TAP_TOLERANCE_PX:
                return f"step{k}"
    if _dist_px(nx, ny, spec.x, spec.y, tw, th) <= TAP_TOLERANCE_PX:
        return ""
    return None


def _target(profile: Profile, hit: Hit):
    """(binding, spec, what) for a tap hit, found by IDENTITY: the binding by its id (when the hit knows it), the action by its uid.
    `spec` is the binding itself for its own first action. None if the binding or the action is gone (nothing is guessed)."""
    index = tap_position(profile, hit)
    if index is None:
        return None
    binding = profile.bindings[index]
    uid, rest = split_part(hit.part)
    if uid is None:
        return binding, binding, rest
    found = [a for a in binding.extra_actions if a.uid == uid]
    return (binding, found[0], rest) if len(found) == 1 else None


def hit_test(profile: Profile, nx: float, ny: float, tw: float, th: float) -> Hit | None:
    """What is at normalized position (nx, ny)? `tw`/`th` = size in pixels of the picture, for tolerances."""
    for i in reversed(range(len(profile.bindings))):
        b = profile.bindings[i]
        for action in reversed(b.extra_actions):                # an additional action's markers are on top of the first action's
            part = _spec_part(action, nx, ny, tw, th)
            if part is not None:
                return Hit("tap", i, action_part(action.uid, part), b.id)
        part = _spec_part(b, nx, ny, tw, th)
        if part is not None:
            return Hit("tap", i, part, b.id)
    for i, stick in enumerate(profile.sticks):
        r_px = stick.radius * min(tw, th)
        offsets = {"up": (0.0, -r_px / th), "down": (0.0, r_px / th), "left": (-r_px / tw, 0.0), "right": (r_px / tw, 0.0)}
        for part in STICK_NODE_ORDER:
            ox, oy = offsets[part]
            if _dist_px(nx, ny, stick.x + ox, stick.y + oy, tw, th) <= NODE_TOLERANCE_PX:
                return Hit("stick", i, part)
        if _dist_px(nx, ny, stick.x, stick.y, tw, th) <= TAP_TOLERANCE_PX:
            return Hit("stick", i, "center")
    for i, look in enumerate(profile.looks):
        corners = {"tl": (look.x0, look.y0), "tr": (look.x1, look.y0), "bl": (look.x0, look.y1), "br": (look.x1, look.y1)}
        for part, (cx, cy) in corners.items():
            if _dist_px(nx, ny, cx, cy, tw, th) <= CORNER_TOLERANCE_PX:
                return Hit("look", i, part)
    for i, stick in enumerate(profile.sticks):
        if _dist_px(nx, ny, stick.x, stick.y, tw, th) <= stick.radius * min(tw, th):
            return Hit("stick", i, "center")                    # grab the ring's inside to move the whole stick
    for i, look in enumerate(profile.looks):
        if look.x0 <= nx <= look.x1 and look.y0 <= ny <= look.y1:
            return Hit("look", i, "move")
    return None


@dataclass
class DragState:
    hit: Hit
    grab_dx: float = 0.0               # pointer offset from the object's anchor, so it does not jump
    grab_dy: float = 0.0
    origin: tuple = ()                 # what the object looked like when the drag began


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def begin_drag(profile: Profile, hit: Hit, nx: float, ny: float) -> DragState:
    if hit.kind == "tap":
        found = _target(profile, hit)
        if found is None:                                       # the control or action is gone: a drag that does nothing
            return DragState(hit)
        _b, spec, rest = found
        if rest == "end":
            return DragState(hit, spec.x2 - nx, spec.y2 - ny)
        k = step_index(rest)
        if k is not None:
            return DragState(hit, spec.steps[k][0] - nx, spec.steps[k][1] - ny)
        a = also_index(rest)
        if a is not None:
            return DragState(hit, spec.also[a][0] - nx, spec.also[a][1] - ny)
        return DragState(hit, spec.x - nx, spec.y - ny, (spec.x, spec.y, spec.x2, spec.y2, list(spec.steps), list(spec.also)))
    if hit.kind == "stick":
        s = profile.sticks[hit.index]
        return DragState(hit, s.x - nx, s.y - ny)
    look = profile.looks[hit.index]
    return DragState(hit, 0.0, 0.0, (look.x0, look.y0, look.x1, look.y1, nx, ny))


def update_drag(profile: Profile, state: DragState, nx: float, ny: float, tw: float, th: float) -> None:
    hit = state.hit
    if hit.kind == "tap":
        found = _target(profile, hit)
        if found is None:
            return
        _b, b, rest = found                                     # `b` = the binding itself or the additional Action: same field names
        px, py = _clamp01(nx + state.grab_dx), _clamp01(ny + state.grab_dy)
        k = step_index(rest)
        a = also_index(rest)
        if rest == "end":
            b.x2, b.y2 = px, py
        elif k is not None:
            b.steps[k] = (px, py, b.steps[k][2])
        elif a is not None:
            b.also[a] = (px, py)
        elif state.origin:                                      # the whole action moves, with its second point(s)
            ox, oy, ox2, oy2, osteps, oalso = state.origin
            dx, dy = px - ox, py - oy
            b.x, b.y = px, py
            if ox2 is not None:
                b.x2, b.y2 = _clamp01(ox2 + dx), _clamp01(oy2 + dy)
            if osteps:
                b.steps = [(_clamp01(sx + dx), _clamp01(sy + dy), delay) for sx, sy, delay in osteps]
            if oalso:
                b.also = [(_clamp01(ax + dx), _clamp01(ay + dy)) for ax, ay in oalso]
    elif hit.kind == "stick":
        s = profile.sticks[hit.index]
        if hit.part == "center":
            s.x, s.y = _clamp01(nx + state.grab_dx), _clamp01(ny + state.grab_dy)
        else:                                                       # dragging a key node resizes the ring
            s.radius = max(MIN_STICK_RADIUS, min(MAX_STICK_RADIUS,
                                                 _dist_px(nx, ny, s.x, s.y, tw, th) / min(tw, th)))
    else:
        look = profile.looks[hit.index]
        x0, y0, x1, y1, sx, sy = state.origin
        gap = Look.MIN_SIZE
        if hit.part == "move":
            dx, dy = nx - sx, ny - sy
            dx = max(-x0, min(1.0 - x1, dx))
            dy = max(-y0, min(1.0 - y1, dy))
            look.x0, look.x1, look.y0, look.y1 = x0 + dx, x1 + dx, y0 + dy, y1 + dy
        else:
            nx, ny = _clamp01(nx), _clamp01(ny)
            if "l" in hit.part:
                look.x0 = min(nx, look.x1 - gap)
            else:
                look.x1 = max(nx, look.x0 + gap)
            if "t" in hit.part:
                look.y0 = min(ny, look.y1 - gap)
            else:
                look.y1 = max(ny, look.y0 + gap)


# ---------------------------------------------------------------- palette
def free_spot(profile: Profile, x: float = 0.40, y: float = 0.62, step: float = 0.045) -> tuple[float, float]:
    """A position near (x, y) that does not sit on top of an existing marker."""
    taken = [(b.x, b.y) for b in profile.bindings] + [(s.x, s.y) for s in profile.sticks]
    for i in range(40):
        cx, cy = _clamp01(x + (i % 8) * step), _clamp01(y + (i // 8) * step)
        if all(abs(cx - tx) > 0.03 or abs(cy - ty) > 0.04 for tx, ty in taken):
            return cx, cy
    return x, y


def add_preset(profile: Profile, kind: str) -> Hit | None:
    """Add a ready-made tap control. Returns None if its default key is already used by something else."""
    key, mode, label = PRESETS[kind]
    if profile.key_owner(key) or users_of(profile, key):              # (also a key that is only another control's additional trigger)
        return None
    x, y = free_spot(profile)
    profile.add(Binding(key, x, y, mode, label, scope=(kind == "scope")))
    return Hit("tap", len(profile.bindings) - 1, "", profile.bindings[-1].id)


def add_stick(profile: Profile) -> tuple[Hit, list[str]]:
    removed = profile.set_stick(Stick(0.14, 0.70))
    return Hit("stick", 0, "center"), removed


def add_look(profile: Profile) -> Hit:
    profile.looks = [Look(0.40, 0.15, 0.65, 0.50)]
    return Hit("look", 0, "move")


def pickup_binding(name: str, spot: tuple[float, float], count: int, step_delay_ms: int = PICKUP_STEP_DELAY_MS) -> Binding:
    """"Pick up several items" with ONE key: it taps the same spot `count` times, one after the other. The spot is the FIRST row of a game's
    loot list: after an item is picked up the list moves up, so the next tap picks the next item. It is an ordinary multi-tap (mode
    "sequence"), so it can be edited in Options like any other: number of taps, spots, waits."""
    count = max(PICKUP_MIN, min(PICKUP_MAX, int(count)))
    return binding_from_points(name, "sequence", [spot] * count, f"Pick up x{count}", step_delay_ms)


def binding_from_points(name: str, mode: str, points: list[tuple[float, float]], label: str = "",
                        step_delay_ms: int = DEFAULT_STEP_DELAY_MS) -> Binding:
    """Builds a binding from the spots the user clicked, for the modes that need more than one."""
    x, y = points[0]
    if mode in TWO_POINT_MODES:
        x2, y2 = points[1] if len(points) > 1 else (None, None)
        return Binding(name, x, y, mode, label, x2=x2, y2=y2)
    if mode == "sequence":
        steps = [(px, py, 0 if i == 0 else step_delay_ms) for i, (px, py) in enumerate(points)]
        return Binding(name, x, y, mode, label, steps=steps)
    return Binding(name, x, y, mode, label)


def add_also(profile: Profile, index: int, spot: tuple[float, float], uid: str | None = None) -> bool:
    """Make the key of binding `index` touch one more spot at the same time (as an extra spot of its first action, or of the additional
    action `uid`). False if its mode cannot do that."""
    if not (0 <= index < len(profile.bindings)):
        return False
    b = profile.bindings[index]
    if uid is not None:
        found = [a for a in b.extra_actions if a.uid == uid]
        if len(found) != 1:
            return False
        b = found[0]
    if b.mode not in ALSO_MODES or len(b.also) >= MAX_ALSO:
        return False
    b.also.append((_clamp01(spot[0]), _clamp01(spot[1])))
    return True


def pin(profile: Profile, hit: Hit | None) -> Hit | None:
    """Make a tap hit remember WHICH binding it is (its id), at the moment the list is known to be right. A hit that already
    knows, or is not about a binding, or points outside the list, is returned as it is."""
    if hit is not None and hit.kind == "tap" and not hit.binding_id and 0 <= hit.index < len(profile.bindings):
        return replace(hit, binding_id=profile.bindings[hit.index].id)
    return hit


def resolve(profile: Profile, hit: Hit):
    """(binding, spec, what) for a tap hit, by identity: `spec` is the binding itself (its first action) or the additional Action the hit
    is about, `what` what of it ("", "end", "stepN", "alsoN"). None if the binding or the action is gone."""
    return _target(profile, hit) if hit.kind == "tap" else None


def tap_position(profile: Profile, hit: Hit) -> int | None:
    """Where the binding of a tap hit is in the list NOW. A pinned hit is found by its id wherever the list has moved it; None if
    that id is gone or more than one binding has it (never guess). An unpinned hit means its position, if that exists."""
    if hit.kind != "tap":
        return None
    if hit.binding_id:
        found = [i for i, b in enumerate(profile.bindings) if b.id == hit.binding_id]
        return found[0] if len(found) == 1 else None
    return hit.index if 0 <= hit.index < len(profile.bindings) else None


def delete(profile: Profile, hit: Hit) -> bool:
    """Delete what the hit points at: a whole control, just one of its extra spots or multi-tap points, the stick, or the look
    area. A control is found by identity when the hit knows it (see pin), so deleting never takes a different binding that now sits
    at the old position, nor one that shares an input. Returns False, changing nothing, if the target cannot be identified."""
    if hit.kind == "tap":
        found = _target(profile, hit)
        if found is None:
            return False
        b, spec, rest = found
        a = also_index(rest)
        k = step_index(rest)
        if a is not None:
            if a >= len(spec.also):
                return False                                    # that extra spot is gone: never fall through to deleting the control
            del spec.also[a]                                    # just that extra spot; the control stays
        elif k is not None and k >= len(spec.steps):
            return False                                        # that point is gone: never fall through to deleting the control
        elif k is not None and len(spec.steps) > 2:
            del spec.steps[k]                                   # removing one point of a multi-tap keeps the rest
        elif spec is not b:
            b.extra_actions = [x for x in b.extra_actions if x is not spec]       # one ADDITIONAL action goes; the control and the rest stay
        else:                                                   # the control's own marker: the whole control (to drop only its first
                                                                # action use binding_edit.ProfileEditor.remove_action)
            del profile.bindings[tap_position(profile, hit)]
            profile.prune_sharing()                              # a sharing group it belonged to loses it
        return True
    if hit.kind == "stick" and 0 <= hit.index < len(profile.sticks):
        del profile.sticks[hit.index]
        return True
    if hit.kind == "look" and 0 <= hit.index < len(profile.looks):
        del profile.looks[hit.index]
        return True
    return False


def remove_binding(profile: Profile, binding_id: str) -> Binding | None:
    """Remove the ONE binding with this id and return it. If no binding has the id, or several do, nothing is removed (None):
    an identity that is not unambiguous is never guessed at. Other bindings that share its inputs are untouched."""
    if not binding_id or sum(1 for b in profile.bindings if b.id == binding_id) != 1:
        return None
    return profile.remove_by_id(binding_id)
