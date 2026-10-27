"""ai-resources verify: what `ai-resources setup` left behind, checked read-only.

One implementation, three doors: the last wizard step, `ai-resources doctor` (section 4) and this
module's own `verify` subcommand all call `run_all` and print with `print_findings`.

Nothing here writes, and nothing here needs the network: the wizard runs it on every setup.
Cockpit modules are imported inside the functions that need them, because a cockpit module imports
`Finding` from here and the other direction at import time would be a cycle.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .setup import state

LEVELS = ("ok", "warn", "error")
_RANK = {"error": 0, "warn": 1, "ok": 2}
# The instruction file each simple cockpit writes its managed block into.
_INSTRUCTION_ATTRS = ("INSTRUCTIONS_PATH", "CLAUDE_MD_PATH", "GEMINI_MD_PATH", "CONVENTIONS_PATH")


@dataclass(frozen=True)
class Finding:
    """One observation about one cockpit. `error` is genuinely broken; everything advisory is `warn`."""
    level: str
    cockpit: str
    message: str
    remedy: str = ""

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"finding level must be one of {LEVELS}, not {self.level!r}")


def run_all(s: state.SetupState, cockpit_ids: list[str] | None = None,
            ctx: dict[str, Any] | None = None) -> list[Finding]:
    """Verify each cockpit in registry order (openclaw stays last), restricted to `cockpit_ids`.

    Never raises: a cockpit whose `verify()` blows up becomes one `error` Finding naming it, and
    the others still run. `ctx` carries optional extras (a test runner) next to the state.
    """
    from .setup import cockpits

    wanted = None if cockpit_ids is None else set(cockpit_ids)
    findings: list[Finding] = []
    for cid, mod in cockpits.ALL.items():
        if wanted is not None and cid not in wanted:
            continue
        try:
            fn = getattr(mod, "verify", None)
            found = fn({**(ctx or {}), "state": s}) if callable(fn) else _generic(cid, mod, s)
            findings.extend(f for f in (found or []) if isinstance(f, Finding))
        except Exception as e:  # noqa: BLE001 — verification must never take the wizard down
            findings.append(Finding("error", cid, f"verification of {cid} failed to run: {type(e).__name__}: {e}",
                                    "this is a bug in the kit; re-run `ai-resources verify --cockpit "
                                    f"{cid}` and report it"))
    if wanted is None or "lanes" in wanted:
        findings.extend(lanes_findings(s))
    return findings


def lanes_findings(s: state.SetupState) -> list[Finding]:
    """Subscription lanes (ADR-0007): drift warnings and the state of `lanes.yaml`. Read-only; [] without lanes.
    Never opens openclaw.json."""
    if not state.lanes_enabled(s):
        return []
    from .setup import lanes_render
    try:
        by_role = lanes_render.current_by_role(s)
        want = lanes_render.expected_text(s, by_role)
    except Exception as e:  # noqa: BLE001 - verification must never take the wizard down
        return [Finding("warn", "lanes", f"the lanes table could not be checked: {type(e).__name__}: {e}")]
    out = [Finding("warn", "lanes", w, "re-run `ai-resources setup --lanes`")
           for w in lanes_render.drift_warnings(s.profile.lanes, by_role)]
    lanes = s.profile.lanes if isinstance(s.profile.lanes, dict) else {}
    if (lanes.get("gates") or {}).get("antigravity"):
        # the operator opened the Antigravity lane: say which candidates are still held, and why
        for h in lanes.get("held") or []:
            out.append(Finding("warn", "lanes", f"{h.get('role')}: {h.get('candidate')} {h.get('reason')}",
                               "tick the role in the Google acknowledgement: `ai-resources setup --lanes "
                               "--lanes-antigravity` from a terminal"))
    path = state.lanes_path()
    have = path.read_text(encoding="utf-8") if path.is_file() else None
    if have is None:
        out.append(Finding("warn", "lanes", f"{path} is missing although lanes are enabled",
                           "re-run `ai-resources setup --lanes`"))
    elif have != want:
        out.append(Finding("warn", "lanes", f"{path} is out of date with the setup state",
                           "re-run `ai-resources setup --lanes`"))
    elif not out:
        out.append(Finding("ok", "lanes", f"lanes.yaml matches the setup state ({path})"))
    return out


def live_findings(s: state.SetupState) -> list[Finding]:
    """`verify --live`: the RUNNING system against what setup claims (gateway health, the plugin the gateway runs,
    hooks, lane agents, MCP children, the off-box credential). Needs OpenClaw; one `error` finding when its check
    cannot run. Read-only, but it asks the live gateway, so it is opt-in and slower than the file checks."""
    if not ((s.cockpits.get("openclaw") and s.cockpits["openclaw"].installed) or s.openclaw.host):
        return [Finding("ok", "live", "no OpenClaw on this machine; the live checks were skipped")]
    try:
        from .openclaw_host import kit_root
        from .setup.cockpits import openclaw as cockpit
        return cockpit.live_findings(s, str(kit_root()))
    except Exception as e:  # noqa: BLE001 - the live probe must not take verify down
        return [Finding("error", "live", f"the live checks failed to run: {type(e).__name__}: {e}")]


def _instruction_file(mod: Any) -> Path | None:
    for attr in _INSTRUCTION_ATTRS:
        value = getattr(mod, attr, None)
        if isinstance(value, Path):
            return value
    return None


def block_findings(cockpit: str, path: Path, what: str = "the kit block") -> list[Finding]:
    """The managed-block check shared by the generic verifier and the cockpits that add their own."""
    from .setup.cockpits import _shared

    if not path.is_file():
        return [Finding("warn", cockpit, f"{path} is missing although setup configured {cockpit}",
                        "re-run `ai-resources setup`")]
    pairs, orphan = _shared.managed_block_pairs(path)
    if pairs == 1 and not orphan:
        return [Finding("ok", cockpit, f"{what} is present in {path}")]
    if pairs == 0 and not orphan:
        return [Finding("error", cockpit, f"{what} was removed from {path} after setup wrote it",
                        "re-run `ai-resources setup` (it rewrites only the marked block; every byte "
                        "outside the markers is kept)")]
    return [Finding("error", cockpit, f"{path} has {pairs} kit block(s)" + (" and a BEGIN marker without an END"
                                                                            if orphan else ""),
                    "re-run `ai-resources setup` (it repairs the marked block and keeps every byte outside it)")]


def _generic(cid: str, mod: Any, s: state.SetupState) -> list[Finding]:
    """What can be checked for any cockpit from what the state records.

    When the record is not the shape this expects it reports nothing: a false "file missing" on
    the first run would discredit the step for good.
    """
    cs = (s.cockpits or {}).get(cid) if isinstance(getattr(s, "cockpits", None), dict) else None
    if not isinstance(getattr(cs, "configured", None), bool) or not isinstance(getattr(cs, "config_root", ""), str):
        return []
    if not cs.configured:
        return [Finding("ok", cid, f"{cid} is not configured by the kit; nothing to verify")]
    out: list[Finding] = []
    if cs.config_root and not Path(cs.config_root).expanduser().exists():
        out.append(Finding("warn", cid, f"the recorded config directory {cs.config_root} no longer exists",
                           "re-run `ai-resources setup`"))
    path = _instruction_file(mod)
    if path is not None:
        out.extend(block_findings(cid, path))
    elif not out:
        out.append(Finding("ok", cid, f"{cid} is configured ({cs.config_root or 'no config directory recorded'})"))
    return out


# --- rendering ----------------------------------------------------------------------------------------------

def ordered(findings: list[Finding]) -> list[Finding]:
    """Cockpits in the order they first appear, errors first inside each. Stable for a fixed list."""
    first: dict[str, int] = {}
    for f in findings:
        first.setdefault(f.cockpit, len(first))
    return sorted(findings, key=lambda f: (first[f.cockpit], _RANK[f.level]))


def counts(findings: list[Finding]) -> dict[str, int]:
    return {level: sum(f.level == level for f in findings) for level in LEVELS}


def has_errors(findings: list[Finding]) -> bool:
    return any(f.level == "error" for f in findings)


def failing_cockpits(findings: list[Finding]) -> list[str]:
    return sorted({f.cockpit for f in findings if f.level == "error"})


def summary(findings: list[Finding], selected: int | None = None) -> str:
    """The closing line: all clear, or how many of the selected toolings did not reach the state."""
    total = selected if selected is not None else len({f.cockpit for f in findings})
    bad = len(failing_cockpits(findings))
    c = counts(findings)
    tail = f"{c['error']} error(s), {c['warn']} warning(s), {c['ok']} ok"
    if bad:
        return f"{bad} of {total} selected toolings did not reach the expected state ({tail})"
    return f"All {total} selected toolings reached the expected state ({tail})"


def render(findings: list[Finding], *, show_ok: bool = True) -> str:
    """Plain-text form of the findings (what a test or a pipe sees)."""
    lines: list[str] = []
    for f in ordered(findings):
        if f.level == "ok" and not show_ok:
            continue
        lines.append(f"[{f.level}] {f.cockpit}: {f.message}")
        if f.remedy:
            lines.append(f"        -> {f.remedy}")
    return "\n".join(lines)


def print_findings(findings: list[Finding], *, show_ok: bool = True) -> None:
    """The same lines through the wizard's UI: errors first within each cockpit, remedy as detail."""
    from .setup import ui

    emit = {"error": ui.error, "warn": ui.warn, "ok": ui.ok}
    for f in ordered(findings):
        if f.level == "ok" and not show_ok:
            continue
        emit[f.level](f"{f.cockpit}: {f.message}")
        if f.remedy:
            ui.detail(f"  {f.remedy}")


# --- the `verify` subcommand --------------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    s = state.load()
    ids = list(args.cockpit) if args.cockpit else [cid for cid, cs in s.cockpits.items() if cs.configured]
    if not args.cockpit and state.lanes_enabled(s):
        ids.append("lanes")
    findings = run_all(s, ids)
    if getattr(args, "live", False):
        findings += live_findings(s)
    if args.json:
        for f in ordered(findings):
            print(json.dumps(asdict(f), ensure_ascii=False))
        return 1 if has_errors(findings) else 0
    from .setup import ui

    ui.require_deps()
    ui.banner("ai-resources verify", subtitle="What setup left behind (read-only)")
    if not findings:
        ui.warn("No cockpits configured; run `ai-resources setup`" if not ids
                else "No checks ran for " + ", ".join(ids))
        return 0
    print_findings(findings)
    (ui.error if has_errors(findings) else ui.info)(summary(findings))
    return 1 if has_errors(findings) else 0


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("verify", help="Check, read-only, that setup left every configured tooling in the expected state")
    p.add_argument("--json", action="store_true", help="One JSON object per finding (level, cockpit, message, remedy)")
    p.add_argument("--cockpit", action="append", default=[], metavar="ID",
                   help="Check only this cockpit (repeatable); default: every configured one")
    p.add_argument("--live", action="store_true",
                   help="Also check the running system: gateway health, the plugin it runs against the disk, hooks, "
                        "lane agents, MCP children, the off-box credential")
    p.set_defaults(func=cmd_verify)
