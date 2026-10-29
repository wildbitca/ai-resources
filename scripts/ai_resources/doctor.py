"""ai-resources doctor — full health check of multi-model setup."""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import billing, residue, scratch, verify
from .setup import state, detection, credentials, litellm, providers, smoke, ui

WEEK_S = 7 * 86400

# Who closes an issue. `setup` re-running fixes it; `operator` needs the named command; `history` is a past record
# that nothing can change (it ages out) and so is listed but never counted as open.
SETUP = "setup"
OPERATOR = "operator"
HISTORY = "history"


@dataclass(frozen=True)
class Issue:
    """One finding of the doctor: a stable `name`, what is wrong, and who fixes it."""
    name: str
    message: str
    fixed_by: str
    command: str = ""

    def label(self) -> str:
        if self.fixed_by == SETUP:
            return "fixed by: setup"
        if self.fixed_by == OPERATOR:
            return f"fixed by: operator ({self.command})" if self.command else "fixed by: operator"
        return "fixed by: nobody (history; it leaves the 7-day window on its own)"


_ISSUES: list[Issue] = []


def _flag(name: str, message: str, fixed_by: str, command: str = "") -> int:
    """Register one issue and return 1, so a check can `return _flag(...)`. `cmd_doctor` clears the list first."""
    _ISSUES.append(Issue(name, message, fixed_by, command))
    return 1


def _open(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.fixed_by != HISTORY]


def banner_text(issues: list[Issue]) -> tuple[str, str]:
    """Title and subtitle of the closing banner: only open issues are counted, and `setup` is suggested only when
    at least one of them is something setup fixes."""
    open_, history = _open(issues), len(issues) - len(_open(issues))
    note = f"{history} historical row(s) listed above, nothing to do" if history else ""
    if not open_:
        return "✓ Doctor: all checks passed", note
    parts = []
    if any(i.fixed_by == SETUP for i in open_):
        parts.append("Re-run `ai-resources setup` to fix the ones marked setup")
    cmds = sorted({i.command for i in open_ if i.fixed_by == OPERATOR and i.command})
    if cmds:
        parts.append("Operator: " + "; ".join(f"`{c}`" for c in cmds))
    if note:
        parts.append(note)
    return f"⚠ Doctor: {len(open_)} issue(s) open", "\n".join(parts)


def _check_antigravity_quota(s: state.SetupState) -> int:
    """Doctor sub-check 4d: Antigravity's two weekly quota pools.

    Exhaustion is otherwise indistinguishable from a hang: agy retries a 429
    RESOURCE_EXHAUSTED five times with backoff (~93 s), then OpenClaw's stall detector
    kills the turn ("This turn was interrupted because it stopped making progress") and
    respawns — no quota error ever reaches the user. agy's own background refresh is
    broken ("Singleflight refresh failed: You are not logged into Antigravity"), so this
    on-demand `/usage` read is the only reliable signal.

    Only called when `s.openclaw.antigravity_applied`. Returns the number of new issues
    (0, 1, or one per pool at/near its limit) — never raises.
    """
    from .setup.cockpits import _agy_quota

    issues = 0
    agy_bin = detection._which_extra("agy")
    if not agy_bin:
        ui.detail("agy not found — skipping quota check.")
        return issues

    pools, reason = _agy_quota.read_usage(agy_bin=agy_bin, timeout=30)
    if reason:
        ui.warn(f"Could not read Antigravity quota: {reason}")
        ui.detail("agy's background quota refresh is known broken (Singleflight refresh "
                  "failed) — this on-demand read is the only reliable source.")
        return issues

    applied_model = s.openclaw.model
    applied_pool = _agy_quota.pool_for_model(applied_model) if applied_model else ""
    for severity, message in _agy_quota.report(pools, applied_model=applied_model):
        if severity == "error":
            ui.error(message)
            _flag("agy-quota-exhausted", message, OPERATOR, _agy_quota.remedy(pools, applied_pool))
            ui.detail(_agy_quota.remedy(pools, applied_pool))
            if applied_pool == _agy_quota.POOL_GEMINI:
                ui.detail("Voice notes on agy share this same Gemini pool — consider "
                          "switching voice notes off agy too.")
            issues += 1
        elif severity == "warn":
            ui.warn(message)
            _flag("agy-quota-near-limit", message, OPERATOR, "wait for the quota reset or move the role to another pool")
            issues += 1
        else:
            ui.detail(message)
    return issues


def _check_antigravity_lane(s: state.SetupState) -> int:
    """Doctor sub-check 4f: agy in one line. Read-only; it runs nothing. Reports whether agy is installed and signed in
    (the only condition under which an agy cell of the matrix runs) and reads its quota (`_check_antigravity_quota`). An
    inactive agy is a recorded cause (`agy-missing`), not an error: the matrix falls back for every role it holds."""
    if detection.agy_active():
        ui.info("Antigravity: installed and signed in; agy cells of the matrix run.")
        return 0 if s.openclaw.antigravity_applied else _check_antigravity_quota(s)    # 4d already ran when applied
    ui.info("Antigravity: not installed or not signed in; agy cells are skipped with cause agy-missing "
            "(each such role runs on its fallback).")
    return 0


# MCP children under the gateway unit (ADR-0007 OQ-5 follow-up FU-1). Read-only: nothing is ever killed. The count
# and the threshold live in `openclaw_reconcile`, which is also what clears an overflow (a drained restart).
from . import openclaw_reconcile as _reconcile  # noqa: E402

MCP_CHILD_WARN = _reconcile.MCP_CHILD_WARN
count_mcp_children = _reconcile.count_mcp_children


def _check_mcp_children(*, runner: "Callable[..., tuple[int, str]] | None" = None,
                        cgroup_root: Path | None = None, proc_root: Path | None = None,
                        threshold: int = MCP_CHILD_WARN) -> int:
    """Doctor sub-check 4e. Returns the number of new issues (0 or 1); never raises, never kills a process.

    The kit denies `bundle-mcp` on the kit-role agents (`tools.deny`) so they stop loading stdio MCP servers; children
    that already exist are cleared by the drained restart `ai-resources openclaw reconcile` (setup runs it too)."""
    from . import openclaw_host
    run = runner or openclaw_host.default_runner
    counts = _reconcile.gateway_mcp_children(run, cgroup_root=cgroup_root, proc_root=proc_root)
    if counts is None:
        ui.detail("MCP children: gateway unit not running, systemd unavailable or unreadable; skipped.")
        return 0
    total = sum(counts.values())
    if total <= threshold:
        ui.detail(f"MCP children under the gateway unit: {total} (warn above {threshold}).")
        return 0
    per = ", ".join(f"{k} {v}" for k, v in counts.items())
    ui.warn(f"{total} MCP server child processes under {openclaw_host.GATEWAY_UNIT} ({per}); "
            f"warn threshold {threshold}.")
    ui.detail(f"Remedy (when no turn is running): `{_reconcile.RECONCILE_COMMAND}` -- a drained restart that "
              "refuses while agent turns are in flight; doctor itself does not kill or restart anything.")
    return _flag("mcp-children", f"{total} MCP server child processes under the gateway unit", SETUP,
                 _reconcile.RECONCILE_COMMAND)


def _check_credit_ledger(args: argparse.Namespace) -> int:
    """Doctor sub-check 4g0: run the collector (measured spend per prepaid pool from the machine's own usage records) and
    rewrite `~/.config/ai-resources/credits.json`. Neither provider exposes a balance API, so `remaining` is declared
    minus measured and says so. A failure is a detail line: money bookkeeping never fails the doctor."""
    if getattr(args, "no_collect", False):
        return 0
    try:
        from . import credits
        credits.collect()
    except Exception as e:  # noqa: BLE001 - a collector must never break the doctor
        ui.detail(f"Credit ledger not refreshed ({e.__class__.__name__}).")
    return 0


def _check_keys(s: state.SetupState, args: argparse.Namespace) -> int:
    """Doctor sub-check 4k: probe the Gemini and Anthropic keys held in `gateway.systemd.env` AND in the OpenClaw auth
    profile with one free `models.list` call each. A rejected env key beside a working profile key is rewritten from the
    profile (backup first, 0600, the value never printed) and the gateway restart that applies it goes through the
    existing drained reconcile. Reports `gemini key: env ok / profile ok`, `... / mismatch fixed`. Returns the number of
    keys left rejected."""
    from . import key_check
    try:
        results = key_check.check(fix=not getattr(args, "no_fix_keys", False))
    except Exception as e:  # noqa: BLE001 - a probe must never break the doctor
        ui.detail(f"Key check skipped ({e.__class__.__name__}).")
        return 0
    issues = 0
    for r in results:
        if r.env == key_check.ERROR or r.profile == key_check.ERROR:
            ui.detail(r.line() + " (no answer from the provider; nothing changed)")
        elif r.problem:
            ui.warn(r.line())
            _flag("key-rejected", r.line(), OPERATOR,
                  "replace the rejected key in the gateway env file or the OpenClaw auth profile")
            issues += 1
        elif r.fixed:
            ui.ok(r.line())
            ui.detail(f"Backup of the env file: {r.backup}")
        else:
            ui.ok(r.line()) if (r.env, r.profile) == (key_check.OK, key_check.OK) else ui.detail(r.line())
    if key_check.restart_owed(results) and _openclaw_present(s):
        deferred = key_check.restart_gateway(out=ui.info)
        if deferred:
            ui.warn(deferred)
    return issues


def _check_billing() -> int:
    """Doctor sub-check 4g (ADR-0014 D2): how Claude Code is billed, the quota and the manual credit file.

    Read-only. These are warnings about money and quota, not defects of the install, so they do not count as issues
    (the exit code stays about the configuration)."""
    try:
        snap = billing.snapshot()
    except Exception as e:  # noqa: BLE001 - a reader must never break the doctor
        ui.detail(f"Billing awareness skipped ({e.__class__.__name__}).")
        return 0
    for line in billing.summary_lines(snap):
        ui.detail(line)
    found = billing.alerts_for(snap)
    for a in found:
        (ui.warn if a.severity == "warn" else ui.info)(a.message)
    if not found:
        ui.ok("Billing and quota: nothing to flag")
    return 0


def _check_pool_usage() -> int:
    """Doctor sub-check 4g2 (ADR-0018): usage of the four pools the routing matrix balances. Read-only; a pool with no
    reading is `unknown`, never zero. A pool at or above 80% is a warning about spend, not a defect (exit code intact)."""
    try:
        from . import pool_usage
        snap = pool_usage.snapshot()
        for line in pool_usage.lines(snap):
            (ui.warn if "at or above 80%" in line else ui.detail)(line)
    except Exception as e:  # noqa: BLE001 - a reader must never break the doctor
        ui.detail(f"Pool usage skipped ({e.__class__.__name__}).")
    return 0


def _check_matrix_actual() -> int:
    """Doctor sub-check 4g3 (ADR-0019 D7): the dispatch ledger of the last 7 days against the routing matrix. A dispatch
    that ran outside its role's primary with no recorded fallback cause is an issue; a spill with a cause is shown."""
    from . import dispatch_ledger
    try:
        sm = dispatch_ledger.summarize(dispatch_ledger.load(since=time.time() - WEEK_S))
    except Exception as e:  # noqa: BLE001 - a reader must never break the doctor
        ui.detail(f"Matrix actual-vs-plan skipped ({e.__class__.__name__}).")
        return 0
    for d in sm["deviations"]:
        ui.warn(f"{d['role']} ran {d['cell']} (matrix primary {d['primary']}) with no recorded fallback cause "
                f"[{d['ts']}, run {d['run_id'] or '-'}]")
    if sm["deviations"]:
        # Past records: nothing sets them right, they leave the 7-day window by themselves.
        _flag("matrix-deviation", f"{len(sm['deviations'])} dispatch(es) of the last 7 days ran off the matrix primary "
              "with no recorded fallback cause", HISTORY)
    if not sm["deviations"]:
        ui.ok("Matrix actual: every dispatch of the last 7 days ran on its primary or has a recorded fallback cause"
              if sm["dispatches"] else "Matrix actual: no dispatches in the last 7 days")
    return len(sm["deviations"])


def _check_gemini_tools() -> int:
    """Doctor sub-check 4g3b (ADR-0019 D8): the heartbeat gating verdict `ai-resources acceptance` left (a small json under
    ~/.cache/ai-resources). Read-only; the kit never edits the OpenClaw config for it."""
    from . import acceptance
    line = acceptance.gemini_verdict_line()
    (ui.ok if "PASSED" in line else ui.info)(line or "Gemini tool-calling is unproven (`ai-resources acceptance --group gemini-tools`); "
                                              "the matrix heartbeat is Gemini with a Haiku fallback")
    return 0


def _check_weekly_summary() -> int:
    """Doctor sub-check 4g4 (ADR-0019 D7): the week in a few lines. Pool usage and the credit ledger come from their own
    modules, the dispatch aggregation from the dispatch ledger, the run line from the run ledger (sizing scorecard)."""
    from . import credits, dispatch_ledger, pool_usage, run_ledger
    ui.info("Weekly summary (last 7 days)")
    for label, make in (("pools", lambda: [ln for ln in pool_usage.lines(pool_usage.snapshot()) if not ln.startswith("route now")]),
                        ("credits", credits.lines),
                        ("dispatches", lambda: dispatch_ledger.summary_lines(
                            dispatch_ledger.summarize(dispatch_ledger.load(since=time.time() - WEEK_S)))),
                        ("runs", lambda: [run_ledger.summary_line(run_ledger.load())])):
        try:
            for line in make():
                ui.detail(line)
        except Exception as e:  # noqa: BLE001 - a summary must never break the doctor
            ui.detail(f"{label}: skipped ({e.__class__.__name__}).")
    return 0


def _check_poll_share() -> int:
    """Doctor sub-check 4h: the poll-turn cost share of the newest cost snapshot (ADR-0014 D7). Reads a file only."""
    from . import audit, audit_snapshot
    try:
        found = audit_snapshot.latest_snapshot()
    except Exception:  # noqa: BLE001 - best-effort
        found = None
    if not found:
        ui.detail("No cost snapshot yet: run `ai-resources audit snapshot <label>` to measure the poll-turn share.")
        return 0
    label, share = found
    if audit.poll_alert(share):
        ui.warn(f"Poll turns are {share:.0%} of the cost in snapshot {label!r} (alert above "
                f"{audit.poll_share_threshold():.0%}). Wait with one blocking call or run_in_background, not sleep loops.")
    else:
        ui.ok(f"Poll-turn share {share:.1%} in snapshot {label!r}")
    return 0


def _check_vendor_pins() -> int:
    """Doctor sub-check 4j (ADR-0015 S9): vendored skill pins against upstream. Warn-only; skipped offline."""
    from . import vendor_pin
    try:
        found = vendor_pin.check()
    except Exception as e:  # noqa: BLE001 - a reader must never break the doctor
        ui.detail(f"Vendor pin check skipped ({e.__class__.__name__}).")
        return 0
    for st in found:
        if st.status == "behind":
            ui.warn(f"Vendored skills {st.id}: pinned {st.pinned[:12]}, {st.detail}. "
                    "Review the upstream change, then `ai-resources generate --force` to re-import.")
        elif st.status == "current":
            ui.ok(f"Vendored skills {st.id}: pin is current ({st.pinned[:12]})")
        else:
            ui.detail(f"Vendored skills {st.id}: pin not compared ({st.detail}).")
    return 0


ANNOUNCE_INFO = ("OpenClaw completion announcements: no config key exists to suppress them (schema checked 2026-10-22; "
                 "only agents.defaults.subagents.announceTimeoutMs, a timeout). The kit writes nothing for this.")


def _openclaw_present(s: state.SetupState) -> bool:
    cs = s.cockpits.get("openclaw")
    return bool((cs and cs.installed) or s.openclaw.applied or s.openclaw.host)


def _check_openclaw_cost(s: state.SetupState, args: argparse.Namespace) -> int:
    """Doctor sub-check 4i (ADR-0014 D5): the OpenClaw cost notes and the opt-in utility-model probe. Informational:
    never counted as an issue. The probe is one tiny model turn (printed first) and runs only with the flag."""
    if not _openclaw_present(s):
        return 0
    ui.info(ANNOUNCE_INFO)
    if not getattr(args, "probe_utility_model", False):
        return 0
    from . import openclaw_utility_probe as up, openclaw_host
    from .setup.cockpits import openclaw as cockpit
    ref = (((cockpit.read_config(cockpit.config_path()).get("agents") or {}).get("defaults") or {}).get("utilityModel"))
    if not isinstance(ref, str) or not ref:
        ui.detail("utilityModel is not set; nothing to probe (setup fills the pinned Haiku).")
        return 0
    ui.info(up.NOTICE)
    result = up.probe(ref, lambda argv: openclaw_host.default_runner(["openclaw", *argv], timeout=120))
    if result.status == "ok":
        ui.ok(f"utility model {ref}: the probe got an answer")
    elif result.status == "invalid":
        ui.warn(f"utility model {ref}: {result.detail}")
    else:
        ui.detail(f"utility model {ref}: {result.detail}")
    return 0


def _check_scratch(args: argparse.Namespace, cache_root: Path | None = None) -> int:
    """Scratch hygiene (ADR-0020): legacy top-level ~/.cache names, the runs root, security-scan-results size.

    `--fix` only LISTS what it would delete; `--apply` with it deletes: a legacy ~/.cache entry only when nothing inside it
    changed for 24 h and no process has its cwd or an open file there, a legacy ~/.openclaw entry only after 7 days. The
    allowlist of real tool caches (go-build, uv, pnpm, claude, Homebrew ...) and credential-shaped names are never touched."""
    import os
    apply = bool(getattr(args, "fix", False)) and bool(getattr(args, "apply", False))
    fix = apply
    issues = 0
    ui.detail("Sizes: " + "; ".join(f"{k} {scratch._fmt(v)}" for k, v in residue.sizes().items()))
    for r in residue.openclaw_legacy_report(apply=apply):
        if r["removed"]:
            ui.detail(f"removed {r['path']} ({scratch._fmt(r['bytes'])})")
    oc_rows = [r for r in residue.openclaw_legacy_report(apply=False)]
    if oc_rows:
        total = sum(r["bytes"] for r in oc_rows)
        ui.warn(f"{len(oc_rows)} entr{'y' if len(oc_rows) == 1 else 'ies'} in legacy ~/.openclaw scratch dirs ({scratch._fmt(total)}); "
                "the kit writes scratch to ~/.cache/ai-resources/runs only")
        for r in oc_rows[:10]:
            why = ("protected" if r["protected"] else "in use" if r["in_use"]
                   else "would remove" if r["age_s"] >= residue.LEGACY_MIN_AGE_S else f"{r['age_s'] / 86400:.0f}d old, kept until 7d")
            ui.detail(f"{Path(r['path']).name}  {scratch._fmt(r['bytes'])}  [{why}]")
        ui.detail("`ai-resources doctor --fix` lists what it would delete; add `--apply` to delete.")
        _flag("scratch-legacy-openclaw", f"{len(oc_rows)} entries in legacy ~/.openclaw scratch dirs", OPERATOR,
              "ai-resources doctor --fix --apply")
        issues += 1
    cache_root = cache_root or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    ui.detail("Scratch root and legacy ~/.cache scratch")
    rows = scratch.legacy_report(cache_root, fix=fix)
    if not rows:
        ui.ok("no legacy scratch entries directly under ~/.cache")
    else:
        total = sum(r["bytes"] for r in rows)
        ui.warn(f"{len(rows)} legacy scratch entr{'y' if len(rows) == 1 else 'ies'} under {cache_root} "
                f"({scratch._fmt(total)}); the scratch root is {scratch.runs_dir()}")
        for r in rows[:15]:
            state_ = ("removed" if r["removed"] else "in use" if r["in_use"]
                      else f"{r['age_s'] / 3600:.0f}h old" if r["age_s"] < 86400 else "removable")
            ui.detail(f"{Path(r['path']).name}  {scratch._fmt(r['bytes'])}  [{state_}]")
        if len(rows) > 15:
            ui.detail(f"... and {len(rows) - 15} more")
        left = [r for r in rows if not r["removed"]]
        if left:
            ui.detail("`ai-resources doctor --fix` lists the removable ones (older than 24 h, not in use); add `--apply` to delete.")
            _flag("scratch-legacy-cache", f"{len(left)} legacy scratch entries under {cache_root}", OPERATOR,
                  "ai-resources doctor --fix --apply")
            issues += 1
    runs = scratch.entries()
    run_bytes = sum(e["size"] for e in runs)
    if run_bytes > scratch.DEFAULT_CAP_BYTES:
        ui.warn(f"scratch runs weigh {scratch._fmt(run_bytes)} (cap {scratch._fmt(scratch.DEFAULT_CAP_BYTES)})")
        ui.detail("Run `ai-resources scratch prune`.")
        _flag("scratch-runs-over-cap", f"scratch runs weigh {scratch._fmt(run_bytes)}", OPERATOR,
              "ai-resources scratch prune")
        issues += 1
    scans = scratch.scan_results_bytes()
    if scans > scratch.SCAN_WARN_BYTES:
        ui.warn(f"security-scan-results weighs {scratch._fmt(scans)} (warn above {scratch._fmt(scratch.SCAN_WARN_BYTES)})")
        ui.detail("Run `ai-resources scratch prune-scans` (keeps the newest 7 runs); the writer should call it at the end of every run.")
        _flag("scratch-scan-results-large", f"security-scan-results weighs {scratch._fmt(scans)}", OPERATOR,
              "ai-resources scratch prune-scans")
        issues += 1
    return issues


def cmd_doctor(args: argparse.Namespace) -> int:
    ui.require_deps()
    ui.banner("ai-resources doctor", subtitle="Health check across all components")

    s = state.load()
    _ISSUES.clear()

    # 1. State file present
    ui.section(1, 6, "Configuration state")
    if state.state_path().is_file():
        ui.ok(f"setup-state.yaml present at {state.state_path()}")
        ui.detail(f"Mode: {s.mode}" + (" (CLI subscriptions)" if getattr(s, "cli_team", False) else ""))
    else:
        ui.error("setup-state.yaml missing — run `ai-resources setup`")
        _flag("state-missing", "setup-state.yaml missing", SETUP)

    # 2. .env file + permissions
    ui.section(2, 6, "Credentials")
    env_path = state.env_path()
    if env_path.is_file():
        mode = oct(env_path.stat().st_mode)[-3:]
        if mode == "600":
            ui.ok(f".env present at {env_path} (chmod {mode})")
        else:
            ui.warn(f".env permissions are {mode}, expected 600")
            _flag("env-permissions", f".env permissions are {mode}, expected 600", OPERATOR, f"chmod 600 {env_path}")
        env = credentials.load_env()
        for pid, ps in s.providers.items():
            if not ps.enabled:
                continue
            prov = providers.get(pid)
            if pid == "ollama":
                continue
            if pid == "vertex":
                if env.get("GOOGLE_CLOUD_PROJECT"):
                    ui.ok(f"vertex: GOOGLE_CLOUD_PROJECT set")
                else:
                    ui.warn(f"vertex: GOOGLE_CLOUD_PROJECT missing")
                    _flag("vertex-project-missing", "GOOGLE_CLOUD_PROJECT missing", SETUP)
                continue
            if env.get(prov.primary_env_var):
                ui.ok(f"{prov.name}: {prov.primary_env_var} set")
            else:
                ui.warn(f"{prov.name}: {prov.primary_env_var} missing")
                _flag(f"provider-key-missing-{pid}", f"{prov.primary_env_var} missing", SETUP)
        # Under the openrouter backend the gateway credential IS the provider
        # credential, so the provider loop above has already reported it.
        # Repeating the check here printed the line twice and, when the key was
        # missing, counted one problem as two issues.
        # Only a LiteLLM gateway has a master key; single-model mode runs none.
        if s.mode == "multi-model" and getattr(s, "backend", "litellm") != "openrouter":
            if env.get("LITELLM_MASTER_KEY"):
                ui.ok("LITELLM_MASTER_KEY set")
            else:
                ui.warn("LITELLM_MASTER_KEY missing — gateway auth will fail")
                _flag("litellm-master-key", "LITELLM_MASTER_KEY missing", SETUP)
    else:
        ui.warn(f".env not found at {env_path}")
        if s.mode == "multi-model":
            _flag("env-missing", f".env not found at {env_path}", SETUP)

    # 3. Gateway
    ui.section(3, 6, "Gateway")
    if s.mode != "multi-model":
        ui.detail("Skipped (single-model mode)")
    elif getattr(s, "backend", "litellm") == "openrouter":
        # Hosted: nothing is installed or supervised here, so the only local
        # precondition is the credential. Reachability is proven by the
        # round-trip in section 6 rather than by a health endpoint.
        if credentials.get_key("OPENROUTER_API_KEY"):
            ui.ok("OpenRouter (hosted) — credential present")
            ui.detail("Nothing to install or supervise; `ai-resources daemon` does not apply.")
            ui.detail("Spend limits live on the key: https://openrouter.ai/settings/keys")
        else:
            # Section 2 already counted the missing credential. Repeating the
            # count here would report one root cause as two problems, so state
            # the consequence and leave the tally alone.
            ui.warn("No credential — every request will fail (see Credentials above)")
    elif s.litellm.deployment == "local":
        mode = s.litellm.local.runtime
        if mode in ("pipx", "pip-venv"):
            det = detection.detect_litellm_binary()
            if det.installed:
                ui.ok(f"litellm v{det.version or '?'}  {det.binary_path}")
            else:
                ui.error("litellm binary not found in PATH")
                _flag("litellm-binary-missing", "litellm binary not found in PATH", SETUP)
            lc = litellm.lifecycle_path()
            if lc.exists():
                ui.ok(f"Service file: {lc}")
            else:
                ui.warn(f"Service file missing: {lc}")
                _flag("litellm-service-file-missing", f"Service file missing: {lc}", SETUP)
        elif litellm.is_container_mode(mode):
            runtime, det = detection.detect_container_runtime()
            if det.installed:
                ui.ok(f"{runtime} {det.version}")
            else:
                ui.error("Container runtime unhealthy")
                _flag("container-runtime", "Container runtime unhealthy", OPERATOR, "start the container runtime")

        status = litellm.service_status()
        url = f"http://{s.litellm.local.bind_address}:{s.litellm.local.port}"
        if status == "running":
            if litellm.health_check(url + "/health/liveliness"):
                ui.ok(f"Gateway healthy at {url}")
            else:
                ui.warn("Service running but health check failed")
                _flag("gateway-unhealthy", "Service running but health check failed", OPERATOR, "ai-resources daemon restart")
        elif status == "stopped":
            ui.warn("Service stopped — run `ai-resources daemon start`")
            _flag("gateway-stopped", "Service stopped", OPERATOR, "ai-resources daemon start")
        else:
            ui.error(f"Service status: {status}")
            _flag("gateway-status", f"Service status: {status}", OPERATOR, "ai-resources daemon status")
    elif s.litellm.deployment == "remote":
        master = credentials.get_key("LITELLM_MASTER_KEY")
        ok, msg = litellm.validate_remote(s.litellm.remote.url, master)
        if ok:
            ui.ok(f"Remote {s.litellm.remote.url} reachable")
        else:
            ui.error(f"Remote unreachable: {msg}")
            _flag("gateway-remote-unreachable", f"Remote unreachable: {msg}", OPERATOR, "check the remote LiteLLM URL and key")

    # 4. Cockpits configured
    ui.section(4, 6, "Cockpits")
    configured = [(cid, cs) for cid, cs in s.cockpits.items() if cs.configured]
    if not configured:
        ui.warn("No cockpits configured — run `ai-resources setup`")
        _flag("no-cockpits", "No cockpits configured", SETUP)
    for cid, cs in configured:
        ui.ok(f"{cid}  v{cs.version or '?'}  → {cs.config_root}")
    if configured:
        # The same read-only verification as `ai-resources verify` and the last setup step. Only an
        # error-level finding is an issue: a warning is printed and does not fail the check.
        findings = verify.run_all(s, [cid for cid, _ in configured])
        verify.print_findings(findings, show_ok=False)
        for f in findings:
            if f.level == "error":
                via_setup = "ai-resources setup" in f.remedy
                _flag(f"verify-{f.cockpit}", f.message, SETUP if via_setup else OPERATOR,
                      "" if via_setup else (f.remedy or "see the finding above"))

    # 4b. Claude Code OAuth session — informational, not an error
    if s.mode == "multi-model":
        claude_cs = s.cockpits.get("claude")
        if claude_cs and claude_cs.configured:
            from .setup.cockpits import claude as _claude_cockpit
            if _claude_cockpit.is_logged_in_via_oauth():
                ui.info("Claude Code is signed in via OAuth (claude.ai subscription).")
                if getattr(s, "backend", "litellm") == "openrouter":
                    # OpenRouter authenticates with a bearer token; a leftover OAuth
                    # login makes Claude Code send both credentials and the request
                    # is rejected. Unlike the LiteLLM path, this does not work as-is.
                    ui.warn("OpenRouter rejects requests that also carry an OAuth login.")
                    ui.detail("Run: claude /logout → new terminal → ai-resources doctor")
                    _flag("openrouter-oauth-conflict", "OpenRouter rejects requests that also carry an OAuth login",
                          OPERATOR, "claude /logout")
                else:
                    ui.detail("Gateway uses allow_requests_on_db_unavailable=true — works as-is.")
                    ui.detail("To switch to API key mode: claude /logout → new terminal → ai-resources doctor")

    # 4c. OpenClaw: the gateway must run the plugin the kit links. The wizard asks when to report
    # it; engine `keep` with a linked plugin is the host where the engine gate alone stayed silent.
    # "linked" asks the gateway, not the state file: `plugin_linked` records only what the kit did.
    from .setup.cockpits import openclaw as _openclaw_cockpit
    stale_check = s.openclaw.stale_plugin_check
    if (_openclaw_cockpit.plugin_loaded() if stale_check == "linked"
            else s.openclaw.antigravity_applied if stale_check == "engine"
            else False):
        from .setup.cockpits import _shared as _shared_cockpit
        from . import repo_root as _repo_root
        plugin_dir = str(Path(_shared_cockpit.stable_kit_root(_repo_root()))
                         / "openclaw-plugin" / "ai-resources")
        if (why := _openclaw_cockpit.stale_reason(plugin_dir)):
            # `brew upgrade` moves the kit; until the gateway restarts it holds the old
            # path and answers "Unknown CLI backend" to every chat message. When only the
            # time rule trips the paths are equal, so the message must not talk about a directory.
            ui.warn("OpenClaw is running the ai-resources plugin from an older kit directory." if why == "path"
                    else "OpenClaw is running older ai-resources plugin code than the kit on disk "
                         "(restart to load it).")
            ui.detail("Run: openclaw gateway restart")
            _flag("openclaw-plugin-stale", "OpenClaw runs older ai-resources plugin code", OPERATOR,
                  "openclaw gateway restart")
        elif not s.openclaw.antigravity_applied:
            # Only the antigravity engine needs the agy-cli backend; a host that never asked for
            # it has no defect to report here.
            pass
        elif not _openclaw_cockpit.backend_registered("agy-cli"):
            ui.warn("OpenClaw has not loaded the ai-resources plugin (no agy-cli backend).")
            ui.detail("Run: openclaw gateway restart")
            _flag("openclaw-plugin-not-loaded", "OpenClaw has not loaded the ai-resources plugin", OPERATOR,
                  "openclaw gateway restart")
        else:
            ui.ok("OpenClaw runs the kit plugin (agy-cli backend registered)")

    # 4d. Antigravity quota: about the engine, not the plugin, so the answer above does not gate it.
    if s.openclaw.antigravity_applied:
        _check_antigravity_quota(s)

    # 4e. MCP server children under the gateway unit (only on a host where setup wrote the kit-role agents).
    if s.openclaw.role_agents_applied or state.lanes_enabled(s):
        _check_mcp_children()

    # 4f. Antigravity: installed and signed in (the one condition for an agy cell), quota.
    _check_antigravity_lane(s)

    # 4g. Billing mode, quota and the manual credit file; 4h. the poll-turn share of the newest snapshot.
    _check_credit_ledger(args)
    _check_billing()
    _check_pool_usage()
    _check_matrix_actual()
    _check_gemini_tools()
    _check_weekly_summary()
    _check_keys(s, args)
    _check_poll_share()
    _check_vendor_pins()
    _check_openclaw_cost(s, args)
    _check_scratch(args)

    # 5. Executors mapping
    ui.section(5, 6, "Role → model mapping")
    if state.executors_path().is_file():
        ui.ok(f"executors.yaml at {state.executors_path()}")
    else:
        if s.mode == "multi-model":
            ui.error("executors.yaml missing")
            _flag("executors-missing", "executors.yaml missing", SETUP)
        else:
            ui.detail("Skipped (single-model mode)")

    # 6. Smoke tests (optional)
    ui.section(6, 6, "Connectivity smoke tests")
    if not args.skip_smoke and s.mode == "multi-model":
        try:
            from .setup import profiles
            backend = getattr(s, "backend", "litellm")
            executors = profiles.to_executors(profiles.load_profile(profiles.saved_or_recommended(s.profile.name, backend), "multi-model"), backend)
            if s.profile.customized:
                executors = profiles.to_executors(
                    profiles.merge_customizations(
                        profiles.load_profile(profiles.saved_or_recommended(s.profile.name, backend), "multi-model"),
                        {k: v for k, v in s.profile.customizations.items()
                         if k in profiles.KNOWN_ROLES},
                    ),
                    backend,
                )
            if backend == "openrouter":
                gw = profiles.GATEWAYS["openrouter"]
                gateway, master = gw["url"], credentials.get_key(gw["api_key_env"])
            else:
                gateway = (s.litellm.remote.url if s.litellm.deployment == "remote"
                           else f"http://{s.litellm.local.bind_address}:{s.litellm.local.port}")
                master = credentials.get_key("LITELLM_MASTER_KEY")
            results = smoke.run_all(executors, gateway, master, backend)
            for label, ok, msg in results:
                if ok:
                    ui.ok(label)
                else:
                    ui.warn(f"{label}  ({msg[:80]})")
                    _flag("smoke-failed", f"{label}: {msg[:80]}", OPERATOR, "ai-resources daemon status")
        except Exception as e:
            ui.warn(f"Smoke tests skipped: {e}")
    else:
        ui.detail("Skipped (--skip-smoke or single-model mode)")

    ui.console().print()
    if _ISSUES:
        ui.info("Issues, by name")
        for i in _ISSUES:
            ui.detail(f"{i.name}: {i.message} — {i.label()}")
    title, subtitle = banner_text(_ISSUES)
    ui.banner(title, subtitle=subtitle)
    return 1 if _open(_ISSUES) else 0


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("doctor", help="Run full health check across all components")
    p.add_argument("--probe-utility-model", dest="probe_utility_model", action="store_true",
                   help="Also check that OpenClaw's utilityModel works with your key (one tiny model turn, billed)")
    p.add_argument("--no-fix-keys", dest="no_fix_keys", action="store_true",
                   help="Only report a rejected gateway-env key; do not rewrite it from the working profile key")
    p.add_argument("--no-collect", dest="no_collect", action="store_true",
                   help="Do not refresh the credit ledger (credits.json) from the local usage records")
    p.add_argument("--fix", action="store_true",
                   help="List the legacy scratch entries under ~/.cache (>24 h, not in use) and ~/.openclaw (>7 d) that would be deleted; dry run unless --apply")
    p.add_argument("--apply", action="store_true", help="With --fix: really delete them (real tool caches and credential-shaped names are never touched)")
    p.add_argument("--skip-smoke", action="store_true",
                   help="Don't run live API round-trips (saves a few tokens)")
    p.set_defaults(func=cmd_doctor)
