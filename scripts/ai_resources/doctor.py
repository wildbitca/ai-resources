"""ai-resources doctor — full health check of multi-model setup."""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any, Callable

from . import verify
from .setup import state, detection, credentials, litellm, providers, smoke, ui


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
            ui.detail(_agy_quota.remedy(pools, applied_pool))
            if applied_pool == _agy_quota.POOL_GEMINI:
                ui.detail("Voice notes on agy share this same Gemini pool — consider "
                          "switching voice notes off agy too.")
            issues += 1
        elif severity == "warn":
            ui.warn(message)
            issues += 1
        else:
            ui.detail(message)
    return issues


# MCP children under the gateway unit (ADR-0007 OQ-5 follow-up FU-1). Read-only: nothing is ever killed.
MCP_CHILD_WARN = 24
_MCP_SERVERS = ("supabase", "engram")
_MCP_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(" + "|".join(_MCP_SERVERS) + r")(?![A-Za-z0-9])")


def count_mcp_children(control_group: str, *, cgroup_root: Path | None = None,
                       proc_root: Path | None = None) -> dict[str, int]:
    """Processes in the unit's cgroup tree whose command line names a known MCP server, per server."""
    from . import openclaw_host
    cgroup_root = cgroup_root or openclaw_host.CGROUP_ROOT
    proc_root = proc_root or openclaw_host.PROC_ROOT
    unit_dir = cgroup_root / control_group.lstrip("/")
    counts: dict[str, int] = {}
    for pid in openclaw_host._unit_pids(unit_dir, None):
        try:
            raw = (proc_root / str(pid) / "cmdline").read_bytes()
        except OSError:
            continue
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace")
        if not cmd.strip().split(" ")[0] or "openclaw-gateway" in cmd.split(" ")[0]:
            continue
        m = _MCP_RE.search(cmd)
        if m:
            counts[m.group(1).lower()] = counts.get(m.group(1).lower(), 0) + 1
    return dict(sorted(counts.items()))


def _check_mcp_children(*, runner: "Callable[..., tuple[int, str]] | None" = None,
                        cgroup_root: Path | None = None, proc_root: Path | None = None,
                        threshold: int = MCP_CHILD_WARN) -> int:
    """Doctor sub-check 4e. Returns the number of new issues (0 or 1); never raises, never kills a process.

    `tools.deny: ["bundle-mcp"]` on the kit-lane agents is expected to stop new lane children from loading
    MCP servers, but its effect on live children is NOT proven (only the isolated merge gate can show it)."""
    from . import openclaw_host
    run = runner or openclaw_host.default_runner
    try:
        rc, out = run(["systemctl", "--user", "show", openclaw_host.GATEWAY_UNIT, "-p", "ControlGroup"],
                      timeout=10)
        cg = ""
        for line in (out or "").splitlines():
            k, _, v = line.partition("=")
            if rc == 0 and k.strip() == "ControlGroup":
                cg = v.strip()
        if not cg:
            ui.detail("MCP children: gateway unit not running or systemd unavailable; skipped.")
            return 0
        counts = count_mcp_children(cg, cgroup_root=cgroup_root, proc_root=proc_root)
    except Exception as e:  # noqa: BLE001 - a read-only probe never fails doctor
        ui.detail(f"MCP children: could not be counted ({type(e).__name__}); skipped.")
        return 0
    total = sum(counts.values())
    if total <= threshold:
        ui.detail(f"MCP children under the gateway unit: {total} (warn above {threshold}).")
        return 0
    per = ", ".join(f"{k} {v}" for k, v in counts.items())
    ui.warn(f"{total} MCP server child processes under {openclaw_host.GATEWAY_UNIT} ({per}); "
            f"warn threshold {threshold}.")
    ui.detail("Remedy (operator, when no turn is running): systemctl --user restart "
              f"{openclaw_host.GATEWAY_UNIT}  -- doctor does not kill or restart anything.")
    ui.detail("The kit scopes kit-lane-* agents with tools.deny bundle-mcp; its effect on already running "
              "children is not proven (isolated merge gate only).")
    return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    ui.require_deps()
    ui.banner("ai-resources doctor", subtitle="Health check across all components")

    s = state.load()
    issues = 0

    # 1. State file present
    ui.section(1, 6, "Configuration state")
    if state.state_path().is_file():
        ui.ok(f"setup-state.yaml present at {state.state_path()}")
        ui.detail(f"Mode: {s.mode}" + (" (CLI subscriptions)" if getattr(s, "cli_team", False) else ""))
    else:
        ui.error("setup-state.yaml missing — run `ai-resources setup`")
        issues += 1

    # 2. .env file + permissions
    ui.section(2, 6, "Credentials")
    env_path = state.env_path()
    if env_path.is_file():
        mode = oct(env_path.stat().st_mode)[-3:]
        if mode == "600":
            ui.ok(f".env present at {env_path} (chmod {mode})")
        else:
            ui.warn(f".env permissions are {mode}, expected 600")
            issues += 1
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
                    issues += 1
                continue
            if env.get(prov.primary_env_var):
                ui.ok(f"{prov.name}: {prov.primary_env_var} set")
            else:
                ui.warn(f"{prov.name}: {prov.primary_env_var} missing")
                issues += 1
        # Under the openrouter backend the gateway credential IS the provider
        # credential, so the provider loop above has already reported it.
        # Repeating the check here printed the line twice and, when the key was
        # missing, counted one problem as two issues.
        if getattr(s, "backend", "litellm") != "openrouter":
            if env.get("LITELLM_MASTER_KEY"):
                ui.ok("LITELLM_MASTER_KEY set")
            else:
                ui.warn("LITELLM_MASTER_KEY missing — gateway auth will fail")
                issues += 1
    else:
        ui.warn(f".env not found at {env_path}")
        if s.mode == "multi-model":
            issues += 1

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
                issues += 1
            lc = litellm.lifecycle_path()
            if lc.exists():
                ui.ok(f"Service file: {lc}")
            else:
                ui.warn(f"Service file missing: {lc}")
                issues += 1
        elif litellm.is_container_mode(mode):
            runtime, det = detection.detect_container_runtime()
            if det.installed:
                ui.ok(f"{runtime} {det.version}")
            else:
                ui.error("Container runtime unhealthy")
                issues += 1

        status = litellm.service_status()
        url = f"http://{s.litellm.local.bind_address}:{s.litellm.local.port}"
        if status == "running":
            if litellm.health_check(url + "/health/liveliness"):
                ui.ok(f"Gateway healthy at {url}")
            else:
                ui.warn("Service running but health check failed")
                issues += 1
        elif status == "stopped":
            ui.warn("Service stopped — run `ai-resources daemon start`")
            issues += 1
        else:
            ui.error(f"Service status: {status}")
            issues += 1
    elif s.litellm.deployment == "remote":
        master = credentials.get_key("LITELLM_MASTER_KEY")
        ok, msg = litellm.validate_remote(s.litellm.remote.url, master)
        if ok:
            ui.ok(f"Remote {s.litellm.remote.url} reachable")
        else:
            ui.error(f"Remote unreachable: {msg}")
            issues += 1

    # 4. Cockpits configured
    ui.section(4, 6, "Cockpits")
    configured = [(cid, cs) for cid, cs in s.cockpits.items() if cs.configured]
    if not configured:
        ui.warn("No cockpits configured — run `ai-resources setup`")
        issues += 1
    for cid, cs in configured:
        ui.ok(f"{cid}  v{cs.version or '?'}  → {cs.config_root}")
    if configured:
        # The same read-only verification as `ai-resources verify` and the last setup step. Only an
        # error-level finding is an issue: a warning is printed and does not fail the check.
        findings = verify.run_all(s, [cid for cid, _ in configured])
        verify.print_findings(findings, show_ok=False)
        issues += verify.counts(findings)["error"]

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
                    issues += 1
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
            issues += 1
        elif not s.openclaw.antigravity_applied:
            # Only the antigravity engine needs the agy-cli backend; a host that never asked for
            # it has no defect to report here.
            pass
        elif not _openclaw_cockpit.backend_registered("agy-cli"):
            ui.warn("OpenClaw has not loaded the ai-resources plugin (no agy-cli backend).")
            ui.detail("Run: openclaw gateway restart")
            issues += 1
        else:
            ui.ok("OpenClaw runs the kit plugin (agy-cli backend registered)")

    # 4d. Antigravity quota: about the engine, not the plugin, so the answer above does not gate it.
    if s.openclaw.antigravity_applied:
        issues += _check_antigravity_quota(s)

    # 4e. MCP server children under the gateway unit (only on a host that enabled subscription lanes).
    if state.lanes_enabled(s):
        issues += _check_mcp_children()

    # 5. Executors mapping
    ui.section(5, 6, "Role → model mapping")
    if state.executors_path().is_file():
        ui.ok(f"executors.yaml at {state.executors_path()}")
    else:
        if s.mode == "multi-model":
            ui.error("executors.yaml missing")
            issues += 1
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
                    issues += 1
        except Exception as e:
            ui.warn(f"Smoke tests skipped: {e}")
    else:
        ui.detail("Skipped (--skip-smoke or single-model mode)")

    ui.console().print()
    if issues == 0:
        ui.banner("✓ Doctor: all checks passed")
        return 0
    ui.banner(f"⚠ Doctor: {issues} issue(s) found", subtitle="Re-run `ai-resources setup` to fix")
    return 1


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("doctor", help="Run full health check across all components")
    p.add_argument("--skip-smoke", action="store_true",
                   help="Don't run live API round-trips (saves a few tokens)")
    p.set_defaults(func=cmd_doctor)
