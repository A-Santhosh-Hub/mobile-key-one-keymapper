"""Smart pick-up of a loot list: the setting (validation, saving), the picture check (list open or closed), the profile checks, and the
engine's one-off taps. The picture check was also run on two real PUBG Mobile screenshots (list open: 77 % of the box yellow; closed: 0 %);
the tests here use synthetic frames with the same colours, so they do not depend on those pictures."""
import numpy as np
import pytest

from app.mapping import loot
from app.mapping.loot import SmartLoot
from app.mapping.profile import Binding, Profile, ProfileError, ProfileStore, Stick
from app.stream import protocol
from app.video.frame import YuvFrame
from test_characterization_engine import Rig

D, U = protocol.TOUCH_DOWN, protocol.TOUCH_UP
W, H = 1608, 720
YELLOW = (232, 196, 40)                                  # the loot list's yellow, as measured on a real screenshot
GROUND = (170, 160, 150)


def frame_of(rgb: np.ndarray, bt601: bool = False, full: bool = False) -> YuvFrame:
    """A YuvFrame from an RGB picture (the inverse of what the app does), so tests can draw what they need."""
    a = rgb.astype(np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    kr, kb = (0.299, 0.114) if bt601 else (0.2126, 0.0722)
    y = kr * r + (1 - kr - kb) * g + kb * b
    cb, cr = (b - y) / (2 * (1 - kb)), (r - y) / (2 * (1 - kr))
    if full:
        Y, u, v = y, 128 + cb, 128 + cr
    else:
        Y, u, v = 16 + y * 219 / 255, 128 + cb * 224 / 255, 128 + cr * 224 / 255
    h, w = rgb.shape[:2]
    return YuvFrame(w, h, np.clip(Y, 0, 255).astype(np.uint8).tobytes(), np.clip(u, 0, 255).astype(np.uint8)[::2, ::2].tobytes(),
                    np.clip(v, 0, 255).astype(np.uint8)[::2, ::2].tobytes(), bt601=bt601, full_range=full)


def picture(rows: int = 3, hand: bool = False, **kw) -> YuvFrame:
    """The ground, and (rows > 0) a loot list on the right: rows of yellow starting at the first-row position; (hand) only a small icon."""
    rgb = np.zeros((H, W, 3), np.uint8)
    rgb[...] = GROUND
    for index in range(rows):
        y0 = int((0.308 + index * 0.097) * H)
        rgb[y0:y0 + int(0.088 * H), int(0.626 * W):int(0.755 * W)] = YELLOW
    if hand:
        rgb[int(0.235 * H):int(0.275 * H), int(0.730 * W):int(0.755 * W)] = YELLOW
    return frame_of(rgb, **kw)


def config(**kw) -> SmartLoot:
    base = dict(key="F", toggle=(0.742, 0.255), row=(0.690, 0.352), count=3)
    return SmartLoot(**{**base, **kw})


# ===================================================================== the setting
def test_a_setting_gets_sensible_defaults_and_a_box_around_the_first_row_only():
    c = config()
    assert (c.count, c.wait_ms, c.step_ms, c.close_key) == (3, 350, 220, "")
    x0, y0, x1, y1 = c.box
    assert x0 < c.row[0] < x1 and y0 < c.row[1] < y1 and (y1 - y0) < 0.12                  # only the first row (one item or ten look the same)


def test_it_round_trips_and_keeps_fields_a_newer_version_wrote():
    c = SmartLoot.from_dict({**config(close_key="G", count=5).to_dict(), "future": {"a": 1}})
    assert c.close_key == "G" and c.count == 5 and c.unknown == {"future": {"a": 1}}
    again = SmartLoot.from_dict(c.to_dict())
    assert again == c and again.to_dict()["future"] == {"a": 1}


@pytest.mark.parametrize("change", [dict(count=-1), dict(count=11), dict(row_gap=0.01), dict(row_gap=0.6), dict(pick_keys=["G", "G"]), dict(pick_keys=["F"]),
                                    dict(pick_keys=["G"], close_key="G"), dict(pick_keys=list("GHJKLNB")), dict(threshold=0.01), dict(threshold=0.95), dict(wait_ms=10), dict(wait_ms=5000),
                                    dict(step_ms=10), dict(toggle=(1.2, 0.5)), dict(row=(0.5, -0.1)), dict(key=""), dict(close_key="F"),
                                    dict(box=(0.5, 0.5, 0.4, 0.6))])
def test_unusable_values_are_refused(change):
    with pytest.raises(ProfileError):
        config(**change)


@pytest.mark.parametrize("data", [None, [], {}, {"key": "F"}, {"key": "F", "toggle": [0.5], "row": [0.5, 0.5]}, {"key": "F", "toggle": "ab", "row": [0.5, 0.5]}])
def test_an_incomplete_or_unreadable_setting_is_refused_not_guessed(data):
    with pytest.raises(ProfileError):
        SmartLoot.from_dict(data)


def test_the_taps_that_take_the_items():
    assert config(count=3, step_ms=200).row_taps() == [(0.69, 0.352, 0), (0.69, 0.352, 200), (0.69, 0.352, 400)]


def test_the_keys_it_listens_to():
    assert config().keys() == ["F"] and config(close_key="G").keys() == ["F", "G"]
    assert config(close_key="Z", pick_keys=["G", "H", "K"]).keys() == ["F", "Z", "G", "H", "K"]


# ---- pick keys (G = 1st row, H = 2nd ...) and scrolling
def test_count_zero_means_the_key_only_opens_the_list():
    assert config(count=0).row_taps() == []


def test_pick_keys_tap_the_rows_in_order_from_the_first_row_down():
    c = config(pick_keys=["G", "H", "K"])
    assert c.row_point(0) == (0.69, 0.352)
    assert [round(c.row_point(i)[1], 3) for i in range(3)] == [0.352, 0.45, 0.548]          # PUBG Mobile's rows are about 0.098 apart
    assert c.row_point(0)[0] == c.row_point(2)[0]


def test_a_row_never_leaves_the_screen():
    assert config(row=(0.5, 0.95), pick_keys=["G", "H"]).row_point(1)[1] == 1.0


def test_pick_keys_and_scrolling_round_trip_and_older_files_still_load():
    c = config(pick_keys=["G", "H"], row_gap=0.1, scroll=False)
    again = SmartLoot.from_dict(c.to_dict())
    assert again == c and again.pick_keys == ["G", "H"] and again.row_gap == 0.1 and again.scroll is False
    old = SmartLoot.from_dict({"key": "F", "toggle": [0.74, 0.25], "row": [0.69, 0.35], "count": 3})       # written before pick keys existed
    assert old.pick_keys == [] and old.scroll is True and old.row_gap == loot.DEFAULT_ROW_GAP
    assert "pick_keys" not in config().to_dict()


def test_the_wheel_scrolls_by_two_rows_wheel_down_moves_the_finger_up():
    c = config()
    (sx, sy), (ex, ey) = c.scroll_path(wheel_up=False)
    assert sx == ex == c.row[0] and sy > ey and round(sy - ey, 3) == round(loot.SCROLL_ROWS * c.row_gap, 3) and ey == c.row[1]
    (ux, uy), (vx, vy) = c.scroll_path(wheel_up=True)
    assert (ux, uy) == (ex, ey) and (vx, vy) == (sx, sy)


# ===================================================================== looking at the picture
def test_an_open_list_is_recognised_and_a_closed_one_is_not():
    c = config()
    assert loot.yellow_share(picture(rows=3), c.box) > 0.6
    assert loot.yellow_share(picture(rows=0), c.box) == 0.0


def test_the_hand_icon_alone_is_not_an_open_list():
    c = config()
    assert loot.yellow_share(picture(rows=0, hand=True), c.box) < 0.05
    assert not loot.list_is_open(picture(rows=0, hand=True), c)


@pytest.mark.parametrize("rows", [1, 2, 3, 8])
def test_a_list_with_one_item_looks_as_open_as_a_list_with_many(rows):
    c = config()
    assert loot.list_is_open(picture(rows=rows), c)                                         # (else the key would tap the X and CLOSE a one-item list)


@pytest.mark.parametrize("bt601,full", [(False, False), (True, False), (False, True), (True, True)])
def test_every_colour_convention_of_the_phone_stream_gives_the_same_answer(bt601, full):
    c = config()
    assert loot.yellow_share(picture(rows=3, bt601=bt601, full=full), c.box) > 0.6
    assert loot.yellow_share(picture(rows=0, bt601=bt601, full=full), c.box) == 0.0


@pytest.mark.parametrize("colour", [(120, 160, 90), (200, 120, 40), (240, 240, 240), (20, 20, 20), (60, 90, 200), (170, 160, 150), (110, 140, 190)])
def test_other_colours_are_not_taken_for_the_list(colour):
    rgb = np.zeros((H, W, 3), np.uint8)
    rgb[...] = colour
    assert loot.yellow_share(frame_of(rgb), config().box) < 0.05


def test_no_picture_or_a_tiny_one_means_not_open():
    c = config()
    assert loot.yellow_share(None, c.box) == 0.0 and not loot.list_is_open(None, c)
    assert loot.yellow_share(YuvFrame(8, 8, bytes(64), bytes(16), bytes(16)), c.box) == 0.0


def test_the_threshold_follows_the_phone_it_was_measured_on_within_limits():
    assert loot.calibrated_threshold(0.77) == pytest.approx(0.385)
    assert loot.calibrated_threshold(0.1) == 0.12 and loot.calibrated_threshold(1.0) == 0.45


def test_the_set_up_refuses_a_list_that_is_not_there():
    assert loot.yellow_share(picture(rows=0), loot.default_box((0.69, 0.352))) < loot.VISIBLE_MIN_SHARE
    assert loot.yellow_share(picture(rows=3), loot.default_box((0.69, 0.352))) >= loot.VISIBLE_MIN_SHARE


# ===================================================================== in the profile
def test_a_profile_without_it_is_written_exactly_as_before():
    p = Profile("p", bindings=[Binding("Q", 0.1, 0.1, "tap")])
    assert "smart_loot" not in p.to_dict() and p.to_dict()["version"] == 1


def test_it_is_saved_and_loaded_with_the_profile(tmp_path):
    store = ProfileStore(tmp_path)
    p = Profile("p", bindings=[Binding("Q", 0.1, 0.1, "tap")], smart_loot=config(count=4).to_dict())
    store.save(p)
    back = store.load("p")
    assert SmartLoot.from_dict(back.smart_loot) == config(count=4) and back.to_dict()["version"] == 1


def test_a_profile_from_a_newer_writer_keeps_an_unreadable_setting_untouched():
    raw = {"version": 1, "name": "p", "bindings": [], "smart_loot": {"key": "F", "weird": True}}
    p = Profile.from_dict(raw)
    assert p.to_dict()["smart_loot"] == {"key": "F", "weird": True}


def test_the_checks_accept_a_good_setting_and_name_every_problem():
    p = Profile("p", bindings=[Binding("Q", 0.1, 0.1, "tap")], smart_loot=config().to_dict())
    assert [i for i in p.analyze() if i.code.startswith("smart_loot")] == []
    p.smart_loot = {"key": "F"}
    assert [i.code for i in p.analyze() if i.code.startswith("smart_loot")] == ["smart_loot_invalid"]
    p.smart_loot = config(key="Q").to_dict()
    found = [i for i in p.analyze() if i.code == "smart_loot_key_conflict"]
    assert len(found) == 1 and found[0].severity == "error" and found[0].binding_ids == (p.bindings[0].id,)
    p.sticks = [Stick(0.15, 0.7)]
    p.smart_loot = config(key="W").to_dict()
    assert any("movement-stick" in i.message for i in p.analyze())
    p.smart_loot = config(key="F9").to_dict()
    assert any("shortcut" in i.message for i in p.analyze(reserved={"F9": "a shortcut"}))
    p.smart_loot = config(close_key="Q").to_dict()
    assert any(i.code == "smart_loot_key_conflict" and i.trigger == "Q" for i in p.analyze())
    p.smart_loot = config(pick_keys=["Q", "H"]).to_dict()                                  # a pick key that another control already uses
    assert any(i.code == "smart_loot_key_conflict" and i.trigger == "Q" for i in p.analyze())
    p.smart_loot = config(pick_keys=["G", "H"]).to_dict()
    assert not [i for i in p.analyze() if i.code.startswith("smart_loot")]


# ===================================================================== the engine's one-off taps
def rig() -> Rig:
    return Rig([Binding("Q", 0.1, 0.1, "tap")])


def test_taps_run_in_order_one_finger_each_and_end_cleanly():
    r = rig()
    assert r.engine.run_taps([(0.5, 0.5, 0), (0.5, 0.5, 100), (0.5, 0.5, 200)]) is True
    r.advance(1000)
    assert r.kinds() == [D, U] * 3 and r.fingers() == 0
    assert len({(t[2], t[3]) for t in r.touches() if t[0] == D}) == 1


def test_the_delays_are_counted_from_now():
    r = rig()
    r.engine.run_taps([(0.5, 0.5, 0), (0.5, 0.5, 150)])
    r.advance(120)
    assert r.kinds() == [D, U]
    r.advance(40)
    assert r.kinds() == [D, U, D]


def test_nothing_runs_while_mapping_is_off():
    r = rig()
    r.engine.set_mapping_active(False)
    assert r.engine.run_taps([(0.5, 0.5, 0)]) is False
    r.advance(500)
    assert r.kinds() == []


def test_everything_that_cancels_a_binding_cancels_the_taps_too():
    for cancel in (lambda r: r.engine.release_all(), lambda r: r.engine.screen_changed(), lambda r: r.engine.set_mapping_active(False),
                   lambda r: r.engine.refresh_profile()):
        r = rig()
        r.engine.run_taps([(0.5, 0.5, 0), (0.5, 0.5, 300), (0.5, 0.5, 600)])
        r.advance(10)                                                                         # the first tap is down
        cancel(r)
        r.advance(2000)
        assert r.fingers() == 0 and r.kinds().count(D) == 1 and r.kinds()[-1] == U, r.kinds()


def test_the_epoch_changes_when_everything_is_cancelled():
    r = rig()
    before = r.engine.epoch
    r.engine.release_all()
    assert r.engine.epoch != before


def test_two_overlapping_runs_use_separate_fingers_and_a_failure_to_touch_is_not_an_error():
    r = rig()
    r.engine.run_taps([(0.2, 0.2, 0)])
    r.engine.run_taps([(0.8, 0.8, 0)])
    assert r.fingers() == 2
    r.advance(500)
    assert r.fingers() == 0
    r.control = None                                                                       # no connection: the tap is simply not made
    r.engine.run_taps([(0.5, 0.5, 0)])
    r.advance(500)
    assert r.fingers() == 0


def test_bindings_still_work_beside_the_taps():
    r = rig()
    r.engine.run_taps([(0.5, 0.5, 0)])
    r.press("Q", 200)
    r.release("Q", 200)
    assert r.kinds().count(D) == 2 and r.fingers() == 0


# ===================================================================== scrolling: one finger, drags, lifts
def test_a_scroll_swipe_is_one_finger_that_goes_down_moves_and_lifts():
    r = rig()
    assert r.engine.run_swipe((0.69, 0.55), (0.69, 0.35), 200) is True
    r.advance(400)
    t = r.touches()
    assert [x[0] for x in t][0] == D and [x[0] for x in t][-1] == U and r.fingers() == 0
    moves = [x for x in t if x[0] == protocol.TOUCH_MOVE]
    assert len(moves) >= 10 and moves[0][3] > moves[-1][3]                                  # upwards, in small steps
    assert abs(moves[-1][3] - 0.35 * r.engine.injector.size()[1]) <= 2


def test_a_scroll_swipe_does_not_run_while_mapping_is_off_and_stops_when_everything_is_cancelled():
    r = rig()
    r.engine.set_mapping_active(False)
    assert r.engine.run_swipe((0.5, 0.5), (0.5, 0.3), 200) is False and r.kinds() == []
    r = rig()
    r.engine.run_swipe((0.5, 0.5), (0.5, 0.3), 200)
    r.advance(40)
    r.engine.release_all()                                                                  # focus loss, rotation, Esc ...
    before = len(r.touches())
    r.advance(500)
    assert r.fingers() == 0 and len(r.touches()) == before
