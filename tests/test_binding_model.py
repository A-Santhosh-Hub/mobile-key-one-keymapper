"""The binding data model (Phase 2, step 2): Binding.id, extra triggers, extra actions, Action, the failure policy and the
read-only views. Nothing here runs the input engine: extra triggers and actions are STORED, not executed yet (the strict
tests in test_multi_trigger.py / test_multi_action.py / test_shared_runtime.py cover that part)."""
import pytest

from app.mapping.profile import (MAX_ALSO, MAX_DELAY_MS, MODES, ON_FAILURE, Action, Binding, Profile, ProfileError,
                                 Stick, valid_binding_id)


# ===================================================================== the existing constructor still works
def test_the_original_positional_constructor_and_defaults_are_unchanged():
    b = Binding("Shift+W", 0.8, 0.3, "hold", "Sprint", None, None, 12.0, 200, 0.2, True, [(0.1, 0.1, 0)], [(0.2, 0.2)])
    assert (b.input, b.x, b.y, b.mode, b.label, b.rate_hz, b.duration_ms, b.span, b.scope) == \
           ("Shift+W", 0.8, 0.3, "hold", "Sprint", 12.0, 200, 0.2, True)
    assert b.also == [(0.2, 0.2)]
    plain = Binding("W", 0.5, 0.5)
    assert (plain.mode, plain.label, plain.x2, plain.y2, plain.scope, plain.steps, plain.also) == \
           ("hold", "", None, None, False, [], [])
    assert (plain.extra_triggers, plain.extra_actions, plain.on_failure) == ([], [], "continue")


def test_the_new_fields_come_after_all_the_old_ones_so_old_positional_calls_cannot_shift():
    from dataclasses import fields
    names = [f.name for f in fields(Binding)]
    old = ["input", "x", "y", "mode", "label", "x2", "y2", "rate_hz", "duration_ms", "span", "scope", "steps", "also"]
    assert names[:len(old)] == old
    assert names[len(old):] == ["id", "extra_triggers", "extra_actions", "on_failure", "unknown", "primary_unknown"]


def test_a_binding_without_the_new_capabilities_looks_exactly_like_before():
    b = Binding("R", 0.7, 0.9, "tap", "Reload", scope=True, also=[(0.1, 0.2)])
    assert b.triggers == ("R",) and len(b.actions) == 1 and b.needs_v2 is False
    assert b.to_dict() == b.to_dict_v1() == {"type": "tap", "input": "R", "x": 0.7, "y": 0.9, "mode": "tap",
                                             "label": "Reload", "scope": True, "also": [[0.1, 0.2]]}


def test_equality_compares_what_the_binding_does_not_its_id():
    assert Binding("R", 0.5, 0.5, "tap") == Binding("R", 0.5, 0.5, "tap")
    assert Binding("R", 0.5, 0.5, "tap") != Binding("R", 0.5, 0.5, "tap", extra_triggers=["F"])
    assert Binding("R", 0.5, 0.5, "tap", id="a") == Binding("R", 0.5, 0.5, "tap", id="b")


# ===================================================================== triggers
def test_extra_triggers_are_canonicalised_like_the_first_one():
    b = Binding("E", 0.5, 0.5, "tap", extra_triggers=["Shift+Ctrl+F", "MMB", "WheelUp"])
    assert b.extra_triggers == ["Ctrl+Shift+F", "MMB", "WheelUp"]
    assert b.triggers == ("E", "Ctrl+Shift+F", "MMB", "WheelUp") and b.needs_v2


@pytest.mark.parametrize("bad", [["E"], ["F", "F"], ["Shift+F", "Shift+F"], ["Ctrl+Shift+F", "Shift+Ctrl+F"], [""], [None],
                                 [5], ["Ctrl+Shift"]])
def test_a_trigger_cannot_repeat_the_first_one_or_another_extra_or_be_unusable(bad):
    with pytest.raises(ProfileError):
        Binding("E", 0.5, 0.5, "tap", extra_triggers=bad)


def test_the_first_trigger_is_still_the_input_and_a_lone_modifier_is_still_allowed_as_one():
    b = Binding("Shift", 0.5, 0.5, "hold", extra_triggers=["Ctrl+Shift+F"])
    assert b.input == "Shift" and b.combo == (frozenset(), "Shift") and b.triggers[0] == "Shift"


def test_the_triggers_view_is_a_tuple_and_cannot_be_used_to_change_the_binding():
    b = Binding("E", 0.5, 0.5, "tap", extra_triggers=["F"])
    assert isinstance(b.triggers, tuple)
    with pytest.raises(AttributeError):
        b.triggers = ("X",)
    assert b.triggers == ("E", "F")


# ===================================================================== actions
def test_the_primary_action_view_mirrors_the_binding_and_is_a_snapshot():
    b = Binding("Q", 0.2, 0.5, "swipe", x2=0.6, y2=0.3, duration_ms=200, scope=True, also=[])
    primary = b.actions[0]
    assert isinstance(primary, Action)
    assert (primary.mode, primary.x, primary.y, primary.x2, primary.y2, primary.duration_ms, primary.scope, primary.delay_ms) == \
           ("swipe", 0.2, 0.5, 0.6, 0.3, 200, True, 0)
    primary.x = 0.9                                                  # a change to the snapshot is not a change to the binding
    assert b.x == 0.2 and b.actions[0].x == 0.2


def test_every_mode_survives_the_trip_into_the_primary_action_view_without_losing_a_setting():
    specs = {"repeat": {"rate_hz": 20}, "swipe": {"x2": 0.7, "y2": 0.1, "duration_ms": 300}, "drag": {"x2": 0.7, "y2": 0.1},
             "pinch-out": {"span": 0.25, "duration_ms": 500}, "pinch-in": {"span": 0.05},
             "sequence": {"steps": [(0.1, 0.1, 0), (0.3, 0.3, 250)]}, "hold": {"also": [(0.9, 0.9), (0.8, 0.8)]},
             "tap": {"scope": True}, "toggle": {"also": [(0.9, 0.9)], "scope": True}, "double": {}}
    assert set(specs) == set(MODES)
    for mode, kw in specs.items():
        b = Binding("K", 0.4, 0.6, mode, **kw)
        a = b.actions[0]
        for name in ("mode", "x", "y", "x2", "y2", "rate_hz", "duration_ms", "span", "scope", "steps", "also"):
            assert getattr(a, name) == getattr(b, name), (mode, name)


def test_extra_actions_have_their_own_mode_and_settings_and_are_the_live_objects():
    shoot = Action("tap", 0.5, 0.2, delay_ms=60, scope=True)
    b = Binding("LMB", 0.8, 0.7, "hold", extra_actions=[shoot, Action("repeat", 0.3, 0.3, rate_hz=20)])
    assert [a.mode for a in b.actions] == ["hold", "tap", "repeat"]
    assert b.actions[1] is shoot and b.actions[2].rate_hz == 20 and b.actions[1].scope and not b.scope
    assert [a.delay_ms for a in b.actions] == [0, 60, 0]


def test_also_and_extra_actions_are_different_things_and_both_are_kept():
    b = Binding("LMB", 0.8, 0.7, "hold", also=[(0.1, 0.4)], extra_actions=[Action("tap", 0.5, 0.2, also=[(0.6, 0.6)])])
    assert b.also == [(0.1, 0.4)] and b.actions[0].also == [(0.1, 0.4)]
    assert b.actions[1].also == [(0.6, 0.6)] and len(b.extra_actions) == 1
    assert Binding("LMB", 0.8, 0.7, "hold", also=[(0.1, 0.4)]).needs_v2 is False         # `also` alone needs nothing new


def test_the_action_list_given_to_the_constructor_is_copied_and_must_hold_actions():
    shared = [Action("tap", 0.5, 0.5)]
    b = Binding("K", 0.5, 0.5, extra_actions=shared)
    shared.append(Action("tap", 0.1, 0.1))
    assert len(b.extra_actions) == 1
    for bad in ([{"mode": "tap", "x": 0.5, "y": 0.5}], ["tap"], [None]):
        with pytest.raises(ProfileError):
            Binding("K", 0.5, 0.5, extra_actions=bad)


def test_an_action_is_cleaned_exactly_like_the_binding_fields_it_mirrors():
    raw = dict(x=1.5, y=-0.2, rate_hz=99, duration_ms=10, span=9, also=[(2, 2)] * 9)
    b = Binding("K", mode="repeat", **raw)
    a = Action("repeat", **raw)
    assert (a.x, a.y, a.rate_hz, a.duration_ms, a.span, a.also) == (b.x, b.y, b.rate_hz, b.duration_ms, b.span, b.also)
    assert (a.x, a.y, a.rate_hz, a.duration_ms, len(a.also)) == (1.0, 0.0, 30.0, 40, MAX_ALSO)
    swipe = Action("swipe", 0.4, 0.5)
    assert (swipe.x2, swipe.y2) == (pytest.approx(0.55), 0.5)             # a swipe gets a default end like a Binding does
    assert Action("sequence", 0.3, 0.3).steps == [(0.3, 0.3, 0)]
    with pytest.raises(ProfileError):
        Action("fly", 0.5, 0.5)
    with pytest.raises(ProfileError):
        Action("sequence", 0.5, 0.5, steps=[(1, 2)])
    with pytest.raises(ProfileError):
        Action("hold", 0.5, 0.5, also=[(1,)])


# ===================================================================== delays and the failure policy
def test_delays_are_whole_non_negative_numbers_of_milliseconds():
    assert Action("tap", 0.5, 0.5).delay_ms == 0
    assert Action("tap", 0.5, 0.5, delay_ms=0).delay_ms == 0 and Action("tap", 0.5, 0.5, delay_ms=MAX_DELAY_MS).delay_ms == MAX_DELAY_MS
    for bad in (-1, MAX_DELAY_MS + 1, 1.5, 60.0, "60", None, True, False, [60]):
        with pytest.raises(ProfileError):
            Action("tap", 0.5, 0.5, delay_ms=bad)


def test_the_failure_policy_defaults_to_continue_and_only_known_values_are_accepted():
    assert ON_FAILURE == ("continue", "abort")
    assert Binding("K", 0.5, 0.5).on_failure == "continue"
    b = Binding("K", 0.5, 0.5, on_failure="abort")
    assert b.on_failure == "abort" and b.needs_v2
    for bad in ("Abort", "stop", "", None, 0, ["abort"]):
        with pytest.raises(ProfileError):
            Binding("K", 0.5, 0.5, on_failure=bad)


def test_editing_a_binding_and_validating_it_again_checks_the_new_fields_too():
    b = Binding("K", 0.5, 0.5, "tap", extra_triggers=["F"])
    b.on_failure = "explode"
    with pytest.raises(ProfileError):
        b.__post_init__()                                             # what the options dialog does after editing
    b.on_failure = "abort"
    b.extra_triggers.append("F")
    with pytest.raises(ProfileError):
        b.__post_init__()


# ===================================================================== ids
def test_every_binding_gets_a_valid_unique_runtime_id():
    ids = [Binding("K", 0.5, 0.5).id for _ in range(200)]
    assert all(valid_binding_id(i) for i in ids) and len(set(ids)) == 200


def test_an_id_stays_the_same_when_the_binding_is_edited_or_revalidated():
    b = Binding("K", 0.5, 0.5, "tap")
    before = b.id
    b.set_mode("swipe")
    b.x = 0.9
    b.__post_init__()
    assert b.id == before and b.mode == "swipe"


def test_an_explicit_id_is_kept_and_an_unusable_one_is_refused():
    assert Binding("K", 0.5, 0.5, id="fire-1").id == "fire-1"
    for bad in ("has space", "x" * 65, "ü", "a/b", 5, ["a"]):
        with pytest.raises(ProfileError):
            Binding("K", 0.5, 0.5, id=bad)
    assert valid_binding_id("A_b-c.9") and not valid_binding_id("") and not valid_binding_id(None)


def test_ids_are_unique_inside_a_profile_even_if_two_bindings_are_made_with_the_same_one():
    p = Profile("p")
    p.add(Binding("A", 0.1, 0.1, id="same"))
    p.add(Binding("B", 0.2, 0.2, id="same"))
    assert len({b.id for b in p.bindings}) == 2 and p.find("A").id == "same"      # the one already there keeps it


def test_replacing_a_binding_by_input_may_reuse_the_replaced_ones_id():
    p = Profile("p")
    p.add(Binding("A", 0.1, 0.1, id="keep"))
    p.add(Binding("A", 0.9, 0.9, id="keep"))
    assert [(b.input, b.id, b.x) for b in p.bindings] == [("A", "keep", 0.9)]


# ===================================================================== the public Profile API
def test_add_find_remove_and_key_owner_behave_as_before_for_single_input_bindings():
    p = Profile("p")
    p.add(Binding("A", 0.1, 0.1))
    p.add(Binding("A", 0.3, 0.3, "tap"))
    p.add(Binding("Shift+A", 0.5, 0.5))
    assert [(b.input, b.mode) for b in p.bindings] == [("A", "tap"), ("Shift+A", "hold")]
    assert p.find("A").mode == "tap" and p.find("Shift+A") is not None and p.find("B") is None
    assert p.key_owner("A") == "another control" and p.key_owner("Z") is None
    p.set_stick(Stick(0.15, 0.7))
    assert p.key_owner("W") == "the movement stick"
    p.remove("A")
    assert p.find("A") is None and p.find("Shift+A") is not None


def test_find_key_owner_and_remove_look_at_the_first_input_only_extra_triggers_are_asked_through_bindings_for():
    # Deliberate for this step: the editor flows that use find / key_owner / remove know nothing about extra triggers.
    b = Binding("E", 0.5, 0.5, "tap", extra_triggers=["F"])
    other = Binding("G", 0.1, 0.1, extra_triggers=["F"])
    p = Profile("p", bindings=[b, other])
    assert p.find("F") is None and p.key_owner("F") is None
    assert p.bindings_for("F") == [b, other] and p.bindings_for("E") == [b] and p.bindings_for("Z") == []
    p.remove("F")
    assert len(p.bindings) == 2
    p.remove("E")
    assert p.bindings == [other]


def test_a_new_stick_removes_bindings_on_its_keys_and_extra_triggers_on_its_keys():
    p = Profile("p", bindings=[Binding("W", 0.1, 0.1), Binding("R", 0.2, 0.2, extra_triggers=["A", "F"]),
                               Binding("Q", 0.3, 0.3)])
    assert p.set_stick(Stick(0.15, 0.7)) == ["W", "A"]
    assert [b.input for b in p.bindings] == ["R", "Q"] and p.find("R").extra_triggers == ["F"]


def test_a_binding_that_asks_for_version_2_does_not_change_how_the_others_in_memory_look():
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1), Binding("B", 0.2, 0.2, extra_triggers=["F"])])
    assert [b.needs_v2 for b in p.bindings] == [False, True] and p.required_version == 2
    assert p.find("A").to_dict() == p.find("A").to_dict_v1()                    # a binding only becomes v2 inside a v2 profile
