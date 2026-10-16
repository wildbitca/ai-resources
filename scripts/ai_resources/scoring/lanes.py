"""Lane resolution (ADR-0007): where each role runs, decided after the model choice.

Pure and deterministic: no I/O, no clock, no dataset access. The caller precomputes what it needs
(`usage_verdicts` from `gate.usage_gate`, effective headroom `weights`) and passes it in. Compose never
emits a lane and a lane never moves the model or the band: a candidate is admitted when it runs the
role's own model, or when the operator pinned another model with the pin flag.

Rules, in order (plan section 5):
1. eligibility from `roles_policy.LANE_ELIGIBILITY`; an unknown role resolves to claude-code;
2. admission: same model (or an operator pin), not stale, account gate open;
3. agy gate source: the pool's `direct-only` citation; the ADR-0007 exception lives only here;
4. pick by least projected pressure among admitted candidates (they all run the same model, so
   headroom is the only thing that separates them); ties by candidate order, then agent id;
5. optional pool caps for writer and non-checker roles; checkers are never capped;
6. second members, resolved the same way;
7. checker independence by model family on the models that can actually run (hard);
8. only declared ids; the digest covers every input that can change the table.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .. import roles_policy
from . import headroom

CLAUDE_CODE = "claude-code"
OPENCLAW_AGENT = "openclaw-agent"
CLAUDE_CLI = "claude-cli"
AGY_BACKENDS = frozenset({"agy-cli", "agy-cli-lane"})
CODEX_CLI = "codex-cli"
CODEX_POOL = "codex-subscription"
KNOWN_BACKENDS = frozenset({CLAUDE_CLI, CODEX_CLI}) | AGY_BACKENDS

CLAUDE_POOL = "claude-subscription"
AGY_CLAUDE_POOL = "agy-claude-gpt"
AGY_GEMINI_POOL = "agy-gemini"
AGY_STATIC_WEIGHT = 0.7
STATIC_WEIGHTS = {AGY_CLAUDE_POOL: AGY_STATIC_WEIGHT, AGY_GEMINI_POOL: AGY_STATIC_WEIGHT}

MODES = ("single-model", "multi-model")
ADR_AGY = "ADR-0006 section 4 / ADR-0007"


class LaneError(ValueError):
    """A malformed lanes input: bad agent, unknown backend, ineligible role, wrong mode."""


class LaneConflict(ValueError):
    """Checker independence violated: a checker can run the same model family as a writer."""

    def __init__(self, message: str, *, roles: Sequence[str] = (), models: Sequence[str] = (),
                 edges: Sequence[str] = ()):
        super().__init__(message)
        self.roles = tuple(roles)
        self.models = tuple(models)
        self.edges = tuple(edges)


@dataclass(frozen=True)
class LaneAgent:
    id: str
    backend: str
    model: str
    pool: str = ""
    stale: bool = False


@dataclass(frozen=True)
class Lane:
    backend: str                      # claude-code | openclaw-agent
    agentId: str | None = None

    @property
    def ref(self) -> str:
        return CLAUDE_CODE if self.backend == CLAUDE_CODE else f"{OPENCLAW_AGENT}:{self.agentId}"


@dataclass(frozen=True)
class Why:
    role: str
    lane: str
    pool: str
    text: str


@dataclass(frozen=True)
class LaneTable:
    roles: dict[str, Lane]
    second: dict[str, Lane]
    agents: dict[str, LaneAgent]
    why: tuple[Why, ...]
    digest: str
    models: dict[str, str] = field(default_factory=dict)       # role -> model that runs on its lane
    blocked: dict[str, str] = field(default_factory=dict)      # role -> reason (checker, nothing admissible)

    def to_lanes(self) -> dict[str, Any]:
        """The rendered shape: `{agents, roles, second}`; `by_role` is never touched."""
        return {
            "agents": {i: {"backend": a.backend, "model": a.model, "pool": a.pool, **({"stale": True} if a.stale else {})}
                       for i, a in sorted(self.agents.items())},
            "roles": {r: {"lane": ln.ref} for r, ln in sorted(self.roles.items())},
            "second": {r: {"lane": ln.ref} for r, ln in sorted(self.second.items())},
        }


# ---------------------------------------------------------------------------------------------
# helpers


def agy_pool_for(model: str) -> str | None:
    """The agy pool of a model (plan section 4.4): Claude -> agy-claude-gpt, Gemini -> agy-gemini."""
    fam = roles_policy.model_family(model)
    if fam is None:
        return None
    return AGY_GEMINI_POOL if fam.startswith("gemini") else AGY_CLAUDE_POOL


def pool_of_agent(agent: LaneAgent) -> str:
    if agent.backend == CLAUDE_CLI:
        return CLAUDE_POOL
    if agent.backend == CODEX_CLI:
        return CODEX_POOL
    pool = agy_pool_for(agent.model)
    if pool is None:
        raise LaneError(f"lane agent {agent.id}: cannot map model {agent.model!r} to an agy pool")
    return pool


def _same_model(role_model: str, agent_model: str) -> bool:
    if role_model == agent_model:
        return True
    a, b = roles_policy.model_family(role_model), roles_policy.model_family(agent_model)
    return a is not None and a == b


def _as_agent(raw: Any, key: str | None = None) -> LaneAgent:
    if isinstance(raw, LaneAgent):
        agent = raw
    elif isinstance(raw, Mapping):
        agent = LaneAgent(id=str(raw.get("id", key or "")), backend=str(raw.get("backend", "")),
                          model=str(raw.get("model", "")), pool=str(raw.get("pool", "")),
                          stale=bool(raw.get("stale", False)))
    else:
        raise LaneError(f"lane agent {key!r}: not a mapping")
    if key is not None and agent.id != key:
        raise LaneError(f"lane agent key {key!r} != id {agent.id!r}")
    if not agent.id or not agent.model:
        raise LaneError(f"lane agent {agent.id!r}: id and model are required")
    if agent.backend not in KNOWN_BACKENDS:
        raise LaneError(f"lane agent {agent.id}: unknown backend {agent.backend!r} "
                        f"(known: {', '.join(sorted(KNOWN_BACKENDS))})")
    expected = pool_of_agent(agent)
    if agent.pool and agent.pool != expected:
        raise LaneError(f"lane agent {agent.id}: pool {agent.pool!r} does not match backend/model ({expected!r})")
    return LaneAgent(agent.id, agent.backend, agent.model, expected, agent.stale)


def _parse_ref(ref: str) -> tuple[str, str | None]:
    if ref == CLAUDE_CODE:
        return CLAUDE_CODE, None
    prefix = f"{OPENCLAW_AGENT}:"
    if isinstance(ref, str) and ref.startswith(prefix) and len(ref) > len(prefix):
        return OPENCLAW_AGENT, ref[len(prefix):]
    raise LaneError(f"unknown lane {ref!r} (expected 'claude-code' or 'openclaw-agent:<id>')")


def _model_of(by_role: Mapping[str, Any], role: str) -> str:
    cfg = by_role.get(role)
    return str((cfg or {}).get("model") or "") if isinstance(cfg, Mapping) else ""


def fallback_union(by_role: Mapping[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """The LiteLLM per-model fallback union (`setup/litellm.py`): primary -> [(fallback, declaring role)].

    LiteLLM keys fallbacks by model name, so two roles sharing a primary share one entry.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for role in sorted(by_role):
        cfg = by_role[role]
        if not isinstance(cfg, Mapping):
            continue
        fbs = cfg.get("fallbacks") or []
        model = cfg.get("model")
        if not (fbs and model):
            continue
        entry = out.setdefault(str(model), [])
        for fb in fbs:
            if all(fb != seen for seen, _ in entry):
                entry.append((str(fb), role))
    return out


def runnable_models(model: str, union: Mapping[str, Sequence[tuple[str, str]]], mode: str,
                    default_fallbacks: Sequence[str] = ()) -> list[tuple[str, str]]:
    """Models a role can run: `(model, edge)` where edge is '' for the primary. Single-model: primary only."""
    result = [(model, "")]
    if mode != "multi-model":
        return result
    for fb, declarer in union.get(model, ()):
        result.append((fb, f"{model} -> {fb} (fallback declared by {declarer})"))
    for fb in default_fallbacks:
        result.append((str(fb), f"{model} -> {fb} (defaults.fallbacks)"))
    return result


def _digest(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _verdict(usage_verdicts: Mapping[str, Any], pool: str) -> tuple[bool, str]:
    v = usage_verdicts.get(pool)
    if v is None:
        return False, ""
    allowed = bool(v["allowed"] if isinstance(v, Mapping) else getattr(v, "allowed", False))
    reason = str(v.get("reason", "") if isinstance(v, Mapping) else getattr(v, "reason", ""))
    return allowed, reason


def _acked(acks: Mapping[str, Any], account: str, role: str) -> bool:
    ack = acks.get(account)
    if not isinstance(ack, Mapping):
        return False
    return role in (ack.get("roles") or ())


# ---------------------------------------------------------------------------------------------
# resolution


def resolve_lanes(by_role: Mapping[str, Any],
                  agents: Mapping[str, Any],
                  candidates: Mapping[str, Sequence[str]],
                  second_candidates: Mapping[str, Sequence[str]] | None = None,
                  weights: Mapping[str, float] | None = None,
                  usage_verdicts: Mapping[str, Any] | None = None,
                  gates: Mapping[str, Any] | None = None,
                  acks: Mapping[str, Any] | None = None,
                  pool_caps: Mapping[str, int] | None = None,
                  mode: str = "single-model",
                  *,
                  default_fallbacks: Sequence[str] = (),
                  role_loads: Mapping[str, float] | None = None) -> LaneTable:
    if mode not in MODES:
        raise LaneError(f"unknown mode {mode!r} (expected one of {', '.join(MODES)})")
    second_candidates = second_candidates or {}
    weights = dict(weights or {})
    usage_verdicts = usage_verdicts or {}
    gates = gates or {}
    acks = acks or {}
    pool_caps = dict(pool_caps or {})
    role_loads = role_loads or {}

    agent_map = {k: _as_agent(v, k) for k, v in sorted(agents.items())}

    # input validation: ineligible role / undeclared ids are errors, an unknown role is not
    for source in (candidates, second_candidates):
        for role, refs in source.items():
            for ref in refs:
                backend, agent_id = _parse_ref(ref)
                if backend == OPENCLAW_AGENT and agent_id not in agent_map:
                    raise LaneError(f"role {role}: lane {ref!r} names an agent that is not declared")
    for role in second_candidates:
        if role not in roles_policy.REPORT_ROLES:
            raise LaneError(f"role {role}: only report-contract roles take a second member")
        if role in roles_policy.CHECKER_ROLES:
            raise LaneError(f"role {role}: a checker role never takes a second member")

    why: list[Why] = []
    roles_out: dict[str, Lane] = {}
    second_out: dict[str, Lane] = {}
    models_out: dict[str, str] = {}
    second_models: dict[str, str] = {}
    blocked: dict[str, str] = {}
    load: dict[str, float] = {}
    capped_count: dict[str, int] = {}

    def weight_of(pool: str) -> float:
        return float(weights.get(pool, STATIC_WEIGHTS.get(pool, headroom.DEFAULT_WEIGHT)))

    def check_admission(role: str, role_model: str, ref: str) -> tuple[bool, str, str, str]:
        """(admitted, reason, pool, model that would run)."""
        backend, agent_id = _parse_ref(ref)
        if backend == CLAUDE_CODE:
            return True, "", CLAUDE_POOL, role_model
        agent = agent_map[agent_id]  # type: ignore[index]
        pinned = not _same_model(role_model, agent.model)
        if pinned and not gates.get("operator_pin"):
            return False, (f"{agent.id} runs {agent.model}, not the role model {role_model}; "
                           f"an operator pin needs the pin flag"), agent.pool, agent.model
        if agent.stale:
            return False, f"{agent.id} is stale (model drift); re-run `ai-resources setup --lanes`", agent.pool, agent.model
        if agent.backend == CLAUDE_CLI:
            if not _acked(acks, "anthropic", role):
                return False, f"no Anthropic acknowledgement lists role {role}", agent.pool, agent.model
            allowed, reason = _verdict(usage_verdicts, agent.pool)
            if usage_verdicts and not allowed and agent.pool in usage_verdicts:
                return False, f"pool {agent.pool} is not dispatchable: {reason}", agent.pool, agent.model
        elif agent.backend == CODEX_CLI:
            missing = []
            if role not in roles_policy.REPORT_ROLES:
                return False, (f"{agent.id}: the Codex lane is read-only (report roles); "
                               f"role {role} may write"), agent.pool, agent.model
            if not gates.get("codex_flag"):
                missing.append("--lanes-codex flag")
            if not gates.get("adr_0007_codex_accepted"):
                missing.append("ADR-0007 Codex section Accepted")
            if not _acked(acks, "openai", role):
                missing.append(f"an OpenAI acknowledgement listing {role}")
            if missing:
                return False, (f"{agent.pool} is held; open only with {', '.join(missing)} "
                               f"(ADR-0007 FU-2)"), agent.pool, agent.model
            allowed, reason = _verdict(usage_verdicts, agent.pool)
            if usage_verdicts and not allowed and agent.pool in usage_verdicts:
                return False, f"pool {agent.pool} is not dispatchable: {reason}", agent.pool, agent.model
        else:
            allowed, reason = _verdict(usage_verdicts, agent.pool)
            cite = reason or f"direct-only in composition ({ADR_AGY})"
            missing = []
            if not gates.get("antigravity_flag"):
                missing.append("--lanes-antigravity flag")
            if not gates.get("adr_0007_agy_accepted"):
                missing.append("ADR-0007 Antigravity section Accepted")
            if not _acked(acks, "google", role):
                missing.append(f"a Google acknowledgement listing {role}")
            if missing:
                return False, (f"{agent.pool} is direct-only ({cite}); open only with "
                               f"{', '.join(missing)} ({ADR_AGY})"), agent.pool, agent.model
        return True, "pinned: operator pin, not scored" if pinned else "", agent.pool, agent.model

    def pick(role: str, role_model: str, refs: Sequence[str], *, checker: bool):
        """Returns (ref, pool, model, notes) or (None, ..., reasons)."""
        admitted: list[tuple[float, int, str, str, str, str, bool]] = []
        rejected: list[str] = []
        rl = float(role_loads.get(role, 1.0))
        for idx, ref in enumerate(refs):
            ok, reason, pool, model = check_admission(role, role_model, ref)
            if not ok:
                rejected.append(f"{ref}: {reason}")
                continue
            cap = pool_caps.get(pool)
            if (not checker) and cap is not None and capped_count.get(pool, 0) >= cap:
                rejected.append(f"{ref}: cap reached on {pool} ({cap})")
                continue
            w = weight_of(pool)
            p = headroom.pressure(load.get(pool, 0.0), rl, w)
            is_pin = bool(reason.startswith("pinned"))
            admitted.append((p, idx, ref, pool, model, reason, is_pin))
        if not admitted:
            return None, "", "", rejected
        pins = [a for a in admitted if a[6]]
        field_ = pins if pins else admitted
        field_.sort(key=lambda a: (round(a[0], 9), a[1], a[2]))
        best = field_[0]
        notes = []
        if len(admitted) > 1:
            same_pool = len({a[3] for a in field_}) == 1
            if same_pool:
                notes.append("same pool, no quota gain")
            else:
                notes.append("least projected pressure: " + ", ".join(
                    f"{a[3]} {a[0]:.2f}" for a in sorted(field_, key=lambda a: a[1])))
        if best[5]:
            notes.append(best[5])
        notes.extend(f"rejected {r}" for r in rejected)
        return best[2], best[3], best[4], notes

    def lane_of(ref: str) -> Lane:
        backend, agent_id = _parse_ref(ref)
        return Lane(CLAUDE_CODE) if backend == CLAUDE_CODE else Lane(OPENCLAW_AGENT, agent_id)

    def note_version_drift(role_model: str, ref: str, notes: list[str]) -> None:
        backend, agent_id = _parse_ref(ref)
        if backend != OPENCLAW_AGENT:
            return
        agent = agent_map[agent_id]  # type: ignore[index]
        if role_model != agent.model and _same_model(role_model, agent.model):
            notes.append(f"lane agent pinned {agent.model}; alias may resolve newer")

    ordered = sorted(set(by_role) | set(candidates) | set(second_candidates),
                     key=lambda r: (r in roles_policy.CHECKER_ROLES, r))
    for role in ordered:
        checker = role in roles_policy.CHECKER_ROLES
        role_model = _model_of(by_role, role)
        refs = list(candidates.get(role, ()))
        if not roles_policy.lane_eligible(role):
            roles_out[role] = Lane(CLAUDE_CODE)
            models_out[role] = role_model
            why.append(Why(role, CLAUDE_CODE, CLAUDE_POOL, "no write contract declared: claude-code"))
            continue
        if not role_model:
            roles_out[role] = Lane(CLAUDE_CODE)
            models_out[role] = ""
            why.append(Why(role, CLAUDE_CODE, CLAUDE_POOL, "no model for the role in by_role: claude-code"))
            continue
        if not refs:
            roles_out[role] = Lane(CLAUDE_CODE)
            models_out[role] = role_model
            load[CLAUDE_POOL] = load.get(CLAUDE_POOL, 0.0) + float(role_loads.get(role, 1.0))
            if not checker:
                capped_count[CLAUDE_POOL] = capped_count.get(CLAUDE_POOL, 0) + 1
            why.append(Why(role, CLAUDE_CODE, CLAUDE_POOL, "no lane candidates: claude-code (the off switch)"))
            continue
        ref, pool, model, notes = pick(role, role_model, refs, checker=checker)
        if ref is None:
            if checker:
                reason = "; ".join(notes) or "no admissible candidate"
                blocked[role] = reason
                why.append(Why(role, "blocked", "", f"BLOCKED, no admissible lane: {reason}"))
                continue
            ref, pool, model = CLAUDE_CODE, CLAUDE_POOL, role_model
            notes = ["no admissible lane candidate: claude-code"] + notes
        else:
            note_version_drift(role_model, ref, notes)
        roles_out[role] = lane_of(ref)
        models_out[role] = model
        load[pool] = load.get(pool, 0.0) + float(role_loads.get(role, 1.0))
        if not checker:
            capped_count[pool] = capped_count.get(pool, 0) + 1
        why.append(Why(role, ref, pool, "; ".join(notes) if notes else "single candidate"))

    # second members (report-contract roles only)
    for role in sorted(second_candidates):
        role_model = _model_of(by_role, role)
        refs = list(second_candidates[role])
        checker = role in roles_policy.CHECKER_ROLES
        if not role_model or not refs or role in blocked:
            continue
        ref, pool, model, notes = pick(role, role_model, refs, checker=checker)
        if ref is None:
            why.append(Why(role, "second", "", "no admissible second member: " + "; ".join(notes)))
            continue
        primary = roles_out.get(role)
        if primary is not None and primary.ref == ref and models_out.get(role) == model:
            why.append(Why(role, ref, pool, "second member equals the primary in lane and model: adds nothing"))
            continue
        note_version_drift(role_model, ref, notes)
        second_out[role] = lane_of(ref)
        second_models[role] = model
        load[pool] = load.get(pool, 0.0) + float(role_loads.get(role, 1.0))
        if not checker:
            capped_count[pool] = capped_count.get(pool, 0) + 1
        why.append(Why(role, f"second:{ref}", pool, "; ".join(notes) if notes else "second member"))

    if agent_map or candidates or second_candidates:
        _check_independence(by_role, models_out, second_models, mode, default_fallbacks)

    digest = _digest({
        "by_role": {r: {"model": c.get("model"), "fallbacks": list(c.get("fallbacks") or [])}
                    for r, c in sorted(by_role.items()) if isinstance(c, Mapping)},
        "agents": {i: [a.backend, a.model, a.pool, a.stale] for i, a in sorted(agent_map.items())},
        "candidates": {r: list(v) for r, v in sorted(candidates.items())},
        "second": {r: list(v) for r, v in sorted(second_candidates.items())},
        "weights": {k: float(v) for k, v in sorted(weights.items())},
        "verdicts": {p: list(_verdict(usage_verdicts, p)) for p in sorted(usage_verdicts)},
        "gates": {k: bool(v) for k, v in sorted(gates.items())},
        "acks": {a: {"roles": sorted((v or {}).get("roles") or ()), "text_sha256": (v or {}).get("text_sha256", "")}
                 for a, v in sorted(acks.items()) if isinstance(v, Mapping)},
        "caps": {k: int(v) for k, v in sorted(pool_caps.items())},
        "mode": mode,
        "default_fallbacks": list(default_fallbacks),
        "loads": {k: float(v) for k, v in sorted(role_loads.items())},
    })
    return LaneTable(roles=roles_out, second=second_out, agents=agent_map,
                     why=tuple(sorted(why, key=lambda w: (w.role, w.lane, w.text))), digest=digest,
                     models=models_out, blocked=blocked)


def _check_independence(by_role: Mapping[str, Any], models_out: Mapping[str, str],
                        second_models: Mapping[str, str], mode: str,
                        default_fallbacks: Sequence[str]) -> None:
    """Hard rule: no checker can run a model family a writer can run (single-model: primaries only)."""
    union = fallback_union(by_role)

    def models_for(role: str) -> list[tuple[str, str]]:
        primaries = []
        if role in models_out:
            primaries.append(models_out[role])
        if role in second_models:
            primaries.append(second_models[role])
        out: list[tuple[str, str]] = []
        for p in primaries:
            out.extend(runnable_models(p, union, mode, default_fallbacks))
        # the by_role primary stays runnable too when a lane agent pinned another model, because the
        # claude-code side of the same role is still configured with it
        base = _model_of(by_role, role)
        if base and all(base != m for m, _ in out):
            out.extend(runnable_models(base, union, mode, default_fallbacks))
        return out

    writers = sorted(r for r in by_role
                     if roles_policy.lane_eligible(r) and r not in roles_policy.CHECKER_ROLES
                     and (roles_policy.SCOPE in roles_policy.contracts_for(r)
                          or roles_policy.INTEGRATING in roles_policy.contracts_for(r)))
    checkers = sorted(r for r in by_role if r in roles_policy.CHECKER_ROLES)
    for checker in checkers:
        c_models = models_for(checker)
        for model, edge in c_models:
            if roles_policy.model_family(model) is None:
                raise LaneConflict(
                    f"checker {checker} can run model {model!r} whose family is unknown; "
                    f"independence cannot be shown (fail closed)",
                    roles=[checker], models=[model], edges=[edge] if edge else [])
        for writer in writers:
            for wm, wedge in models_for(writer):
                wf = roles_policy.model_family(wm)
                for cm, cedge in c_models:
                    cf = roles_policy.model_family(cm)
                    if (wf is not None and wf == cf) or (wf is None and wm == cm):
                        edges = [e for e in (wedge, cedge) if e]
                        raise LaneConflict(
                            f"checker {checker} ({cm}) shares model family {cf} with writer {writer} ({wm})"
                            + (f"; remove the fallback edge(s): {'; '.join(edges)}" if edges else ""),
                            roles=[checker, writer], models=[cm, wm], edges=edges)
