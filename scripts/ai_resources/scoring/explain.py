"""The why table: a text rendering and a JSON document of a `Composition`.

Pure: it formats what the engine already decided. Every assignment has a row, the reference profiles are
listed with their gate status, and the footer says plainly when headroom was unknown (static weights used)
and when a team is unverified.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from . import evidence as _evidence
from .compose import Assignment, Composition, WhyRow


def _price(a: Assignment | None) -> str:
    if a is None or a.price_in is None:
        return "n/a"
    out = "?" if a.price_out is None else f"{a.price_out:g}"
    text = f"{a.price_in:g}/{out}"
    if a.price_valid_until:
        text += f" until {a.price_valid_until}"
    if a.price_tier == "long-context":
        text += " (long ctx)"
    if a.price_verify:
        text += " [verify]"
    return text


def _headroom(comp: Composition, a: Assignment | None) -> str:
    if a is None:
        return "-"
    note = next((p for p in comp.pools if p.pool_id == a.pool_id), None)
    if note is None:
        return "static weight"
    if note.basis == "live":
        pct = "" if note.used_fraction is None else f" {note.used_fraction * 100:.0f}% used"
        reset = "" if note.resets_at is None else f", resets_at {note.resets_at}"
        return f"{note.state} via {note.source}{pct}{reset}"
    return f"{note.state}; static weight {note.static_weight:g}"


def _gate(a: Assignment | None) -> str:
    return "-" if a is None else ("verified" if a.verified else "UNVERIFIED")


def footer_lines(comp: Composition) -> list[str]:
    lines = [f"weighted loss {comp.weighted_loss:.2f} (cap {comp.loss_cap:g}); dataset_version {comp.dataset_version}; "
             f"input digest {comp.input_digest}; as of {comp.as_of}"]
    unv = [a.role for a in comp.assignments if not a.verified]
    if comp.mode == "mixed" or unv:
        if unv:
            lines.append("This team is UNVERIFIED today: the compat matrix has no verified cross-provider cell, so "
                         f"{len(unv)} role(s) rest on cells marked unverified ({', '.join(sorted(unv))}).")
        else:
            lines.append("Mixed mode is unverified today: no cross-provider cell is verified in the compat matrix; "
                         "every non-Anthropic cell needs --allow-unverified and is marked.")
    unknown = [p.pool_id for p in comp.pools if p.basis != "live"]
    if unknown:
        lines.append("Headroom unknown or stale for: " + ", ".join(sorted(unknown)) + "; static weights were used.")
    if comp.kept_default:
        lines.append(f"Default kept: {comp.recommendation} is within band on every role; no override is written.")
    lines.append("Composition writes no backend or agentId and no cross-vendor fallback; "
                 "agy (Antigravity) is direct use only and never a target.")
    return lines


def lane_column(lanes: Any) -> dict[str, str] | None:
    """role -> lane text for the Lane column, from a `LaneTable` or a `{role: ref}` mapping.

    None (or an empty table) means no column at all: the output is then byte-identical to the one the
    engine produced before lanes existed (the golden fixture).
    """
    if not lanes:
        return None
    roles = getattr(lanes, "roles", None)
    if roles is not None:
        out = {r: ln.ref for r, ln in roles.items()}
        out.update({r: f"{out.get(r, '-')} + second {ln.ref}" for r, ln in getattr(lanes, "second", {}).items()})
        for r, why in getattr(lanes, "blocked", {}).items():
            out[r] = "BLOCKED"
        return out
    return {str(r): str(v) for r, v in dict(lanes).items()}


def render_text(comp: Composition, lanes: Any = None) -> str:
    if comp.refused:
        return f"No team proposed: {comp.refusal}\n" + "\n".join(f"  rejected {r.role} {r.model}@{r.route}: "
                                                              f"{r.reason}" for r in comp.rejected[:12]) + "\n"
    head = ["role", "imp", "class", "band", "chosen", "score", "label", "as_of", "price in/out", "pool",
            "headroom", "gate", "usage", "runner-up / why it lost", "flags"]
    lane_of = lane_column(lanes)
    if lane_of is not None:
        head.append("lane")
    rows: list[list[str]] = []
    for w in comp.why_rows:
        a = w.chosen
        chosen = "-" if a is None else (f"{a.model} @{a.route}" + ("" if a.effort == "default" else f" ({a.effort})"))
        rows.append([
            w.role, f"{w.importance:g}", f"{w.tolerance_class} {w.tolerance:g}",
            f"{len(w.band)} in band" if w.band else "-", chosen,
            "-" if a is None else f"{a.score:g}", "-" if a is None else a.label, "-" if a is None else a.as_of,
            _price(a), "-" if a is None else f"{a.pool_id} ({a.billing_class})", _headroom(comp, a), _gate(a),
            "-" if a is None else a.usage,
            (w.runner_up + (f" / {w.runner_up_reason}" if w.runner_up_reason else "")) if w.runner_up else w.note or "-",
            ",".join(a.flags) if a is not None else "",
        ] + ([lane_of.get(w.role, "claude-code")] if lane_of is not None else []))
    widths = [max(len(head[i]), *(len(r[i]) for r in rows)) for i in range(len(head))]
    fmt = lambda cells: "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    out = [f"Team composition: mode {comp.mode} ({comp.shape})", fmt(head), fmt(["-" * w for w in widths])]
    out += [fmt(r) for r in rows]
    if comp.references:
        out += ["", "Reference profiles (shown, never chosen automatically):"]
        for ref in comp.references:
            out.append(f"  {ref.name}: weighted score {ref.weighted_score:g} over {ref.covered_roles} role(s); "
                       f"gate {ref.gate}" + (f"; {ref.detail}" if ref.detail else ""))
    if comp.unassigned:
        out += ["", "Unassigned: " + ", ".join(f"{r} ({why})" for r, why in comp.unassigned)]
    out += [""] + footer_lines(comp)
    return "\n".join(out) + "\n"


def _assignment_json(comp: Composition, a: Assignment | None) -> dict[str, Any] | None:
    if a is None:
        return None
    note = next((p for p in comp.pools if p.pool_id == a.pool_id), None)
    return {
        "model": a.model, "route": a.route, "effort": a.effort, "score": a.score, "label": a.label,
        "as_of": a.as_of, "source_ref": a.source_ref, "pool": a.pool_id, "billing_class": a.billing_class,
        "headroom": None if note is None else {
            "state": note.state, "source": note.source, "used_fraction": note.used_fraction,
            "resets_at": note.resets_at, "basis": note.basis, "static_weight": note.static_weight,
            "effective_weight": round(note.effective_weight, 6)},
        "gate": "verified" if a.verified else "UNVERIFIED", "grade": a.grade, "usage": a.usage,
        "price": {"in": a.price_in, "out": a.price_out, "verify": a.price_verify, "tier": a.price_tier,
                  "valid_until": a.price_valid_until or None},
        "flags": list(a.flags), "fallbacks": list(a.fallbacks), "conditions": a.conditions,
        "evidence": {"tag": "score-derived", "label": a.label, "confidence": _evidence.confidence(a.label),
                     "effort": a.effort},
    }


def to_json(comp: Composition, lanes: Any = None) -> dict[str, Any]:
    """A JSON-serialisable document (deterministic: lists are in engine order, keys are sorted on dump).

    With a lanes table every row gains `lane` and the document gains `lanes`; without one the document is
    exactly what it was before lanes existed."""
    by_role = {w.role: w for w in comp.why_rows}
    lane_of = lane_column(lanes)
    doc = {
        "mode": comp.mode, "shape": comp.shape, "dataset_version": comp.dataset_version, "as_of": comp.as_of,
        "allow_unverified": comp.allow_unverified, "input_digest": comp.input_digest,
        "recommendation": comp.recommendation, "kept_default": comp.kept_default, "refusal": comp.refusal,
        "weighted_loss": comp.weighted_loss, "weighted_score": comp.weighted_score, "loss_cap": comp.loss_cap,
        "flags": list(comp.flags),
        "rows": [{
            "role": w.role, "importance": w.importance, "tolerance_class": w.tolerance_class,
            "tolerance": w.tolerance, "s_star": w.s_star, "band": list(w.band),
            "chosen": _assignment_json(comp, w.chosen), "runner_up": w.runner_up,
            "runner_up_reason": w.runner_up_reason, "note": w.note,
        } for w in comp.why_rows] if by_role else [],
        "pools": [{"pool": p.pool_id, "billing_class": p.billing_class, "state": p.state, "source": p.source,
                   "used_fraction": p.used_fraction, "resets_at": p.resets_at, "static_weight": p.static_weight,
                   "effective_weight": round(p.effective_weight, 6), "basis": p.basis, "usage": p.usage}
                  for p in comp.pools],
        "references": [{"name": r.name, "weighted_score": r.weighted_score, "covered_roles": r.covered_roles,
                        "gate": r.gate, "detail": r.detail} for r in comp.references],
        "rejected": [{"role": r.role, "model": r.model, "route": r.route, "kind": r.kind, "reason": r.reason}
                     for r in comp.rejected],
        "unassigned": [{"role": r, "reason": why} for r, why in comp.unassigned],
        "footer": footer_lines(comp),
    }
    if lane_of is not None:
        for row in doc["rows"]:
            row["lane"] = lane_of.get(row["role"], "claude-code")
        doc["lanes"] = dict(sorted(lane_of.items()))
    return doc


def dumps(comp: Composition, lanes: Any = None) -> str:
    return json.dumps(to_json(comp, lanes), indent=2, sort_keys=True) + "\n"
