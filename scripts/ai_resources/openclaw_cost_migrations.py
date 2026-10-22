"""One-time, operator-confirmed cost migrations for values an OpenClaw host already sets (ADR-0003, ADR-0013).

The host profile is fill-only: it never replaces a value the operator has, so the lean values reach a host that already
set `thinkingDefault: high` or an Opus fallback only through this module. Every proposal is shown as `old -> new` and
applied per path after its own confirm; nothing is applied unattended or non-interactively. An applied migration id is
recorded in the setup state, so a re-run proposes nothing and a value the operator changed back stays theirs. A path in
`keep` (kit-host-overrides.json5) is never proposed.

Fallbacks are lists, and `config patch` (and `force`) union lists, so an Opus fallback is removed by replacing the list
(`replace_paths`), never by a force entry; the declined output for it is an `openclaw config set` snippet.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Proposal:
    id: str
    path: tuple[str, ...]
    old: object
    new: object
    why: str
    replace: bool = False          # a list: replace it, do not union
    snippet: str = field(default="", compare=False)

    @property
    def dotted(self) -> str:
        return ".".join(self.path)


# (id, path, new value, why). A path not present in the live config is a fill, not a migration.
VALUE_MIGRATIONS: tuple[tuple[str, tuple[str, ...], object, str], ...] = (
    ("cost-thinking-default-low", ("agents", "defaults", "thinkingDefault"), "low",
     "routine turns do not need deep thinking (output tokens are the expensive ones)"),
    ("cost-thinking-main-medium", ("agents", "entries", "main", "thinkingDefault"), "medium",
     "main was on high for every turn"),
)


def _is_opus(ref: object) -> bool:
    return isinstance(ref, str) and "opus" in ref.lower()


def _primary(model) -> str:
    if isinstance(model, dict):
        return str(model.get("primary") or "")
    return str(model or "")


def _get(doc: dict, path: tuple[str, ...]):
    cur = doc
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None, False
        cur = cur[k]
    return cur, True


def opus_fallback_agents(doc: dict) -> list[str]:
    """Agents (and "defaults") whose fallbacks include an Opus ref although their primary is not Opus.

    Such a fallback escalates a failed Sonnet/Haiku turn to the most expensive model (the 2026-10-22 analysis found
    sonnet -> opus -> haiku on every agent). An agent whose primary IS Opus is a deliberate choice and is not listed."""
    out = []
    agents = (doc.get("agents") or {})
    targets = [("defaults", (agents.get("defaults") or {}).get("model"))]
    targets += [(k, (v or {}).get("model")) for k, v in (agents.get("entries") or {}).items() if isinstance(v, dict)]
    for name, model in targets:
        if not isinstance(model, dict):
            continue
        fb = model.get("fallbacks")
        if isinstance(fb, list) and any(_is_opus(r) for r in fb) and not _is_opus(_primary(model)):
            out.append(name)
    return out


def _covered(path: tuple[str, ...], entries) -> bool:
    for entry in entries or ():
        segs = tuple(entry.split("."))
        if len(segs) <= len(path) and all(a == "*" or a == b for a, b in zip(segs, path)):
            return True
    return False


def proposals(doc: dict, *, applied=(), keep=frozenset(), force=frozenset()) -> list[Proposal]:
    """The migrations still worth proposing for `doc`. A path in `keep` is skipped; one already in `force` is left to
    the normal forced fill (it is applied without a second question)."""
    done = set(applied or ())
    out: list[Proposal] = []
    for mid, path, new, why in VALUE_MIGRATIONS:
        old, had = _get(doc, path)
        if mid in done or not had or old == new or _covered(path, keep) or _covered(path, force):
            continue
        out.append(Proposal(mid, path, old, new, why,
                            snippet=f'{{"force": ["{".".join(path)}"]}} in ~/.openclaw/kit-host-overrides.json5'))
    for name in opus_fallback_agents(doc):
        path = ("agents", "defaults", "model", "fallbacks") if name == "defaults" else \
               ("agents", "entries", name, "model", "fallbacks")
        mid = f"cost-fallbacks-no-opus:{name}"
        if mid in done or _covered(path, keep):
            continue
        old, _ = _get(doc, path)
        new = [r for r in old if not _is_opus(r)]
        out.append(Proposal(mid, path, old, new, "a failed turn must not escalate to Opus", replace=True,
                            snippet=f"openclaw config set {'.'.join(path)} '{json.dumps(new)}'"))
    return out


def build_patch(accepted: list[Proposal]) -> tuple[dict, list[str]]:
    """(patch, replace_paths) for the accepted proposals."""
    patch: dict = {}
    replace: list[str] = []
    for p in accepted:
        cur = patch
        for k in p.path[:-1]:
            cur = cur.setdefault(k, {})
        cur[p.path[-1]] = p.new
        if p.replace:
            replace.append(p.dotted)
    return patch, replace


def change_records(accepted: list[Proposal]) -> list[dict]:
    """Records for the setup state, shaped like the profile's own, so teardown restores the old values."""
    return [{"path": list(p.path), "previous": p.old, "had": True, "action": "forced"} for p in accepted]
