"""Qt-free editing operations for the advanced binding editor: triggers, actions and sharing, found by IDENTITY (binding id, action
uid) and never by list position.

Every operation is a TRANSACTION. It runs on a deep copy of the profile; only if it succeeds, and only if it creates no new
analyzer ERROR, is the copy adopted into the real profile (Binding objects that did not change keep their identity, so references held
elsewhere stay valid). A refused or cancelled operation therefore leaves the profile - bindings, actions and sharing metadata -
exactly as it was. Operations that change the members of a sharing group report it (`Result.group_changes`) and can ask for a
confirmation first (`confirm`).

Nothing here decides which bindings run: that is `Profile.runners()` / `Profile.analyze()`; this module only reads them.
Sharing is never inferred from a repeated key: it is authorized only by the explicit choice "share" (Profile.share_trigger)."""
from __future__ import annotations

import copy
from dataclasses import dataclass, fields
from typing import Callable, Mapping

from .profile import (ALWAYS_RESERVED, Action, Binding, Issue, Profile, ProfileError, canonical_trigger, make_combo,
                      split_combo)

PRIMARY = "primary"                 # the reference of a binding's own (first) action; other actions are referred to by Action.uid

CONTEXT_ADD = "add"                 # give an existing control another key
CONTEXT_REBIND = "rebind"           # change one key of an existing control
CONTEXT_NEW = "new"                 # a control that does not exist yet wants the key

REPLACE, SHARE, SWAP, CANCEL = "replace", "share", "swap", "cancel"

DEFAULT_NEW_DELAY_MS = 100          # the delay a new or duplicated additional action gets, so it does not start with the first
DUPLICATE_OFFSET = 0.04             # how far (fraction of the screen) a duplicated action is moved, so it can be seen and grabbed


# ---------------------------------------------------------------- small values
@dataclass(frozen=True)
class Result:
    ok: bool
    message: str = ""
    notes: tuple = ()               # what else the operation changed, as sentences
    group_changes: tuple = ()       # sharing groups whose members changed, as sentences (also set when the operation was cancelled)
    ref: str = ""                   # the action created or kept by the operation
    needs_choice: bool = False      # the key is used by another control: the caller must choose replace / share / ...


@dataclass(frozen=True)
class KeyConflict:
    trigger: str                    # canonical name ("" if unusable)
    kind: str                       # free | own | taken | stick | reserved | invalid
    users: tuple = ()               # ids of the OTHER bindings that use it (any of their triggers)
    labels: tuple = ()              # the same bindings, as words
    shared: bool = False            # an authorized sharing group already exists on this key
    choices: tuple = ()             # what the user may do (REPLACE / SHARE / SWAP; CANCEL is always possible)
    message: str = ""


@dataclass(frozen=True)
class SharingInfo:
    trigger: str
    state: str                      # private (only this control) | shared (authorized group) | unauthorized (others use it, not shared)
    members: tuple = ()             # (binding id, words) of every control that uses the key, in list order
    runs: bool = True               # does THIS control run when the key is pressed (the engine's own answer)


@dataclass(frozen=True)
class RowInfo:
    keys: str                       # "E +2"
    keys_tip: str                   # every key
    actions: str                    # "3 actions" ("" for a single action)
    info: str                       # one line: sharing and problems
    tip: str                        # the same in full, for a tooltip
    severity: str                   # "" | "info" | "warning" | "error": the worst thing the analyzer says about this control
    shared: bool = False


def describe(binding: Binding | None) -> str:
    """A control in words: its label, else the key it is on."""
    if binding is None:
        return "a control"
    return f"'{binding.label}'" if binding.label else f"the control on {binding.input}"


def _canon(trigger: str) -> str:
    return make_combo(*split_combo(canonical_trigger(trigger)))


def _triggers(binding: Binding) -> set:
    names = set()
    for t in binding.triggers:
        try:
            names.add(_canon(t))
        except ProfileError:                       # a hand-edited, unusable key: the analyzer reports it; it matches nothing here
            pass
    return names


def ordered_triggers(binding: Binding) -> list[str]:
    """The canonical keys of a control, first key first, unusable ones left out."""
    names: list[str] = []
    for t in binding.triggers:
        try:
            name = _canon(t)
        except ProfileError:
            continue
        if name not in names:
            names.append(name)
    return names


def users_of(profile: Profile, trigger: str, *, exclude_id: str | None = None) -> list[Binding]:
    """Every binding that has this key as ANY of its triggers (Profile.find / key_owner only look at the first one)."""
    try:
        wanted = _canon(trigger)
    except ProfileError:
        return []
    return [b for b in profile.bindings if wanted in _triggers(b) and (exclude_id is None or b.id != exclude_id)]


def issue_key(issue: Issue) -> tuple:
    return (issue.code, issue.binding_ids, issue.trigger)


def issues_for(profile: Profile, binding_id: str, reserved: Mapping[str, str] | None = None) -> list[Issue]:
    """What Profile.analyze() says that concerns this binding (nothing is decided here)."""
    return [i for i in profile.analyze(reserved=reserved) if binding_id in i.binding_ids]


def describe_group_changes(before: Profile, after: Profile) -> list[str]:
    """The sharing groups whose members differ between two states of a profile, as sentences."""
    old = {t: [b.id for b in members] for t, members in before.sharing_groups().items()}
    new = {t: [b.id for b in members] for t, members in after.sharing_groups().items()}
    words = {b.id: describe(b) for b in [*before.bindings, *after.bindings]}

    def names(ids) -> str:
        return ", ".join(words[i] for i in ids)

    out: list[str] = []
    for trigger in sorted(set(old) | set(new)):
        was, now = old.get(trigger, []), new.get(trigger, [])
        if set(was) == set(now):
            continue
        if was and not now:
            out.append(f"Sharing of {trigger} ends: {names(was)} no longer run together on it.")
        elif now and not was:
            out.append(f"{trigger} will be shared: {names(now)} will all run when it is pressed.")
        else:
            left, joined = [i for i in was if i not in now], [i for i in now if i not in was]
            if left:
                out.append(f"{names(left)} no longer share {trigger} with the others.")
            if joined:
                out.append(f"{names(joined)} will share {trigger} with the others.")
    return out


# ---------------------------------------------------------------- adopting a working copy
def adopt(profile: Profile, working: Profile) -> None:
    """Make `profile` equal to `working` (same bindings in the same order, same sharing) WITHOUT replacing the Binding objects that did not
    change, so anything holding one still holds the right object. A changed binding is updated in place; a new one is added; a missing one
    is dropped. Sticks, looks and everything else of the profile are not touched."""
    current = {b.id: b for b in profile.bindings}
    merged: list[Binding] = []
    for wb in working.bindings:
        original = current.get(wb.id)
        if original is None:
            merged.append(wb)
            continue
        if original.to_dict_v2() != wb.to_dict_v2():
            for f in fields(Binding):
                setattr(original, f.name, getattr(wb, f.name))
        merged.append(original)
    profile.bindings[:] = merged
    profile.shared_triggers = {t: list(ids) for t, ids in working.shared_triggers.items()}
    profile.shared_unknown = copy.deepcopy(working.shared_unknown)


class ProfileEditor:
    """The editing operations of one profile. `reserved` = {key: what the application uses it for} (the window's hotkeys); a key in it
    can never become a trigger."""

    def __init__(self, profile: Profile, reserved: Mapping[str, str] | None = None) -> None:
        self.profile = profile
        self.reserved = {**ALWAYS_RESERVED, **(reserved or {})}

    # -- the transaction --------------------------------------------------------------------------
    def working_copy(self) -> Profile:
        return copy.deepcopy(self.profile)

    def run(self, change: Callable[[Profile], object], *, confirm: Callable[[list], bool] | None = None, ok_message: str = "",
            dry_run: bool = False) -> Result:
        """Apply `change(working)` to a copy; adopt it only if it worked, created no new analyzer error, and (when the sharing groups'
        members change) `confirm` agrees. `change` may return a dict with message / notes / ref. Raising ProfileError refuses.
        `dry_run=True` does everything except adopt (and never asks `confirm`): the result says what WOULD happen."""
        working = self.working_copy()
        before = {issue_key(i) for i in self.profile.analyze(reserved=self.reserved)}
        try:
            extra = change(working)
        except ProfileError as exc:
            return Result(False, str(exc))
        extra = extra if isinstance(extra, dict) else {}
        worse = [i for i in working.analyze(reserved=self.reserved) if i.severity == "error" and issue_key(i) not in before]
        if worse:
            return Result(False, "Not done, it would make the controls invalid: " + worse[0].message)
        changes = tuple(describe_group_changes(self.profile, working))
        if dry_run:
            return Result(True, extra.get("message", ok_message), tuple(extra.get("notes", ())), changes, extra.get("ref", ""))
        if changes and confirm is not None and not confirm(list(changes)):
            return Result(False, "Cancelled - nothing changed.", group_changes=changes)
        adopt(self.profile, working)
        return Result(True, extra.get("message", ok_message), tuple(extra.get("notes", ())), changes, extra.get("ref", ""))

    def apply_working(self, working: Profile, *, confirm: Callable[[list], bool] | None = None) -> Result:
        """Adopt a copy the caller edited step by step (the options dialog), under the same checks as run()."""
        return self.run(lambda target: adopt(target, working), confirm=confirm, ok_message="Saved.")

    # -- questions ----------------------------------------------------------------------------------
    def inspect(self, trigger: str, binding_id: str | None = None, context: str = CONTEXT_ADD, old: str | None = None) -> KeyConflict:
        """Who already uses this key and what may be done about it. `old` = the key being replaced (CONTEXT_REBIND)."""
        try:
            name = _canon(trigger)
        except ProfileError as exc:
            return KeyConflict("", "invalid", message=str(exc))
        if name in self.profile.stick_keys():
            return KeyConflict(name, "stick", message=f"'{name}' is already used by the movement stick. Pick another key.")
        if name in self.reserved:
            return KeyConflict(name, "reserved", message=f"{name} is {self.reserved[name]}.")
        binding = self.profile.find_by_id(binding_id) if binding_id else None
        if binding is not None and name in _triggers(binding):
            return KeyConflict(name, "own", message=f"{describe(binding)} already has {name}.")
        others = users_of(self.profile, name, exclude_id=binding_id)
        if not others:
            return KeyConflict(name, "free")
        choices = [REPLACE, SHARE]
        if context == CONTEXT_REBIND and binding is not None and old is not None and len(others) == 1:
            other = others[0]
            try:
                old_name = _canon(old)
            except ProfileError:
                old_name = ""
            if binding.input == old_name and other.input == name and old_name not in _triggers(other):
                choices.append(SWAP)
        labels = tuple(describe(b) for b in others)
        return KeyConflict(name, "taken", tuple(b.id for b in others), labels, self.profile.is_shared(name), tuple(choices),
                           f"{name} is already used by {', '.join(labels)}.")

    def sharing_info(self, binding_id: str) -> list[SharingInfo]:
        """For each key of one control: is it its own, shared on purpose, or used by others without sharing - and does it run?"""
        binding = self.profile.find_by_id(binding_id)
        if binding is None:
            return []
        runners = self.profile.runners()
        groups = self.profile.sharing_groups()
        out = []
        for trigger in ordered_triggers(binding):
            users = users_of(self.profile, trigger)
            members = tuple((b.id, describe(b)) for b in users)
            runs = any(b is binding for b in runners.get(trigger, ()))
            if len(users) < 2:
                state = "private"
            elif trigger in groups and any(b is binding for b in groups[trigger]):
                state = "shared"
            else:
                state = "unauthorized"
            out.append(SharingInfo(trigger, state, members, runs))
        return out

    # -- triggers -------------------------------------------------------------------------------------
    def _take(self, working: Profile, trigger: str, keep_id: str | None) -> list[str]:
        """REPLACE: every other control stops using the key. A control with other keys keeps working with those; one whose ONLY key it is
        goes (it would have no way to run). Returns what happened, as sentences."""
        notes = []
        for other in users_of(working, trigger, exclude_id=keep_id):
            words = describe(other)
            if len(other.triggers) > 1:
                working.remove_trigger(other.id, trigger)
                notes.append(f"{words} no longer uses {trigger} (it keeps its other keys).")
            else:
                working.remove_by_id(other.id)
                notes.append(f"{words} was removed: {trigger} was its only key.")
        return notes

    def _check_choice(self, info: KeyConflict, choice: str | None) -> Result | None:
        if info.kind in ("invalid", "stick", "reserved", "own"):
            return Result(False, info.message)
        if info.kind == "taken":
            if choice is None:
                return Result(False, info.message + " Choose: replace it, share it, or cancel.", needs_choice=True)
            if choice == CANCEL:
                return Result(False, "Cancelled - nothing changed.")
            if choice not in info.choices:
                return Result(False, f"'{choice}' is not possible for {info.trigger}.")
        return None

    def add_trigger(self, binding_id: str, trigger: str, choice: str | None = None, *,
                    confirm: Callable[[list], bool] | None = None, dry_run: bool = False) -> Result:
        """Give one control another key. It does not create another action. If another control uses the key, `choice` says what to do."""
        info = self.inspect(trigger, binding_id, CONTEXT_ADD)
        refused = self._check_choice(info, choice)
        if refused is not None:
            return refused
        name = info.trigger

        def change(w: Profile):
            notes = self._take(w, name, binding_id) if choice == REPLACE else []
            w.add_trigger(binding_id, name, shared=(choice == SHARE))
            if choice == SHARE and not w.is_shared(name):
                raise ProfileError(f"{name} could not be shared (a control id is not unique)")
            return {"message": f"{name} added.", "notes": notes}
        return self.run(change, confirm=confirm, dry_run=dry_run)

    def change_trigger(self, binding_id: str, old: str, new: str, choice: str | None = None, *,
                       confirm: Callable[[list], bool] | None = None, dry_run: bool = False) -> Result:
        """Make a control use `new` instead of its key `old` (same position in its list of keys). The destination is never authorized for
        sharing unless the choice is SHARE."""
        binding = self.profile.find_by_id(binding_id)
        if binding is None:
            return Result(False, "That control no longer exists.")
        try:
            old_name = _canon(old)
        except ProfileError as exc:
            return Result(False, str(exc))
        if old_name not in _triggers(binding):
            return Result(False, f"{old_name} is not one of this control's keys.")
        info = self.inspect(new, binding_id, CONTEXT_REBIND, old=old_name)
        if info.kind == "invalid":
            return Result(False, info.message)
        if info.trigger == old_name:
            return Result(True, "Unchanged.")
        refused = self._check_choice(info, choice)
        if refused is not None:
            return refused
        name = info.trigger

        def change(w: Profile):
            target = w.find_by_id(binding_id)
            notes: list[str] = []
            if choice == SWAP:
                other = users_of(w, name, exclude_id=binding_id)[0]
                target.input, other.input = name, old_name
                notes.append(f"{describe(other)} now uses {old_name}.")
            else:
                if choice == REPLACE:
                    notes = self._take(w, name, binding_id)
                position = [_canon(t) for t in target.triggers].index(old_name)
                if position == 0:
                    target.input = name
                else:
                    target.extra_triggers[position - 1] = name
                if choice == SHARE:
                    w.share_trigger(name, [b.id for b in users_of(w, name)])
            w.prune_sharing()
            return {"message": f"{old_name} changed to {name}.", "notes": notes}
        return self.run(change, confirm=confirm, dry_run=dry_run)

    def remove_trigger(self, binding_id: str, trigger: str, *, confirm: Callable[[list], bool] | None = None) -> Result:
        """Take one key away from one control. The first key may go: the next one takes its place. The LAST key cannot go (the control
        would never run); delete the control instead."""
        def change(w: Profile):
            what = w.remove_trigger(binding_id, trigger)
            return {"message": ("The next key is now the first key." if what == "promoted" else "Key removed.")}
        return self.run(change, confirm=confirm)

    def add_binding(self, binding: Binding, choice: str | None = None, *, confirm: Callable[[list], bool] | None = None,
                    dry_run: bool = False) -> Result:
        """Add a NEW control. If its first key is free it is simply added. If another control uses the key, `choice` says what happens:
        REPLACE (the others lose the key, see _take) or SHARE (all of them are authorized to run together). Nothing is replaced or shared
        without a choice. `result.ref` is the new control's id."""
        info = self.inspect(binding.input, None, CONTEXT_NEW)
        refused = self._check_choice(info, choice)
        if refused is not None:
            return refused

        def change(w: Profile):
            notes = self._take(w, info.trigger, None) if choice == REPLACE else []
            w.add_shared(binding, authorize=(choice == SHARE))
            if choice == SHARE and not w.is_shared(info.trigger):
                raise ProfileError(f"{info.trigger} could not be shared (a control id is not unique)")
            w.prune_sharing()
            return {"message": f"{binding.input} added.", "notes": notes, "ref": binding.id}
        return self.run(change, confirm=confirm, dry_run=dry_run)

    def make_first(self, binding_id: str, trigger: str) -> Result:
        """Put one of a control's keys first (the key its marker is named after). No key is added or lost."""
        def change(w: Profile):
            b = w._need(binding_id)
            name = canonical_trigger(trigger)
            if name not in b.triggers:
                raise ProfileError(f"{name} is not one of this control's keys")
            rest = [t for t in b.triggers if t != name]
            b.input, b.extra_triggers = name, rest
            return {"message": f"{name} is now the first key."}
        return self.run(change)

    # -- sharing --------------------------------------------------------------------------------------
    def share(self, trigger: str, binding_ids, *, confirm: Callable[[list], bool] | None = None) -> Result:
        """AUTHORIZE the controls to run together on `trigger` (Profile.share_trigger). The only way sharing starts, besides choosing SHARE."""
        def change(w: Profile):
            w.share_trigger(trigger, binding_ids)
            return {"message": f"{canonical_trigger(trigger)} is shared: its controls all run when it is pressed."}
        return self.run(change, confirm=confirm)

    def unshare(self, trigger: str, binding_id: str | None = None, *, confirm: Callable[[list], bool] | None = None) -> Result:
        """Withdraw the authorization of one control, or of the whole group. The controls themselves stay."""
        def change(w: Profile):
            if not w.unshare_trigger(trigger, binding_id):
                raise ProfileError(f"{trigger} is not shared")
            return {"message": "Sharing withdrawn. If several controls still use that key, only one of them runs."}
        return self.run(change, confirm=confirm)

    # -- actions --------------------------------------------------------------------------------------
    @staticmethod
    def find_action(binding: Binding, ref: str):
        """The binding itself for PRIMARY (its own fields ARE the first action) or the Action with this uid; None if there is none."""
        if ref == PRIMARY:
            return binding
        found = [a for a in binding.extra_actions if a.uid == ref]
        return found[0] if len(found) == 1 else None

    @staticmethod
    def action_refs(binding: Binding) -> list[tuple[str, object]]:
        return [(PRIMARY, binding), *((a.uid, a) for a in binding.extra_actions)]

    def add_action(self, binding_id: str, mode: str = "tap", x: float | None = None, y: float | None = None,
                   delay_ms: int = DEFAULT_NEW_DELAY_MS) -> Result:
        """Give a control one more action (its own mode, spot and delay). Does not touch the key list. `result.ref` identifies it."""
        created: list[str] = []

        def change(w: Profile):
            b = w._need(binding_id)
            px = x if x is not None else min(1.0, b.x + 0.06)
            py = y if y is not None else min(1.0, b.y + 0.06)
            action = Action(mode, px, py, delay_ms=delay_ms)
            b.extra_actions.append(action)
            created.append(action.uid)
            return {"message": "Action added.", "ref": action.uid}
        return self.run(change)

    def duplicate_action(self, binding_id: str, ref: str) -> Result:
        """A copy of one action, moved a little, as an additional action. Fields a newer writer added are NOT copied (they may be ids)."""
        def change(w: Profile):
            b = w._need(binding_id)
            spec = self.find_action(b, ref)
            if spec is None:
                raise ProfileError("That action no longer exists.")
            dx = DUPLICATE_OFFSET

            def shift(v: float) -> float:
                return min(1.0, v + dx)
            copy_ = Action(spec.mode, shift(spec.x), min(1.0, spec.y + dx),
                           None if spec.x2 is None else shift(spec.x2), None if spec.y2 is None else min(1.0, spec.y2 + dx),
                           spec.rate_hz, spec.duration_ms, spec.span, spec.scope,
                           [(shift(sx), min(1.0, sy + dx), d) for sx, sy, d in spec.steps],
                           [(shift(ax), min(1.0, ay + dx)) for ax, ay in spec.also],
                           DEFAULT_NEW_DELAY_MS if ref == PRIMARY else spec.delay_ms)
            b.extra_actions.append(copy_)
            return {"message": "Action duplicated.", "ref": copy_.uid}
        return self.run(change)

    def remove_action(self, binding_id: str, ref: str, *, confirm: Callable[[list], bool] | None = None) -> Result:
        """Remove one action. The LAST action cannot go (delete the control instead). Removing the first action makes the next one the
        first: it now starts at once, so its delay becomes 0."""
        def change(w: Profile):
            b = w._need(binding_id)
            if self.find_action(b, ref) is None:
                raise ProfileError("That action no longer exists.")
            if len(b.extra_actions) == 0:
                raise ProfileError("This is the control's only action; delete the control instead.")
            notes = []
            if ref == PRIMARY:
                waited = b.drop_first_action()
                if waited:
                    notes.append(f"The next action is now the first one and starts at once (it waited {waited} ms).")
            else:
                b.extra_actions = [a for a in b.extra_actions if a.uid != ref]
            return {"message": "Action removed.", "notes": notes}
        return self.run(change, confirm=confirm)

    def delete_binding(self, binding_id: str, *, confirm: Callable[[list], bool] | None = None) -> Result:
        def change(w: Profile):
            if not binding_id or sum(1 for b in w.bindings if b.id == binding_id) != 1:
                raise ProfileError("That control could not be identified (it is gone, or its id is not unique), so nothing was deleted.")
            w.remove_by_id(binding_id)
            return {"message": "Control removed."}
        return self.run(change, confirm=confirm)

    # -- the controls table ----------------------------------------------------------------------------
    def row_infos(self) -> dict[str, RowInfo]:
        """What the controls table shows beside each control, from the analyzer and the engine's own resolver."""
        profile = self.profile
        issues = profile.analyze(reserved=self.reserved)
        by_binding: dict[str, list[Issue]] = {}
        for issue in issues:
            for binding_id in issue.binding_ids:
                by_binding.setdefault(binding_id, []).append(issue)
        groups = profile.sharing_groups()
        out: dict[str, RowInfo] = {}
        rank = {"error": 3, "warning": 2, "info": 1, "": 0}
        for b in profile.bindings:
            extra = len(b.extra_triggers)
            keys = b.input + (f" +{extra}" if extra else "")
            tip_keys = "Keys: " + ", ".join(b.triggers)
            actions = f"{len(b.extra_actions) + 1} actions" if b.extra_actions else ""
            shared_with = [describe(o) for t in ordered_triggers(b) for o in groups.get(t, ()) if o is not b]
            shared_with = list(dict.fromkeys(shared_with))
            mine = by_binding.get(b.id, [])
            problems = [i for i in mine if i.severity in ("warning", "error")]
            worst = max((i.severity for i in mine), key=lambda s: rank[s], default="")
            pieces = []
            if shared_with:
                pieces.append("shares a key with " + ", ".join(shared_with))
            if problems:
                pieces.append(f"{len(problems)} to check")
            tip = "\n".join([tip_keys, *(([f"{len(b.extra_actions) + 1} actions (the first at once, the others after their delay)"])
                                         if b.extra_actions else []), *[i.message for i in mine]])
            out[b.id] = RowInfo(keys, tip_keys, actions, "; ".join(pieces), tip, worst, bool(shared_with))
        return out
