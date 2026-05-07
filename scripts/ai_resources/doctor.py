"""ai-resources doctor — full health check of multi-model setup."""
from __future__ import annotations

import argparse
from pathlib import Path

from .setup import state, detection, credentials, litellm, providers, smoke, ui


def cmd_doctor(args: argparse.Namespace) -> int:
    ui.require_deps()
    ui.banner("ai-resources doctor", subtitle="Health check across all components")

    s = state.load()
    issues = 0

    # 1. State file present
    ui.section(1, 6, "Configuration state")
    if state.state_path().is_file():
        ui.ok(f"setup-state.yaml present at {state.state_path()}")
        ui.detail(f"Mode: {s.mode}")
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
        if env.get("LITELLM_MASTER_KEY"):
            ui.ok("LITELLM_MASTER_KEY set")
        else:
            ui.warn("LITELLM_MASTER_KEY missing — gateway auth will fail")
            issues += 1
    else:
        ui.warn(f".env not found at {env_path}")
        if s.mode == "multi-model":
            issues += 1

    # 3. LiteLLM gateway
    ui.section(3, 6, "LiteLLM gateway")
    if s.mode != "multi-model":
        ui.detail("Skipped (single-model mode)")
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
        elif mode == "docker":
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

    # 4b. Claude Code OAuth session — informational, not an error
    if s.mode == "multi-model":
        claude_cs = s.cockpits.get("claude")
        if claude_cs and claude_cs.configured:
            from .setup.cockpits import claude as _claude_cockpit
            if _claude_cockpit.is_logged_in_via_oauth():
                ui.info("Claude Code is signed in via OAuth (claude.ai subscription).")
                ui.detail("Gateway uses allow_requests_on_db_unavailable=true — works as-is.")
                ui.detail("To switch to API key mode: claude /logout → new terminal → ai-resources doctor")

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
            executors = profiles.to_executors(profiles.load_profile(s.profile.name))
            if s.profile.customized:
                executors = profiles.to_executors(
                    profiles.merge_customizations(
                        profiles.load_profile(s.profile.name),
                        {k: v for k, v in s.profile.customizations.items()
                         if k in profiles.KNOWN_ROLES},
                    )
                )
            gateway = (s.litellm.remote.url if s.litellm.deployment == "remote"
                       else f"http://{s.litellm.local.bind_address}:{s.litellm.local.port}")
            master = credentials.get_key("LITELLM_MASTER_KEY")
            results = smoke.run_all(executors, gateway, master)
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
