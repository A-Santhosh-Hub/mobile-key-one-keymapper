"""Deleting ONE binding deletes exactly that binding (Phase 2, step 6.1).

The editor used to delete by key (`Profile.remove(input)`, which takes every binding on that first input) and by list position
(which can point at a different binding after the list changed). Now a delete names the binding by its id: the controls panel
reads it from the row it displayed, and a hit on the picture remembers it (`Hit.binding_id`, set by `pin`). If the id cannot be
resolved to exactly one binding the delete is refused and nothing changes. These tests cover the Qt-free logic; the real panel,
overlay and context menu are exercised by tools/binding_delete_ui_test.py on the simulated phone."""
import copy
import json

import pytest

from app.mapping import layout_edit as le
from app.mapping.profile import Action, Binding, Profile, ProfileStore, Stick

TW, TH = 1920.0, 864.0


def shared_profile():
    """Two bindings with the SAME first input (A, B), a binding whose extra trigger overlaps their input (C), and a plain one (D);
    every one has several actions or triggers."""
    p = Profile("Shared", orientation="landscape", id="aaaabbbbcccc")
    p.bindings = [
        Binding("LMB", 0.80, 0.70, "hold", "Fire", id="a", also=[(0.10, 0.40)],
                extra_actions=[Action("tap", 0.50, 0.20, delay_ms=60)]),
        Binding("LMB", 0.30, 0.30, "tap", "Peek", id="b", extra_triggers=["Z"],
                extra_actions=[Action("swipe", 0.20, 0.50, x2=0.60, y2=0.30, delay_ms=120), Action("hold", 0.40, 0.40)]),
        Binding("R", 0.70, 0.90, "tap", "Reload", id="c", extra_triggers=["LMB", "Shift+R"], on_failure="abort",
                extra_actions=[Action("repeat", 0.60, 0.60, rate_hz=12)]),
        Binding("Q", 0.20, 0.80, "sequence", "Menu", id="d", steps=[(0.1, 0.1, 0), (0.2, 0.2, 300)], extra_triggers=["E", "F"]),
    ]
    p.set_stick(Stick(0.15, 0.7))
    return p


def records(profile):
    """Everything that identifies the bindings, in order: the full version-2 record of each."""
    return [b.to_dict_v2() for b in profile.bindings]


def ids(profile):
    return [b.id for b in profile.bindings]


# ===================================================================== a Hit knows which binding it is
def test_hit_test_pins_every_kind_of_tap_hit_to_its_binding_and_equality_ignores_the_pin():
    p = Profile("p", bindings=[Binding("F", 0.2, 0.5, "tap", id="f", also=[(0.6, 0.5)]),
                               Binding("Q", 0.3, 0.3, "swipe", id="q", x2=0.7, y2=0.3),
                               Binding("X", 0.4, 0.8, "sequence", id="x", steps=[(0.4, 0.8, 0), (0.5, 0.8, 100)])])
    assert le.hit_test(p, 0.2, 0.5, TW, TH).binding_id == "f"
    assert le.hit_test(p, 0.6, 0.5, TW, TH).binding_id == "f" and le.hit_test(p, 0.6, 0.5, TW, TH) == le.Hit("tap", 0, "also0")
    assert le.hit_test(p, 0.7, 0.3, TW, TH).binding_id == "q" and le.hit_test(p, 0.5, 0.8, TW, TH).binding_id == "x"
    assert le.hit_test(p, 0.2, 0.5, TW, TH) == le.Hit("tap", 0)                    # the pin is not part of what a hit IS


def test_pin_adds_the_id_once_and_leaves_other_hits_alone():
    p = shared_profile()
    hit = le.Hit("tap", 1)
    assert le.pin(p, hit).binding_id == "b" and le.pin(p, le.pin(p, hit)).binding_id == "b"
    p.bindings.insert(0, Binding("N", 0.1, 0.1, id="n"))
    assert le.pin(p, le.Hit("tap", 1, "", "b")).binding_id == "b"                  # an already pinned hit is not re-pinned
    assert le.pin(p, le.Hit("tap", 99)) == le.Hit("tap", 99) and le.pin(p, None) is None
    assert le.pin(p, le.Hit("stick", 0, "center")) == le.Hit("stick", 0, "center")


def test_a_preset_added_by_the_palette_comes_back_pinned():
    p = Profile("p")
    hit = le.add_preset(p, "reload")
    assert hit.binding_id == p.bindings[-1].id and le.tap_position(p, hit) == 0


# ===================================================================== deleting follows identity, not position
def test_deleting_a_pinned_hit_removes_that_binding_even_after_the_list_changed():
    p = shared_profile()
    hit = le.pin(p, le.Hit("tap", 1))                                              # B, position 1
    p.bindings.insert(0, Binding("N", 0.1, 0.1, id="n"))                            # B is at position 2 now; position 1 is A
    p.bindings.reverse()                                                            # ... and now somewhere else again
    assert le.delete(p, hit) is True
    assert sorted(ids(p)) == ["a", "c", "d", "n"]


def test_deleting_a_pinned_hit_whose_binding_is_gone_is_refused_and_removes_nothing_else():
    p = shared_profile()
    hit = le.pin(p, le.Hit("tap", 1))
    p.remove_by_id("b")                                                             # somebody else deleted it first
    before = copy.deepcopy(records(p))
    assert le.delete(p, hit) is False                                               # the binding now at position 1 (C) is NOT taken
    assert records(p) == before and ids(p) == ["a", "c", "d"]


def test_an_ambiguous_identity_is_refused_not_guessed():
    p = shared_profile()
    hit = le.pin(p, le.Hit("tap", 0))
    p.bindings[1].id = "a"                                                          # two bindings now claim the id
    before = records(p)
    assert le.delete(p, hit) is False and records(p) == before
    assert le.remove_binding(p, "a") is None and records(p) == before


def test_partial_deletes_follow_identity_too():
    p = shared_profile()
    spot = le.pin(p, le.Hit("tap", 0, "also0"))
    p.bindings.insert(0, Binding("N", 0.1, 0.1, "tap", id="n", also=[(0.9, 0.9)]))
    assert le.delete(p, spot) is True
    assert p.find_by_id("a").also == [] and p.find_by_id("n").also == [(0.9, 0.9)] and len(p.bindings) == 5
    stale = le.pin(p, le.Hit("tap", 1, "also0"))
    p.remove_by_id("a")
    before = records(p)
    assert le.delete(p, stale) is False and records(p) == before


def test_a_hit_made_without_an_id_still_means_its_position_as_before():
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1), Binding("B", 0.2, 0.2, "tap", also=[(0.3, 0.3)])])
    assert le.delete(p, le.Hit("tap", 1, "also0")) is True and p.bindings[1].also == []
    assert le.delete(p, le.Hit("tap", 9)) is False and len(p.bindings) == 2         # out of range: ignored, and now reported
    assert le.delete(p, le.Hit("tap", 0)) is True and ids(p) == [p.bindings[0].id] and p.bindings[0].input == "B"
    assert le.delete(p, le.Hit("look", 0)) is False and le.delete(p, le.Hit("stick", 0, "center")) is False


def test_the_stick_and_the_look_area_are_deleted_by_position_as_before():
    p = shared_profile()
    assert le.delete(p, le.Hit("stick", 0, "center")) is True and p.sticks == []


# ===================================================================== remove_binding (the controls panel's path)
def test_remove_binding_removes_exactly_the_one_binding_with_that_id():
    p = shared_profile()
    removed = le.remove_binding(p, "b")
    assert removed.id == "b" and ids(p) == ["a", "c", "d"]


@pytest.mark.parametrize("bad", ["", "nope", None])
def test_remove_binding_refuses_an_id_that_names_nothing(bad):
    p = shared_profile()
    before = records(p)
    assert le.remove_binding(p, bad) is None and records(p) == before


def test_the_old_remove_by_input_would_have_taken_both_bindings_on_the_same_input():
    # This is why the editor no longer uses it (kept as it was for scripts and older callers).
    p = shared_profile()
    p.remove("LMB")
    assert ids(p) == ["c", "d"]                                                     # a AND b gone
    q = shared_profile()
    le.remove_binding(q, "b")
    assert ids(q) == ["a", "c", "d"]


# ===================================================================== the version-2 scenario, saved and reloaded
@pytest.mark.parametrize("how", ["by_id", "pinned_hit", "unpinned_hit"])
def test_deleting_one_of_two_bindings_on_the_same_input_keeps_everything_else_through_a_save_and_a_reload(tmp_path, how):
    original = shared_profile()
    store = ProfileStore(tmp_path)
    store.save(original)
    profile = store.load("Shared")
    before = {b.id: b.to_dict_v2() for b in profile.bindings}
    order = ids(profile)
    assert order == ["a", "b", "c", "d"] and profile.required_version == 2

    if how == "by_id":
        assert le.remove_binding(profile, "b").id == "b"
    elif how == "pinned_hit":
        assert le.delete(profile, le.pin(profile, le.hit_test(profile, 0.30, 0.30, TW, TH))) is True
    else:
        assert le.delete(profile, le.Hit("tap", 1)) is True
    store.save(profile)

    loaded = store.load("Shared")
    assert ids(loaded) == ["a", "c", "d"]                                           # the same order, B is the only one gone
    for binding in loaded.bindings:
        assert binding.to_dict_v2() == before[binding.id]                           # every trigger, action, delay, also, policy
    a, c, d = loaded.bindings
    assert a.input == "LMB" and a.also == [(0.10, 0.40)] and a.extra_actions[0].delay_ms == 60
    assert c.extra_triggers == ["LMB", "Shift+R"] and c.on_failure == "abort" and c.extra_actions[0].rate_hz == 12
    assert d.extra_triggers == ["E", "F"] and d.steps == [(0.1, 0.1, 0), (0.2, 0.2, 300)]
    assert loaded.id == "aaaabbbbcccc" == profile.id and loaded.schema_version == 2 and loaded.required_version == 2
    assert json.loads((next(tmp_path.glob("*.json"))).read_text(encoding="utf-8"))["version"] == 2
    assert loaded.sticks == original.sticks and loaded.orientation == "landscape"


def test_deleting_the_other_binding_on_the_shared_input_leaves_the_first_one_alone():
    p = shared_profile()
    before = p.find_by_id("a").to_dict_v2()
    assert le.remove_binding(p, "b") is not None
    assert p.find_by_id("a").to_dict_v2() == before and p.find("LMB").id == "a"


def test_deleting_the_binding_whose_extra_trigger_overlaps_another_ones_input_keeps_that_other_one():
    p = shared_profile()
    assert le.remove_binding(p, "c") is not None
    assert ids(p) == ["a", "b", "d"] and [b.input for b in p.bindings].count("LMB") == 2


def test_a_refused_delete_changes_nothing_at_all_including_what_would_be_saved(tmp_path):
    p = shared_profile()
    store = ProfileStore(tmp_path)
    store.save(p)
    on_disk = (tmp_path / next(f.name for f in tmp_path.glob("*.json"))).read_text(encoding="utf-8")
    hit = le.pin(p, le.Hit("tap", 3))
    p.remove_by_id("d")
    store.save(p)
    after_first = (tmp_path / next(f.name for f in tmp_path.glob("*.json"))).read_text(encoding="utf-8")
    assert le.delete(p, hit) is False and le.remove_binding(p, "d") is None
    store.save(p)
    assert (tmp_path / next(f.name for f in tmp_path.glob("*.json"))).read_text(encoding="utf-8") == after_first != on_disk


def test_a_version_1_profile_is_deleted_from_in_the_same_way_and_stays_version_1(tmp_path):
    p = Profile("old", bindings=[Binding("A", 0.1, 0.1), Binding("B", 0.2, 0.2), Binding("C", 0.3, 0.3)])
    store = ProfileStore(tmp_path)
    store.save(p)
    profile = store.load("old")
    assert le.delete(profile, le.pin(profile, le.Hit("tap", 1))) is True
    store.save(profile)
    loaded = store.load("old")
    assert [b.input for b in loaded.bindings] == ["A", "C"] and loaded.schema_version == 1 and loaded.required_version == 1


def test_an_extra_spot_hit_whose_spot_is_gone_is_refused_instead_of_deleting_the_whole_control():
    # Found by the GUI test: such a hit used to fall through to "delete the control".
    p = Profile("p", bindings=[Binding("A", 0.1, 0.1, "tap", id="a", also=[(0.5, 0.5)]), Binding("B", 0.2, 0.2, id="b")])
    for pinned in (True, False):
        hit = le.Hit("tap", 0, "also0")
        hit = le.pin(p, hit) if pinned else hit
        p.bindings[0].also = []
        assert le.delete(p, hit) is False and ids(p) == ["a", "b"]
        p.bindings[0].also = [(0.5, 0.5)]
