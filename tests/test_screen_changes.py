"""A rotation or resolution change while fingers are down: every finger must be lifted at the NEW size, every running
action cancelled, nothing may be sent for the old screen, and play works again at the new size."""
import pytest

from app.input.engine import InputEngine
from app.input.touch import TouchInjector
from app.mapping.profile import Binding, Look, Profile, Stick
from app.stream import protocol
from test_mapping import FakeControl

D, U, M = protocol.TOUCH_DOWN, protocol.TOUCH_UP, protocol.TOUCH_MOVE
LAND, PORT = (1920, 864), (864, 1920)


# ===================================================================== the injector alone
def make_injector(size=LAND):
    holder = [size]
    control = FakeControl()
    return TouchInjector(lambda: control, lambda: holder[0]), control, holder


def test_a_finger_is_lifted_at_the_new_size_at_its_scaled_position():
    inj, control, size = make_injector(LAND)
    inj.down("a", 0.25, 0.5)
    size[0] = PORT
    assert inj.release_stale() == ["a"] and inj.active_count == 0
    assert control.touches()[-1] == (U, 0, 216, 960, 864, 1920)           # 0.25 * 864, 0.5 * 1920


def test_every_finger_keeps_its_pointer_id_when_lifted():
    inj, control, size = make_injector(LAND)
    for i, owner in enumerate("abc"):
        inj.down(owner, 0.1 * (i + 1), 0.5)
    size[0] = PORT
    inj.release_all()
    ups = [t for t in control.touches() if t[0] == U]
    assert sorted(t[1] for t in ups) == [0, 1, 2] and all(t[4:] == PORT for t in ups) and inj.active_count == 0


def test_up_after_a_size_change_sends_exactly_one_up_and_reports_success():
    inj, control, size = make_injector(LAND)
    inj.down("a", 0.5, 0.5)
    size[0] = PORT
    assert inj.up("a") is True
    assert inj.up("a") is False                                                # already up
    assert [t[0] for t in control.touches()].count(U) == 1


def test_move_after_a_size_change_lifts_the_finger_instead_of_sending_a_stale_move():
    inj, control, size = make_injector(LAND)
    inj.down("a", 0.5, 0.5)
    size[0] = PORT
    assert inj.move("a", 0.6, 0.6) is False
    kinds = [t[0] for t in control.touches()]
    assert kinds == [D, U] and M not in kinds


def test_nothing_is_ever_sent_with_a_size_other_than_the_current_one():
    inj, control, size = make_injector(LAND)
    inj.down("a", 0.5, 0.5)
    inj.down("b", 0.2, 0.2)
    before = len(control.touches())
    size[0] = PORT
    inj.move("a", 0.4, 0.4)
    inj.down("c", 0.9, 0.9)
    inj.move("c", 0.8, 0.8)
    inj.release_all()
    assert all(t[4:] == PORT for t in control.touches()[before:])


def test_a_new_finger_after_a_change_reuses_the_freed_pointer_ids_without_clashes():
    inj, control, size = make_injector(LAND)
    inj.down("a", 0.5, 0.5)
    inj.down("b", 0.5, 0.5)
    size[0] = PORT
    assert inj.down("c", 0.5, 0.5) and inj.down("d", 0.1, 0.1)
    ids = sorted(t[1] for t in control.touches() if t[0] == D and t[4:] == PORT)
    assert ids == [0, 1] and inj.active_count == 2


def test_a_resolution_change_without_rotation_is_handled_the_same_way():
    inj, control, size = make_injector((1920, 864))
    inj.down("a", 0.5, 0.5)
    size[0] = (1600, 720)
    inj.release_all()
    assert control.touches()[-1] == (U, 0, 800, 360, 1600, 720)


def test_if_the_video_is_gone_the_fingers_are_forgotten_without_sending_anything():
    inj, control, size = make_injector(LAND)
    inj.down("a", 0.5, 0.5)
    sent = len(control.touches())
    size[0] = (0, 0)
    inj.release_all()
    assert inj.active_count == 0 and len(control.touches()) == sent


def test_a_dead_control_channel_during_cleanup_is_not_an_error():
    inj, control, size = make_injector(LAND)
    inj.down("a", 0.5, 0.5)
    inj.down("b", 0.5, 0.5)
    control.send = lambda data: False
    size[0] = PORT
    inj.release_all()
    assert inj.active_count == 0


def test_no_change_means_no_extra_messages():
    inj, control, size = make_injector(LAND)
    inj.down("a", 0.5, 0.5)
    assert inj.release_stale() == [] and len(control.touches()) == 1


# ===================================================================== the engine on top of it
class Rig:
    """Engine + injector + manual timers, with a screen size the test can change."""

    def __init__(self, bindings=(), stick=None, look=None, size=LAND):
        self.size = [size]
        self.control = FakeControl()
        self.timers = []
        self.now = 0.0
        injector = TouchInjector(lambda: self.control, lambda: self.size[0])
        self.engine = InputEngine(injector, lambda ms, fn: self.timers.append((self.now + ms, fn)),
                                  clock=lambda: 100.0 + self.now / 1000)
        profile = Profile("t")
        for b in bindings:
            profile.add(b)
        if stick:
            profile.set_stick(stick)
        if look:
            profile.looks.append(look)
        self.engine.set_profile(profile)
        self.engine.set_mapping_active(True)

    def advance(self, ms):
        end = self.now + ms
        while True:
            due = sorted((t for t in self.timers if t[0] <= end), key=lambda t: t[0])
            if not due:
                break
            when, fn = due[0]
            self.timers.remove(due[0])
            self.now = when
            fn()
        self.now = end

    def press(self, key, ms=0):
        self.engine.key_event(key, True)
        self.advance(ms)

    def release(self, key, ms=0):
        self.engine.key_event(key, False)
        self.advance(ms)

    def touches(self):
        return self.control.touches()

    def rotate(self, size=PORT):
        self.size[0] = size
        self.engine.screen_changed()


def test_a_held_button_is_lifted_and_works_again_at_the_new_size():
    rig = Rig([Binding("Space", 0.9, 0.5, "hold")])
    rig.press("Space")
    rig.rotate()
    assert rig.touches()[-1] == (U, 0, 777, 960, 864, 1920) and rig.engine.injector.active_count == 0
    sent = len(rig.touches())
    rig.release("Space", 100)
    assert len(rig.touches()) == sent                                          # releasing the key now does nothing
    rig.press("Space")
    assert rig.touches()[-1] == (D, 0, 777, 960, 864, 1920)                    # 0.9 * 864 = 777.6 (floored), 0.5 * 1920


def test_a_running_stick_ramp_stops_and_restarts_at_the_new_geometry():
    rig = Rig(stick=Stick(0.15, 0.70, radius=0.10, ramp_ms=60))
    rig.press("W", 20)                                                         # ramp ticks are still scheduled
    assert rig.engine.injector.active_count == 1
    rig.rotate()
    after_change = len(rig.touches())
    assert rig.touches()[-1][0] == U and rig.engine.injector.active_count == 0
    rig.advance(500)
    assert len(rig.touches()) == after_change                                  # no stray ramp tick for the old screen
    rig.release("W")
    rig.press("W", 100)
    down = [t for t in rig.touches()[after_change:] if t[0] == D][0]
    assert down[2:] == (129, 1344, 864, 1920)                                  # 0.15 * 864 = 129.6, 0.70 * 1920 (pixels are floored)
    assert all(t[4:] == PORT for t in rig.touches()[after_change:])


def test_mouse_look_lifts_and_restarts_in_the_new_zone():
    rig = Rig(look=Look(0.40, 0.10, 0.60, 0.50))
    rig.engine.set_look_captured(True)
    rig.engine.look_delta(12, 0)
    rig.advance(20)
    assert rig.engine.injector.active_count == 1
    rig.rotate()
    after_change = len(rig.touches())
    rig.advance(1000)                                                          # flush and idle timers fire into nothing
    assert len(rig.touches()) == after_change and rig.engine.injector.active_count == 0
    rig.engine.look_delta(5, 0)
    rig.advance(20)
    down = [t for t in rig.touches()[after_change:] if t[0] == D][0]
    assert down[2:] == (432, 576, 864, 1920)                                   # the zone centre (0.5, 0.3) of the new screen


def test_a_swipe_in_flight_is_cancelled_cleanly():
    rig = Rig([Binding("Q", 0.2, 0.5, "swipe", x2=0.6, y2=0.5, duration_ms=400)])
    rig.press("Q", 80)
    assert rig.engine.injector.active_count == 1
    rig.rotate()
    after_change = len(rig.touches())
    rig.advance(2000)
    assert len(rig.touches()) == after_change
    assert [t[0] for t in rig.touches()].count(U) == 1 and rig.touches()[-1][0] == U


def test_auto_repeat_stops_for_good():
    rig = Rig([Binding("F", 0.7, 0.6, "repeat", rate_hz=10)])
    rig.press("F", 250)
    downs = [t for t in rig.touches() if t[0] == D]
    assert len(downs) >= 2
    rig.rotate()
    after_change = len(rig.touches())
    rig.advance(1000)
    assert len(rig.touches()) == after_change and rig.engine.injector.active_count == 0


def test_a_toggled_on_button_is_lifted_and_toggle_state_starts_fresh():
    rig = Rig([Binding("T", 0.5, 0.5, "toggle")])
    rig.press("T")
    rig.release("T")
    assert rig.engine.injector.active_count == 1                               # toggled on: the finger stays down
    rig.rotate()
    assert rig.engine.injector.active_count == 0 and rig.touches()[-1][0] == U
    rig.press("T")
    rig.release("T")
    assert rig.engine.injector.active_count == 1 and rig.touches()[-1][0] == D  # a fresh press toggles ON, not off


def test_a_multi_tap_sequence_in_progress_is_cancelled():
    rig = Rig([Binding("G", 0.1, 0.1, "sequence", steps=[(0.1, 0.1, 0), (0.5, 0.5, 300), (0.9, 0.9, 300)])])
    rig.press("G", 100)
    rig.rotate()
    after_change = len(rig.touches())
    rig.advance(3000)
    assert len(rig.touches()) == after_change


def test_a_two_finger_pinch_lifts_both_fingers():
    rig = Rig([Binding("WheelUp", 0.5, 0.5, "pinch-out", duration_ms=400)])
    rig.engine.wheel_binding(1)
    rig.advance(100)
    assert rig.engine.injector.active_count == 2
    rig.rotate()
    ups = [t for t in rig.touches() if t[0] == U]
    assert sorted(t[1] for t in ups) == [0, 1] and all(t[4:] == PORT for t in ups)
    after_change = len(rig.touches())
    rig.advance(1000)
    assert len(rig.touches()) == after_change


def test_a_button_and_the_stick_and_the_look_finger_are_all_lifted_together():
    rig = Rig([Binding("LMB", 0.8, 0.7, "hold")], stick=Stick(0.15, 0.7, radius=0.1, ramp_ms=0),
              look=Look(0.4, 0.1, 0.6, 0.5))
    rig.engine.set_look_captured(True)
    rig.press("W", 20)
    rig.press("LMB")
    rig.engine.look_delta(5, 5)
    rig.advance(20)
    assert rig.engine.injector.active_count == 3
    rig.rotate()
    assert rig.engine.injector.active_count == 0
    assert sorted(t[1] for t in rig.touches() if t[0] == U and t[4:] == PORT) == [0, 1, 2]


def test_two_changes_in_a_row_leave_no_duplicates_and_no_pressed_finger():
    rig = Rig([Binding("Space", 0.9, 0.5, "hold")])
    rig.press("Space")
    rig.rotate(PORT)
    rig.rotate(LAND)
    assert [t[0] for t in rig.touches()].count(U) == 1 and rig.engine.injector.active_count == 0


def test_a_change_with_nothing_active_sends_nothing_and_play_continues():
    rig = Rig([Binding("R", 0.7, 0.8, "tap")])
    rig.rotate()
    assert rig.touches() == []
    rig.press("R", 100)
    assert [t[0] for t in rig.touches()] == [D, U] and rig.touches()[0][4:] == PORT
    assert rig.engine.mapping_active


def test_keys_still_held_during_the_change_do_nothing_until_pressed_again():
    rig = Rig([Binding("Space", 0.9, 0.5, "hold")], stick=Stick(0.15, 0.7, radius=0.1, ramp_ms=0))
    rig.press("W")
    rig.press("Space")
    rig.rotate()
    after_change = len(rig.touches())
    rig.release("W", 50)
    rig.release("Space", 50)
    assert len(rig.touches()) == after_change
    rig.press("W", 50)
    assert rig.engine.injector.active_count == 1 and rig.touches()[-1][4:] == PORT


@pytest.mark.parametrize("new_size", [PORT, (1600, 720), (2400, 1080), (720, 1600)])
def test_any_new_size_works(new_size):
    rig = Rig([Binding("Space", 0.5, 0.5, "hold")])
    rig.press("Space")
    rig.rotate(new_size)
    assert rig.touches()[-1] == (U, 0, new_size[0] // 2, new_size[1] // 2, *new_size)
    rig.release("Space")
    rig.press("Space")
    assert rig.touches()[-1][0] == D and rig.touches()[-1][4:] == new_size


def test_stopping_play_while_the_connection_is_already_dead_is_harmless():
    """Disconnect during an action: the releases cannot be delivered, but nothing raises and nothing stays 'pressed' here."""
    rig = Rig([Binding("Space", 0.9, 0.5, "hold")], stick=Stick(0.15, 0.7, radius=0.1, ramp_ms=40))
    rig.press("Space")
    rig.press("W", 10)
    assert rig.engine.injector.active_count == 2
    rig.control.send = lambda data: False                                       # the control channel has closed
    rig.engine.set_mapping_active(False)
    rig.advance(500)
    assert rig.engine.injector.active_count == 0 and not rig.engine.mapping_active
