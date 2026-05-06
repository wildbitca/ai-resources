"""Interactive setup wizard — 9-step flow for ai-resources kit configuration."""
from __future__ import annotations

import argparse
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    state,
    detection,
    credentials,
    litellm,
    profiles,
    providers,
    smoke,
    ui,
)
from .cockpits import ALL as ALL_COCKPITS
from .. import __version__, repo_root


TOTAL_STEPS = 9


def run(args: argparse.Namespace) -> int:
    """Main wizard entry point."""
    ui.require_deps()
    ui.banner(
        "AI Resources Kit — Setup Wizard",
        subtitle="Multi-model orchestration via LiteLLM gateway",
        version=f"v{__version__}",
    )

    s = state.load()
    is_first = state.is_first_run()
    if not is_first:
        last = s.last_run or "(unknown)"
        ui.info(f"Previous config detected at {state.state_path()}")
        ui.detail(f"Last run: {last}")
        if not ui.confirm("Use previous answers as defaults?", default=True):
            s = state.SetupState()
    else:
        ui.info("First-time setup detected.")

    # ------------------------------------------------------------------
    rc = _step1_mode(s)
    if rc != 0:
        return rc

    rc = _step2_cockpits(s)
    if rc != 0:
        return rc

    if s.mode == "multi-model":
        rc = _step3_litellm(s)
        if rc != 0:
            return rc

        rc = _step4_providers(s)
        if rc != 0:
            return rc

        rc = _step5_credentials(s)
        if rc != 0:
            return rc

        rc = _step6_profile(s)
        if rc != 0:
            return rc
    else:
        ui.section(3, TOTAL_STEPS, "LiteLLM gateway")
        ui.info("Skipped (single-model mode).")
        ui.section(4, TOTAL_STEPS, "Provider backends")
        ui.info("Skipped (single-model uses cockpit's native auth).")
        ui.section(5, TOTAL_STEPS, "Credentials")
        ui.info("Skipped.")
        ui.section(6, TOTAL_STEPS, "Per-role model assignment")
        ui.info("Skipped.")
        # Set default profile for single-mode (legacy behavior)
        s.profile = state.ProfileState(name="all-claude", customized=False)

    rc = _step7_cockpit_config(s)
    if rc != 0:
        return rc

    if s.mode == "multi-model":
        rc = _step8_lifecycle(s)
        if rc != 0:
            return rc
    else:
        ui.section(8, TOTAL_STEPS, "Lifecycle")
        ui.info("Skipped (single-model mode).")

    rc = _step9_apply(s)
    if rc != 0:
        return rc

    state.save(s)
    _print_completion(s)
    return 0


# --- Step 1 — Mode ---------------------------------------------------------------
def _step1_mode(s: state.SetupState) -> int:
    ui.section(1, TOTAL_STEPS, "Setup mode")
    choices = [
        ui.Choice("single-model — One provider, current behavior", value="single-model",
                  description=" "),
        ui.Choice("multi-model — Per-role routing via LiteLLM gateway", value="multi-model",
                  description="◀ recommended"),
    ]
    default = s.mode or "multi-model"
    s.mode = ui.select("Choose mode:", choices, default=default)
    if s.mode is None:
        ui.warn("Setup cancelled.")
        return 130
    return 0


# --- Step 2 — Cockpit detection --------------------------------------------------
def _step2_cockpits(s: state.SetupState) -> int:
    ui.section(2, TOTAL_STEPS, "Cockpit detection")
    ui.info("Scanning installed AI CLIs...")
    detected = detection.detect_all_cockpits()

    rows = []
    for cid, det in detected.items():
        status = "✔" if det.installed else "✘"
        info = f"v{det.version}  {det.binary_path}" if det.installed else ""
        rows.append((status, det.name, info))
    ui.detected_table(rows)

    # Persist detection results
    installed_count = 0
    for cid, det in detected.items():
        cs = s.cockpits.get(cid) or state.CockpitState()
        cs.installed = det.installed
        cs.version = det.version
        cs.binary_path = det.binary_path
        s.cockpits[cid] = cs
        if det.installed:
            installed_count += 1

    if installed_count == 0:
        ui.console().print()
        ui.warn("No cockpits detected. Install at least one (e.g. Claude Code) and re-run setup.")
        return 1

    ui.console().print()
    ui.info(
        f"Configuring {installed_count} detected cockpit(s). "
        f"To add another, install it via your usual method, then re-run `ai-resources setup`."
    )
    return 0


# --- Step 3 — LiteLLM gateway ----------------------------------------------------
def _step3_litellm(s: state.SetupState) -> int:
    ui.section(3, TOTAL_STEPS, "LiteLLM gateway")

    choices = [
        ui.Choice("Install local container (Docker/Podman/Colima required)", value="local",
                  description="◀ recommended"),
        ui.Choice("Use existing remote endpoint (URL + master key)", value="remote"),
        ui.Choice("Show install instructions only (manual)", value="instructions"),
        ui.Choice("Skip (advanced — direct API calls without gateway)", value="skipped"),
    ]
    default = s.litellm.deployment or "local"
    choice = ui.select("Where is your gateway?", choices, default=default)
    if choice is None:
        return 130
    s.litellm.deployment = choice

    if choice == "local":
        return _step3_local(s)
    if choice == "remote":
        return _step3_remote(s)
    if choice == "instructions":
        _step3_instructions(s)
        s.litellm.deployment = "skipped"  # downgrade to skipped
        return 0
    return 0  # skipped


def _step3_local(s: state.SetupState) -> int:
    ui.console().print()
    ui.info("Detecting container runtime...")
    runtime, det = detection.detect_container_runtime()
    if not runtime:
        ui.error("No container runtime found (docker/podman/colima).")
        ui.detail("Install Docker Desktop: https://docs.docker.com/get-docker/")
        ui.detail("Or Colima: brew install colima && colima start")
        return 1
    ui.ok(f"{runtime} {det.version}  {det.binary_path}")
    s.litellm.local.runtime = runtime

    if runtime == "docker" and not detection.detect_compose():
        ui.error("docker compose plugin not detected. Install or use podman.")
        return 1

    image = ui.text(
        "LiteLLM image tag",
        default=s.litellm.local.image or litellm.DEFAULT_IMAGE,
    )
    s.litellm.local.image = image or litellm.DEFAULT_IMAGE

    if ui.confirm(f"Pull {s.litellm.local.image}?", default=True):
        with ui.spinner("Pulling image"):
            if not litellm.pull_image(s.litellm.local.image):
                ui.error("Image pull failed")
                return 1
        ui.ok("Image ready")

    # Bind interface (default 127.0.0.1)
    bind_choices = [
        ui.Choice("127.0.0.1  (localhost only — recommended)", value="127.0.0.1"),
        ui.Choice("0.0.0.0  (DANGEROUS — exposes to LAN)", value="0.0.0.0"),
    ]
    s.litellm.local.bind_address = ui.select(
        "Bind interface:", bind_choices,
        default=s.litellm.local.bind_address or "127.0.0.1",
    )

    s.litellm.local.port = int(ui.text(
        "Container port",
        default=str(s.litellm.local.port or 4000),
    ) or 4000)

    return 0


def _step3_remote(s: state.SetupState) -> int:
    ui.console().print()
    url = ui.text(
        "Remote LiteLLM URL (e.g. https://litellm.example.com)",
        default=s.litellm.remote.url,
    )
    if not url.startswith("http"):
        ui.error("URL must start with http:// or https://")
        return 1
    s.litellm.remote.url = url.rstrip("/")

    master_key = ui.password("Master key (will be stored in .env)")
    if not master_key:
        ui.error("Master key required for remote endpoint.")
        return 1
    credentials.update_env({s.litellm.remote.master_key_env: master_key})

    with ui.spinner(f"Validating {url}"):
        ok, msg = litellm.validate_remote(url, master_key)
    if not ok:
        ui.error(f"Remote unreachable: {msg}")
        if not ui.confirm("Continue anyway?", default=False):
            return 1
    else:
        ui.ok("Remote endpoint reachable")
    return 0


def _step3_instructions(s: state.SetupState) -> None:
    ui.console().print()
    ui.info("LiteLLM install instructions:")
    ui.detail("Local container:")
    ui.detail(f"  docker run -d -p 127.0.0.1:4000:4000 \\")
    ui.detail(f"    -v ~/.config/ai-resources/litellm.yaml:/app/config.yaml:ro \\")
    ui.detail(f"    -v ~/.config/ai-resources/.env:/app/.env:ro \\")
    ui.detail(f"    --name {litellm.CONTAINER_NAME} \\")
    ui.detail(f"    {litellm.DEFAULT_IMAGE} \\")
    ui.detail(f"    --config /app/config.yaml --port 4000")
    ui.detail("")
    ui.detail("Or as a hosted service: see https://docs.litellm.ai/docs/proxy/quick_start")


# --- Step 4 — Providers ----------------------------------------------------------
def _step4_providers(s: state.SetupState) -> int:
    ui.section(4, TOTAL_STEPS, "Provider backends")

    enabled_now = {p for p, ps in s.providers.items() if ps.enabled}
    choices = [
        ui.Choice(f"{providers.get(pid).name}  — {providers.get(pid).description}",
                  value=pid)
        for pid in providers.all_ids()
    ]
    selected = ui.checkbox(
        "Select providers to enable:",
        choices,
        default=list(enabled_now) or ["anthropic", "google"],
    )
    if not selected:
        ui.error("Select at least one provider.")
        return 1

    # Update state
    new_providers: dict[str, state.ProviderState] = {}
    for pid in selected:
        prov = providers.get(pid)
        existing = s.providers.get(pid) or state.ProviderState()
        existing.enabled = True
        existing.env_var = prov.primary_env_var
        # Determine auth method
        if pid == "vertex":
            existing.auth_method = "vertex_adc"
        else:
            existing.auth_method = prov.auth_methods[0]
        new_providers[pid] = existing
    # Disable any not selected
    for pid in s.providers:
        if pid not in selected:
            s.providers[pid].enabled = False
            new_providers[pid] = s.providers[pid]
    s.providers = new_providers

    return 0


# --- Step 5 — Credentials --------------------------------------------------------
def _step5_credentials(s: state.SetupState) -> int:
    ui.section(5, TOTAL_STEPS, "Credentials")
    ui.info(f"Stored in {state.env_path()} (chmod 600)")
    ui.info("Press Enter to keep an existing value")

    updates: dict[str, str] = {}

    # Master key for LiteLLM (always)
    if not credentials.has_key("LITELLM_MASTER_KEY"):
        master = credentials.generate_master_key()
        updates["LITELLM_MASTER_KEY"] = master
        ui.detail(f"Generated LITELLM_MASTER_KEY ({credentials.mask(master)})")

    # Per-provider keys
    for pid, ps in s.providers.items():
        if not ps.enabled:
            continue
        prov = providers.get(pid)

        if pid == "vertex":
            # Vertex needs project + location, not an API key
            existing_proj = credentials.get_key("GOOGLE_CLOUD_PROJECT")
            project = ui.text("GOOGLE_CLOUD_PROJECT (GCP project ID)", default=existing_proj)
            if project:
                updates["GOOGLE_CLOUD_PROJECT"] = project
            existing_loc = credentials.get_key("GOOGLE_CLOUD_LOCATION") or "us-east5"
            location = ui.text("GOOGLE_CLOUD_LOCATION (region)", default=existing_loc)
            if location:
                updates["GOOGLE_CLOUD_LOCATION"] = location
            ui.info("Run `gcloud auth application-default login` if you haven't already.")
            continue

        if pid == "ollama":
            ui.detail("(Ollama: no credentials needed; ensure localhost:11434 is reachable)")
            continue

        env_var = prov.primary_env_var
        existing = credentials.get_key(env_var)
        if existing:
            ui.detail(f"{env_var}: {credentials.mask(existing)}  (saved)")
            if ui.confirm(f"Update {env_var}?", default=False):
                new_val = ui.password(f"{env_var}")
                if new_val:
                    updates[env_var] = new_val
        else:
            new_val = ui.password(f"{env_var}")
            if not new_val:
                ui.warn(f"{env_var} skipped — provider may fail validation")
            else:
                updates[env_var] = new_val

    if updates:
        credentials.update_env(updates)
        ui.ok(f"Credentials written to {state.env_path()}")

    return 0


# --- Step 6 — Profile + per-role customization ------------------------------------
def _step6_profile(s: state.SetupState) -> int:
    ui.section(6, TOTAL_STEPS, "Per-role model assignment")

    available = profiles.list_profiles()
    if not available:
        ui.error(f"No profiles found at {profiles.profiles_dir()}")
        return 1

    # Build choices with descriptions
    choices = []
    for name in available:
        try:
            p = profiles.load_profile(name)
            desc = p.get("description", "").split("\n")[0][:80]
        except (FileNotFoundError, RuntimeError):
            desc = ""
        label = f"{name}"
        if desc:
            label += f"  — {desc}"
        choices.append(ui.Choice(label, value=name))
    choices.append(ui.Choice("custom — Configure each role individually", value="__custom__"))

    default = s.profile.name if s.profile.name in available else "unified-default"
    chosen = ui.select("Choose profile:", choices, default=default)
    if chosen is None:
        return 130

    if chosen == "__custom__":
        # Start from unified-default, then customize each role
        base = profiles.load_profile("unified-default")
        chosen = "unified-default"
    else:
        base = profiles.load_profile(chosen)

    s.profile.name = chosen

    # Show proposed mapping
    executors = profiles.to_executors(base)
    rows = profiles.role_table(executors)
    ui.console().print()
    ui.role_table(rows, title=f"Profile: {chosen}")

    if not ui.confirm("Customize any role?", default=False):
        s.profile.customized = False
        s.profile.customizations = {}
        # Stash full executors in state.profile.customizations as overrides
        return 0

    s.profile.customized = True
    customizations: dict[str, dict] = dict(s.profile.customizations or {})

    # Per-role customization loop
    enabled_providers = [p for p, ps in s.providers.items() if ps.enabled]
    while True:
        role = ui.select(
            "Pick a role to customize (or done):",
            [ui.Choice(r, value=r) for r in profiles.KNOWN_ROLES] + [ui.Choice("(done)", value="__done__")],
        )
        if role is None or role == "__done__":
            break
        provider = ui.select(
            f"Provider for {role}:",
            [ui.Choice(p, value=p) for p in enabled_providers],
            default=executors["by_role"].get(role, {}).get("provider"),
        )
        if provider is None:
            continue
        models = providers.models_for(provider)
        model = ui.select(
            f"Model for {role}:",
            [ui.Choice(m, value=m) for m in models] + [ui.Choice("(custom — type below)", value="__custom__")],
        )
        if model == "__custom__":
            model = ui.text(f"Model id for {role}")
        customizations[role] = {"provider": provider, "model": model}

    s.profile.customizations = customizations
    # Show updated table
    merged = profiles.merge_customizations(base, customizations)
    executors = profiles.to_executors(merged)
    ui.role_table(profiles.role_table(executors), title="Final mapping")
    return 0


# --- Step 7 — Cockpit configuration ----------------------------------------------
def _step7_cockpit_config(s: state.SetupState) -> int:
    ui.section(7, TOTAL_STEPS, "Cockpit configuration")

    detected_ids = [cid for cid, cs in s.cockpits.items() if cs.installed]
    if not detected_ids:
        ui.warn("No cockpits detected — nothing to configure.")
        return 0

    choices = []
    for cid in detected_ids:
        mod = ALL_COCKPITS.get(cid)
        if not mod:
            continue
        name = getattr(mod, "NAME", cid)
        choices.append(ui.Choice(name, value=cid))

    selected = ui.checkbox(
        "Configure detected cockpits:",
        choices,
        default=detected_ids,
    )
    s.profile.customizations.setdefault("__targets__", {})["selected"] = selected  # stash for apply step
    return 0


# --- Step 8 — Lifecycle ---------------------------------------------------------
def _step8_lifecycle(s: state.SetupState) -> int:
    ui.section(8, TOTAL_STEPS, "Lifecycle")

    if s.litellm.deployment != "local":
        ui.info("Skipped (gateway is not local).")
        return 0

    s.litellm.local.auto_start = ui.confirm(
        "Auto-start LiteLLM at login?",
        default=s.litellm.local.auto_start,
    )

    if s.litellm.local.auto_start:
        path = litellm.lifecycle_path()
        s.litellm.local.lifecycle_path = str(path)
        ui.detail(f"Lifecycle file: {path}")
    return 0


# --- Step 9 — Apply -------------------------------------------------------------
def _step9_apply(s: state.SetupState) -> int:
    ui.section(9, TOTAL_STEPS, "Apply")
    ui.console().print()
    ui.info("Generating configuration files...")

    # Build the final executors dict
    if s.mode == "multi-model":
        base = profiles.load_profile(s.profile.name)
        if s.profile.customized and s.profile.customizations:
            customizations = {k: v for k, v in s.profile.customizations.items()
                              if k in profiles.KNOWN_ROLES}
            base = profiles.merge_customizations(base, customizations)
        executors_doc = profiles.to_executors(base)

        # Write executors.yaml
        profiles.write_executors(executors_doc, state.executors_path())
        ui.ok(str(state.executors_path()))

        # Write LiteLLM configs (litellm.yaml + docker-compose.yaml)
        if s.litellm.deployment == "local":
            try:
                paths = litellm.write_configs(executors_doc, dict(s.providers))
                for label, p in paths.items():
                    ui.ok(str(p))
            except (RuntimeError, OSError) as e:
                ui.error(f"Failed to write LiteLLM config: {e}")
                return 1
    else:
        executors_doc = profiles.to_executors(profiles.load_profile("all-claude"))

    # Master key from .env
    master_key = credentials.get_key("LITELLM_MASTER_KEY")
    gateway_url = (
        s.litellm.remote.url if s.litellm.deployment == "remote"
        else f"http://{s.litellm.local.bind_address}:{s.litellm.local.port}"
    )

    # Apply per-cockpit configurators
    targets = s.profile.customizations.get("__targets__", {}).get("selected", [])
    if not targets:
        targets = [cid for cid, cs in s.cockpits.items() if cs.installed]
    for cid in targets:
        mod = ALL_COCKPITS.get(cid)
        if not mod:
            continue
        try:
            ctx = {
                "state": s,
                "executors": executors_doc,
                "master_key": master_key,
                "gateway_url": gateway_url,
                "detected_version": s.cockpits.get(cid, state.CockpitState()).version,
                "detected_path": s.cockpits.get(cid, state.CockpitState()).binary_path,
            }
            written = mod.configure(ctx)
            for w in written:
                ui.ok(str(w))
        except Exception as e:
            ui.error(f"{cid}: {e}")

    # Cleanup: remove __targets__ stash
    s.profile.customizations.pop("__targets__", None)

    # Start container if local
    if s.mode == "multi-model" and s.litellm.deployment == "local":
        ui.console().print()
        with ui.spinner("Starting LiteLLM container"):
            ok = litellm.start_container() and litellm.wait_for_health()
        if ok:
            ui.ok(f"Container running at {gateway_url}")
        else:
            ui.error("Container failed to start or didn't pass health check")
            ui.detail("Run `ai-resources daemon logs` to investigate")
            return 1

        # Lifecycle (auto-start)
        if s.litellm.local.auto_start:
            try:
                p = litellm.install_lifecycle()
                ui.ok(f"Auto-start: {p}")
            except (OSError, RuntimeError) as e:
                ui.warn(f"Lifecycle install skipped: {e}")

    # Smoke tests (multi-model only)
    if s.mode == "multi-model":
        ui.console().print()
        ui.info("Running smoke tests (~$0.001 of tokens)...")
        results = smoke.run_all(executors_doc, gateway_url, master_key)
        for label, ok, msg in results:
            if ok:
                ui.ok(label)
            else:
                ui.warn(f"{label}  ({msg[:80]})")

        s.smoke_tests = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "status": "pass" if all(ok for _, ok, _ in results) else "partial",
        }

    return 0


# --- Completion banner -----------------------------------------------------------
def _print_completion(s: state.SetupState) -> None:
    ui.console().print()
    ui.banner(
        "✓ Setup complete",
        subtitle="Try:\n"
                 "  claude                       open Claude Code as cockpit\n"
                 "  ai-resources doctor          full health check\n"
                 "  ai-resources executors       show role → model map\n"
                 "  ai-resources daemon logs     LiteLLM logs\n"
                 "  ai-resources audit           cost report",
    )
