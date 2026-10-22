"""'Pick up items': one key taps the FIRST row of a game's loot list several times (the list moves up after each pick-up, so every tap takes
the next item). It is an ordinary multi-tap built from one clicked spot; these tests pin what it builds and that the engine runs it correctly."""
import pytest

from app.input.engine import TAP_DURATION_MS
from app.mapping import layout_edit as le
from app.mapping.profile import Profile
from app.stream import protocol
from test_characterization_engine import Rig

D, U = protocol.TOUCH_DOWN, protocol.TOUCH_UP


def test_it_builds_a_multi_tap_of_one_spot_repeated():
    b = le.pickup_binding("F", (0.82, 0.31), 3)
    assert (b.input, b.mode, b.label) == ("F", "sequence", "Pick up x3")
    assert [(x, y) for x, y, _ in b.steps] == [(0.82, 0.31)] * 3
    assert [d for _, _, d in b.steps] == [0, le.PICKUP_STEP_DELAY_MS, le.PICKUP_STEP_DELAY_MS]
    assert (b.x, b.y) == (0.82, 0.31)


@pytest.mark.parametrize("asked,built", [(0, le.PICKUP_MIN), (1, le.PICKUP_MIN), (4, 4), (99, le.PICKUP_MAX)])
def test_the_count_is_kept_inside_sensible_limits(asked, built):
    assert len(le.pickup_binding("F", (0.5, 0.5), asked).steps) == built


def test_the_ordinary_multi_tap_still_uses_its_own_default_wait():
    b = le.binding_from_points("F", "sequence", [(0.1, 0.1), (0.2, 0.2), (0.3, 0.3)])
    assert [d for _, _, d in b.steps] == [0, le.DEFAULT_STEP_DELAY_MS, le.DEFAULT_STEP_DELAY_MS]


def test_it_survives_a_save_and_a_load():
    p = Profile("p", bindings=[le.pickup_binding("F", (0.82, 0.31), 4)])
    back = Profile.from_dict(p.to_dict())
    assert back.find("F").steps == p.find("F").steps and back.find("F").label == "Pick up x4"


@pytest.mark.parametrize("count", [2, 3, 6])
def test_one_key_press_taps_the_same_spot_count_times_one_finger_at_a_time(count):
    b = le.pickup_binding("F", (0.82, 0.31), count)
    r = Rig([b])
    r.press("F", 50)
    r.release("F", 5000)
    downs = [t for t in r.touches() if t[0] == D]
    assert len(downs) == count and len({(t[2], t[3]) for t in downs}) == 1            # the same spot every time
    kinds = r.kinds()
    assert kinds == [D, U] * count                                                      # each tap ends before the next begins
    assert r.fingers() == 0


def test_the_taps_are_spaced_by_the_wait_and_never_overlap():
    b = le.pickup_binding("F", (0.5, 0.5), 3)
    r = Rig([b])
    r.press("F")
    r.advance(le.PICKUP_STEP_DELAY_MS + TAP_DURATION_MS + 10)
    assert r.kinds() == [D, U, D, U]                                                    # two taps done after one wait, the third not yet
    r.advance(le.PICKUP_STEP_DELAY_MS)
    assert r.kinds() == [D, U] * 3


def test_releasing_the_key_early_does_not_cut_the_pickup_short():
    r = Rig([le.pickup_binding("F", (0.5, 0.5), 3)])
    r.press("F")
    r.release("F", 5000)
    assert r.kinds() == [D, U] * 3 and r.fingers() == 0


def test_stopping_the_mapping_midway_lifts_the_finger_and_cancels_the_rest():
    r = Rig([le.pickup_binding("F", (0.5, 0.5), 5)])
    r.press("F", 10)
    r.engine.set_mapping_active(False)
    r.advance(5000)
    assert r.fingers() == 0 and r.kinds().count(D) == 1 and r.kinds()[-1] == U
