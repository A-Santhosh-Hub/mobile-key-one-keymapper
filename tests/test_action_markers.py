"""Additional actions on the picture: each has its own marker that can be hit, dragged and deleted ON ITS OWN, found by identity
(binding id + action uid), and kept apart from the `also` spots of an action (extra touches that share one action's settings)."""
from app.mapping import layout_edit as le
from app.mapping.profile import Action, Binding, Profile

TW, TH = 1920.0, 864.0


def hit(p, x, y):
    return le.hit_test(p, x, y, TW, TH)


def drag(p, h, start, end):
    state = le.begin_drag(p, h, *start)
    le.update_drag(p, state, *end, TW, TH)


def two_actions():
    p = Profile("p", bindings=[
        Binding("F", 0.2, 0.2, "hold", "Fire", id="fire", also=[(0.1, 0.5)],
                extra_actions=[Action("tap", 0.6, 0.6, delay_ms=100, uid="s1", also=[(0.7, 0.7)]),
                               Action("swipe", 0.4, 0.8, x2=0.5, y2=0.9, delay_ms=200, uid="s2")]),
        Binding("R", 0.9, 0.1, "tap", "Reload", id="reload")])
    return p


def test_an_additional_action_marker_is_hit_by_its_identity_not_its_index():
    p = two_actions()
    h = hit(p, 0.6, 0.6)
    assert h == le.Hit("tap", 0, "act:s1") and h.binding_id == "fire"
    assert hit(p, 0.4, 0.8) == le.Hit("tap", 0, "act:s2")
    assert hit(p, 0.2, 0.2) == le.Hit("tap", 0)                                    # the control's own marker is still just the control


def test_the_also_spots_of_the_first_action_and_of_an_additional_action_are_different_things():
    p = two_actions()
    assert hit(p, 0.1, 0.5) == le.Hit("tap", 0, "also0")                           # an extra touch of the FIRST action
    assert hit(p, 0.7, 0.7) == le.Hit("tap", 0, "act:s1/also0")                    # an extra touch of the ADDITIONAL action
    assert hit(p, 0.5, 0.9) == le.Hit("tap", 0, "act:s2/end")                      # the end of the additional swipe


def test_an_action_hit_still_means_that_action_after_the_list_was_reordered_or_actions_were_removed():
    p = two_actions()
    h = hit(p, 0.4, 0.8)
    p.bindings.insert(0, Binding("N", 0.9, 0.9, "tap", id="n"))                    # the position of Fire changed
    p.bindings[1].extra_actions.pop(0)                                             # and its first additional action is gone
    assert le.tap_position(p, h) == 1
    drag(p, h, (0.4, 0.8), (0.3, 0.7))
    s2 = p.bindings[1].extra_actions[0]
    assert (round(s2.x, 3), round(s2.y, 3)) == (0.3, 0.7) and (round(s2.x2, 3), round(s2.y2, 3)) == (0.4, 0.8)   # its end moved along
    assert (p.bindings[1].x, p.bindings[1].y) == (0.2, 0.2) and (p.bindings[0].x, p.bindings[0].y) == (0.9, 0.9)


def test_dragging_one_action_moves_only_that_action_and_its_own_spots():
    p = two_actions()
    drag(p, hit(p, 0.6, 0.6), (0.6, 0.6), (0.5, 0.4))
    fire, s1 = p.bindings[0], p.bindings[0].extra_actions[0]
    assert (round(s1.x, 3), round(s1.y, 3)) == (0.5, 0.4) and [(round(a, 3), round(b, 3)) for a, b in s1.also] == [(0.6, 0.5)]
    assert (fire.x, fire.y) == (0.2, 0.2) and fire.also == [(0.1, 0.5)]                    # the first action did not move
    assert (p.bindings[0].extra_actions[1].x, p.bindings[0].extra_actions[1].y) == (0.4, 0.8)


def test_dragging_an_additional_actions_extra_spot_or_end_moves_just_that_point():
    p = two_actions()
    drag(p, hit(p, 0.7, 0.7), (0.7, 0.7), (0.8, 0.5))
    drag(p, hit(p, 0.5, 0.9), (0.5, 0.9), (0.6, 0.8))
    s1, s2 = p.bindings[0].extra_actions
    assert [(round(a, 3), round(b, 3)) for a, b in s1.also] == [(0.8, 0.5)] and (s1.x, s1.y) == (0.6, 0.6)
    assert (round(s2.x2, 3), round(s2.y2, 3)) == (0.6, 0.8) and (s2.x, s2.y) == (0.4, 0.8)


def test_a_drag_on_an_action_that_is_gone_does_nothing_and_does_not_fail():
    p = two_actions()
    h = hit(p, 0.6, 0.6)
    p.bindings[0].extra_actions = [a for a in p.bindings[0].extra_actions if a.uid != "s1"]
    before = p.to_dict()
    drag(p, h, (0.6, 0.6), (0.1, 0.1))
    assert p.to_dict() == before


def test_deleting_an_additional_action_keeps_the_control_and_the_other_actions():
    p = two_actions()
    assert le.delete(p, hit(p, 0.6, 0.6))
    fire = p.bindings[0]
    assert [a.uid for a in fire.extra_actions] == ["s2"] and (fire.x, fire.y) == (0.2, 0.2) and fire.also == [(0.1, 0.5)]
    assert [b.id for b in p.bindings] == ["fire", "reload"]


def test_deleting_an_extra_spot_of_an_additional_action_keeps_the_action():
    p = two_actions()
    assert le.delete(p, hit(p, 0.7, 0.7))
    assert p.bindings[0].extra_actions[0].also == [] and len(p.bindings[0].extra_actions) == 2


def test_deleting_the_controls_own_marker_still_deletes_the_whole_control_and_its_sharing():
    p = two_actions()
    p.bindings.append(Binding("F", 0.3, 0.3, "tap", id="other"))
    p.share_trigger("F", ["fire", "other"])
    assert le.delete(p, hit(p, 0.2, 0.2))
    assert [b.id for b in p.bindings] == ["reload", "other"] and p.shared_triggers == {}


def test_deleting_with_a_stale_action_or_point_is_refused_and_changes_nothing():
    p = two_actions()
    stale = le.Hit("tap", 0, "act:gone", "fire")
    stale_also = le.Hit("tap", 0, "act:s1/also5", "fire")
    stale_step = le.Hit("tap", 0, "act:s2/step7", "fire")
    before = p.to_dict()
    assert not le.delete(p, stale) and not le.delete(p, stale_also) and not le.delete(p, stale_step)
    assert p.to_dict() == before


def test_a_two_point_multi_tap_action_is_removed_as_a_whole_but_a_longer_one_loses_a_point():
    p = Profile("p", bindings=[Binding("F", 0.1, 0.1, "tap", id="f", extra_actions=[
        Action("sequence", 0.4, 0.4, steps=[(0.4, 0.4, 0), (0.5, 0.5, 100), (0.6, 0.6, 100)], uid="seq", delay_ms=50)])])
    assert hit(p, 0.5, 0.5) == le.Hit("tap", 0, "act:seq/step1")
    assert le.delete(p, hit(p, 0.5, 0.5)) and len(p.bindings[0].extra_actions[0].steps) == 2
    assert le.delete(p, hit(p, 0.6, 0.6)) and p.bindings[0].extra_actions == []                      # two points left: the action goes


def test_add_also_to_an_additional_action_by_identity():
    p = two_actions()
    assert le.add_also(p, 0, (0.3, 0.3), "s1") and p.bindings[0].extra_actions[0].also[-1] == (0.3, 0.3)
    assert not le.add_also(p, 0, (0.3, 0.3), "gone")
    assert not le.add_also(p, 0, (0.3, 0.3), "s2")                                    # a swipe cannot touch extra spots
    assert le.add_also(p, 0, (0.31, 0.31)) and p.bindings[0].also[-1] == (0.31, 0.31)  # the first action's own


def test_part_helpers_round_trip_and_ignore_plain_parts():
    assert le.split_part("act:s1/also2") == ("s1", "also2") and le.split_part("act:s1") == ("s1", "") and le.split_part("also1") == (None, "also1")
    assert le.action_part("s1", "end") == "act:s1/end" and le.action_part("s1") == "act:s1"
    assert le.also_index("act:s1/also2") == 2 and le.step_index("act:s1/step3") == 3 and le.also_index("end") is None


def test_the_legacy_hit_test_answers_are_unchanged_for_a_control_without_additional_actions():
    p = Profile("p", bindings=[Binding("Q", 0.2, 0.5, "swipe", x2=0.6, y2=0.5, id="q"), Binding("F", 0.1, 0.1, "sequence", id="s",
                                                                                              steps=[(0.1, 0.1, 0), (0.3, 0.3, 200)])])
    assert hit(p, 0.6, 0.5) == le.Hit("tap", 0, "end") and hit(p, 0.2, 0.5) == le.Hit("tap", 0) and hit(p, 0.3, 0.3) == le.Hit("tap", 1, "step1")
