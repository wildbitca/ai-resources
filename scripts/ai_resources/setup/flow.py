"""Wizard control-flow primitives (stdlib only: no rich, no questionary).

The wizard's questions run as an ordered list of `StepSpec`. `Wizard.run` walks them, stops on the first
non-zero rc, and handles two control-flow exceptions raised from inside a step: `GoBack` (Ctrl-B) rewinds to
the previous step when that step is rewindable and made no change on this machine; `JumpTo` (the review's
"Edit a section") continues linearly from the named step. `Cancelled` is not handled here: it leaves the runner
and is reported by the caller.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Optional


class Cancelled(BaseException):
    """The operator cancelled (Ctrl-C / EOF) at a wizard prompt.

    A BaseException, like KeyboardInterrupt, so a broad `except Exception` in a step cannot swallow it."""

    def __init__(self, step: str = ""):
        super().__init__(step)
        self.step = step


class GoBack(BaseException):
    """The operator asked to return to the previous step."""


class JumpTo(BaseException):
    """Continue the run from the step with this id (the review's "Edit a section")."""

    def __init__(self, step_id: str):
        super().__init__(step_id)
        self.step_id = step_id


BACK = object()
"""Result a prompt exits with when Ctrl-B is pressed (ui converts it to GoBack)."""

STEP_LABELS = ("Tools", "How", "Gateway", "Providers", "API keys", "Team", "Tool setup", "Start at login",
               "Review", "Verify")
"""One short rail label per numbered step (1..10); 6a, 6b and 6c share "Team"."""


@dataclass
class StepSpec:
    id: str
    number: int
    title: str
    run: Callable[[Any], int]
    rewindable: bool = True             # may be re-asked after Back / Edit (no installer, no apply)
    back: bool = False                  # Ctrl-B is offered at this step's prompts
    # Returns (header title, reason) when the step does not apply to this run; the runner prints both.
    skip: Optional[Callable[[Any], Optional[tuple[str, str]]]] = None
    edit_label: str = ""                # the label in the review's "Edit a section" list ("" = not editable)


@dataclass
class _Done:
    index: int
    snapshot: Any
    locked: bool
    skipped: bool = False


def _noop(*_a, **_k):
    return None


@dataclass
class Wizard:
    steps: list[StepSpec]
    on_skip: Callable[[StepSpec, str, str], None] = _noop
    on_enter: Callable[[StepSpec, "Wizard"], None] = _noop
    snapshot: Callable[[], Any] = lambda: None
    restore: Callable[[Any], None] = _noop
    notify: Callable[[str], None] = print
    scope: Callable[[StepSpec, bool], ContextManager] = lambda spec, can_back: nullcontext()
    changes: Callable[[], int] = lambda: 0
    history: list[_Done] = field(default_factory=list)

    # --- queries ------------------------------------------------------------------------------------------
    def index_of(self, step_id: str) -> int:
        for i, spec in enumerate(self.steps):
            if spec.id == step_id:
                return i
        raise KeyError(step_id)

    def editable(self) -> list[StepSpec]:
        """Steps already asked in this run that may be asked again: rewindable, labelled, no change made."""
        out = []
        for pos, d in enumerate(self.history):
            spec = self.steps[d.index]
            if d.skipped or d.locked or not spec.rewindable or not spec.edit_label:
                continue
            # A jump re-runs every later step: refuse when one of them already changed this machine.
            blocked = False
            for later in self.history[pos + 1:]:
                lspec = self.steps[later.index]
                if later.skipped or lspec.id in ("review", "apply"):
                    continue
                if later.locked or not lspec.rewindable:
                    blocked = True
                    break
            if not blocked:
                out.append(spec)
        return out

    def jump_refusal(self, step_id: str, reentrant: tuple = ()) -> Optional[str]:
        """Why a jump to `step_id` is refused, or None. A jump re-runs that step and every later one: refuse
        when one of them, already run in this run, changed this machine or cannot be asked again. `reentrant`
        names the steps built to be run again (More options > Gateway re-asks the gateway and its keys); they
        never refuse a jump, any other step does."""
        target = self.index_of(step_id)
        for d in self.history:
            spec = self.steps[d.index]
            if d.index < target or d.skipped or spec.id in ("review", "apply") or spec.id in reentrant:
                continue
            if d.locked or not spec.rewindable:
                return (f"! {spec.title} already changed this machine in this run; it cannot be asked again. "
                        "Re-run setup to change it.")
        return None

    def _previous(self) -> Optional[_Done]:
        for d in reversed(self.history):
            if not d.skipped:
                return d
        return None

    # --- the run ------------------------------------------------------------------------------------------
    def run(self, ctx: Any) -> int:
        i = 0
        while i < len(self.steps):
            spec = self.steps[i]
            skip = spec.skip(ctx) if spec.skip else None
            if skip is not None:
                self.on_skip(spec, skip[0], skip[1])
                self.history.append(_Done(i, None, False, skipped=True))
                i += 1
                continue
            prev = self._previous()
            can_back = bool(prev and prev.snapshot is not None and self.steps[prev.index].rewindable
                            and not prev.locked)
            before = self.snapshot()
            marker = self.changes()
            self.on_enter(spec, self)
            try:
                with self.scope(spec, spec.back):
                    rc = spec.run(ctx)
            except GoBack:
                if self.changes() > marker:
                    # This step already saved something; restoring would drop the tracking of it.
                    self.notify(f"! {spec.title} already saved something in this run; Back stops here.")
                    continue                         # the same step is asked again, state kept
                self.restore(before)               # drop whatever the aborted step had already set in memory
                if not can_back:
                    self.notify(self._refusal(spec, prev))
                    continue                         # the same step is asked again
                i = prev.index
                self.history = [d for d in self.history if d.index < i]
                continue
            except JumpTo as jump:
                target = self.index_of(jump.step_id)
                self.history = [d for d in self.history if d.index < target]
                i = target
                continue
            if rc != 0:
                return rc
            locked = (not spec.rewindable) or self.changes() > marker
            self.history = [d for d in self.history if d.index != i]
            self.history.append(_Done(i, before, locked))
            i += 1
        return 0

    def _refusal(self, spec: StepSpec, prev: Optional[_Done]) -> str:
        if prev is None:
            return "! There is no earlier step to go back to."
        before = self.steps[prev.index]
        return (f"! {before.title} already changed this machine in this run; Back stops here. "
                "Re-run setup to change it.")


# --- the progress rail (pure) ------------------------------------------------------------------------------

_UNICODE = {"done": "✔", "current": "●", "pending": "○", "skipped": "–"}
_ASCII = {"done": "[x]", "current": "[>]", "pending": "[ ]", "skipped": "[-]"}


def rail_model(current: int, done: Any = (), skipped: Any = ()) -> list[tuple[int, str, str]]:
    """`(number, label, status)` for the ten numbered steps; status is done, current, pending or skipped."""
    out = []
    for n, label in enumerate(STEP_LABELS, start=1):
        if n == current:
            status = "current"
        elif n in skipped:
            status = "skipped"
        elif n in done or n < current:
            status = "done"
        else:
            status = "pending"
        out.append((n, label, status))
    return out


def format_rail(model: list[tuple[int, str, str]], width: int, unicode_ok: bool = True, title: str = "") -> str:
    """The rail as plain text. 80+ columns: one line when it fits; 60-79: wrapped to `width`;
    below 60: one line `Step n/10  title` and no rail."""
    glyphs = _UNICODE if unicode_ok else _ASCII
    total = len(model)
    if width < 60:
        cur = next((m for m in model if m[2] == "current"), None)
        if cur is None:
            return ""
        return f"Step {cur[0]}/{total}  {title or cur[1]}"
    items = []
    for _n, label, status in model:
        suffix = " (skipped)" if status == "skipped" and not unicode_ok else ""
        items.append(f"{glyphs[status]} {label}{suffix}")
    lines, line = [], ""
    for item in items:
        candidate = f"{line}  {item}" if line.strip() else f" {item}"
        if len(candidate) > width and line.strip():
            lines.append(line)
            candidate = f" {item}"
        line = candidate
    lines.append(line)
    return "\n".join(lines)
