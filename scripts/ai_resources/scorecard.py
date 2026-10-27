"""Deterministic task scorecard: six 0-3 dimension scores -> mode (lite|standard|full), roster, model tiers.

Single source of truth for the numbers is skills/kit-orchestration/scorecard.json; the prose is in the
kit-orchestration skill ("Scorecard"). Pure: no model, no network. The orchestrator scores the dimensions
(a judgement), this module maps them (a rule), so the same scores always give the same team.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parents[2] / "skills" / "kit-orchestration" / "scorecard.json"
MODES = ("lite", "standard", "full")
_ORDER = {m: i for i, m in enumerate(MODES)}
# Short keys used when a card is recorded in one line (handoff front matter key `scorecard`).
ABBREV = json.loads(RULES_PATH.read_text(encoding="utf-8"))["abbrev"]
TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "handoff.md.template"


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


def _roster(mode: str, extras: list[str], rules: dict) -> list[str]:
    """The roles a mode runs: none for LITE, the base trio plus `extras` for STANDARD, everyone for FULL."""
    order = rules["base"]["full"]
    if mode == "full":
        return list(order)
    if mode == "standard":
        return sorted([*rules["base"]["standard"], *extras], key=order.index)
    return []


def _card(mode: str, roster: list[str], rules: dict, rationale: str, **extra) -> dict:
    """The card shape every producer returns; `extra` overrides or adds fields (total, scores, invalid...)."""
    return {"mode": mode, "scored_mode": mode, "bump_reason": None, "total": None, "scores": None, "roster": roster,
            "handoff": mode == "full" or "planner" in roster, "tiers": {r: rules["tiers"][r] for r in roster},
            "rationale": rationale, **extra}


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
          and s["uncertainty"] <= lite["max_uncertainty"] and s["resumability"] <= lite["max_resumability"]
          and s["verifiability"] <= lite.get("max_verifiability", 3)):
        mode, why = "lite", "small, resumable, no risk"
    else:
        mode, why = "standard", "needs an independent test gate and verifier"
    rationale = f"{why}; total {total}/18" + (f"; added {', '.join(extras)}" if mode == "standard" and extras else "")
    return _card(mode, _roster(mode, extras, rules), rules, rationale, total=total, scores=s)


def full_card(reason: str) -> dict:
    """The FULL card used when the scores cannot be trusted: the whole loop, never a cheaper guess."""
    rules = load_rules()
    return _card("full", _roster("full", [], rules), rules, f"{reason}; defaulting to FULL", invalid=True)


def mode_of_spelling(name) -> str | None:
    """A mode for any accepted spelling of a size or mode (`size_flags` in scorecard.json), else None."""
    rules = load_rules()
    size = rules["size_flags"].get(name)
    return next((m for m, sz in rules["sizes"].items() if sz == size), None) if size else None


def score_or_full(scores, legacy_mode: str | None = None) -> dict:
    """`score`, except that unusable scores mean FULL (the JS scripts do the same).

    Scores that are present but invalid give FULL even when a bare legacy `legacy_mode` comes with them.
    Only when no scores are given at all is a valid legacy mode honoured (any spelling in `size_flags`:
    lite/standard/full or the script names inline/light/full); anything else is FULL.
    """
    if scores is None:
        mode = mode_of_spelling(legacy_mode)
        if mode:
            rules = load_rules()
            return _card(mode, _roster(mode, [], rules), rules, f"legacy mode {mode} given without scores")
        return full_card("no scores and no valid mode")
    try:
        return score(scores)
    except (ValueError, TypeError) as e:
        return full_card(f"invalid scores ({e})")


def bump_for_risk(card: dict, risk_items: list) -> dict:
    """A named risk item takes a LITE card out of the inline path: mode becomes STANDARD (a verifier is added).

    The card records the effective mode in `mode`, the scored one in `scored_mode` and why in `bump_reason`.
    """
    if card["mode"] != "lite" or not risk_items:
        return card
    rules = load_rules()
    roster = _roster("standard", [], rules)
    return {**card, "mode": "standard", "roster": roster, "handoff": False,
            "tiers": {r: rules["tiers"][r] for r in roster},
            "bump_reason": f"risk items named: {'; '.join(str(i) for i in risk_items)}",
            "rationale": f"{card['rationale']}; bumped lite -> standard for a named risk item"}


def compact(card: dict) -> str:
    """One-line form for the handoff: `STANDARD res1 blast1 risk0 unc2 ver1 spr0` (see ABBREV)."""
    parts = [card["mode"].upper()]
    if card.get("scores"):
        parts += [f"{ABBREV[d]}{v}" for d, v in card["scores"].items()]
    if card.get("bump_reason"):
        parts.append(f"(scored {card['scored_mode'].upper()}, bumped: {card['bump_reason']})")
    if card.get("invalid"):
        parts.append("(invalid scores)")
    return " ".join(parts)


def diff_lines(base: str | None = None, cwd: str | None = None) -> int:
    """Changed lines (added plus removed) of the working tree against `base` (default: merge-base with origin/HEAD, then main)."""
    def git(*a: str) -> str:
        r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    ref = base or git("merge-base", "HEAD", "origin/HEAD") or git("merge-base", "HEAD", "main") or "HEAD"
    total = 0
    for line in git("diff", "--numstat", ref).splitlines():
        add, rem, *_ = line.split("\t")
        if add.isdigit() and rem.isdigit():
            total += int(add) + int(rem)
    return total


_KEY = re.compile(r"^scorecard:.*$", re.M)


def record(handoff: Path | str, value: str, *, append: bool = False) -> str:
    """Write `value` to the handoff front matter key `scorecard`; with append=True add it to the current value.

    The file is created from handoff.md.template when it does not exist. Returns the stored value.
    """
    path = Path(handoff)
    text = path.read_text(encoding="utf-8") if path.is_file() else TEMPLATE_PATH.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise ValueError(f"{path}: no front matter to record the scorecard in")
    cur = _KEY.search(m.group(1))
    old = ""
    if cur:
        try:
            old = json.loads(cur.group(0).split(":", 1)[1].split("#", 1)[0].strip() or '""')
        except json.JSONDecodeError:
            old = cur.group(0).split(":", 1)[1].split("#", 1)[0].strip().strip('"')
    stored = f"{old} {value}".strip() if append and old else value
    line = f"scorecard: {json.dumps(stored)}"
    head = _KEY.sub(lambda _m: line, m.group(1)) if cur else m.group(1) + "\n" + line
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{head}\n---\n{text[m.end():]}", encoding="utf-8")
    return stored


def observe(mode: str, failures: int = 0, lines: int = 0, rules: dict | None = None) -> tuple[str, str | None]:
    """Escalate from observed data: (new_mode, reason or None). This is the code-enforced part of escalation."""
    new = escalate(mode, failures, lines, rules)
    if new == mode:
        return mode, None
    return new, f"{failures} failed test round(s), diff of {lines} lines"


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
    rows = "\n".join(f"| {d} | {v} |" for d, v in (card["scores"] or {}).items()) or "| (none: scores invalid or absent) | - |"
    roster = ", ".join(f"{r} ({card['tiers'][r]})" for r in card["roster"]) or "none (inline)"
    return (f"## Scorecard\n| dimension | 0-3 |\n|---|---|\n{rows}\n"
            f"- mode: {card['mode'].upper()} | handoff: {'yes' if card['handoff'] else 'no'}\n"
            + (f"- scored {card['scored_mode'].upper()}, effective {card['mode'].upper()}: {card['bump_reason']}\n" if card.get("bump_reason") else "")
            + f"- roster: {roster}\n- rationale: {card['rationale']}\n")


def _pop(args: list[str], flag: str) -> str | None:
    if flag in args:
        i = args.index(flag)
        if i + 1 >= len(args):
            raise ValueError(f"{flag} needs a value")
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return None


USAGE = ("usage: scorecard '<json scores>' [--risk-item TEXT]... [--handoff PATH]\n"
         "       scorecard escalate <mode> <failures> <diff_lines> [--handoff PATH] [--reason TEXT]\n"
         "       scorecard gate <mode> [--failures N] [--diff-lines N | --base REF] [--handoff PATH]")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        handoff = _pop(args, "--handoff")
        reason = _pop(args, "--reason")
        risk_items = []
        while "--risk-item" in args:
            risk_items.append(_pop(args, "--risk-item"))
        if args[0] in ("escalate", "gate"):
            if args[0] == "escalate":
                mode, failures, lines = args[1], int(args[2]), int(args[3])
            else:
                mode = args[1]
                failures = int(_pop(args, "--failures") or 0)
                lines_arg = _pop(args, "--diff-lines")
                lines = int(lines_arg) if lines_arg is not None else diff_lines(_pop(args, "--base"))
            new, why = observe(mode, failures, lines)
            if handoff and why:
                record(handoff, f"-> {new.upper()} ({reason or why})", append=True)
            print(new)
        else:
            try:
                card = score_or_full(json.loads(args[0]))
            except json.JSONDecodeError as e:
                card = full_card(f"scores are not JSON ({e})")
            card = bump_for_risk(card, risk_items)
            if card.get("invalid"):
                print(f"scorecard: {card['rationale']}", file=sys.stderr)
            if handoff and card["mode"] != "lite":
                record(handoff, compact(card))
            print(render(card))
    except (ValueError, IndexError) as e:
        print(f"scorecard: {e}\n{USAGE}", file=sys.stderr)
        return 2
    return 0
