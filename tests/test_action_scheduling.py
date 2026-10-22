"""Delayed actions start in a deterministic order (Phase 2, step 6.1).

The rule: delayed actions start in order of their ABSOLUTE deadline (the activation instant plus their delay_ms); actions with
the same deadline start in the order they were queued, which is the binding's action-list order, and for different activations
the order the activations happened. Actions with a delay of 0 start immediately, as before.

A real timer system gives no promise for timers that are due at the same instant, so these tests do not use a friendly one: the
`TieRig` below runs equal-deadline timers in FIFO, in LIFO or in a seeded random order. The engine's start order must be the same
in all of them. (Timers of actions that are already running, such as the end of a tap, are unchanged and are not part of this
rule.)"""
import random

import pytest

from app.mapping.profile import Action, Binding, Look, Profile
from app.stream import protocol
from test_characterization_engine import SPECS, Rig

D, U, M = protocol.TOUCH_DOWN, protocol.TOUCH_UP, protocol.TOUCH_MOVE
W, H = 2400, 1080
ORDERS = ["fifo", "lifo", 1, 2, 3, 4]               # a number = a random tie order with that seed


class TieRig(Rig):
    """A Rig whose manual scheduler breaks ties between timers due at the same instant in the order the test asks for."""

    def __init__(self, *args, order="fifo", **kwargs):
        self.order = order
        self._rng = random.Random(order if isinstance(order, int) else 0)
        super().__init__(*args, **kwargs)

    def advance(self, ms):
        end = self.now + ms
        while True:
            due = [t for t in self.timers if t[0] <= end]
            if not due:
                break
            first = min(t[0] for t in due)
            tied = [t for t in due if t[0] == first]
            pick = tied[0] if self.order == "fifo" else tied[-1] if self.order == "lifo" else self._rng.choice(tied)
            self.timers.remove(pick)
            self.now = pick[0]
            pick[1]()
        self.now = end


def act(mode, x, y, delay_ms=0, **kw):
    return Action(mode, x, y, delay_ms=delay_ms, **{**SPECS.get(mode, {}), **kw})


def multi(key, extras, mode="hold", x=0.1, y=0.9, **kw):
    return Binding(key, x, y, mode, extra_actions=list(extras), **{**SPECS.get(mode, {}), **kw})


def px(x, y):
    return (int(x * W + 1e-6), int(y * H + 1e-6))


def downs(r):
    """[(pointer id, (x, y))] of every DOWN, in the order sent."""
    return [(t[1], t[2:4]) for t in r.touches() if t[0] == D]


def spots(r):
    return [spot for _pid, spot in downs(r)]


# ===================================================================== equal deadlines
@pytest.mark.parametrize("order", ORDERS)
def test_actions_with_the_same_deadline_start_in_list_order_whatever_the_timer_system_does(order):
    r = TieRig([multi("F", [act("hold", 0.2, 0.2, 100), act("hold", 0.3, 0.3, 100), act("hold", 0.4, 0.4, 100), act("hold", 0.5, 0.5, 100)])],
               order=order)
    r.press("F", 99)
    assert spots(r) == [px(0.1, 0.9)]                              # only the first action so far
    r.advance(1)
    assert spots(r) == [px(0.1, 0.9), px(0.2, 0.2), px(0.3, 0.3), px(0.4, 0.4), px(0.5, 0.5)]
    assert [pid for pid, _ in downs(r)] == [0, 1, 2, 3, 4]         # so the pointer ids follow the list too
    r.release("F")
    assert r.fingers() == 0 and r.timers == []


@pytest.mark.parametrize("order", ORDERS)
def test_actions_with_different_deadlines_start_in_deadline_order_not_list_order(order):
    r = TieRig([multi("F", [act("hold", 0.2, 0.2, 120), act("hold", 0.3, 0.3, 60), act("hold", 0.4, 0.4, 90)])], order=order)
    r.press("F", 59)
    assert len(spots(r)) == 1
    r.advance(1)
    assert spots(r)[-1] == px(0.3, 0.3)                            # 60 ms: list position 2
    r.advance(30)
    assert spots(r)[-1] == px(0.4, 0.4)                            # 90 ms: list position 3
    r.advance(30)
    assert spots(r)[-1] == px(0.2, 0.2) and len(spots(r)) == 4     # 120 ms: list position 1
    assert [pid for pid, _ in downs(r)] == [0, 1, 2, 3]


@pytest.mark.parametrize("order", ORDERS)
def test_deadlines_a_few_milliseconds_apart_are_still_ordered_by_deadline_and_start_on_time(order):
    r = TieRig([multi("F", [act("hold", 0.2, 0.2, 105), act("hold", 0.3, 0.3, 100), act("hold", 0.4, 0.4, 103), act("hold", 0.5, 0.5, 101)])],
               order=order)
    r.press("F", 99)
    assert len(spots(r)) == 1
    started = []
    for _ in range(7):
        r.advance(1)
        started.append(len(spots(r)))
    assert spots(r)[1:] == [px(0.3, 0.3), px(0.5, 0.5), px(0.4, 0.4), px(0.2, 0.2)]      # 100, 101, 103, 105 ms
    assert started[0] == 2 and started[1] == 3 and started[3] == 4 and started[5] == 5      # each on its own millisecond


@pytest.mark.parametrize("order", ORDERS)
def test_an_immediate_action_and_delayed_ones_keep_their_own_rules(order):
    r = TieRig([multi("F", [act("hold", 0.2, 0.2, 100), act("hold", 0.3, 0.3, 0), act("hold", 0.4, 0.4, 100), act("hold", 0.5, 0.5, 50)])],
               order=order)
    r.press("F")
    assert spots(r) == [px(0.1, 0.9), px(0.3, 0.3)]                # the first action and the zero-delay one, at once, in list order
    assert len(r.timers) == 3                                      # a wake-up per delayed action (100, 100, 50), nothing for the immediate one
    r.advance(49)
    assert len(spots(r)) == 2
    r.advance(1)
    assert spots(r)[-1] == px(0.5, 0.5)
    r.advance(50)
    assert spots(r)[3:] == [px(0.2, 0.2), px(0.4, 0.4)]            # equal deadlines 100: list order again
    r.release("F")
    assert r.fingers() == 0


@pytest.mark.parametrize("order", ORDERS)
def test_the_delay_is_counted_from_the_activation_even_when_a_wake_up_comes_late(order):
    # nothing runs for 300 ms (a stalled event loop): everything that is due then starts, still by deadline and then list order
    r = TieRig([multi("F", [act("hold", 0.2, 0.2, 100), act("hold", 0.3, 0.3, 50), act("hold", 0.4, 0.4, 100), act("hold", 0.5, 0.5, 250)])],
               order=order)
    r.press("F")
    r.advance(300)
    assert spots(r)[1:] == [px(0.3, 0.3), px(0.2, 0.2), px(0.4, 0.4), px(0.5, 0.5)]


@pytest.mark.parametrize("order", ORDERS)
def test_the_first_wake_up_to_arrive_starts_everything_that_is_due_and_the_others_find_nothing(order):
    r = TieRig([multi("F", [act("tap", 0.2, 0.2, 100), act("tap", 0.3, 0.3, 100)], mode="tap")], order=order)
    r.press("F", 500)
    assert spots(r) == [px(0.1, 0.9), px(0.2, 0.2), px(0.3, 0.3)]
    assert r.kinds().count(D) == 3 and r.kinds().count(U) == 3      # nothing started twice
    assert r.engine._starts == [] and r.timers == []


# ===================================================================== across activations
@pytest.mark.parametrize("order", ORDERS)
def test_equal_deadlines_of_different_activations_start_in_the_order_the_activations_happened(order):
    first = Binding("A", 0.1, 0.1, "hold", extra_actions=[act("hold", 0.2, 0.2, 100)])
    second = Binding("B", 0.3, 0.3, "hold", extra_actions=[act("hold", 0.4, 0.4, 50)])
    r = TieRig([first, second], order=order)
    r.press("A")                                                    # t = 0: its extra is due at 100
    r.advance(50)
    r.press("B")                                                    # t = 50: its extra is due at 50 + 50 = 100 as well
    r.advance(49)
    assert len(spots(r)) == 2
    r.advance(1)
    assert spots(r)[2:] == [px(0.2, 0.2), px(0.4, 0.4)]            # A's activation came first
    assert [pid for pid, _ in downs(r)] == [0, 1, 2, 3]


@pytest.mark.parametrize("order", ORDERS)
def test_a_binding_activated_again_queues_the_same_deadlines_in_the_same_order(order):
    r = TieRig([multi("F", [act("tap", 0.2, 0.2, 60), act("tap", 0.3, 0.3, 60)])], order=order)
    expected = []
    for _ in range(3):
        before = len(spots(r))
        r.press("F", 100)
        r.release("F", 100)
        expected.append(spots(r)[before:])
    assert expected == [[px(0.1, 0.9), px(0.2, 0.2), px(0.3, 0.3)]] * 3


@pytest.mark.parametrize("order", ORDERS)
def test_a_cancelled_run_cannot_influence_a_new_activation_with_the_same_deadline(order):
    r = TieRig([multi("F", [act("tap", 0.2, 0.2, 100)])], order=order)
    r.press("F", 20)
    r.engine.release_all()                                          # the first activation (due at 100) is gone for good
    r.release("F")
    r.advance(20)                                                   # t = 40
    r.press("F", 59)                                                # a new one, also due at 100 + 40 = 140
    assert len([s for s in spots(r) if s == px(0.2, 0.2)]) == 0
    r.advance(100)
    assert len([s for s in spots(r) if s == px(0.2, 0.2)]) == 1      # started once, by the new activation, at 140
    assert r.engine._starts == []


@pytest.mark.parametrize("order", ORDERS)
def test_a_finished_run_leaves_nothing_behind_for_the_next_one(order):
    r = TieRig([multi("F", [act("tap", 0.2, 0.2, 60)], mode="tap")], order=order)
    r.press("F", 500)
    assert r.engine._starts == [] and r.timers == [] and r.engine._runs == {}
    r.press("F", 59)
    assert len([s for s in spots(r) if s == px(0.2, 0.2)]) == 1
    r.advance(1)
    assert len([s for s in spots(r) if s == px(0.2, 0.2)]) == 2


# ===================================================================== cancellation
@pytest.mark.parametrize("order", ORDERS)
def test_cancelling_before_a_shared_deadline_stops_all_of_them(order):
    r = TieRig([multi("F", [act("tap", 0.2, 0.2, 100), act("tap", 0.3, 0.3, 100), act("tap", 0.4, 0.4, 100)])], order=order)
    r.press("F", 99)
    r.engine.release_all()
    r.advance(1000)
    assert spots(r) == [px(0.1, 0.9)] and r.fingers() == 0 and r.engine._starts == []


@pytest.mark.parametrize("order", ORDERS)
def test_releasing_the_trigger_before_a_shared_deadline_cancels_the_sustained_ones_and_keeps_the_one_shots_in_order(order):
    r = TieRig([multi("F", [act("hold", 0.2, 0.2, 100), act("tap", 0.3, 0.3, 100), act("repeat", 0.4, 0.4, 100), act("toggle", 0.5, 0.5, 100)],
                      mode="tap")], order=order)
    r.press("F", 50)
    r.release("F", 100)                                             # 50 ms past the shared deadline
    assert spots(r) == [px(0.1, 0.9), px(0.3, 0.3), px(0.5, 0.5)]   # the tap and the toggle, in list order; no hold, no repeat
    assert r.fingers() == 1                                         # only the toggle stays


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("how", ["release_all", "mapping_off", "screen_changed", "disconnect"])
def test_focus_loss_mapping_off_rotation_and_disconnect_cancel_every_queued_start(order, how):
    r = TieRig([multi("F", [act("tap", 0.2, 0.2, 100), act("hold", 0.3, 0.3, 100), act("tap", 0.4, 0.4, 250)])], order=order)
    r.press("F", 50)
    live = r.control
    if how == "disconnect":
        r.control = None
    {"release_all": r.engine.release_all, "mapping_off": lambda: r.engine.set_mapping_active(False),
     "screen_changed": lambda: (setattr(r, "size", (1080, 2400)), r.engine.screen_changed()),
     "disconnect": r.engine.release_all}[how]()
    assert r.engine._starts == [] and r.fingers() == 0
    r.control = live
    sent = len(r.touches())
    r.advance(5000)
    assert len(r.touches()) == sent and r.fingers() == 0 and r.timers == []


@pytest.mark.parametrize("order", ORDERS)
def test_a_new_activation_right_after_a_cleanup_gets_its_own_queue(order):
    r = TieRig([multi("F", [act("tap", 0.2, 0.2, 100)])], order=order)
    r.press("F", 10)
    r.engine.release_all()
    r.release("F")
    r.press("F", 10)
    r.engine.set_mapping_active(False)
    r.engine.set_mapping_active(True)
    r.release("F")
    r.press("F", 1000)
    assert len([s for s in spots(r) if s == px(0.2, 0.2)]) == 1


# ===================================================================== failures keep the order
@pytest.mark.parametrize("order", ORDERS)
def test_an_action_that_fails_at_a_shared_deadline_does_not_reorder_the_others_under_continue(order):
    fillers = [Binding(k, 0.05 + 0.09 * i, 0.95, "hold") for i, k in enumerate("ABCDEFGH")]       # eight pointers taken
    r = TieRig([multi("L", [act("hold", 0.2, 0.2, 100), act("hold", 0.3, 0.3, 100), act("hold", 0.4, 0.4, 100)], x=0.1, y=0.1)] + fillers, order=order)
    for k in "ABCDEFGH":
        r.press(k)
    r.press("L", 100)                                               # pointer 8 for the first action; at 100 ms pointer 9 for the first extra
    assert spots(r)[8:] == [px(0.1, 0.1), px(0.2, 0.2)]             # the extras after it find no pointer
    assert [(f[1], f[2]) for f in r.engine.recent_failures] == [(2, "no free touch pointer"), (3, "no free touch pointer")]
    assert r.fingers() == 10
    r.release("L")
    assert r.fingers() == 8


@pytest.mark.parametrize("order", ORDERS)
def test_failures_are_recorded_in_list_order_and_the_survivors_are_untouched(order):
    fillers = [Binding(k, 0.05 + 0.09 * i, 0.95, "hold") for i, k in enumerate("ABCDEFGHI")]       # nine pointers taken
    r = TieRig([multi("L", [act("hold", 0.2, 0.2, 100), act("hold", 0.3, 0.3, 100), act("hold", 0.4, 0.4, 100)], x=0.1, y=0.1)] + fillers, order=order)
    for k in "ABCDEFGHI":
        r.press(k)
    r.press("L", 100)                                               # the first action took the last pointer: all three extras fail
    assert [f[1] for f in r.engine.recent_failures] == [1, 2, 3]    # in list order, each once
    assert r.fingers() == 10 and spots(r)[9:] == [px(0.1, 0.1)]


@pytest.mark.parametrize("order", ORDERS)
def test_under_abort_the_first_failure_at_a_shared_deadline_cancels_the_rest_of_them(order):
    fillers = [Binding(k, 0.05 + 0.09 * i, 0.95, "hold") for i, k in enumerate("ABCDEFGHI")]
    r = TieRig([multi("L", [act("hold", 0.2, 0.2, 100), act("hold", 0.3, 0.3, 100)], x=0.1, y=0.1, on_failure="abort")] + fillers, order=order)
    for k in "ABCDEFGHI":
        r.press(k)
    r.press("L", 100)
    assert [f[1] for f in r.engine.recent_failures] == [1]          # only the first attempt; the second was cancelled with the run
    assert r.fingers() == 9                                         # L's own first finger was lifted by the abort


# ===================================================================== the clock
def test_a_frozen_or_wrong_clock_cannot_strand_a_delayed_action_or_change_the_order():
    # The queue orders by its own deadlines; a clock that does not move still lets every wake-up start what is its due.
    r = TieRig([multi("F", [act("hold", 0.2, 0.2, 100), act("hold", 0.3, 0.3, 100), act("hold", 0.4, 0.4, 40)])], order="lifo")
    r.engine._clock = lambda: 100.0
    r.press("F", 300)
    assert spots(r) == [px(0.1, 0.9), px(0.4, 0.4), px(0.2, 0.2), px(0.3, 0.3)]
    assert r.engine._starts == []


def test_zero_delay_actions_and_legacy_bindings_never_touch_the_queue():
    r = TieRig([multi("F", [act("hold", 0.2, 0.2, 0), act("hold", 0.3, 0.3, 0)]), Binding("G", 0.5, 0.5, "tap")], order="lifo")
    r.press("F")
    r.press("G", 100)
    assert r.engine._starts == [] and r.engine._start_seq == 0
    assert len(spots(r)) == 4
