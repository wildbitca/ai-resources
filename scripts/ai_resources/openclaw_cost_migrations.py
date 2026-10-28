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
    had: bool = True               # False: the path is absent today, so applying it is a fill (teardown deletes it)
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
    # ADR-0014 D5: the workboard plugin adds its tools to every agent session. Proposed only when it is enabled today; a
    # host that applies it keeps it off (the host section does not re-enable it, see _configure_workboard).
    ("cost-workboard-disable", ("plugins", "entries", "workboard", "enabled"), False,
     "the workboard plugin adds its tools to every agent session; nobody on this host may use the shared board"),
)

WORKBOARD_DISABLE_ID = "cost-workboard-disable"
HEARTBEAT_OFF_ID = "cost-heartbeat-off"
HEARTBEAT_PATH = ("agents", "entries", "main", "heartbeat", "every")
UTILITY_MODEL_ID = "cost-utility-model"
UTILITY_MODEL_PATH = ("agents", "defaults", "utilityModel")


# Balanced credit policy (docs/decisions/0017-balanced-credit-policy.md): the SAFE subset. Heartbeat stays on Haiku until a
# Gemini tool-calling run is verified. Proposed only on a host that already uses Google models, one confirm per path.
BALANCED_UTILITY_ID = "credit-utility-gemini-flash"
BALANCED_ALIAS_ID = "credit-gemini-flash-alias"
GEMINI_FLASH_ALIAS_PATH = ("agents", "defaults", "models", "google/gemini-3.8-flash")


def uses_google(doc: dict) -> bool:
    """True when the live config already routes something to a `google/*` model (the host has the Google provider)."""
    return '"google/' in json.dumps(doc, default=str)


def balanced_credit_proposals(doc: dict, *, applied=(), keep=frozenset(), force=frozenset()) -> list[Proposal]:
    from .setup import lanes_render as lr
    done = set(applied or ())
    out: list[Proposal] = []
    if not uses_google(doc):
        return out
    flash = lr.GEMINI_FLASH_REF

    def ok(mid, path):
        return mid not in done and not _covered(path, keep) and not _covered(path, force)

    old, had = _get(doc, UTILITY_MODEL_PATH)
    if had and old != flash and ok(BALANCED_UTILITY_ID, UTILITY_MODEL_PATH):
        out.append(Proposal(BALANCED_UTILITY_ID, UTILITY_MODEL_PATH, old, flash,
                            "titles and summaries are tool-less: the Google credit pays for them",
                            snippet=f"openclaw config set agents.defaults.utilityModel {flash}"))
    old, had = _get(doc, GEMINI_FLASH_ALIAS_PATH)
    if not had and ok(BALANCED_ALIAS_ID, GEMINI_FLASH_ALIAS_PATH):
        out.append(Proposal(BALANCED_ALIAS_ID, GEMINI_FLASH_ALIAS_PATH, None, {"alias": "flash"},
                            "registers the Gemini route under a short alias", had=False,
                            snippet='openclaw config patch with {"agents":{"defaults":{"models":{"google/gemini-3.8-flash":{"alias":"flash"}}}}}'))
    return out


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


def proposals(doc: dict, *, applied=(), keep=frozenset(), force=frozenset(), heartbeat_off: bool = False,
              utility_model: str = "", balanced_credit: bool = False) -> list[Proposal]:
    """The migrations still worth proposing for `doc`. A path in `keep` is skipped; one already in `force` is left to
    the normal forced fill (it is applied without a second question).

    `heartbeat_off` is the operator's answer to the wizard question (default No: the 2h isolated light heartbeat stays);
    `utility_model` is a model ref a probe VERIFIED works with this host's key (never a guessed id): it is proposed only
    when the configured utilityModel is set, differs, and a verified replacement exists. `balanced_credit` adds the
    balanced credit policy's safe subset (see `balanced_credit_proposals`)."""
    done = set(applied or ())
    out: list[Proposal] = []
    if balanced_credit:
        out.extend(balanced_credit_proposals(doc, applied=applied, keep=keep, force=force))
    for mid, path, new, why in VALUE_MIGRATIONS:
        old, had = _get(doc, path)
        if mid in done or not had or old == new or _covered(path, keep) or _covered(path, force):
            continue
        out.append(Proposal(mid, path, old, new, why,
                            snippet=f'{{"force": ["{".".join(path)}"]}} in ~/.openclaw/kit-host-overrides.json5'))
    if heartbeat_off and HEARTBEAT_OFF_ID not in done and not _covered(HEARTBEAT_PATH, keep) \
            and not _covered(HEARTBEAT_PATH, force):
        old, had = _get(doc, HEARTBEAT_PATH)
        if not (had and old == "0m"):
            out.append(Proposal(HEARTBEAT_OFF_ID, HEARTBEAT_PATH, old if had else None, "0m",
                                "the main agent wakes up every couple of hours and re-reads its context each time",
                                had=had, snippet="openclaw config set agents.entries.main.heartbeat.every 0m"))
    if utility_model and UTILITY_MODEL_ID not in done and not _covered(UTILITY_MODEL_PATH, keep) \
            and not _covered(UTILITY_MODEL_PATH, force):
        old, had = _get(doc, UTILITY_MODEL_PATH)
        if had and old != utility_model:
            out.append(Proposal(UTILITY_MODEL_ID, UTILITY_MODEL_PATH, old, utility_model,
                                "titles and progress notes run on a model the probe verified with your key",
                                snippet=f"openclaw config set agents.defaults.utilityModel {utility_model}"))
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
    return [{"path": list(p.path), "previous": p.old if p.had else None, "had": p.had,
             "action": "forced" if p.had else "filled", "written": p.new} for p in accepted]


def pending_entries(accepted: list[Proposal], *, at: str, source: str = "setup") -> list[dict]:
    """Records for `host_restart_pending` of accepted proposals whose key needs a restart (or could not be classified):
    nothing is applied now, `ai-resources openclaw apply-pending` applies them in a drained window (ADR-0003). They carry
    the new value and the value they expect to replace (not secrets), so the apply can skip a path the operator
    changed since."""
    return [{"op": "migration", "id": p.id, "path": p.dotted, "value": p.new, "previous": p.old if p.had else None,
             "had": p.had, "replace": p.replace, "source": source, "at": at} for p in accepted]


# Values the kit itself is known to have written per dotted path, for records made before `written` was recorded
# (3.2.7 and older). Empty on purpose: an old record proves only that the kit touched the path, never what it left there.
KNOWN_KIT_VALUES: dict[str, tuple] = {}


def kit_owned(p: Proposal, *, written_paths=(), written_values: dict | None = None,
              role_agents: dict | None = None) -> bool:
    """True when applying `p` only undoes something the kit itself wrote (ADR-0023): the value sits at a path the kit
    recorded in `host_config_changes` (`written_paths`, dotted) AND is still the value the kit recorded writing
    (`written_values`, dotted path -> value; an operator edit since makes it theirs). A record without the value (older
    setup) counts only when the live value is a known kit value (`KNOWN_KIT_VALUES`). Or the proposal removes Opus
    fallbacks that the kit's own role writer recorded for an agent it created (`role_agents` =
    `OpenClawState.role_agents_applied`).

    An Opus fallback the operator added to an agent, a value they chose, or anything on an agent the kit did not
    create stays a proposal. Hot, reversible changes only: the caller still classifies the patch with the reload rules."""
    if p.dotted in set(written_paths or ()):
        values = written_values or {}
        if p.dotted in values:
            return bool(p.had) and p.old == values[p.dotted]
        return bool(p.had) and p.old in KNOWN_KIT_VALUES.get(p.dotted, ())
    if p.id.startswith("cost-fallbacks-no-opus:") and role_agents:
        name = p.id.split(":", 1)[1]
        if name not in set(role_agents.get("created_agent_ids") or []):
            return False
        ours = set((role_agents.get("fallbacks") or {}).get(name) or [])
        opus_now = [r for r in (p.old or []) if _is_opus(r)] if isinstance(p.old, list) else []
        return bool(opus_now) and set(opus_now) <= ours
    return False
