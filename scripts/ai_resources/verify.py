"""ai-resources verify: what `ai-resources setup` left behind, checked read-only.

One implementation, three doors: the last wizard step, `ai-resources doctor` (section 4) and this
module's own `verify` subcommand all call `run_all` and print with `print_findings`.

Nothing here writes and nothing here needs the network, with one exception: `repair_findings` regenerates the off-box
uploader's credential file (derived from the host federation, no secret in it) when it is missing.
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
    """The state of `lanes.yaml` (generated from the routing matrix, ADR-0019). Read-only; [] without lanes.
    Never opens openclaw.json."""
    if not state.lanes_enabled(s):
        return []
    from .setup import lanes_render
    try:
        want = lanes_render.expected_text(s)
    except Exception as e:  # noqa: BLE001 - verification must never take the wizard down
        return [Finding("warn", "lanes", f"the lanes table could not be checked: {type(e).__name__}: {e}")]
    path = state.lanes_path()
    have = path.read_text(encoding="utf-8") if path.is_file() else None
    if have is None:
        return [Finding("warn", "lanes", f"{path} is missing although lanes are enabled",
                        "re-run `ai-resources setup --lanes`")]
    if have != want:
        return [Finding("warn", "lanes", f"{path} is out of date with the setup state",
                        "re-run `ai-resources setup --lanes`")]
    return [Finding("ok", "lanes", f"lanes.yaml matches the setup state ({path})")]


def repair_findings() -> list[Finding]:
    """Self-heal what the kit derives and a stray process can delete: the off-box credential file. [] when no off-box
    backup is configured. A path the operator named themselves is only checked, never recreated."""
    try:
        from . import openclaw_host as host
        status, detail = host.offbox_cred_repair()
    except Exception:  # noqa: BLE001 - self-healing must not take verify down
        return []
    if status == "skipped":
        return []
    level = {"ok": "ok", "repaired": "ok", "failed": "error"}[status]
    return [Finding(level, "openclaw", ("repaired: " if status == "repaired" else "off-box credential: ") + detail,
                    "run on the host that has the federation (`~/.config/gcloud/agent-wif`), then `ai-resources setup`"
                    if status == "failed" else "")]


def credit_findings() -> list[Finding]:
    """The credit ledger's 80% alert as warnings (read-only, no network): a pool that has spent 80% of the credit the
    operator declared is a warning about money, not a defect of the install."""
    try:
        from . import credits
        return [Finding("warn", "credits", message, "`ai-resources credits show`") for _c, _s, message in credits.alerts()]
    except Exception:  # noqa: BLE001 - bookkeeping must not take verify down
        return []


def key_findings() -> list[Finding]:
    """`verify --live`: the Gemini and Anthropic keys (gateway env and OpenClaw profile), probed with a free models.list
    call and never repaired here. A rejected key is an error; no answer from the provider is not a finding."""
    try:
        from . import key_check
        results = key_check.check(fix=False)
    except Exception:  # noqa: BLE001
        return []
    return [Finding("error" if r.problem else "ok", "keys", "live: " + r.line(),
                    "`ai-resources keys` repairs a rejected env key from the working profile key" if r.problem else "")
            for r in results if key_check.ERROR not in (r.env, r.profile)]


def live_findings(s: state.SetupState, ak_path: str | None = None, runner=None, *, skip_if_absent: bool = True) -> list[Finding]:
    """`verify --live`: the RUNNING system against what setup claims. It runs the acceptance group `setup-live` plus the
    spawn wiring case (the SAME cases `ai-resources acceptance` runs) and maps each result to a finding: PASS is ok, FAIL is
    the case's severity with its remedy, SKIP says nothing. Read-only, but it asks the live gateway, so it is opt-in."""
    if skip_if_absent and not ((s.cockpits.get("openclaw") and s.cockpits["openclaw"].installed) or s.openclaw.host):
        return [Finding("ok", "live", "no OpenClaw on this machine; the live checks were skipped")]
    try:
        from . import acceptance
        env = acceptance.Env(state=s, ak_path=ak_path or "", runner=runner, offline=True)
        cases = acceptance.select("setup-live", operator=False) + acceptance.select("spawn", ("spawn.allow",))
        return [Finding("ok" if r.status == acceptance.PASS else c.severity, "openclaw", "live: " + r.reason,
                        "" if r.status == acceptance.PASS else c.remedy)
                for c, r in acceptance.run_cases(cases, env) if r.status != acceptance.SKIP]
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
    findings += credit_findings()
    findings += repair_findings()
    if getattr(args, "live", False):
        findings += live_findings(s)
        findings += key_findings()
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
