"""Phase 3 gates G0..G7 of the Antigravity lane (ADR-0007 section 11, plan `subscription-lanes-phase3-plan.md`).

`GATES` and `EVIDENCE` are literals: pure data that nobody edits by hand (ADR-0010: the gate symbols `GATES`,
`EVIDENCE`, `AGY_LANE_ADR_ACCEPTED`, `CODEX_LANE_ADR_ACCEPTED`, `WRITABLE_BACKENDS` and
`model_pins.AGY_LANE_MODELS_CONFIRMED` never change by hand). A gate that is not `proven` keeps the agy lane closed
everywhere it matters: admission in setup (`lanes_render.agy_admission`), the OpenClaw writer
(`openclaw_lanes.writable_backends`) and `lanes prepare --lane` (`lanes_cmd._admit_lane`). The checks fail closed with a
message that names the gates.

The statuses below are what the repository can show. V5 (slugs), V6 (sandbox), V7 (tool surface), V9 (agy workspace
and cwd) and V12 (agy home) are verification stages that run only through the isolated agy probe harness
(`~/.cache/ai-resources-live-e2e/bin-agyprobe`) in an operator-announced window. None has run, so none is claimed.

How a result is recorded: the operator runs `ai-resources lanes gates record --run-dir <window dir>` and commits the one
file it writes, `scripts/ai_resources/data/agy_gate_evidence.json`. `agy_gate_evidence.load()` (lazy, cached, no
import-time I/O) validates it and fails closed; `effective_evidence()` overlays a valid record on the literals, and the
helpers below read that overlay. With no record, or with an invalid one, every status is the literal's.
`tests/test_agy_lane_gates.py` pins that a gate cannot be `proven` without evidence.

Operator waiver (2026-10-24, ADR-0007 section 11 addendum): the operator waived G0..G7 as admission preconditions, so
`unproven`, `all_proven` and `refusal` skip the gates in `WAIVED_BY_OPERATOR` and the lane is admitted with the flag and
a valid Google acknowledgement alone. A waived gate is not proven: `gate_proven` and `status_table` still read the
literals and the record only, so G5 (the kit-owned agy home) stays off and agy keeps the operator's real login.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

AGY_LANE_BACKEND = "agy-cli-lane"
AGY_HOME_PARTS = (".local", "share", "ai-resources", "agy-home")
PROVEN = "proven"
PARTIAL = "partial"
NOT_PROVEN = "not proven"
STATUSES = (PROVEN, PARTIAL, NOT_PROVEN)


@dataclass(frozen=True)
class Gate:
    id: str
    name: str
    verification: str          # the V-stage that proves it ("" when the operator decides)
    status: str = NOT_PROVEN
    evidence: str = ""         # run directory / seal / ADR reference; required when status is `proven`
    note: str = ""


GATES: tuple[Gate, ...] = (
    Gate("G0", "phase 2 isolated merge gate PASS, review chain closed, prerequisites landed", "merge-gate",
         note="the phase 2 isolated merge gate has no recorded PASS in ADR-0007 Evidence"),
    Gate("G1", "V5: `agy models` slugs confirmed", "V5",
         note="AGY_LANE_MODELS holds unconfirmed candidates; V5 has not run"),
    Gate("G2", "V6: `agy --sandbox` containment of writes and network", "V6", note="not run"),
    Gate("G3", "V7: tool surface, MCP and OpenClaw tools in the lane child", "V7", note="not run"),
    Gate("G4", "V9 (agy): spawn with cwd in a managed worktree; where bootstrap files land", "V9", note="not run"),
    Gate("G5", "V12: agy home override and its side effects", "V12", note="not run"),
    Gate("G6", "OQ answers and measured cost per step recorded", "operator", PARTIAL,
         "docs/decisions/0007-subscription-lanes.md#11-antigravity-lane",
         "operator decisions of 2026-10-16 are recorded; the measured cost per step is not (needs V6/V7 runs)"),
    Gate("G7", "ADR-0007 section 11 Accepted and the Google acknowledgement text approved", "operator", PARTIAL,
         "docs/decisions/0007-subscription-lanes.md#11-antigravity-lane",
         "section 11 accepted by the operator on 2026-10-16; approval of the final Google text and its "
         "sha256 is not recorded"),
)

EVIDENCE: dict[str, Gate] = {g.id: g for g in GATES}

# The gates the operator waived as admission preconditions (ADR-0007 section 11, operator waiver of 2026-10-24).
WAIVED_BY_OPERATOR: frozenset[str] = frozenset(g.id for g in GATES)
WAIVER_REF = "docs/decisions/0007-subscription-lanes.md#operator-waiver-2026-10-24"


def effective_evidence() -> dict[str, Gate]:
    """The literal `EVIDENCE` with the gates a VALID committed record proves laid over it (lazy: the record is
    read, validated and cached on first use, never at import)."""
    from . import agy_gate_evidence
    merged = dict(EVIDENCE)
    record = agy_gate_evidence.load()
    if record.status == "valid":
        merged.update(record.gates)
    return merged


def unproven(evidence: Mapping[str, Gate] | None = None) -> list[Gate]:
    """The gates that are neither `proven` nor waived by the operator, in order. A missing gate counts as not
    proven (fail closed)."""
    ev = effective_evidence() if evidence is None else evidence
    out: list[Gate] = []
    for g in GATES:
        if g.id in WAIVED_BY_OPERATOR:
            continue
        got = ev.get(g.id)
        if got is None or got.status != PROVEN or not got.evidence:
            out.append(got or g)
    return out


def all_proven(evidence: Mapping[str, Gate] | None = None) -> bool:
    return not unproven(evidence)


def gate_proven(gate_id: str) -> bool:
    got = effective_evidence().get(gate_id)
    return got is not None and got.status == PROVEN and bool(got.evidence)


def record_status():
    """The evidence record's state (`absent`, `valid` or `invalid` with a reason), for doctor and `lanes gates`."""
    from . import agy_gate_evidence
    return agy_gate_evidence.load()


def refusal(evidence: Mapping[str, Gate] | None = None) -> str | None:
    """The fail-closed message, or None when every gate is proven."""
    bad = unproven(evidence)
    if not bad:
        return None
    text = ("held: Antigravity lane gates not proven: "
            + ", ".join(f"{g.id} ({g.verification or 'operator'}: {g.status})" for g in bad)
            + "; evidence is recorded in ADR-0007 section 11")
    if evidence is None:
        record = record_status()
        if record.status == "invalid":
            text += f"; evidence file invalid: {record.reason}"
    return text


def status_table(evidence: Mapping[str, Gate] | None = None) -> dict[str, dict[str, str]]:
    """`{gate: {status, evidence}}`, the shape recorded in a ledger entry or printed by a status command."""
    ev = effective_evidence() if evidence is None else evidence
    return {g.id: {"status": (ev.get(g.id) or g).status, "evidence": (ev.get(g.id) or g).evidence}
            for g in GATES}


def agy_home_path() -> Path:
    """The kit-owned agy home the lane would use (V12, OQ-P3-4). Only a path: nothing here creates it, and it
    is never under /tmp, `workspace_root` or `~/.openclaw/kit-lanes/`."""
    return Path.home().joinpath(*AGY_HOME_PARTS)


def summary() -> dict:
    """The lane's state in numbers and words, for `lanes gates`, doctor and the team view (one source)."""
    ev = effective_evidence()
    proven = [g.id for g in GATES if (ev.get(g.id) or g).status == PROVEN and (ev.get(g.id) or g).evidence]
    partial = [g.id for g in GATES if g.id not in proven and (ev.get(g.id) or g).status == PARTIAL]
    waived = [g.id for g in GATES if g.id not in proven and g.id in WAIVED_BY_OPERATOR]
    return {"proven": proven, "partial": partial, "waived": waived, "total": len(GATES),
            "on": len(proven) + len(waived) == len(GATES), "record": record_status()}


def summary_line() -> str:
    """`Antigravity lane: held, gates 0/8 proven (G6, G7 partial); evidence record: absent; operator action: ...`"""
    info = summary()
    record = info["record"]
    count = f"gates {len(info['proven'])}/{info['total']} proven"
    if info["on"] and info["waived"]:
        return (f"Antigravity lane: on, {count}, {', '.join(info['waived'])} waived by the operator "
                f"({WAIVER_REF}); admission needs the flag and a valid Google acknowledgement")
    if info["on"]:
        who = f"{record.recorded_by}" + (f", commit {record.commit[:8]} by {record.author}" if record.commit else "")
        return (f"Antigravity lane: on, {count} (record {(record.commit or record.file_sha8)[:8]}, "
                f"run {record.run_id}, {record.recorded_at}, {who})")
    text = f"Antigravity lane: held, {count}"
    if info["partial"]:
        text += f" ({', '.join(info['partial'])} partial)"
    if record.status == "invalid":
        text += f"; evidence record: invalid ({record.reason})"
    elif record.status == "valid":
        text += f"; evidence record: valid for run {record.run_id}, other gates still unproven"
    else:
        text += "; evidence record: absent"
    return text + "; operator action: probe window, then ai-resources lanes gates record"
