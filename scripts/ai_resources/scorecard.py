"""Deterministic task scorecard: six 0-3 dimension scores -> mode (lite|standard|full), roster, model tiers.

Single source of truth for the numbers is skills/kit-orchestration/scorecard.json; the prose is in the
kit-orchestration skill ("Scorecard"). Pure: no model, no network. The orchestrator scores the dimensions
(a judgement), this module maps them (a rule), so the same scores always give the same team.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parents[2] / "skills" / "kit-orchestration" / "scorecard.json"
MODES = ("lite", "standard", "full")
_ORDER = {m: i for i, m in enumerate(MODES)}


def load_rules(path: Path = RULES_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(scores: dict, rules: dict | None = None) -> dict[str, int]:
    dims = (rules or load_rules())["dimensions"]
    missing = [d for d in dims if d not in scores]
    extra = [k for k in scores if k not in dims]
    if missing or extra:
        raise ValueError(f"scores need exactly {dims}; missing={missing} unexpected={extra}")
    out = {}
    for d in dims:
        v = scores[d]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 3:
            raise ValueError(f"{d} must be an integer 0-3, got {v!r}")
        out[d] = v
    return out


def _triggered(scores: dict[str, int], rules: dict) -> list[str]:
    return [role for role, cond in rules["triggers"].items() if any(scores[d] >= t for d, t in cond.items())]


def score(scores: dict, rules: dict | None = None) -> dict:
    """Map dimension scores to {mode, roster, handoff, total, tiers, rationale}."""
    rules = rules or load_rules()
    s = validate(scores, rules)
    total = sum(s.values())
    lite, full = rules["lite"], rules["full"]
    extras = [r for r in _triggered(s, rules) if r not in rules["base"]["standard"]]
    if total >= full["min_total"] or s["risk"] >= full["min_risk"] or len(extras) >= full["min_extras"]:
        mode, why = "full", "total/risk/triggers reach the full threshold"
    elif (not extras and total <= lite["max_total"] and s["risk"] <= lite["max_risk"]
          and s["uncertainty"] <= lite["max_uncertainty"] and s["resumability"] <= lite["max_resumability"]):
        mode, why = "lite", "small, resumable, no risk"
    else:
        mode, why = "standard", "needs an independent test gate and verifier"
    if mode == "full":
        roster = list(rules["base"]["full"])
    elif mode == "standard":
        roster = list(rules["base"]["standard"])
        for role in extras:
            roster.append(role)
        order = rules["base"]["full"]
        roster.sort(key=lambda r: order.index(r))
    else:
        roster = []
    tiers = {r: rules["tiers"][r] for r in roster}
    return {
        "mode": mode,
        "total": total,
        "scores": s,
        "roster": roster,
        "handoff": mode == "full" or "planner" in roster,
        "tiers": tiers,
        "rationale": f"{why}; total {total}/18" + (f"; added {', '.join(extras)}" if mode == "standard" and extras else ""),
    }


def escalate(mode: str, failures: int, diff_lines: int, rules: dict | None = None) -> str:
    """The mode after observing failed test rounds and diff size; never lower than `mode`."""
    if mode not in _ORDER:
        raise ValueError(f"unknown mode {mode!r}")
    table = (rules or load_rules())["escalation"].get(mode, {})
    best = mode
    for target, limit in table.items():
        if (failures >= limit["failures"] or diff_lines >= limit["diff_lines"]) and _ORDER[target] > _ORDER[best]:
            best = target
    return best


def render(card: dict) -> str:
    """The scorecard block the orchestrator prints first and copies into the handoff."""
    rows = "\n".join(f"| {d} | {v} |" for d, v in card["scores"].items())
    roster = ", ".join(f"{r} ({card['tiers'][r]})" for r in card["roster"]) or "none (inline)"
    return (f"## Scorecard\n| dimension | 0-3 |\n|---|---|\n{rows}\n"
            f"- mode: {card['mode'].upper()} | handoff: {'yes' if card['handoff'] else 'no'}\n"
            f"- roster: {roster}\n- rationale: {card['rationale']}\n")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: scorecard '<json scores>' | scorecard escalate <mode> <failures> <diff_lines>", file=sys.stderr)
        return 2
    try:
        if args[0] == "escalate":
            print(escalate(args[1], int(args[2]), int(args[3])))
        else:
            print(render(score(json.loads(args[0]))))
    except (ValueError, IndexError, json.JSONDecodeError) as e:
        print(f"scorecard: {e}", file=sys.stderr)
        return 2
    return 0
