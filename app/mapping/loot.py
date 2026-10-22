"""Smart pick-up for a game's loot list (PUBG Mobile).

In PUBG Mobile a hand icon appears when items are nearby. Tapping it opens a list of the nearby items (solid YELLOW rows on the right);
the same button, now an "X", closes it. After an item is taken the list moves up, so tapping the FIRST row again takes the next one.

One key does all of that, and only what is needed. When it is pressed the app LOOKS at the phone picture:
  * the list is already open  -> tap the first row `count` times (one item per tap);
  * the list is closed        -> tap the hand/X button, wait for the list to appear, check again, and only then tap the rows. If no list
                                 appears (nothing is nearby) nothing else is tapped.
An optional second key closes the list (taps the button only if the list is open).

"Open" is recognised by how much of a box around the first rows is loot-list yellow. The threshold is measured from the picture when
the control is set up (with the list open), so it fits the phone's colours. This module is Qt-free; the window does the tapping."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .profile import ProfileError, canonical_trigger

COUNT_RANGE = (0, 10)                # 0 = the key only opens the list; the pick keys choose the items
THRESHOLD_RANGE = (0.05, 0.90)
WAIT_RANGE_MS = (100, 1500)
STEP_RANGE_MS = (100, 1000)
ROW_GAP_RANGE = (0.04, 0.25)
MAX_PICK_KEYS = 6
DEFAULT_ROW_GAP = 0.098             # measured on PUBG Mobile: rows at about 0.352 / 0.449 / 0.547
SCROLL_ROWS = 2.0                   # one wheel notch scrolls the list by this many rows
SCROLL_MS = 200
VISIBLE_MIN_SHARE = 0.15            # at set-up the list must cover at least this much of the box, or it is not really on screen
BOX_HALF_WIDTH = 0.065              # the box around the first rows, from the first row's x
BOX_ABOVE, BOX_BELOW = 0.04, 0.04   # ... and its vertical extent: ONLY the first row (about 0.09 tall), so one item or ten look the same


def default_box(row: tuple[float, float]) -> tuple[float, float, float, float]:
    x, y = row
    return (max(0.0, x - BOX_HALF_WIDTH), max(0.0, y - BOX_ABOVE), min(1.0, x + BOX_HALF_WIDTH), min(1.0, y + BOX_BELOW))


def _unit(value, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"Smart pick-up: {what} must be a number") from exc
    if not 0.0 <= number <= 1.0:
        raise ProfileError(f"Smart pick-up: {what} must be between 0 and 1")
    return number


def _in(value, low, high, what: str, cast=float):
    try:
        number = cast(value)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"Smart pick-up: {what} must be a number") from exc
    if not low <= number <= high:
        raise ProfileError(f"Smart pick-up: {what} must be between {low} and {high}")
    return number


@dataclass
class SmartLoot:
    key: str                                            # the pick-up key
    toggle: tuple[float, float]                         # the hand / X button (fractions of the screen)
    row: tuple[float, float]                            # the first row of the list
    count: int = 3                                      # items per press
    box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)      # where to look for the yellow rows (default: around `row`)
    threshold: float = 0.25                             # share of yellow in the box that means "the list is open"
    wait_ms: int = 350                                  # how long the list takes to appear after the button is tapped
    step_ms: int = 220                                  # wait between two row taps
    close_key: str = ""                                 # optional: closes the list
    pick_keys: list = field(default_factory=list)       # key i takes the i-th row of the open list (G, H, K ... -> row 1, 2, 3 ...)
    row_gap: float = DEFAULT_ROW_GAP                    # distance between two rows (fraction of the screen height)
    scroll: bool = True                                 # the mouse wheel scrolls the list while it is open
    unknown: dict = field(default_factory=dict, compare=False, repr=False)       # fields a newer version wrote, kept

    def __post_init__(self) -> None:
        self.key = canonical_trigger(self.key)
        self.close_key = canonical_trigger(self.close_key) if self.close_key else ""
        if self.close_key == self.key:
            raise ProfileError("Smart pick-up: the close key must be a different key")
        self.toggle = (_unit(self.toggle[0], "the button's x"), _unit(self.toggle[1], "the button's y"))
        self.row = (_unit(self.row[0], "the row's x"), _unit(self.row[1], "the row's y"))
        keys = [canonical_trigger(k) for k in self.pick_keys]
        if len(keys) > MAX_PICK_KEYS:
            raise ProfileError(f"Smart pick-up: at most {MAX_PICK_KEYS} pick keys")
        if len(set(keys)) != len(keys) or self.key in keys or (self.close_key and self.close_key in keys):
            raise ProfileError("Smart pick-up: every key must be different")
        self.pick_keys = keys
        self.row_gap = round(_in(self.row_gap, *ROW_GAP_RANGE, "the gap between rows"), 5)
        self.scroll = bool(self.scroll)
        self.count = _in(self.count, *COUNT_RANGE, "the number of items", int)
        self.threshold = _in(self.threshold, *THRESHOLD_RANGE, "the threshold")
        self.wait_ms = _in(self.wait_ms, *WAIT_RANGE_MS, "the wait for the list", int)
        self.step_ms = _in(self.step_ms, *STEP_RANGE_MS, "the wait between taps", int)
        box = tuple(self.box)
        if len(box) != 4 or box == (0.0, 0.0, 0.0, 0.0):
            box = default_box(self.row)
        x0, y0, x1, y1 = (_unit(v, "the look box") for v in box)
        if x1 <= x0 or y1 <= y0:
            raise ProfileError("Smart pick-up: the look box is empty")
        # the same rounding as in the file, so a setting equals itself after a save and a load
        self.box = tuple(round(v, 5) for v in (x0, y0, x1, y1))
        self.toggle = tuple(round(v, 5) for v in self.toggle)
        self.row = tuple(round(v, 5) for v in self.row)
        self.threshold = round(self.threshold, 4)

    def to_dict(self) -> dict:
        data = {"key": self.key, "toggle": [round(v, 5) for v in self.toggle], "row": [round(v, 5) for v in self.row], "count": self.count,
                "box": [round(v, 5) for v in self.box], "threshold": round(self.threshold, 4), "wait_ms": self.wait_ms, "step_ms": self.step_ms}
        if self.close_key:
            data["close_key"] = self.close_key
        if self.pick_keys:
            data["pick_keys"] = list(self.pick_keys)
        data["row_gap"] = self.row_gap
        data["scroll"] = self.scroll
        for key, value in self.unknown.items():
            data.setdefault(key, value)
        return data

    @staticmethod
    def from_dict(data) -> "SmartLoot":
        if not isinstance(data, dict):
            raise ProfileError("Smart pick-up must be an object")
        known = {"key", "toggle", "row", "count", "box", "threshold", "wait_ms", "step_ms", "close_key", "pick_keys", "row_gap", "scroll"}
        try:
            return SmartLoot(key=str(data["key"]), toggle=tuple(data["toggle"]), row=tuple(data["row"]), count=data.get("count", 3),
                             box=tuple(data.get("box") or (0.0, 0.0, 0.0, 0.0)), threshold=data.get("threshold", 0.25),
                             wait_ms=data.get("wait_ms", 350), step_ms=data.get("step_ms", 220), close_key=str(data.get("close_key") or ""),
                             pick_keys=list(data.get("pick_keys") or []), row_gap=data.get("row_gap", DEFAULT_ROW_GAP),
                             scroll=data.get("scroll", True),
                             unknown={k: v for k, v in data.items() if k not in known})
        except (KeyError, TypeError, IndexError) as exc:
            raise ProfileError(f"Smart pick-up is incomplete or unreadable: {exc}") from exc

    def keys(self) -> list[str]:
        return [k for k in (self.key, self.close_key, *self.pick_keys) if k]

    def row_taps(self) -> list[tuple[float, float, int]]:
        """The taps that take `count` items: (x, y, delay from now in ms)."""
        return [(self.row[0], self.row[1], index * self.step_ms) for index in range(self.count)]

    def row_point(self, index: int) -> tuple[float, float]:
        """The middle of the `index`-th row (0 = the first) of the open list."""
        return (self.row[0], min(1.0, self.row[1] + index * self.row_gap))

    def scroll_path(self, wheel_up: bool) -> tuple[tuple[float, float], tuple[float, float]]:
        """Start and end of the finger swipe that scrolls the list one notch: the wheel DOWN moves the finger up (the list moves up, later
        items come into view); the wheel UP the other way."""
        x, y = self.row
        top, bottom = y, min(1.0, y + SCROLL_ROWS * self.row_gap)
        return ((x, top), (x, bottom)) if wheel_up else ((x, bottom), (x, top))


# ------------------------------------------------------------------------------------------ looking at the picture
def _rgb_block(frame, box: tuple[float, float, float, float]) -> np.ndarray:
    """The picture inside `box` (fractions) as RGB, at CHROMA resolution (half size): all that is needed to tell yellow, and cheap."""
    w, h = frame.width, frame.height
    cw, ch = frame.chroma_size
    cx0, cx1 = int(box[0] * cw), max(int(box[0] * cw) + 1, int(box[2] * cw))
    cy0, cy1 = int(box[1] * ch), max(int(box[1] * ch) + 1, int(box[3] * ch))
    cx1, cy1 = min(cx1, cw), min(cy1, ch)
    luma = np.frombuffer(frame.y, np.uint8).reshape(h, w)[::2, ::2][:ch, :cw][cy0:cy1, cx0:cx1].astype(np.float32)
    u = np.frombuffer(frame.u, np.uint8).reshape(ch, cw)[cy0:cy1, cx0:cx1].astype(np.float32) - 128.0
    v = np.frombuffer(frame.v, np.uint8).reshape(ch, cw)[cy0:cy1, cx0:cx1].astype(np.float32) - 128.0
    if frame.full_range:
        y = luma
        scale_c = 1.0
    else:
        y = (luma - 16.0) * (255.0 / 219.0)
        scale_c = 255.0 / 224.0
    u, v = u * scale_c, v * scale_c
    kr, kb = (0.299, 0.114) if frame.bt601 else (0.2126, 0.0722)
    r = y + 2.0 * (1.0 - kr) * v
    b = y + 2.0 * (1.0 - kb) * u
    g = (y - kr * r - kb * b) / (1.0 - kr - kb)
    return np.stack([r, g, b], axis=-1)


def yellow_share(frame, box: tuple[float, float, float, float]) -> float:
    """What share of the box is the loot list's yellow (0..1)."""
    if frame is None or frame.width < 16 or frame.height < 16:
        return 0.0
    rgb = _rgb_block(frame, box)
    if rgb.size == 0:
        return 0.0
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return float(np.mean((r > 195) & (g > 145) & (b < 100) & (r - b > 110)))


def list_is_open(frame, config: SmartLoot) -> bool:
    return yellow_share(frame, config.box) >= config.threshold


def calibrated_threshold(share_when_open: float) -> float:
    """The 'open' threshold for a phone whose open list covers `share_when_open` of the box: half of it, but never tiny or huge."""
    return round(max(0.12, min(0.45, share_when_open * 0.5)), 3)
