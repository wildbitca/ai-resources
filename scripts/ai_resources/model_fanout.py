"""One apply/rollback path for every file a model change touches.

A model update moves several artifacts: the overlay, `openclaw.json` (through `openclaw config
patch` only) and, from S12 on, the user's executors.yaml, litellm.yaml, the Claude subagent
frontmatter, Claude's settings env and `~/.aider.conf.yml`. Each is an `Artifact` in an ordered
registry. Applying renders each affected artifact in order and records what it needs to undo itself;
a failure restores the artifacts already rendered, in reverse order. The interactive answer, the
systemd timer and the S12 re-render all go through this path, so "updates every config" is literally
true at every phase and never claimed beyond it.

Pure orchestration: every effect is an artifact's own (injected) callable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


class ArtifactError(Exception):
    """An artifact could not render or restore; the message is shown to the operator."""


@dataclass
class Change:
    """{slot: (old_id, new_id)} for the slots that move in this run."""
    slots: dict[str, tuple[str, str]]


@dataclass
class Ctx:
    """What artifacts may need. `extras` carries artifact-specific data (paths, deps)."""
    doc: dict = field(default_factory=dict)
    selection: Any = None
    plan: Any = None
    extras: dict = field(default_factory=dict)


class Artifact:
    """Base class. Subclasses set `id` and override `affected`, `render` and `restore`."""
    id = "artifact"

    def affected(self, change: Change, ctx: Ctx) -> bool:
        return True

    def render(self, change: Change, ctx: Ctx) -> dict:
        """Write the artifact; return a JSON-able payload that `restore` can undo from."""
        raise NotImplementedError

    def restore(self, payload: dict, ctx: Ctx) -> None:
        raise NotImplementedError


@dataclass
class ApplyResult:
    ok: bool
    message: str = ""
    payloads: dict[str, dict] = field(default_factory=dict)       # artifact id -> restore payload, in order
    failed: str = ""                                              # id of the artifact that failed
    restored: list[str] = field(default_factory=list)             # artifacts restored after the failure
    restore_errors: list[str] = field(default_factory=list)


class OpenClawArtifact(Artifact):
    """openclaw.json, only ever through `openclaw config patch` (dry run first). The forward and the
    inverse patch come from `models.build_forward_patch`, injected to keep this module import-free."""
    id = "openclaw"

    def __init__(self, apply_patch: Callable[..., "tuple[bool, str]"], build_patch: Callable[[dict, dict], dict],
                 ref_for: Callable[[str, str], "str | None"]):
        self._apply, self._build, self._ref = apply_patch, build_patch, ref_for

    def _ref_changes(self, change: Change) -> dict[str, str]:
        out: dict[str, str] = {}
        for slot, (old, new) in change.slots.items():
            ref_old, ref_new = self._ref(slot, old), self._ref(slot, new)
            if ref_old and ref_new:
                out[ref_old] = ref_new
        return out

    def affected(self, change: Change, ctx: Ctx) -> bool:
        return bool(self._ref_changes(change))

    def render(self, change: Change, ctx: Ctx) -> dict:
        built = self._build(ctx.doc, self._ref_changes(change))
        if not built["patch"]:
            return {"inverse": {}}
        ok, out = self._apply(built["patch"], dry_run=True, replace_paths=built["replace_paths"])
        if not ok:
            raise ArtifactError(f"config patch dry run rejected the change: {out[-300:]}")
        ok, out = self._apply(built["patch"], dry_run=False, replace_paths=built["replace_paths"])
        if not ok:
            raise ArtifactError(f"config patch failed: {out[-300:]}")
        return {"inverse": built["inverse"]}

    def restore(self, payload: dict, ctx: Ctx) -> None:
        inverse = payload.get("inverse") or {}
        if not inverse:
            return
        ok, out = self._apply(inverse, dry_run=True, replace_paths=[])
        if not ok:
            raise ArtifactError(f"rollback dry run rejected: {out[-300:]}")
        ok, out = self._apply(inverse, dry_run=False, replace_paths=[])
        if not ok:
            raise ArtifactError(f"rollback patch failed: {out[-300:]}")


def affected_ids(artifacts: Iterable[Artifact], change: Change, ctx: Ctx) -> list[str]:
    """Exactly the artifacts a change will touch; the UI lists these and nothing else."""
    return [a.id for a in artifacts if a.affected(change, ctx)]


def apply_all(artifacts: Iterable[Artifact], change: Change, ctx: Ctx) -> ApplyResult:
    """Render every affected artifact in order. On a failure restore the earlier ones in reverse."""
    done: list[tuple[Artifact, dict]] = []
    result = ApplyResult(ok=True)
    for art in artifacts:
        if not art.affected(change, ctx):
            continue
        try:
            payload = art.render(change, ctx)
        except Exception as e:  # noqa: BLE001 - an artifact may fail in any way; the run must unwind
            result.ok, result.failed, result.message = False, art.id, str(e) or type(e).__name__
            for prev, prev_payload in reversed(done):
                try:
                    prev.restore(prev_payload, ctx)
                    result.restored.append(prev.id)
                except Exception as re_:  # noqa: BLE001
                    result.restore_errors.append(f"{prev.id}: {re_}")
            result.payloads = {}
            return result
        done.append((art, payload))
        result.payloads[art.id] = payload
    return result


def restore_all(artifacts: Iterable[Artifact], payloads: dict[str, dict], ctx: Ctx) -> tuple[bool, str]:
    """Undo a recorded change in reverse registry order. Stops at the first artifact that cannot."""
    by_id = {a.id: a for a in artifacts}
    for art_id in reversed(list(payloads)):
        art = by_id.get(art_id)
        if art is None:
            return False, f"no artifact {art_id!r} is registered to restore it"
        try:
            art.restore(payloads[art_id], ctx)
        except Exception as e:  # noqa: BLE001
            return False, str(e) or type(e).__name__
    return True, "restored"
