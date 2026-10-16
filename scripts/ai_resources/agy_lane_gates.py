"""Phase 3 gates G0..G7 of the Antigravity lane (ADR-0007 section 11, plan `subscription-lanes-phase3-plan.md`).

Pure data and pure helpers, no I/O. Each gate carries a status and the evidence that backs it. A gate that is
not `proven` keeps the agy lane closed everywhere it matters: admission in setup (`lanes_render.agy_admission`),
the OpenClaw writer (`openclaw_lanes.writable_backends`) and `lanes prepare --lane` (`lanes_cmd._admit_lane`).
The checks fail closed with a message that names the gates.

The statuses below are what the repository can show. V5 (slugs), V6 (sandbox), V7 (tool surface), V9 (agy
workspace and cwd) and V12 (agy home) are verification stages that run only through the isolated agy probe
harness (`~/.cache/ai-resources-live-e2e/bin-agyprobe`) in an operator-announced window. None has run, so
none is claimed. To record a result, edit `EVIDENCE` in the same commit that adds the run directory and seal
to ADR-0007 "Evidence"; `tests/test_agy_lane_gates.py` pins that a gate cannot be `proven` without evidence.
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


def unproven(evidence: Mapping[str, Gate] | None = None) -> list[Gate]:
    """The gates that are not `proven`, in order. A missing gate counts as not proven (fail closed)."""
    ev = EVIDENCE if evidence is None else evidence
    out: list[Gate] = []
    for g in GATES:
        got = ev.get(g.id)
        if got is None or got.status != PROVEN or not got.evidence:
            out.append(got or g)
    return out


def all_proven(evidence: Mapping[str, Gate] | None = None) -> bool:
    return not unproven(evidence)


def refusal(evidence: Mapping[str, Gate] | None = None) -> str | None:
    """The fail-closed message, or None when every gate is proven."""
    bad = unproven(evidence)
    if not bad:
        return None
    return ("held: Antigravity lane gates not proven: "
            + ", ".join(f"{g.id} ({g.verification or 'operator'}: {g.status})" for g in bad)
            + "; evidence is recorded in ADR-0007 section 11")


def status_table(evidence: Mapping[str, Gate] | None = None) -> dict[str, dict[str, str]]:
    """`{gate: {status, evidence}}`, the shape recorded in a ledger entry or printed by a status command."""
    ev = EVIDENCE if evidence is None else evidence
    return {g.id: {"status": (ev.get(g.id) or g).status, "evidence": (ev.get(g.id) or g).evidence}
            for g in GATES}


def agy_home_path() -> Path:
    """The kit-owned agy home the lane would use (V12, OQ-P3-4). Only a path: nothing here creates it, and it
    is never under /tmp, `workspace_root` or `~/.openclaw/kit-lanes/`."""
    return Path.home().joinpath(*AGY_HOME_PARTS)
