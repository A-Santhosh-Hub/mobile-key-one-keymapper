"""When smooth picture shows a generated picture (app/video/smooth.py). The pictures themselves are checked on the GPU by tools/smooth_gl_test.py."""
from __future__ import annotations

import pytest

from app.video.smooth import MAX_DELAY_S, Pacer


def stream(pacer: Pacer, fps: float, count: int, start: float = 0.0) -> list:
    """`count` real pictures arriving at `fps`; what the pacer answers for each."""
    return [pacer.on_frame(start + i / fps) for i in range(count)]


def on(refresh: float = 144.0) -> Pacer:
    pacer = Pacer()
    pacer.enabled = True
    pacer.refresh_hz = refresh
    return pacer


def test_off_by_default_and_when_off_nothing_is_ever_delayed():
    assert stream(Pacer(), 30, 20) == [None] * 20
    assert Pacer().state() == "off"


def test_the_first_picture_is_shown_straight_away_then_each_waits_half_a_picture():
    answers = stream(on(), 30, 10)
    assert answers[0] is None
    assert all(a == pytest.approx(1 / 60, rel=0.01) for a in answers[2:])


@pytest.mark.parametrize("fps,refresh,smooths", [(30, 60, True), (30, 144, True), (60, 144, True), (45, 90, True), (45, 60, False), (60, 60, False),
                                                 (60, 120, True), (90, 144, False), (120, 144, False)])
def test_only_when_twice_the_stream_rate_fits_the_screen(fps, refresh, smooths):
    answers = stream(on(refresh), fps, 12)
    assert (answers[-1] is not None) is smooths


def test_the_delay_is_never_long():
    assert max(a for a in stream(on(), 9, 10) if a is not None) <= MAX_DELAY_S


def test_a_stall_is_not_interpolated_and_the_measuring_starts_again():
    pacer = on()
    stream(pacer, 30, 10)
    assert pacer.on_frame(5.0) is None                         # 4.7 s since the last picture: shown straight away
    assert pacer.state() == "on (waiting for the picture)"      # (the rate is unknown again)
    assert pacer.on_frame(5.0 + 1 / 30) is not None            # the first gap after it tells the rate again
    assert pacer.state().startswith("on:")


def test_a_very_slow_stream_is_left_alone():
    pacer = on()
    assert stream(pacer, 7, 10)[-1] is None and "too slow" in pacer.state()
    assert stream(on(), 4, 10)[-1] is None                      # slower than 6-7 pictures/s counts as stalling


def test_a_fast_stream_says_why_it_is_not_smoothed():
    pacer = on(60.0)
    stream(pacer, 60, 10)
    assert "already fast" in pacer.state() and "60 Hz" in pacer.state()


def test_a_slow_graphics_chip_pauses_it_after_two_slow_measurements_in_a_row():
    pacer = on()
    stream(pacer, 30, 10)                                                # 33 ms between pictures: 23 ms is the limit
    pacer.report_cost(10)
    pacer.report_cost(40)
    assert not pacer.too_slow                                            # once is forgiven
    pacer.report_cost(10)
    pacer.report_cost(40)
    pacer.report_cost(40)
    assert pacer.too_slow and "graphics chip" in pacer.state()
    assert pacer.on_frame(10.0) is None


def test_switching_it_off_and_on_gives_a_fresh_try():
    pacer = on()
    stream(pacer, 30, 10)
    pacer.report_cost(90)
    pacer.report_cost(90)
    assert pacer.too_slow
    pacer.set_enabled(False)
    pacer.set_enabled(True)
    assert not pacer.too_slow


def test_forced_mode_ignores_the_rate_rules():
    pacer = on(30.0)
    pacer.force = True
    assert stream(pacer, 60, 6)[-1] is not None
