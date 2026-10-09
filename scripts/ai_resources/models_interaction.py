"""The rules of the interactive `models update`, kept free of any UI.

`models.py` never prompts. This module decides WHAT is asked and what an answer changes; the thin
adapter in `models_cmd.py` supplies the buttons (`io.select`). The timer path never reaches it.

Answers and what they change:

    Update now / Update all shown   one time: the exact id is approved and applied in this run
    Not now                         nothing changes; the proposal stays pending
    Always update this family       policy `answer: always` (any newer stable version) AND the id
                                    shown is approved for this run
    Never -> This model only        the id joins `never_ids`; it is never proposed again
    Never -> The whole family       `policy.families[slot].never`; every id of the slot is suppressed
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Protocol

from . import model_pins

UPDATE_NOW = "Update now"
NOT_NOW = "Not now"
ALWAYS = "Always update this family"
NEVER = "Never"
UPDATE_ALL = "Update all shown"
CHOOSE = "Choose per model"
NEVER_MODEL = "This model only"
NEVER_FAMILY = "The whole family"

KIND_NOW = "now"
KIND_NOT_NOW = "not_now"
KIND_ALWAYS = "always"
KIND_NEVER_MODEL = "never_model"
KIND_NEVER_FAMILY = "never_family"


@dataclass
class Answer:
    kind: str
    id: str = ""


@dataclass
class Resolution:
    apply: list = field(default_factory=list)        # auto or approved: applied without a question
    pending: list = field(default_factory=list)      # needs a human
    suppressed: list = field(default_factory=list)   # never / exclude / frozen: not shown, not pending
    ask: list = field(default_factory=list)          # pending, when a prompt is possible


class IO(Protocol):
    def select(self, message: str, choices: list[str], default: str | None = None) -> str | None: ...


def resolve(proposals: list, slot_policy: Callable[[str], dict], can_prompt: bool) -> Resolution:
    """Split proposals by what must happen to them. `ask` is only filled when a prompt is possible,
    so the unattended path can never reach a question."""
    res = Resolution()
    for p in proposals:
        if p.decision in ("suppressed", "excluded", "frozen"):
            res.suppressed.append(p)
        elif p.applicable:
            res.apply.append(p)
        elif p.decision == "needs_approval":
            if slot_policy(p.slot).get("answer") == "never":
                res.suppressed.append(p)
            else:
                res.pending.append(p)
    if can_prompt:
        res.ask = list(res.pending)
    return res


def _describe(p) -> str:
    return f"{p.slot}: {p.old} -> {p.new} ({'; '.join(p.reasons) or p.kind})"


def _one(p, io: IO) -> Answer | None:
    choice = io.select(f"{_describe(p)}. Update?", [UPDATE_NOW, NOT_NOW, ALWAYS, NEVER], default=NOT_NOW)
    if choice is None:
        return None
    if choice == UPDATE_NOW:
        return Answer(KIND_NOW, p.new)
    if choice == ALWAYS:
        return Answer(KIND_ALWAYS, p.new)
    if choice == NEVER:
        scope = io.select(f"Never update {p.slot} to {p.new}:", [NEVER_MODEL, NEVER_FAMILY], default=NEVER_MODEL)
        if scope is None:
            return None
        return Answer(KIND_NEVER_FAMILY if scope == NEVER_FAMILY else KIND_NEVER_MODEL, p.new)
    return Answer(KIND_NOT_NOW, p.new)


def ask(proposals: list, io: IO) -> dict[str, Answer] | None:
    """The questions, in order: one proposal gets the four buttons; several get one batch question
    first and per-model buttons only after "Choose per model". None means the user cancelled."""
    if not proposals:
        return {}
    answers: dict[str, Answer] = {}
    if len(proposals) == 1:
        a = _one(proposals[0], io)
        return None if a is None else {proposals[0].slot: a}
    lines = "\n".join(f"  {_describe(p)}" for p in proposals)
    batch = io.select(f"{len(proposals)} newer models were found:\n{lines}\nWhat now?", [UPDATE_ALL, CHOOSE, NOT_NOW],
                      default=NOT_NOW)
    if batch is None:
        return None
    if batch == UPDATE_ALL:
        return {p.slot: Answer(KIND_NOW, p.new) for p in proposals}
    if batch == NOT_NOW:
        return {p.slot: Answer(KIND_NOT_NOW, p.new) for p in proposals}
    for p in proposals:
        a = _one(p, io)
        if a is None:
            return None
        answers[p.slot] = a
    return answers


def apply_answers(overlay: dict, answers: dict[str, Answer]) -> dict:
    """The overlay with the answers applied. Only Always and Never change the policy."""
    ov = model_pins.migrate(overlay) if overlay else model_pins.empty_overlay()
    policy = ov.setdefault("policy", {})
    slots = policy.setdefault("slots", {})
    families = policy.setdefault("families", {})
    approvals = ov.setdefault("approvals", {})
    failed = (ov.setdefault("state", {}).get("failed") or {})
    for slot, a in answers.items():
        slot = model_pins.slot_key(slot)
        if a.kind in (KIND_NOW, KIND_ALWAYS):
            approvals[slot] = a.id
            failed.pop(slot, None)
        if a.kind == KIND_ALWAYS:
            entry = slots.setdefault(slot, {})
            entry["answer"], entry["max_bump"] = "always", "any"
            families.pop(slot, None)
        elif a.kind == KIND_NEVER_MODEL:
            entry = slots.setdefault(slot, {})
            ids = entry.setdefault("never_ids", [])
            if a.id not in ids:
                ids.append(a.id)
            approvals.pop(slot, None)
            (ov.get("pending") or {}).pop(slot, None)
        elif a.kind == KIND_NEVER_FAMILY:
            families[slot] = {"never": True}
            approvals.pop(slot, None)
            (ov.get("pending") or {}).pop(slot, None)
    return ov


def revision(overlay: dict) -> str:
    """A digest of the parts of the overlay that decide what runs: pins, policy, approvals, the last
    change and the failures. The timer's bookkeeping (last_run, pending, history, price cache) does
    not move it, so an unrelated run during a prompt does not abort the answer."""
    ov = model_pins.migrate(overlay) if overlay else {}
    st = ov.get("state") or {}
    keyed = {"pins": ov.get("pins") or {}, "policy": ov.get("policy") or {}, "approvals": ov.get("approvals") or {},
             "last_change": st.get("last_change"), "failed": st.get("failed") or {},
             "pending_restart": st.get("pending_restart")}
    return hashlib.sha256(json.dumps(keyed, sort_keys=True).encode()).hexdigest()
