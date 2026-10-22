"""Characterization tests: the engine and the control channel working together (Phase 2, step 1).

test_control_channel.py pins the channel with hand-made messages; test_characterization_engine.py pins the engine against
a recording fake. These tests connect the two, so that the ORDER of touch messages, the pointer ids and the releases that
reach the phone are pinned for what the engine really produces. No phone is involved: the far end is a socket pair or the
stalled `Stub` socket of test_control_channel.py, so nothing here says anything about a real device.
"""
import struct
import time

import pytest

from app.input.engine import InputEngine
from app.input.touch import TouchInjector
from app.mapping.profile import Binding, Look, Profile, Stick
from app.stream import protocol
from test_control_channel import DECOY, Phone, Stub, decode, make_channel, without_decoy
from test_mapping import FakeControl

D, U, M = protocol.TOUCH_DOWN, protocol.TOUCH_UP, protocol.TOUCH_MOVE
LAND, PORT = (2400, 1080), (1080, 2400)


class Wired:
    """Engine + injector + manual timers, sending into `control` (a FakeControl or a ControlChannel)."""

    def __init__(self, control, bindings=(), stick=None, look=None, size=LAND):
        self.size = size
        self.now = 0.0
        self.timers = []
        self.injector = TouchInjector(lambda: control, lambda: self.size)
        self.engine = InputEngine(self.injector, lambda ms, fn: self.timers.append((self.now + ms, fn)),
                                  clock=lambda: 100.0 + self.now / 1000)
        profile = Profile("t")
        for binding in bindings:
            profile.add(binding)
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

    def press(self, name, ms=0):
        self.engine.key_event(name, True)
        self.advance(ms)

    def release(self, name, ms=0):
        self.engine.key_event(name, False)
        self.advance(ms)


def parse(data):
    """[(action, pointer id, x, y, width, height)] for every touch message in a byte stream."""
    out = []
    for i in range(0, len(data), protocol.TOUCH_MESSAGE_SIZE):
        _, action, pid, x, y, w, h, _p, _ab, _b = struct.unpack_from(">BBqiiHHHII", data, i)
        out.append((action, pid, x, y, w, h))
    return out


def raw(phone, idle=0.4):
    phone.peer.settimeout(idle)
    data = b""
    try:
        while True:
            chunk = phone.peer.recv(65536)
            if not chunk:
                break
            data += chunk
    except (TimeoutError, OSError):
        pass
    return data


@pytest.fixture
def phone():
    p = Phone()
    yield p
    p.close()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.closed.set()
    s.gate.set()


BINDINGS = [Binding("G", 0.1, 0.9, "hold", also=[(0.2, 0.9)]), Binding("H", 0.5, 0.5, "hold"),
            Binding("T", 0.7, 0.2, "tap")]


def script(w):
    w.press("G")
    w.press("H")
    w.press("T", 100)
    w.release("H")
    w.release("G")


# ===================================================================== order and pointer ids
def test_the_phone_receives_exactly_the_messages_the_engine_produced_in_the_same_order(phone):
    reference = FakeControl()
    script(Wired(reference, BINDINGS))
    channel = phone.channel()
    script(Wired(channel, BINDINGS))
    channel.close(graceful=True)
    assert channel.wait_drained(3)
    assert parse(raw(phone)) == reference.touches()


def test_simultaneous_fingers_go_down_in_press_order_with_stable_pointer_ids_and_come_up_when_released(phone):
    channel = phone.channel()
    script(Wired(channel, BINDINGS))
    channel.close(graceful=True)
    assert channel.wait_drained(3)
    events = [(a, p) for a, p, *_ in parse(raw(phone))]
    assert events == [(D, 0), (D, 1),                  # G: the main spot and its extra spot
                      (D, 2),                          # H
                      (D, 3), (U, 3),                  # T taps with its own finger and lets go again
                      (U, 2),                          # H released
                      (U, 0), (U, 1)]                  # G released: both of its fingers


def test_a_second_run_reuses_the_lowest_free_pointer_ids_and_the_stream_stays_balanced(phone):
    channel = phone.channel()
    w = Wired(channel, BINDINGS)
    script(w)
    script(w)
    channel.close(graceful=True)
    assert channel.wait_drained(3)
    events = parse(raw(phone))
    assert [(a, p) for a, p, *_ in events[:8]] == [(a, p) for a, p, *_ in events[8:]]
    assert [e[0] for e in events].count(D) == [e[0] for e in events].count(U)


# ===================================================================== a phone that stops reading
def test_when_the_phone_stalls_downs_and_ups_survive_in_order_and_only_moves_are_merged(stub):
    channel = make_channel(stub, stall_seconds=30)
    w = Wired(channel, [Binding("G", 0.1, 0.9, "hold"), Binding("Q", 0.2, 0.5, "drag", x2=0.8, y2=0.5, duration_ms=400)],
              stick=Stick(0.15, 0.7, radius=0.1, ramp_ms=200), look=Look(0.5, 0.2, 0.9, 0.8))
    stub.stall(channel)
    w.press("G")
    w.press("Q")
    w.press("W", 100)                                  # drag and stick ramp produce many moves
    w.engine.set_look_captured(True)
    for _ in range(20):
        w.engine.look_delta(3, 1)
        w.advance(5)
    w.release("Q")
    w.release("W")
    w.release("G")
    w.engine.release_all()
    assert channel.coalesced_moves > 0
    stub.resume(channel)
    events = without_decoy(stub.events())
    per_pointer = {}
    for _kind, action, pointer, *_ in events:
        per_pointer.setdefault(pointer, []).append(action)
    assert sorted(per_pointer) == [0, 1, 2, 3]         # G, drag, stick, look
    for pointer, actions in per_pointer.items():
        assert actions[0] == D and actions[-1] == U, pointer         # each finger: down first, up last, nothing after
        assert actions.count(D) == 1 and actions.count(U) == 1
    assert channel.pressed_pointers() == frozenset()


def test_the_engine_is_never_blocked_by_a_stalled_phone(stub):
    channel = make_channel(stub, stall_seconds=30)
    w = Wired(channel, BINDINGS, stick=Stick(0.15, 0.7, ramp_ms=0))
    stub.stall(channel)
    started = time.perf_counter()
    for _ in range(300):
        w.press("G")
        w.press("W")
        w.advance(10)
        w.release("W")
        w.release("G")
    assert time.perf_counter() - started < 5                          # every call returned; nothing waited for the phone


# ===================================================================== cleanup reaches the phone
def test_release_all_sends_one_up_per_finger_at_the_position_it_was_last_at(phone):
    channel = phone.channel()
    w = Wired(channel, [Binding("Q", 0.2, 0.5, "drag", x2=0.8, y2=0.5, duration_ms=400), Binding("G", 0.1, 0.9, "hold")])
    w.press("Q", 160)
    w.press("G")
    w.engine.release_all()
    channel.close(graceful=True)
    assert channel.wait_drained(3)
    events = parse(raw(phone))
    ups = {p: (x, y) for a, p, x, y, *_ in events if a == U}
    assert sorted(ups) == [0, 1] and [a for a, *_ in events].count(U) == 2
    last_move = [e for e in events if e[0] == M][-1]
    assert ups[0] == (last_move[2], last_move[3]) and ups[1] == (240, 972)


def test_a_rotation_sends_every_up_at_the_new_size_and_the_next_press_uses_the_new_size(phone):
    channel = phone.channel()
    w = Wired(channel, BINDINGS)
    w.press("G")
    w.press("H")
    w.size = PORT
    w.engine.screen_changed()
    w.press("H")
    channel.close(graceful=True)
    assert channel.wait_drained(3)
    events = parse(raw(phone))
    assert [(a, p, wh) for a, p, _x, _y, *wh in events[:3]] == [(D, 0, list(LAND)), (D, 1, list(LAND)), (D, 2, list(LAND))]
    ups = [e for e in events[3:6]]
    assert sorted((a, p) for a, p, *_ in ups) == [(U, 0), (U, 1), (U, 2)] and all(e[4:] == PORT for e in ups)
    assert events[6][0] == D and events[6][4:] == PORT                # H pressed again after the rotation
    assert len(events) == 8 and events[7][:2] == (U, 0) and events[7][4:] == PORT      # the close lifts that finger too
    assert channel.pressed_pointers() == frozenset()


def test_a_graceful_close_lifts_what_the_engine_still_holds_and_a_later_release_all_adds_nothing(phone):
    channel = phone.channel()
    w = Wired(channel, BINDINGS)
    w.press("G")
    w.press("H")
    channel.close(graceful=True)                       # the session is ending while two buttons are held
    assert channel.wait_drained(3)
    w.engine.release_all()                             # the engine's own cleanup arrives too late to be sent
    assert w.injector.active_count == 0
    events = parse(raw(phone))
    assert sorted(p for a, p, *_ in events if a == D) == [0, 1, 2] and sorted(p for a, p, *_ in events if a == U) == [0, 1, 2]
    assert len(events) == 6                            # exactly one UP per finger, no duplicates


def test_when_the_phone_disconnects_the_engine_can_still_clean_up_and_forgets_its_fingers(phone):
    channel = phone.channel()
    w = Wired(channel, BINDINGS)
    w.press("G")
    w.press("H")
    phone.peer.close()
    for _ in range(100):
        w.engine.key_event("T", True)
        w.advance(50)
        if channel.closed:
            break
        time.sleep(0.01)
    assert channel.closed
    w.engine.release_all()                             # sending now fails; this must neither raise nor keep ghosts
    assert w.injector.active_count == 0
    w.press("H")                                       # a new press cannot reach the phone and leaves no finger behind
    assert w.injector.active_count == 0


def test_the_stalled_phone_receives_the_lifts_in_the_final_flush_after_it_wakes_up(stub):
    channel = make_channel(stub, stall_seconds=30)
    w = Wired(channel, BINDINGS)
    stub.stall(channel)
    w.press("G")
    w.press("H")
    w.engine.set_mapping_active(False)                 # "key mapping off": lifts everything
    stub.resume(channel)
    events = without_decoy(stub.events())
    assert [(e[1], e[2]) for e in events] == [(D, 0), (D, 1), (D, 2), (U, 0), (U, 1), (U, 2)]
    assert stub.events()[0] == DECOY
