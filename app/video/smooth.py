"""When to show a generated in-between picture ("smooth picture").

The phone sends N real pictures per second. With smooth picture on, every time a real picture arrives the screen first shows the picture
HALFWAY between the previous real one and this one (made on the GPU, see widget.py), and half a frame later the real one itself. So the
screen gets twice as many pictures; every real picture is shown half a frame later than without smoothing (that is the whole price).

This module only decides WHEN, from the arrival times of the real pictures. It knows nothing about Qt or the GPU.

Smoothing is skipped when it cannot help or would only hurt:
  * the stream is already about as fast as the screen can show (twice the stream rate would be above the refresh rate);
  * the stream is too slow or stalls (a long gap is a hitch, not motion to interpolate).
"""
from __future__ import annotations

MIN_FPS = 8.0              # below this the gap between two pictures is too large to guess the middle
MAX_GAP_S = 0.15           # a longer gap is a stall (or a pause): start measuring again, show the real picture
HEADROOM = 1.1             # twice the stream rate may exceed the refresh rate by this much before smoothing is judged useless
MAX_DELAY_S = 0.04         # never hold a real picture back for longer than this
SMOOTHING_OF_INTERVAL = 0.2
COST_BUDGET = 0.7           # making a picture may take at most this share of the time between two real pictures
COST_STRIKES = 2            # ... measured too slow this many times in a row: pause smoothing (it would make the picture stutter)


class Pacer:
    def __init__(self) -> None:
        self.enabled = False
        self.refresh_hz = 60.0
        self.force = False                  # tests: smooth regardless of the stream / screen rates
        self.too_slow = False               # the graphics chip cannot make the pictures in time
        self.last_cost_ms = 0.0
        self._strikes = 0
        self._last: float | None = None
        self._interval: float | None = None

    def reset(self) -> None:
        self._last = None
        self._interval = None

    def report_cost(self, ms: float) -> None:
        """How long making one generated picture really took (measured now and then). If it keeps taking more of the time between two real
        pictures than it can spare, smoothing pauses itself: a late picture is worse than no extra picture."""
        self.last_cost_ms = ms
        if self._interval is None:
            return
        self._strikes = self._strikes + 1 if ms > COST_BUDGET * self._interval * 1000 else 0
        if self._strikes >= COST_STRIKES:
            self.too_slow = True

    def set_enabled(self, enabled: bool) -> None:
        if enabled and not self.enabled:
            self.too_slow, self._strikes = False, 0          # a fresh try each time it is switched on
        self.enabled = bool(enabled)

    @property
    def stream_fps(self) -> float:
        return 1.0 / self._interval if self._interval else 0.0

    def on_frame(self, now: float) -> float | None:
        """A real picture arrived at `now` (seconds). Returns None to show it straight away, or the number of seconds to wait before showing
        it - the generated picture is shown first, in the meantime."""
        last, self._last = self._last, now
        if last is None or now - last <= 0 or now - last > MAX_GAP_S:
            self._interval = None
            return None
        gap = now - last
        self._interval = gap if self._interval is None else (1 - SMOOTHING_OF_INTERVAL) * self._interval + SMOOTHING_OF_INTERVAL * gap
        if not self.enabled:
            return None
        if not self.force and not self.useful():
            return None
        return min(self._interval / 2, MAX_DELAY_S)

    def useful(self) -> bool:
        fps = self.stream_fps
        return fps >= MIN_FPS and fps * 2 <= self.refresh_hz * HEADROOM and not self.too_slow

    def state(self) -> str:
        """One line for the status bar / the settings."""
        if not self.enabled:
            return "off"
        if self._interval is None:
            return "on (waiting for the picture)"
        fps = self.stream_fps
        if self.force or self.useful():
            return f"on: {fps:.0f} -> {fps * 2:.0f} pictures/s"
        if self.too_slow:
            return (f"on, paused (this graphics chip needs {self.last_cost_ms:.0f} ms for one generated picture, too long at {fps:.0f} pictures/s; "
                    "a stronger graphics chip or a slower stream would work)")
        if fps < MIN_FPS:
            return f"on, paused (the stream is too slow: {fps:.0f} pictures/s)"
        return (f"on, paused (the stream, {fps:.0f} pictures/s, is already fast for a {self.refresh_hz:.0f} Hz screen; "
                "smoothing would add delay and show nothing more)")
