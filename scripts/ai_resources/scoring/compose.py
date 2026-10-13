"""The composition engine: a pure, deterministic, explainable greedy over a scores database.

Inputs go in as one frozen `ComposeInput` (the compat/usage gate is an injected callable); a `Composition`
comes out. No I/O, no clock, no randomness, and no import of the wizard, the state or `os`/`time`. Every
input collection is sorted on entry, so the result does not depend on the order it was given in.

Modes (engine name -> `selection.SHAPES`):
    uniform          -> single           one model for every role
    single-provider  -> single-provider  one provider, a model per role
    mixed            -> multi-provider   balance pools within each role's tolerance band

Algorithm, per role r:
    candidates   rows whose provider is enabled, that pass the gate (usage, routable, compat; an
                 unverified cell only under `allow_unverified`, and then marked), that are not stale
                 (unless the operator accepted it), and that have a non-null score and a known label
    S*(r)        best verified score; the best overall only when no verified candidate exists
    band B(r)    candidates with score >= S*(r) - tolerance(class(r))
    mixed        walk the roles in ascending importance; each picks the band member with the least
                 projected pool pressure (ties: higher score, verified first, lower cost, id)
    diversity    code-reviewer and verifier prefer another vendor than the implementer's, softly, within
                 `diversity_tolerance` of S*(r)
    report       weighted_loss = sum importance x (S* - chosen); above `loss_cap` the result is a refusal

Live headroom readings only change WHICH band member is picked; they never widen a band or admit a
candidate. With every pool unknown the result equals a run with no readings at all.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import headroom as hr
from .db import ScoresDB
from .evidence import Label, weakest
from .gate import BLOCKED, GateVerdict

MODES = ("uniform", "single-provider", "mixed")
SHAPE_OF = {"uniform": "single", "single-provider": "single-provider", "mixed": "multi-provider"}
_EPS = 1e-9

# Rejection kinds besides the gate's own.
R_NOT_ENABLED = "not-enabled"
R_STALE = "stale"
R_NO_SCORE = "no-score"
R_OPT_IN = "opt-in-required"
R_ROUTE = "route-collapsed"

Gate = Callable[[str, str], GateVerdict]


@dataclass(frozen=True)
class ComposeInput:
    db: ScoresDB
    mode: str
    enabled: tuple[str, ...]
    as_of: str
    gate: Gate
    allow_unverified: bool = False
    accept_stale: bool = False
    fable_allowed: bool = False
    static_weights: Mapping[str, float] = field(default_factory=dict)      # pool id -> static weight
    readings: Mapping[str, hr.HeadroomReading] = field(default_factory=dict)
    enabled_pools: tuple[str, ...] = ()
    now_epoch: int | None = None
    roles: tuple[str, ...] = ()
    baseline: Mapping[str, str] = field(default_factory=dict)              # role -> model of the default profile
    baseline_name: str = ""
    references: Mapping[str, Mapping[str, str]] = field(default_factory=dict)   # name -> {role: model}


@dataclass(frozen=True)
class Candidate:
    role: str
    model: str
    route: str
    effort: str
    score: float
    label: Label
    as_of: str
    source_ref: str
    vendor: str
    provider: str            # the provider id that must be enabled
    pool_id: str
    verified: bool
    grade: str
    cost: float
    conditions: str = ""
    override: bool = False
    stale_accepted: bool = False
    gate_reason: str = ""


@dataclass(frozen=True)
class Rejection:
    role: str
    model: str
    route: str
    kind: str
    reason: str


@dataclass(frozen=True)
class Assignment:
    role: str
    model: str
    route: str
    effort: str
    score: float
    label: str
    as_of: str
    source_ref: str
    pool_id: str
    verified: bool
    grade: str
    importance: float
    tolerance_class: str
    tolerance: float
    s_star: float
    flags: tuple[str, ...] = ()
    fallbacks: tuple[str, ...] = ()
    conditions: str = ""
    billing_class: str = ""
    usage: str = "dispatch-ok"
    price_in: float | None = None
    price_out: float | None = None
    price_verify: bool = False
    price_tier: str = ""
    price_valid_until: str = ""


@dataclass(frozen=True)
class WhyRow:
    role: str
    importance: float
    tolerance_class: str
    tolerance: float
    s_star: float | None
    band: tuple[str, ...]
    chosen: Assignment | None
    runner_up: str = ""
    runner_up_reason: str = ""
    note: str = ""


@dataclass(frozen=True)
class PoolNote:
    pool_id: str
    billing_class: str
    state: str               # known | stale | unknown
    source: str
    used_fraction: float | None
    resets_at: int | None
    static_weight: float
    effective_weight: float
    basis: str               # static | live
    usage: str


@dataclass(frozen=True)
class ReferenceRow:
    name: str
    weighted_score: float
    covered_roles: int
    gate: str                # verified | unverified | blocked
    detail: str


@dataclass(frozen=True)
class Composition:
    mode: str
    shape: str
    dataset_version: str
    as_of: str
    allow_unverified: bool
    assignments: tuple[Assignment, ...] = ()
    by_role: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    why_rows: tuple[WhyRow, ...] = ()
    rejected: tuple[Rejection, ...] = ()
    unassigned: tuple[tuple[str, str], ...] = ()
    flags: tuple[str, ...] = ()
    weighted_loss: float = 0.0
    weighted_score: float = 0.0
    loss_cap: float = 0.0
    recommendation: str = ""
    kept_default: bool = False
    refusal: str = ""
    pools: tuple[PoolNote, ...] = ()
    references: tuple[ReferenceRow, ...] = ()
    input_digest: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.refusal)


# --- helpers -----------------------------------------------------------------------------------
def _digest(inp: ComposeInput, roles: tuple[str, ...]) -> str:
    rows = sorted((r.key, r.score, r.label.value, r.as_of, r.override) for r in inp.db.rows)
    doc = {
        "mode": inp.mode, "enabled": sorted(inp.enabled), "as_of": inp.as_of, "allow_unverified": inp.allow_unverified,
        "accept_stale": inp.accept_stale, "fable_allowed": inp.fable_allowed,
        "static_weights": sorted((k, v) for k, v in inp.static_weights.items()),
        "readings": sorted((k, r.state, r.used_fraction, r.resets_at, r.source) for k, r in inp.readings.items()),
        "enabled_pools": sorted(inp.enabled_pools), "roles": list(roles),
        "baseline": sorted(inp.baseline.items()), "dataset_version": inp.db.dataset_version,
        "rows": hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest(),
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _cost(db: ScoresDB, model: str, route: str, as_of: str) -> float:
    tokens = db.policy.get("reference_tokens") or {}
    prompt, out = int(tokens.get("prompt", 60000)), int(tokens.get("output", 6000))
    cached = float(tokens.get("cached_fraction", 0.0))
    price = db.price_at(model, as_of, prompt, route)
    if price is None or price.in_price is None or price.out_price is None:
        return math.inf
    in_p = price.in_price
    # Prompt caching is cheap on Anthropic routes and lost elsewhere (rule 017).
    if db.vendor_of(model) == "anthropic" and route in ("anthropic-direct", "vertex-global") \
            and price.cache_read is not None:
        in_p = cached * price.cache_read + (1.0 - cached) * in_p
    return (in_p * prompt + price.out_price * out) / 1e6


def _weight(inp: ComposeInput, pool_id: str) -> hr.EffectiveWeight:
    static = inp.static_weights.get(pool_id, hr.DEFAULT_WEIGHT)
    return hr.effective_weight(static, inp.readings.get(pool_id), inp.now_epoch)


def _role_candidates(inp: ComposeInput, role: str) -> tuple[list[Candidate], list[Rejection]]:
    db = inp.db
    admitted: list[Candidate] = []
    rejected: list[Rejection] = []
    for row in sorted(db.rows_for(role), key=lambda r: (r.model, r.route, r.effort)):
        model, route = row.model, row.route
        vendor = db.vendor_of(model)
        provider = db.required_provider(model, route)

        def reject(kind: str, reason: str) -> None:
            rejected.append(Rejection(role, model, route, kind, reason))

        if provider not in inp.enabled:
            reject(R_NOT_ENABLED, f"provider {provider} is not enabled")
            continue
        verdict = inp.gate(model, route)
        if not verdict.allowed:
            reject(verdict.kind, verdict.reason or verdict.kind)
            continue
        if (db.models.get(model) or {}).get("candidate_policy") == "opt-in" and not inp.fable_allowed:
            reject(R_OPT_IN, "opt-in candidate: declare a Max or Premium seat, or API billing, to allow it")
            continue
        stale = db.is_stale(row, inp.as_of)
        if stale and not inp.accept_stale:
            reject(R_STALE, f"evidence dated {row.as_of} is older than {row.expires_after_days} days")
            continue
        if row.score is None or row.label is Label.UNKNOWN:
            reject(R_NO_SCORE, "no score or UNKNOWN evidence")
            continue
        admitted.append(Candidate(
            role=role, model=model, route=route, effort=row.effort, score=row.score, label=row.label,
            as_of=row.as_of, source_ref=row.source_ref, vendor=vendor, provider=provider,
            pool_id=db.pool_for(vendor, route, inp.enabled_pools), verified=verdict.verified, grade=verdict.grade,
            cost=_cost(db, model, route, inp.as_of), conditions=row.conditions, override=row.override,
            stale_accepted=stale, gate_reason=verdict.reason))
    return admitted, rejected


def _collapse_routes(inp: ComposeInput, per_role: dict[str, list[Candidate]]
                     ) -> tuple[dict[str, list[Candidate]], list[Rejection]]:
    """A profile entry names a model, never a route, so one model keeps ONE route across the whole team.

    The route that covers the most roles wins; then verified-in-every-role, the higher importance-weighted
    score, the lower cost, the route id. Candidates on the other routes are dropped and reported, so the pool
    and the mark shown are the ones of the route the stored overlay stands for (ADR-0006)."""
    db = inp.db
    by_model: dict[str, dict[str, list[Candidate]]] = {}
    for cands in per_role.values():
        for c in cands:
            by_model.setdefault(c.model, {}).setdefault(c.route, []).append(c)
    keep: dict[str, str] = {}
    for model, routes in by_model.items():
        if len(routes) == 1:
            continue

        def rank(route: str, routes: dict[str, list[Candidate]] = routes) -> tuple:
            cs = routes[route]
            return (-len({c.role for c in cs}), 0 if all(c.verified for c in cs) else 1,
                    -round(sum(db.importance(c.role) * c.score for c in cs), 9),
                    sum(c.cost for c in cs), route)
        keep[model] = min(routes, key=rank)
    if not keep:
        return per_role, []
    out: dict[str, list[Candidate]] = {}
    dropped: list[Rejection] = []
    for role, cands in per_role.items():
        kept = []
        for c in cands:
            if c.model in keep and c.route != keep[c.model]:
                dropped.append(Rejection(role, c.model, c.route, R_ROUTE,
                                         f"a profile entry names a model, not a route; {keep[c.model]} is the "
                                         f"route kept for {c.model}"))
            else:
                kept.append(c)
        out[role] = kept
    return out, dropped


def _admitted(inp: ComposeInput, roles) -> tuple[dict[str, list[Candidate]], list[Rejection]]:
    per_role: dict[str, list[Candidate]] = {}
    rejected: list[Rejection] = []
    for role in roles:
        admitted, rej = _role_candidates(inp, role)
        per_role[role] = admitted
        rejected += rej
    per_role, dropped = _collapse_routes(inp, per_role)
    return per_role, rejected + dropped


def _s_star(cands: list[Candidate]) -> float | None:
    if not cands:
        return None
    verified = [c for c in cands if c.verified]
    return max(c.score for c in (verified or cands))


def _tie_key(c: Candidate) -> tuple:
    return (-c.score, 0 if c.verified else 1, c.cost, c.model, c.route)


def _pick(inp: ComposeInput, band: list[Candidate], load: dict[str, float], role_load: float) -> Candidate:
    def key(c: Candidate) -> tuple:
        w = _weight(inp, c.pool_id).weight
        p = hr.pressure(load.get(c.pool_id, 0.0), role_load, w)
        return (round(p, 9),) + _tie_key(c)
    return min(band, key=key)


def _mixed(inp: ComposeInput, per_role: dict[str, list[Candidate]], s_star: dict[str, float | None],
           roles: list[str]) -> dict[str, Candidate]:
    db = inp.db
    chosen: dict[str, Candidate] = {}
    load: dict[str, float] = {}
    order = sorted((r for r in roles if per_role.get(r)), key=lambda r: (db.importance(r), r))
    for role in order:
        cands = per_role[role]
        tol = db.tolerance(role)
        band = [c for c in cands if c.score >= s_star[role] - tol - _EPS]
        pick = _pick(inp, band, load, db.load_weight(role))
        chosen[role] = pick
        load[pick.pool_id] = load.get(pick.pool_id, 0.0) + db.load_weight(role)
    return chosen


def _diversity(inp: ComposeInput, per_role: dict[str, list[Candidate]], s_star: dict[str, float | None],
               chosen: dict[str, Candidate]) -> dict[str, str]:
    """Soft preference: reviewer and verifier on another vendor than the implementer's, within tolerance."""
    flags: dict[str, str] = {}
    impl = chosen.get("implementer")
    if impl is None:
        return flags
    tol = float(inp.db.policy.get("diversity_tolerance", 0.5))
    for role in inp.db.policy.get("diversity_roles") or ():
        cur = chosen.get(role)
        if cur is None or cur.vendor != impl.vendor:
            continue
        alts = [c for c in per_role.get(role, []) if c.vendor != impl.vendor
                and c.score >= s_star[role] - tol - _EPS]
        if alts:
            chosen[role] = min(alts, key=_tie_key)
            flags[role] = "diversity"
    return flags


def _fallbacks(cands: list[Candidate], pick: Candidate) -> tuple[str, ...]:
    """Same-vendor only, never cross-vendor: the next best model of the pick's vendor."""
    same = sorted({c.model for c in cands if c.vendor == pick.vendor and c.model != pick.model
                   and c.score >= pick.score - 2.0 - _EPS}, key=lambda m: m)
    if not same:
        return ()
    best = max((c for c in cands if c.model in same), key=lambda c: (c.score, c.model))
    return (best.model,)


def _assignment(inp: ComposeInput, role: str, pick: Candidate, cands: list[Candidate], s: float,
                extra: tuple[str, ...] = ()) -> Assignment:
    db = inp.db
    flags = list(extra)
    if not pick.verified:
        flags.append("unverified")
    if pick.stale_accepted:
        flags.append("stale-accepted")
    if pick.override:
        flags.append("override")
    if pick.score < s - db.tolerance(role) - _EPS and "diversity" not in flags:
        flags.append("below-band")
    tokens = int((db.policy.get("reference_tokens") or {}).get("prompt", 60000))
    price = db.price_at(pick.model, inp.as_of, tokens, pick.route)
    pool = db.pool(pick.pool_id) or {}
    return Assignment(role=role, model=pick.model, route=pick.route, effort=pick.effort, score=pick.score,
                      label=pick.label.value, as_of=pick.as_of, source_ref=pick.source_ref, pool_id=pick.pool_id,
                      verified=pick.verified, grade=pick.grade, importance=db.importance(role),
                      tolerance_class=db.tolerance_class(role), tolerance=db.tolerance(role), s_star=s,
                      flags=tuple(flags), fallbacks=_fallbacks(cands, pick), conditions=pick.conditions,
                      billing_class=str(pool.get("billing_class", "")), usage=str(pool.get("usage", "dispatch-ok")),
                      price_in=price.in_price if price else None, price_out=price.out_price if price else None,
                      price_verify=bool(price.verify) if price else False, price_tier=price.tier if price else "",
                      price_valid_until=(price.valid_until or "") if price else "")


def _solve(inp: ComposeInput, per_role: dict[str, list[Candidate]], s_star: dict[str, float | None],
           roles: list[str], mode: str) -> tuple[dict[str, Candidate], dict[str, str]]:
    """One assignment pass over the given candidate sets (mixed procedure, or the uniform model)."""
    if mode == "uniform":
        by_model: dict[str, dict[str, list[Candidate]]] = {}
        for role in roles:
            for c in per_role.get(role, []):
                by_model.setdefault(c.model, {}).setdefault(role, []).append(c)

        def total(model: str) -> tuple:
            covered = by_model[model]
            score = sum(inp.db.importance(r) * max(c.score for c in cs) for r, cs in covered.items())
            cost = sum(min(c.cost for c in cs) for cs in covered.values())
            verified = all(any(c.verified for c in cs) for cs in covered.values())
            return (-round(score, 9), 0 if verified else 1, cost, model)
        if not by_model:
            return {}, {}
        best = min(by_model, key=total)
        chosen = {r: min(cs, key=_tie_key) for r, cs in by_model[best].items()}
        return chosen, {}
    chosen = _mixed(inp, per_role, s_star, roles)
    flags = _diversity(inp, per_role, s_star, chosen)
    return chosen, flags


def _pool_notes(inp: ComposeInput, pool_ids: list[str]) -> tuple[PoolNote, ...]:
    out = []
    for pid in sorted(set(pool_ids)):
        if not pid:
            continue
        pool = inp.db.pool(pid) or {}
        reading = inp.readings.get(pid)
        ew = _weight(inp, pid)
        state = reading.state if reading is not None else hr.UNKNOWN
        if ew.basis == "static" and state == hr.KNOWN:
            state = hr.STALE
        out.append(PoolNote(
            pool_id=pid, billing_class=str(pool.get("billing_class", "")), state=state,
            source=(reading.source if reading is not None else "none"),
            used_fraction=(reading.used_fraction if reading is not None and ew.basis == "live" else None),
            resets_at=(reading.resets_at if reading is not None and ew.basis == "live" else None),
            static_weight=float(inp.static_weights.get(pid, hr.DEFAULT_WEIGHT)),
            effective_weight=ew.weight, basis=ew.basis, usage=str(pool.get("usage", ""))))
    return tuple(out)


def _references(inp: ComposeInput, roles: list[str]) -> tuple[ReferenceRow, ...]:
    """Reference profiles scored through the same database and gates; shown, never chosen automatically."""
    db = inp.db
    rank = {"verified": 0, "unverified": 1, "blocked": 2}
    out = []
    for name in sorted(inp.references):
        mapping = inp.references[name]
        total, covered, worst, notes = 0.0, 0, "verified", []
        for role in roles:
            spec = mapping.get(role, "")
            model, _, only_route = spec.partition("@")      # "vendor/id@route" pins the route
            model = db.resolve_model(model)
            if not model:
                continue
            rows = [r for r in db.rows_for(role) if r.model == model and r.score is not None
                    and (not only_route or r.route == only_route)]
            if not rows:
                notes.append(f"{role}: no score for {model}")
                continue
            status, reason, best = "blocked", "", 0.0
            for r in sorted(rows, key=lambda r: (r.route, r.effort)):
                v = inp.gate(r.model, r.route)
                st = "verified" if v.allowed and v.verified else ("unverified" if v.allowed or v.kind == "unverified"
                                                                   else "blocked")
                if rank[st] < rank[status] or not reason:
                    if rank[st] <= rank[status]:
                        status, reason = st, v.reason
                best = max(best, r.score)
            total += db.importance(role) * best
            covered += 1
            if rank[status] > rank[worst]:
                worst = status
            if status != "verified":
                notes.append(f"{role}: {status}" + (f" ({reason})" if reason else ""))
        if covered == 0:
            worst = "unscored"
        out.append(ReferenceRow(name=name, weighted_score=round(total, 3), covered_roles=covered, gate=worst,
                                detail="; ".join(notes[:2]) + (f"; +{len(notes) - 2} more" if len(notes) > 2 else "")))
    return tuple(out)


def _refusal(inp: ComposeInput, roles: tuple[str, ...], why: str, rejected=(), pools=()) -> Composition:
    return Composition(mode=inp.mode, shape=SHAPE_OF.get(inp.mode, ""), dataset_version=inp.db.dataset_version,
                       as_of=inp.as_of, allow_unverified=inp.allow_unverified, refusal=why,
                       rejected=tuple(rejected), loss_cap=float(inp.db.policy.get("loss_cap", 0.0)),
                       input_digest=_digest(inp, roles), pools=tuple(pools))


# --- the entry point ---------------------------------------------------------------------------
def compose(inp: ComposeInput) -> Composition:
    """Compose a team. Never raises on an empty or unusable input: it returns a refusal."""
    db = inp.db
    roles = tuple(sorted(inp.roles or db.roles(), key=lambda r: (-db.importance(r), r)))
    if inp.mode not in MODES:
        return _refusal(inp, roles, f"unknown mode {inp.mode!r}; use one of {', '.join(MODES)}")
    if not inp.enabled:
        return _refusal(inp, roles, "no provider is enabled; enable at least one provider in setup")

    per_role, rejected = _admitted(inp, roles)
    s_star = {r: _s_star(per_role[r]) for r in roles}
    covered = [r for r in roles if per_role[r]]
    if not covered:
        return _refusal(inp, roles, "no candidate passes the gates for any role", rejected)

    if inp.mode == "single-provider":
        groups = sorted({c.provider for r in covered for c in per_role[r]})
        best = None
        for prov in groups:
            sub = {r: [c for c in per_role[r] if c.provider == prov] for r in roles}
            sub_s = {r: _s_star(sub[r]) for r in roles}
            chosen, flags = _solve(inp, sub, sub_s, [r for r in roles if sub[r]], "mixed")
            if not chosen:
                continue
            score = sum(db.importance(r) * c.score for r, c in chosen.items())
            cost = sum(c.cost for c in chosen.values())
            key = (-round(score, 9), cost, prov)
            if best is None or key < best[0]:
                best = (key, prov, chosen, flags, sub, sub_s)
        if best is None:
            return _refusal(inp, roles, "no single provider can serve any role", rejected)
        _, _, chosen, div_flags, cand_view, s_view = best
    else:
        cand_view, s_view = per_role, s_star
        chosen, div_flags = _solve(inp, per_role, s_star, [r for r in roles if per_role[r]], inp.mode)

    # Default kept: an Anthropic-only team within band of the baseline profile on every role.
    kept = False
    if inp.baseline and inp.baseline_name and chosen and all(c.vendor == "anthropic" for c in chosen.values()):
        kept_pick: dict[str, Candidate] = {}
        ok = True
        # The baseline may only replace the team when it honours the mode's shape contract (ADR-0006).
        provider = next(iter({c.provider for c in chosen.values()})) if len({c.provider for c in chosen.values()}) == 1 \
            else None
        for role in chosen:
            base_model = db.resolve_model(inp.baseline.get(role, ""))
            opts = [c for c in per_role[role] if c.model == base_model]
            if inp.mode == "single-provider":
                if provider is None:
                    ok = False
                    break
                opts = [c for c in opts if c.provider == provider]
            if not opts or min(opts, key=_tie_key).score < s_star[role] - db.tolerance(role) - _EPS:
                ok = False
                break
            kept_pick[role] = min(opts, key=_tie_key)
        if ok and inp.mode == "uniform" and len({c.model for c in kept_pick.values()}) != 1:
            ok = False
        if ok and inp.mode == "single-provider" and len({c.provider for c in kept_pick.values()}) != 1:
            ok = False
        if ok:
            kept, chosen, div_flags = True, kept_pick, {}

    assignments: list[Assignment] = []
    for role in roles:
        pick = chosen.get(role)
        if pick is None:
            continue
        extra = (div_flags[role],) if role in div_flags else ()
        if kept:
            extra = extra + ("default-kept",)
        assignments.append(_assignment(inp, role, pick, cand_view.get(role) or per_role[role], s_star[role], extra))

    weighted_loss = round(sum(a.importance * (a.s_star - a.score) for a in assignments), 6)
    weighted_score = round(sum(a.importance * a.score for a in assignments), 6)
    cap = float(db.policy.get("loss_cap", math.inf))
    pools = _pool_notes(inp, [a.pool_id for a in assignments])
    if weighted_loss > cap + _EPS:
        return _refusal(inp, roles, f"weighted loss {weighted_loss:.2f} exceeds the cap {cap:.2f}", rejected, pools)

    unassigned = tuple((r, "no candidate passes the gates") for r in roles if r not in chosen)
    by_role = {a.role: ({"model": a.model, **({"fallbacks": list(a.fallbacks)} if a.fallbacks else {})})
               for a in assignments} if not kept else {}

    why: list[WhyRow] = []
    by_assignment = {a.role: a for a in assignments}
    for role in roles:
        a = by_assignment.get(role)
        s = s_view.get(role)
        tol = db.tolerance(role)
        view = cand_view.get(role, [])
        band = tuple(sorted(f"{c.model}@{c.route}" for c in view
                            if s is not None and c.score >= s - tol - _EPS))
        runner, why_lost = "", ""
        if a is not None:
            others = [c for c in per_role[role] if not (c.model == a.model and c.route == a.route)]
            if others:
                ru = min(others, key=_tie_key)
                runner = f"{ru.model}@{ru.route} ({ru.score:g})"
                if ru.score < (s if s is not None else a.s_star) - tol - _EPS:
                    why_lost = "outside band"
                elif not ru.verified and a.verified:
                    why_lost = "unverified"
                elif "diversity" in a.flags and ru.score >= a.score:
                    why_lost = "diversity"
                elif ru.pool_id != a.pool_id:
                    why_lost = "pool pressure"
                elif ru.score == a.score:
                    why_lost = "price tie"
                else:
                    why_lost = "lower score within band"
            else:
                rej = [x for x in rejected if x.role == role]
                if rej:
                    x = rej[0]
                    runner, why_lost = f"{x.model}@{x.route}", f"{x.kind}: {x.reason}"
        why.append(WhyRow(role=role, importance=db.importance(role), tolerance_class=db.tolerance_class(role),
                          tolerance=tol, s_star=s, band=band, chosen=a, runner_up=runner, runner_up_reason=why_lost,
                          note="" if a is not None else "no candidate"))

    flags: list[str] = []
    if any(not a.verified for a in assignments):
        flags.append("unverified-cells")
    if kept:
        flags.append("default-kept")
    if any(p.state != hr.KNOWN for p in pools):
        flags.append("static-weights")
    recommendation = inp.baseline_name if kept else ""
    return Composition(
        mode=inp.mode, shape=SHAPE_OF[inp.mode], dataset_version=db.dataset_version, as_of=inp.as_of,
        allow_unverified=inp.allow_unverified, assignments=tuple(assignments), by_role=by_role,
        why_rows=tuple(why), rejected=tuple(sorted(rejected, key=lambda x: (x.role, x.model, x.route))),
        unassigned=unassigned, flags=tuple(flags), weighted_loss=weighted_loss, weighted_score=weighted_score,
        loss_cap=cap, recommendation=recommendation, kept_default=kept, pools=pools,
        references=_references(inp, list(roles)), input_digest=_digest(inp, roles))


def optimum(inp: ComposeInput) -> float:
    """The score-only optimum: sum of importance x best admitted score over the covered roles."""
    total = 0.0
    roles = tuple(inp.roles or inp.db.roles())
    per_role, _ = _admitted(inp, roles)
    for role in roles:
        cands = per_role.get(role) or []
        if cands:
            total += inp.db.importance(role) * max(c.score for c in cands)
    return round(total, 6)


def composed_label(a: Assignment) -> str:
    """The weakest label that contributes to an assignment (the score row; hazards are not scores)."""
    return weakest(a.label).value
