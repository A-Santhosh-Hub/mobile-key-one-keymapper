"""Explicit profile operations (Phase 2, step 3): sharing a trigger on purpose, finding bindings by trigger or id, removing
bindings by id, and adding / removing / moving triggers. Pure data model: nothing here runs the input engine, and the
existing Profile.add / find / remove / key_owner keep their old meaning (pinned at the end)."""
import pytest

from app.mapping.profile import Action, Binding, Profile, ProfileError, Stick


def ids(profile):
    return [b.id for b in profile.bindings]


def shape(profile):
    """Everything that identifies the bindings, in order: id, triggers and the first action's settings."""
    return [(b.id, b.triggers, b.mode, b.x, b.y, b.label) for b in profile.bindings]


def trio():
    """Fire on LMB (also on MMB), a second action on LMB, and an unrelated Reload."""
    p = Profile("p")
    p.add(Binding("LMB", 0.8, 0.7, "hold", "Fire", id="fire", extra_triggers=["MMB", "F"]))
    p.add_shared(Binding("LMB", 0.5, 0.2, "tap", "Peek", id="peek"))
    p.add(Binding("R", 0.7, 0.9, "tap", "Reload", id="reload"))
    return p


# ===================================================================== sharing a trigger on purpose
def test_add_shared_keeps_every_binding_and_puts_the_new_one_last():
    p = Profile("p")
    p.add(Binding("LMB", 0.8, 0.7, "hold", id="a"))
    p.add(Binding("R", 0.7, 0.9, "tap", id="r"))
    returned = p.add_shared(Binding("LMB", 0.5, 0.2, "tap", id="b"))
    assert returned is p.bindings[-1] and ids(p) == ["a", "r", "b"]
    p.add_shared(Binding("LMB", 0.1, 0.1, "tap", id="c"))
    assert ids(p) == ["a", "r", "b", "c"] and [b.input for b in p.bindings].count("LMB") == 3


def test_add_replaces_and_add_shared_does_not_that_is_the_whole_difference():
    replaced, shared = Profile("p"), Profile("p")
    for p, how in ((replaced, "add"), (shared, "add_shared")):
        p.add(Binding("LMB", 0.8, 0.7, "hold", id="a"))
        getattr(p, how)(Binding("LMB", 0.5, 0.2, "tap", id="b"))
    assert ids(replaced) == ["b"] and ids(shared) == ["a", "b"]


def test_a_shared_binding_whose_id_is_taken_gets_a_new_one_and_the_older_binding_keeps_its_id():
    p = Profile("p")
    p.add(Binding("A", 0.1, 0.1, id="same"))
    added = p.add_shared(Binding("A", 0.2, 0.2, id="same"))
    assert p.bindings[0].id == "same" and added.id != "same" and len(set(ids(p))) == 2


def test_sharing_with_a_combination_or_a_different_first_key_is_just_a_normal_add():
    p = Profile("p")
    p.add_shared(Binding("E", 0.1, 0.1, id="a"))
    p.add_shared(Binding("Shift+E", 0.2, 0.2, id="b"))
    assert ids(p) == ["a", "b"]


# ===================================================================== finding
def test_bindings_for_lists_every_binding_that_uses_an_input_as_first_or_extra_trigger_in_list_order():
    p = trio()
    assert [b.id for b in p.bindings_for("LMB")] == ["fire", "peek"]
    assert [b.id for b in p.bindings_for("MMB")] == ["fire"] and [b.id for b in p.bindings_for("F")] == ["fire"]
    assert p.bindings_for("Z") == []


def test_bindings_for_reads_names_like_the_binding_model_does_and_each_binding_counts_once():
    p = Profile("p", bindings=[Binding("Shift+E", 0.1, 0.1, id="a", extra_triggers=["Ctrl+Shift+F"])])
    assert [b.id for b in p.bindings_for("Shift+E")] == ["a"]
    assert [b.id for b in p.bindings_for("Shift+Ctrl+F")] == ["a"]              # order of modifiers does not matter
    assert [b.id for b in p.bindings_for("Ctrl+Shift+F")] == ["a"]
    for unusable in ("", "Ctrl+Shift", None, 5):
        assert p.bindings_for(unusable) == []                                    # nothing can match; no exception


def test_find_by_id_finds_exactly_that_binding_even_if_inputs_are_shared():
    p = trio()
    assert p.find_by_id("peek") is p.bindings[1] and p.find_by_id("fire") is p.bindings[0]
    assert p.find_by_id("nope") is None and p.find_by_id("") is None


# ===================================================================== removing by id
def test_remove_by_id_removes_that_binding_only_and_returns_it():
    p = trio()
    before = shape(p)
    removed = p.remove_by_id("fire")
    assert removed.id == "fire" and ids(p) == ["peek", "reload"]
    assert shape(p) == [row for row in before if row[0] != "fire"]               # the others are untouched and keep their order


def test_remove_by_id_leaves_the_other_binding_on_the_same_input_alone():
    p = trio()
    p.remove_by_id("peek")
    assert ids(p) == ["fire", "reload"] and p.find("LMB").id == "fire"


def test_removing_an_unknown_id_changes_nothing():
    p = trio()
    before = shape(p)
    assert p.remove_by_id("nope") is None and p.remove_by_id("") is None
    assert shape(p) == before


def test_if_two_bindings_were_given_the_same_id_by_hand_only_the_first_is_removed():
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1), Binding("B", 0.2, 0.2)])
    p.bindings[1].id = p.bindings[0].id
    p.remove_by_id(p.bindings[0].id)
    assert [b.input for b in p.bindings] == ["B"]


# ===================================================================== binding-level trigger editing
def test_a_binding_can_gain_a_trigger_and_it_is_written_in_canonical_form():
    b = Binding("E", 0.5, 0.5, "tap")
    assert b.add_trigger("Shift+Ctrl+F") == "Ctrl+Shift+F" and b.triggers == ("E", "Ctrl+Shift+F") and b.needs_v2
    for bad in ("E", "Ctrl+Shift+F", "Shift+Ctrl+F", "", "Ctrl+Shift", None):
        with pytest.raises(ProfileError):
            b.add_trigger(bad)
    assert b.triggers == ("E", "Ctrl+Shift+F")


def test_removing_an_extra_trigger_leaves_the_first_one_and_the_action_alone():
    b = Binding("E", 0.5, 0.5, "swipe", x2=0.9, y2=0.1, extra_triggers=["F", "MMB"])
    assert b.remove_trigger("F") == "removed"
    assert b.input == "E" and b.triggers == ("E", "MMB") and (b.mode, b.x2) == ("swipe", 0.9)


def test_removing_the_first_trigger_promotes_the_next_one_and_the_action_does_not_change():
    b = Binding("E", 0.5, 0.5, "repeat", "Use", rate_hz=12, id="use", also=[], extra_triggers=["Shift+F", "MMB"],
                extra_actions=[Action("tap", 0.1, 0.1, delay_ms=40)])
    action_before = b.actions
    assert b.remove_trigger("E") == "promoted"
    assert b.input == "Shift+F" and b.extra_triggers == ["MMB"] and b.triggers == ("Shift+F", "MMB")
    assert b.combo == (frozenset({"Shift"}), "F")                                  # the engine-facing view follows
    assert b.actions == action_before and b.id == "use" and b.label == "Use"


def test_the_last_trigger_cannot_be_removed_from_a_binding_and_unknown_ones_are_refused():
    b = Binding("E", 0.5, 0.5, extra_triggers=["F"])
    with pytest.raises(ProfileError, match="not one"):
        b.remove_trigger("Z")
    b.remove_trigger("F")
    with pytest.raises(ProfileError, match="only key"):
        b.remove_trigger("E")
    assert b.triggers == ("E",)


# ===================================================================== profile-level trigger editing
def test_add_trigger_gives_a_binding_another_input_by_id_and_touches_nothing_else():
    p = trio()
    before = {b.id: b.triggers for b in p.bindings}
    assert p.add_trigger("reload", "Shift+Ctrl+T") == "Ctrl+Shift+T"
    after = {b.id: b.triggers for b in p.bindings}
    assert after.pop("reload") == ("R", "Ctrl+Shift+T") and after == {k: v for k, v in before.items() if k != "reload"}


def test_an_input_that_another_binding_uses_is_refused_unless_sharing_is_requested_and_that_binding_is_untouched():
    p = trio()
    before = shape(p)
    with pytest.raises(ProfileError, match="shared=True"):
        p.add_trigger("reload", "LMB")
    with pytest.raises(ProfileError, match="shared=True"):
        p.add_trigger("reload", "MMB")                                              # an extra trigger of another binding counts too
    assert shape(p) == before
    p.add_trigger("reload", "LMB", shared=True)
    assert [b.id for b in p.bindings_for("LMB")] == ["fire", "peek", "reload"]
    assert [b.triggers for b in p.bindings if b.id in ("fire", "peek")] == [("LMB", "MMB", "F"), ("LMB",)]


def test_a_stick_key_is_never_accepted_even_when_sharing_is_requested():
    p = trio()
    p.set_stick(Stick(0.15, 0.7))
    for shared in (False, True):
        with pytest.raises(ProfileError, match="stick"):
            p.add_trigger("reload", "W", shared=shared)
    assert p.find_by_id("reload").triggers == ("R",)


def test_add_trigger_refuses_a_repeat_an_unusable_name_or_an_unknown_binding():
    p = trio()
    for args in (("reload", "R"), ("reload", ""), ("reload", "Ctrl+Shift"), ("ghost", "Z")):
        with pytest.raises(ProfileError):
            p.add_trigger(*args)
    assert p.find_by_id("reload").triggers == ("R",)


def test_remove_trigger_by_id_removes_an_extra_trigger_and_never_the_same_input_of_another_binding():
    p = trio()
    assert p.remove_trigger("fire", "MMB") == "removed"
    assert p.find_by_id("fire").triggers == ("LMB", "F") and ids(p) == ["fire", "peek", "reload"]
    assert p.remove_trigger("fire", "F") == "removed"
    assert p.find_by_id("fire").triggers == ("LMB",)


def test_removing_a_shared_first_trigger_from_one_binding_promotes_inside_it_and_leaves_the_other_binding_on_that_input():
    p = trio()
    assert p.remove_trigger("fire", "LMB") == "promoted"
    assert p.find_by_id("fire").input == "MMB" and p.find_by_id("fire").triggers == ("MMB", "F")
    assert p.find_by_id("peek").triggers == ("LMB",) and ids(p) == ["fire", "peek", "reload"]
    assert p.find_by_id("fire").mode == "hold" and (p.find_by_id("fire").x, p.find_by_id("fire").y) == (0.8, 0.7)


def test_the_last_trigger_of_a_binding_is_an_error_by_default_and_changes_nothing():
    p = trio()
    before = shape(p)
    with pytest.raises(ProfileError, match="only key"):
        p.remove_trigger("peek", "LMB")
    assert shape(p) == before


def test_the_last_trigger_can_remove_the_binding_when_asked_and_only_that_binding_goes():
    p = trio()
    assert p.remove_trigger("peek", "LMB", remove_binding=True) == "binding_removed"
    assert ids(p) == ["fire", "reload"] and p.find_by_id("fire").triggers == ("LMB", "MMB", "F")


def test_remove_trigger_refuses_an_input_the_binding_does_not_have_and_an_unknown_binding():
    p = trio()
    before = shape(p)
    for args in (("reload", "LMB"), ("reload", ""), ("ghost", "R")):
        with pytest.raises(ProfileError):
            p.remove_trigger(*args)
    assert shape(p) == before


def test_removal_never_affects_unrelated_bindings_whatever_is_removed():
    p = trio()
    unrelated = (p.find_by_id("reload"), shape(p)[2])
    for call in (lambda: p.remove_trigger("fire", "F"), lambda: p.remove_trigger("fire", "LMB"),
                 lambda: p.remove_trigger("peek", "LMB", remove_binding=True), lambda: p.remove_by_id("fire")):
        call()
        assert p.find_by_id("reload") is unrelated[0] and shape(p)[-1] == unrelated[1]


# ===================================================================== moving a trigger
def test_a_trigger_moves_from_one_binding_to_another_and_nothing_is_deleted():
    p = trio()
    p.move_trigger("MMB", "fire", "reload")
    assert p.find_by_id("fire").triggers == ("LMB", "F") and p.find_by_id("reload").triggers == ("R", "MMB")
    assert ids(p) == ["fire", "peek", "reload"] and p.find_by_id("peek").triggers == ("LMB",)


def test_moving_the_first_trigger_promotes_inside_the_source():
    p = trio()
    p.move_trigger("LMB", "fire", "reload", shared=True)                           # peek still uses LMB: sharing is a choice
    assert p.find_by_id("fire").input == "MMB" and p.find_by_id("reload").triggers == ("R", "LMB")
    assert [b.id for b in p.bindings_for("LMB")] == ["peek", "reload"]


@pytest.mark.parametrize("args, why", [
    (("MMB", "fire", "fire"), "different"), (("Z", "fire", "reload"), "not one"), (("MMB", "fire", "ghost"), "no control"),
    (("MMB", "ghost", "reload"), "no control"), (("R", "reload", "fire"), "only key"),
    (("F", "fire", "peek"), None),                                                   # fine: F is free
])
def test_a_refused_move_changes_nothing(args, why):
    p = trio()
    before = shape(p)
    if why is None:
        p.move_trigger(*args)
        assert p.find_by_id("peek").triggers == ("LMB", "F")
        return
    with pytest.raises(ProfileError, match=why):
        p.move_trigger(*args)
    assert shape(p) == before


def test_moving_to_a_binding_that_already_has_the_input_is_refused_without_changes():
    p = trio()
    p.add_trigger("reload", "G")
    p.add_trigger("peek", "G", shared=True)
    before = shape(p)
    with pytest.raises(ProfileError, match="already one"):
        p.move_trigger("G", "reload", "peek", shared=True)
    assert shape(p) == before


def test_moving_an_input_that_a_third_binding_also_uses_needs_sharing_to_be_requested_and_never_touches_that_binding():
    p = trio()
    p.add_trigger("reload", "G")
    p.add_trigger("fire", "G", shared=True)                                          # reload and fire both have G
    before = shape(p)
    with pytest.raises(ProfileError, match="shared=True"):
        p.move_trigger("G", "reload", "peek")
    assert shape(p) == before
    p.move_trigger("G", "reload", "peek", shared=True)
    assert p.find_by_id("reload").triggers == ("R",) and p.find_by_id("peek").triggers == ("LMB", "G")
    assert p.find_by_id("fire").triggers == ("LMB", "MMB", "F", "G")                 # the third binding kept its G


# ===================================================================== order and determinism
def test_every_operation_keeps_the_list_order_deterministic():
    p = Profile("p")
    for key, i in (("A", "a"), ("B", "b"), ("C", "c")):
        p.add(Binding(key, 0.1, 0.1, id=i))
    p.add_shared(Binding("A", 0.2, 0.2, id="a2"))
    p.add_trigger("b", "X")
    p.remove_by_id("c")
    p.add_shared(Binding("B", 0.3, 0.3, id="b2"))
    p.move_trigger("X", "b", "a2")
    assert ids(p) == ["a", "b", "a2", "b2"]
    twin = Profile("p")
    for key, i in (("A", "a"), ("B", "b"), ("C", "c")):
        twin.add(Binding(key, 0.1, 0.1, id=i))
    twin.add_shared(Binding("A", 0.2, 0.2, id="a2"))
    twin.add_trigger("b", "X")
    twin.remove_by_id("c")
    twin.add_shared(Binding("B", 0.3, 0.3, id="b2"))
    twin.move_trigger("X", "b", "a2")
    assert shape(p) == shape(twin)


# ===================================================================== the original API, unchanged
def test_find_key_owner_remove_and_lookup_keep_their_original_meaning_on_a_profile_that_shares_an_input():
    p = trio()
    assert p.find("LMB").id == "fire"                                              # the first binding on that input
    assert p.key_owner("LMB") == "another control" and p.key_owner("MMB") is None  # first inputs only, as before
    assert p.lookup()["LMB"].id == "peek"                                          # a dict keeps the last, as before
    p.remove("LMB")                                                                # removes EVERY binding on that first input
    assert ids(p) == ["reload"]                                                    # (use remove_by_id to remove one of them)
