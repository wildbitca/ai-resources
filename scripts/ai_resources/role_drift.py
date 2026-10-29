"""Drift between the routing matrix and the live OpenClaw `kit-role-*` agents (ADR-0018, ADR-0019).

`setup` writes each `kit-role-<role>` agent from its row of the matrix (`scoring.routing`), but an agent it did not
create is only reconciled through `openclaw_roles.build_patch`'s fill-only rules. This module is the check that notices
when the live config and the matrix disagree anyway: it reads an already parsed `openclaw.json`, never the gateway, and
changes nothing.

Two kinds of finding, kept apart because they call for different action:

* `mismatch`     the live `model.primary`, `model.fallbacks` or the model a spawn lands on differs from the matrix row.
* `billing-pool` the matrix primary is a Max (`claude-code`) cell but the live primary is an Anthropic model that is not
                 pinned to the `claude-cli` runtime, so the role bills the API credit pool instead of the subscription.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import model_pins
from .scoring import routing
from .setup import live_openclaw as live

MISMATCH = "mismatch"
BILLING_POOL = "billing-pool"


@dataclass(frozen=True)
class Drift:
    role: str
    agent_id: str
    field: str
    expected: str
    actual: str
    kind: str = MISMATCH

    def line(self) -> str:
        label = "billing-pool mismatch" if self.kind == BILLING_POOL else "mismatch"
        return (f"{label}: role {self.role} ({self.agent_id}) {self.field}: "
                f"expected {self.expected or '(unset)'}, actual {self.actual or '(unset)'}")


def _chain(model: Any) -> str:
    primary, fallbacks = live._primary(model), live._fallbacks(model)
    return " > ".join([primary, *fallbacks]) if primary else ""


def _pinned_runtime(doc: Mapping, entry: Mapping, ref: str) -> str:
    own = (entry.get("models") or {}) if isinstance(entry.get("models"), dict) else {}
    rid = live._get(own.get(ref), "agentRuntime", "id")
    return rid if isinstance(rid, str) and rid else live.runtime_of(doc, ref)


def check(doc: Mapping, profile_doc: Mapping[str, Any] | None = None,
          effective: Mapping[str, str] | None = None) -> list[Drift]:
    """Every difference between the matrix and the live `kit-role-*` agents of `doc`. Pure. A role whose agent is not in
    the document is skipped (nothing runs it); a model the matrix cannot express is reported by `routing.merged`'s
    `RoutingError`, which propagates."""
    from .setup.cockpits import openclaw_roles as roles

    eff = effective if effective is not None else model_pins.effective()
    entries = live._entries(doc)
    default_spawn = live._get(doc, "agents", "defaults", "subagents", "model")
    out: list[Drift] = []
    for name, row in routing.merged(profile_doc).items():
        if row.kind != "team":
            continue
        aid = roles.agent_id_for(name)
        entry = entries.get(aid)
        if entry is None:
            continue
        primary, fallbacks = routing.openclaw_chain(row, eff)
        want = " > ".join([primary, *fallbacks])
        have_primary = live._primary(entry.get("model"))
        have = _chain(entry.get("model"))
        if have != want:
            if have_primary != primary:
                out.append(Drift(name, aid, "model.primary", primary, have_primary))
            if live._fallbacks(entry.get("model")) != list(fallbacks):
                out.append(Drift(name, aid, "model.fallbacks", ", ".join(fallbacks),
                                 ", ".join(live._fallbacks(entry.get("model")))))
        spawn = entry.get("subagents", {}).get("model") if isinstance(entry.get("subagents"), dict) else None
        spawn = spawn if spawn is not None else default_spawn
        if spawn is not None and _chain(spawn) != want:
            out.append(Drift(name, aid, "subagents.model", want, _chain(spawn)))
        if routing.runtime_of_cell(row.chain[0]) == routing.CLAUDE_CLI_RUNTIME and have_primary \
                and live.provider_of(have_primary) in ("anthropic", "anthropic-api") \
                and _pinned_runtime(doc, entry, have_primary) != routing.CLAUDE_CLI_RUNTIME:
            out.append(Drift(name, aid, "model.primary runtime", f"{routing.CLAUDE_CLI_RUNTIME} (Max subscription)",
                             f"{have_primary} on the Anthropic API credit pool", BILLING_POOL))
    return out


def mismatches(drifts: list[Drift]) -> list[Drift]:
    return [d for d in drifts if d.kind == MISMATCH]


def billing(drifts: list[Drift]) -> list[Drift]:
    return [d for d in drifts if d.kind == BILLING_POOL]
