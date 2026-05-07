"""Interactive setup wizard — 9-step flow for ai-resources kit configuration."""
from __future__ import annotations

import argparse
import platform
from dataclasses import asdict
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
    dry_run = getattr(args, "dry_run", False)
    ui.banner(
        "AI Resources Kit — Setup Wizard",
        subtitle="Multi-model orchestration via LiteLLM gateway",
        version=f"v{__version__}",
    )
    if dry_run:
        ui.console().print("[bold yellow]DRY RUN — no files will be written.[/bold yellow]\n")

    prev_s = state.load()  # frozen snapshot of the previous run (used for teardown)
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

    # If the user just switched multi-model → single-model, offer to clean up
    # everything we previously installed (tracking record drives the actions).
    if (prev_s.mode == "multi-model"
            and s.mode == "single-model"
            and not is_first):
        rc = _teardown_multi_model(prev_s, s)
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
        s.profile = state.ProfileState(name="cost-optimized", customized=False)

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

    rc = _step9_apply(s, dry_run=dry_run)
    if rc != 0:
        return rc

    if not dry_run:
        state.save(s)
        _print_completion(s)
    return 0


# --- Step 1 — Mode ---------------------------------------------------------------
def _try_install_pipx() -> bool:
    """Offer to install pipx automatically. Returns True if attempt succeeded."""
    import platform
    import subprocess

    options: list[tuple[str, list[str], str]] = []
    if platform.system() == "Darwin" and detection._which("brew"):
        options.append(("brew install pipx", ["brew", "install", "pipx"],
                        "Homebrew (macOS) — recommended"))
    if detection._which("apt"):
        options.append(("apt install pipx (sudo)", ["sudo", "apt", "install", "-y", "pipx"],
                        "Debian/Ubuntu apt"))
    if detection._which("dnf"):
        options.append(("dnf install pipx (sudo)", ["sudo", "dnf", "install", "-y", "pipx"],
                        "Fedora/RHEL dnf"))
    # Always available: pip --user
    options.append(("python3 -m pip install --user pipx",
                    ["python3", "-m", "pip", "install", "--user", "pipx"],
                    "Universal — pip user install"))
    options.append(("Skip — I'll install manually", [], ""))

    choices = [ui.Choice(f"{label}  {hint}", value=i) for i, (label, _, hint) in enumerate(options)]
    pick = ui.select("How should I install pipx?", choices, default=0)
    if pick is None:
        return False
    label, cmd, _ = options[pick]
    if not cmd:
        ui.detail("Install pipx manually then re-run `ai-resources setup`.")
        return False

    with ui.spinner(f"Running: {' '.join(cmd)}"):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            ui.error(f"Install failed: {e}")
            return False
    if r.returncode != 0:
        ui.error(f"Install failed: {(r.stderr or r.stdout)[:200]}")
        return False
    ui.ok("pipx installed")

    # Run pipx ensurepath so the binary lands in PATH for future shells
    pipx_path = detection._which("pipx") or str(Path.home() / ".local" / "bin" / "pipx")
    if Path(pipx_path).exists():
        subprocess.run([pipx_path, "ensurepath"], capture_output=True, text=True)
    return True


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


# --- Teardown — multi-model → single-model ---------------------------------------
def _teardown_multi_model(prev_s: state.SetupState, s: state.SetupState) -> int:
    """When switching multi-model → single-model, undo what we previously installed.

    Drives off `prev_s.tracking` (the audit trail from the prior run). Asks once
    for confirmation, listing every action it will take. On approval, executes
    each action and resets the relevant `s` fields so the wizard continues with
    a clean slate.
    """
    ui.console().print()
    if hasattr(ui.console(), "rule"):
        ui.console().rule("[bold yellow]Switching to single-model — cleanup[/]",
                          style="yellow", align="left")
    else:
        ui.info("Switching to single-model — cleanup")

    actions = litellm.plan_multi_model_teardown(prev_s)
    if not actions:
        ui.info("Nothing to clean up (no tracked artifacts from previous run).")
        return 0

    ui.info("The previous setup installed the following artifacts. "
            "Switching to single-model will undo them:")
    for _aid, desc in actions:
        ui.detail(f"  • {desc}")
    ui.console().print()

    if not ui.confirm("Proceed with cleanup?", default=True):
        ui.warn("Cleanup skipped — multi-model artifacts will remain on disk.")
        ui.detail("You can re-run setup later or remove them manually.")
        return 0

    results = litellm.execute_multi_model_teardown(prev_s)
    for aid, ok, msg in results:
        if ok:
            ui.ok(f"{aid}: {msg}")
        else:
            ui.warn(f"{aid}: {msg}")

    # Reset the parts of `s` that no longer apply in single-model
    s.litellm = state.LiteLLMState()
    s.providers = {}
    s.profile = state.ProfileState(name="cost-optimized", customized=False)
    s.tracking = state.InstallTracking()
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

    # Detect what's available so we can flag disabled options
    docker_runtime, docker_det = detection.detect_container_runtime()

    docker_label = (f"Local — Docker container  ({docker_runtime} {docker_det.version}"
                    f"  recommended, sidesteps Python compat)") if docker_det.installed \
                   else "Local — Docker container  (recommended — install Docker Desktop / Colima first)"

    choices = [
        ui.Choice(docker_label, value="local-docker",
                  disabled=None if docker_det.installed else "docker/podman/colima not detected"),
        ui.Choice("Local — without Docker  (pip + venv, or pipx)", value="local-nodocker"),
        ui.Choice("Use existing remote endpoint  (URL + master key)", value="remote"),
        ui.Choice("Skip  (advanced — direct API calls without gateway)", value="skipped"),
    ]

    # Map saved deployment → choice value for default
    saved = s.litellm.deployment
    if saved == "local":
        default = "local-docker" if s.litellm.local.runtime in ("docker", "podman") else "local-nodocker"
    elif saved == "remote":
        default = "remote"
    elif saved == "skipped":
        default = "skipped"
    else:
        default = "local-docker" if docker_det.installed else "local-nodocker"

    choice = ui.select("Where is your gateway?", choices, default=default)
    if choice is None:
        return 130

    if choice == "local-docker":
        s.litellm.deployment = "local"
        s.litellm.local.runtime = docker_runtime or "docker"
        return _step3_docker(s)
    if choice == "local-nodocker":
        s.litellm.deployment = "local"
        return _step3_local_nodocker(s)
    if choice == "remote":
        s.litellm.deployment = "remote"
        return _step3_remote(s)
    s.litellm.deployment = "skipped"
    return 0  # skipped


def _ask_bind_and_port(s: state.SetupState) -> None:
    """Shared prompt for bind address + port — used by all local modes."""
    bind_choices = [
        ui.Choice("127.0.0.1  (localhost only — recommended)", value="127.0.0.1"),
        ui.Choice("0.0.0.0  (DANGEROUS — exposes to LAN)", value="0.0.0.0"),
    ]
    s.litellm.local.bind_address = ui.select(
        "Bind interface:", bind_choices,
        default=s.litellm.local.bind_address or "127.0.0.1",
    )
    s.litellm.local.port = int(ui.text(
        "Port", default=str(s.litellm.local.port or 4000),
    ) or 4000)


def _step3_docker(s: state.SetupState) -> int:
    """Docker mode — pull image, container managed via docker compose."""
    runtime, det = detection.detect_container_runtime()
    if not det.installed:
        ui.error("No container runtime found. Install Docker Desktop / Colima / Podman first.")
        ui.detail("macOS:  brew install --cask docker  (or)  brew install colima && colima start")
        ui.detail("Linux:  https://docs.docker.com/engine/install/")
        return 1
    ui.ok(f"{runtime} {det.version}  {det.binary_path}")
    s.litellm.local.runtime = runtime  # docker | podman
    if runtime == "docker" and not detection.detect_compose():
        ui.error("docker compose plugin not found. Install Docker Desktop or `docker-compose-plugin`.")
        return 1

    _ask_bind_and_port(s)

    image = ui.text(
        "Container image",
        default=s.litellm.local.image or litellm.DEFAULT_DOCKER_IMAGE,
    ) or litellm.DEFAULT_DOCKER_IMAGE
    s.litellm.local.image = image

    # Save state NOW so pull_image() can read s.litellm.local.runtime
    state.save(s)

    if ui.confirm(f"Pull {image} now?  (~400MB, takes 30-90s)", default=True):
        with ui.spinner(f"Pulling {image}"):
            ok, msg = litellm.pull_image(image)
        if not ok:
            ui.error(f"Pull failed: {msg[:300]}")
            return 1
        # Track only if image wasn't already cached locally before this run.
        if "Already exists" not in msg and "Image is up to date" not in msg:
            s.tracking.docker_image_pulled_by_us = True
            s.tracking.docker_image = image
        ui.ok("Image pulled")
    return 0


def _step3_local_nodocker(s: state.SetupState) -> int:
    """Local install without Docker — choose pip-venv or pipx."""
    ui.console().print()

    runtime_choices = [
        ui.Choice("pip + dedicated venv at ~/.config/ai-resources/venv  (universal — needs python 3.10-3.13)",
                  value="pip-venv"),
        ui.Choice("pipx-managed  (cleanest if you already use pipx)",
                  value="pipx"),
    ]
    default = s.litellm.local.runtime if s.litellm.local.runtime in ("pip-venv", "pipx") else "pip-venv"
    runtime = ui.select("Local mode (no Docker):", runtime_choices, default=default)
    if runtime is None:
        return 130
    s.litellm.local.runtime = runtime

    _ask_bind_and_port(s)

    if runtime == "pip-venv":
        return _step3_pipvenv(s)
    return _step3_pipx(s)


def _step3_pipx(s: state.SetupState) -> int:
    """Install LiteLLM via pipx + verify."""
    pipx_det = detection.detect_pipx()
    if not pipx_det.installed:
        ui.warn("pipx not detected on this system.")
        if not _try_install_pipx():
            return 1
        pipx_det = detection.detect_pipx()
        if not pipx_det.installed:
            ui.error("pipx still not on PATH after install attempt.")
            ui.detail("Run `pipx ensurepath` and start a new shell, then re-run setup.")
            return 1
    ui.ok(f"pipx {pipx_det.version}  {pipx_det.binary_path}")

    # LiteLLM's transitive deps (orjson via pyo3, etc.) max out at Python 3.13.
    # If the system default python is 3.14+, force a compatible interpreter.
    py = detection.find_compatible_python()
    if not py.installed:
        ui.error("No compatible Python found (need 3.10–3.13 for LiteLLM deps).")
        ui.detail("Install one:  brew install python@3.12  (macOS)")
        ui.detail("              sudo apt install python3.12  (Debian/Ubuntu)")
        return 1
    ui.ok(f"Using Python {py.version} for the install  ({py.binary_path})")
    s.litellm.local.python_path = py.binary_path

    # Check if litellm is already installed
    existing = detection.detect_litellm_binary()
    if existing.installed:
        ui.ok(f"litellm already installed: {existing.binary_path} v{existing.version}")
        s.litellm.local.binary_path = existing.binary_path
        # Pre-existing — do NOT track as our install (don't uninstall on teardown)
        if ui.confirm("Reinstall to ensure latest?", default=False):
            with ui.spinner("Reinstalling LiteLLM via pipx"):
                ok, msg = litellm.install_litellm_pipx()
            if not ok:
                ui.error(f"Install failed: {msg[:200]}")
                return 1
            ui.ok("LiteLLM updated")
    else:
        if not ui.confirm(f"Install LiteLLM via `pipx install '{litellm.DEFAULT_LITELLM_PIP_SPEC}' --python {py.binary_path}`?",
                          default=True):
            return 1
        with ui.spinner("Installing LiteLLM (this may take 1-3 minutes)"):
            ok, msg = litellm.install_litellm_pipx(python_path=py.binary_path)
        if not ok:
            ui.error("LiteLLM install failed:")
            for line in msg.splitlines():
                ui.detail(line)
            ui.console().print()
            ui.detail("To debug manually, run: pipx install 'litellm[proxy]' --verbose")
            return 1
        new_det = detection.detect_litellm_binary()
        if not new_det.installed:
            ui.error("Installation completed but litellm not in PATH. Run `pipx ensurepath` and re-run.")
            return 1
        s.litellm.local.binary_path = new_det.binary_path
        # Track that WE installed this — teardown is allowed to uninstall it.
        s.tracking.litellm_installed_by_us = True
        s.tracking.litellm_install_method = "pipx"
        ui.ok(f"LiteLLM installed at {new_det.binary_path}")
    return 0


def _step3_pipvenv(s: state.SetupState) -> int:
    """Install LiteLLM into a dedicated venv at ~/.config/ai-resources/venv/."""
    # Need Python 3.10-3.13 for litellm (pyo3 maxes at 3.13)
    py = detection.find_compatible_python()
    if not py.installed:
        ui.error("No compatible Python found (need 3.10–3.13 for LiteLLM deps).")
        ui.detail("Install one:  brew install python@3.12  (macOS)")
        ui.detail("              sudo apt install python3.12  (Debian/Ubuntu)")
        return 1
    ui.ok(f"Using Python {py.version}  ({py.binary_path})")
    s.litellm.local.python_path = py.binary_path

    # If venv already exists with litellm, skip
    existing_bin = litellm.venv_dir() / "bin" / "litellm"
    if existing_bin.is_file():
        ui.ok(f"LiteLLM already installed at {existing_bin}")
        s.litellm.local.binary_path = str(existing_bin)
        s.litellm.local.venv_path = str(litellm.venv_dir())
        # Pre-existing venv — already tracked by an earlier setup run; if tracking
        # is missing (e.g. user hand-created it), leave the flag alone so teardown
        # won't blow away a venv we didn't make.
        if ui.confirm("Reinstall to ensure latest?", default=False):
            with ui.spinner("Reinstalling LiteLLM via pip"):
                ok, msg = litellm.install_litellm_venv(python_path=py.binary_path)
            if not ok:
                ui.error("LiteLLM reinstall failed:")
                for line in msg.splitlines():
                    ui.detail(line)
                return 1
            ui.ok("LiteLLM updated")
        return 0

    if not ui.confirm("Create venv at ~/.config/ai-resources/venv and pip install LiteLLM?",
                      default=True):
        return 1
    with ui.spinner("Creating venv + installing LiteLLM (this may take 1-3 minutes)"):
        ok, msg = litellm.install_litellm_venv(python_path=py.binary_path)
    if not ok:
        ui.error("LiteLLM install failed:")
        for line in msg.splitlines():
            ui.detail(line)
        ui.console().print()
        ui.detail(f"To debug manually:")
        ui.detail(f"  source {litellm.venv_dir()}/bin/activate")
        ui.detail(f"  pip install 'litellm[proxy]' --verbose")
        return 1
    s.litellm.local.binary_path = msg
    s.litellm.local.venv_path = str(litellm.venv_dir())
    # Track that WE created this venv — teardown may delete it.
    s.tracking.litellm_installed_by_us = True
    s.tracking.litellm_install_method = "pip-venv"
    ui.ok(f"LiteLLM installed at {msg}")
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
    _, newly_added = credentials.update_env_tracked({s.litellm.remote.master_key_env: master_key})
    for k in newly_added:
        if k not in s.tracking.env_keys_added:
            s.tracking.env_keys_added.append(k)

    with ui.spinner(f"Validating {url}"):
        ok, msg = litellm.validate_remote(url, master_key)
    if not ok:
        ui.error(f"Remote unreachable: {msg}")
        if not ui.confirm("Continue anyway?", default=False):
            return 1
    else:
        ui.ok("Remote endpoint reachable")
    return 0


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

    # Master key for LiteLLM.
    # Claude Code v2+ reads its API key from the macOS Keychain ("Claude Code"
    # service) and sends it verbatim — it ignores ANTHROPIC_API_KEY in settings.json.
    # The gateway master key must equal that Keychain key for auth to succeed.
    keychain_key = credentials.get_claude_code_keychain_key()
    current_master = credentials.get_key("LITELLM_MASTER_KEY")
    if keychain_key:
        if current_master != keychain_key:
            updates["LITELLM_MASTER_KEY"] = keychain_key
            ui.detail(f"LITELLM_MASTER_KEY synced from Claude Code Keychain ({credentials.mask(keychain_key)})")
        else:
            ui.detail(f"LITELLM_MASTER_KEY already matches Keychain ({credentials.mask(current_master)})")
    elif not current_master:
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
        _, newly_added = credentials.update_env_tracked(updates)
        for k in newly_added:
            if k not in s.tracking.env_keys_added:
                s.tracking.env_keys_added.append(k)
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

    default = s.profile.name if s.profile.name in available else "quality-first"
    chosen = ui.select("Choose profile:", choices, default=default)
    if chosen is None:
        return 130

    if chosen == "__custom__":
        # Start from quality-first, then customize each role
        base = profiles.load_profile("quality-first")
        chosen = "quality-first"
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
    ui.section(8, TOTAL_STEPS, "Lifecycle (auto-start at login)")

    if s.litellm.deployment != "local":
        ui.info("Skipped (gateway is not local).")
        return 0

    # Docker / Podman containers self-restart via compose's `restart: always`
    # whenever the runtime comes back up — no LaunchAgent / systemd unit needed.
    if s.litellm.local.runtime in ("docker", "podman"):
        s.litellm.local.auto_start = True  # implicit via container restart policy
        s.litellm.local.log_dir = str(litellm.log_dir())
        ui.info(f"Skipped — {s.litellm.local.runtime} container uses "
                f"`restart: always` (no LaunchAgent needed).")
        ui.detail("Container starts automatically when the Docker daemon is running.")
        return 0

    s.litellm.local.auto_start = ui.confirm(
        "Auto-start LiteLLM at every login?",
        default=s.litellm.local.auto_start,
    )

    if s.litellm.local.auto_start:
        path = litellm.lifecycle_path()
        s.litellm.local.lifecycle_path = str(path)
        s.litellm.local.log_dir = str(litellm.log_dir())
        if litellm.is_macos():
            ui.detail(f"Will install macOS LaunchAgent: {path}")
        else:
            ui.detail(f"Will install systemd-user service: {path}")
    return 0


# --- Step 9 — Apply -------------------------------------------------------------
def _step9_apply(s: state.SetupState, dry_run: bool = False) -> int:
    if dry_run:
        return _step9_dry_run(s)
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
                paths = litellm.write_configs(executors_doc, {k: asdict(v) for k, v in s.providers.items()})
                for label, p in paths.items():
                    ui.ok(str(p))
                    if str(p) not in s.tracking.config_files_written:
                        s.tracking.config_files_written.append(str(p))
            except (RuntimeError, OSError) as e:
                ui.error(f"Failed to write LiteLLM config: {e}")
                return 1
        # Always track the executors.yaml we just wrote
        if str(state.executors_path()) not in s.tracking.config_files_written:
            s.tracking.config_files_written.append(str(state.executors_path()))
    else:
        executors_doc = profiles.to_executors(profiles.load_profile("cost-optimized"))

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

    # Local install path: write wrapper + lifecycle, start service
    if s.mode == "multi-model" and s.litellm.deployment == "local":
        ui.console().print()

        if s.litellm.local.runtime in ("pipx", "pip-venv"):
            # 1. Write the wrapper script
            bin_path = Path(s.litellm.local.binary_path or "")
            if not bin_path.is_file():
                ui.error(f"litellm binary not found at {bin_path}")
                return 1
            wrapper = litellm.write_wrapper_script(
                bin_path,
                port=s.litellm.local.port,
                host=s.litellm.local.bind_address,
            )
            s.tracking.wrapper_path = str(wrapper)
            ui.ok(str(wrapper))

            # 2. Install lifecycle (always — auto-start determines whether it actually starts)
            if s.litellm.local.auto_start:
                try:
                    p = litellm.install_lifecycle(wrapper)
                    s.litellm.local.lifecycle_path = str(p)
                    s.tracking.lifecycle_installed = True
                    s.tracking.lifecycle_path_written = str(p)
                    ui.ok(f"Service installed: {p}")
                except (OSError, RuntimeError) as e:
                    ui.error(f"Lifecycle install failed: {e}")
                    return 1
            else:
                # Start once now, no auto-start
                import subprocess
                subprocess.Popen([str(wrapper)],
                                  stdout=open(litellm.log_dir() / "litellm.log", "ab"),
                                  stderr=open(litellm.log_dir() / "litellm.err", "ab"))
                ui.ok("LiteLLM started in background (will not auto-restart on logout)")

        elif s.litellm.local.runtime in ("docker", "podman"):
            # Docker mode — write compose, start container. Auto-start is handled
            # entirely by the container's `restart: always` policy; no LaunchAgent
            # / systemd unit gets installed for this runtime.
            try:
                cp = litellm.write_compose_yaml()
                if str(cp) not in s.tracking.config_files_written:
                    s.tracking.config_files_written.append(str(cp))
                ui.ok(str(cp))
            except Exception as e:
                ui.error(f"Compose file write failed: {e}")
                return 1
            with ui.spinner(f"Starting container via {s.litellm.local.runtime} compose up"):
                if not litellm.start_service():
                    ui.error("Container start failed")
                    ui.detail("Run `ai-resources daemon logs` to investigate")
                    return 1
            ui.ok(f"Container running with restart policy — auto-restarts when "
                  f"{s.litellm.local.runtime} comes up.")

        # Wait for gateway health regardless of mode
        with ui.spinner("Waiting for gateway to become healthy"):
            healthy = litellm.wait_for_health(
                f"{gateway_url}/health/liveliness", max_attempts=60, delay=1.0,
            )
        if healthy:
            ui.ok(f"Gateway running at {gateway_url}")
        else:
            ui.error("Gateway didn't pass health check")
            ui.detail("Run `ai-resources daemon logs` to investigate")
            return 1

    # Smoke tests (multi-model only)
    if s.mode == "multi-model":
        ui.console().print()
        ui.info("Running smoke tests (~$0.001 of tokens)...")
        results = smoke.run_all(executors_doc, gateway_url, master_key)

        # Persist full results so users can investigate truncated errors
        smoke_log = litellm.log_dir() / "smoke.log"
        try:
            with smoke_log.open("a", encoding="utf-8") as fp:
                fp.write(f"\n=== {datetime.now(timezone.utc).isoformat()} ===\n")
                for label, ok, msg in results:
                    status = "PASS" if ok else "FAIL"
                    fp.write(f"[{status}] {label}\n")
                    if msg:
                        fp.write(f"  {msg}\n")
        except OSError:
            pass

        had_failures = False
        for label, ok, msg in results:
            if ok:
                ui.ok(label)
            else:
                had_failures = True
                ui.warn(f"{label}")
                # Show first 300 chars across multiple lines so the cause is visible
                for line in (msg or "(no detail)").splitlines():
                    ui.detail(f"  {line[:300]}")
        if had_failures:
            ui.detail(f"  Full smoke log: {smoke_log}")

        s.smoke_tests = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "status": "pass" if all(ok for _, ok, _ in results) else "partial",
        }

    return 0


# --- Step 9 dry-run preview ------------------------------------------------------
def _step9_dry_run(s: state.SetupState) -> int:
    """Render all configs to a temp dir, print them, start the gateway, run smoke tests, tear down."""
    import json
    import os
    import shutil
    import subprocess
    import tempfile

    from .. import repo_root as _repo_root
    from .cockpits import claude as _claude_cockpit

    console = ui.console()
    ui.section(9, TOTAL_STEPS, "Apply (DRY RUN)")
    console.print()
    console.print("[bold yellow]DRY RUN — configs shown below will NOT be written.[/]")
    console.print("[bold yellow]Gateway will be started temporarily for smoke tests, then stopped.[/]")
    console.print()

    # Build executors
    if s.mode == "multi-model":
        base = profiles.load_profile(s.profile.name)
        if s.profile.customized and s.profile.customizations:
            customizations = {k: v for k, v in s.profile.customizations.items()
                              if k in profiles.KNOWN_ROLES}
            base = profiles.merge_customizations(base, customizations)
        executors_doc = profiles.to_executors(base)
    else:
        executors_doc = profiles.to_executors(profiles.load_profile("cost-optimized"))

    master_key = credentials.get_key("LITELLM_MASTER_KEY") or ""
    bind = s.litellm.local.bind_address or "127.0.0.1"
    port = s.litellm.local.port or 4000
    gateway_url = (
        s.litellm.remote.url if s.litellm.deployment == "remote"
        else f"http://{bind}:{port}"
    )
    ak_path = str(_repo_root())
    mode = s.mode

    # Role table
    ui.role_table(profiles.role_table(executors_doc), title=f"Profile: {s.profile.name}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="ai-resources-dryrun-"))
    tmp_litellm_yaml = tmp_dir / "litellm.yaml"
    tmp_compose_yaml = tmp_dir / "docker-compose.yaml"
    proc = None
    started_docker = False

    try:
        # ── litellm.yaml ──────────────────────────────────────────────────────
        if s.mode == "multi-model" and s.litellm.deployment == "local":
            try:
                yaml_content = litellm._render_litellm_yaml(
                    executors_doc, dict(s.providers), "LITELLM_MASTER_KEY",
                )
                tmp_litellm_yaml.write_text(yaml_content, encoding="utf-8")
                console.print(f"\n[bold cyan]── litellm.yaml  (would write → {state.litellm_path()})[/]")
                for line in yaml_content.splitlines():
                    console.print(f"  {line}")
            except Exception as e:
                ui.warn(f"Cannot render litellm.yaml: {e}")

        # ── docker-compose.yaml ───────────────────────────────────────────────
        runtime = s.litellm.local.runtime
        if (s.mode == "multi-model" and s.litellm.deployment == "local"
                and runtime in ("docker", "podman") and tmp_litellm_yaml.is_file()):
            try:
                import yaml as _yaml
                image = s.litellm.local.image or litellm.DEFAULT_DOCKER_IMAGE
                compose_body = {
                    "services": {
                        "ai-resources-litellm-dryrun": {
                            "image": image,
                            "container_name": "ai-resources-litellm-dryrun",
                            "command": ["--config", "/app/config.yaml", "--port", "4000",
                                        "--host", "0.0.0.0"],
                            "ports": [f"{bind}:{port}:4000"],
                            "volumes": [
                                f"{tmp_litellm_yaml}:/app/config.yaml:ro",
                                f"{state.env_path()}:/app/.env:ro",
                            ],
                            "env_file": [str(state.env_path())],
                            "restart": "no",
                            "networks": ["ai-resources"],
                            # Use python3 (always available in Python-based images) instead of
                            # curl (absent from slim images) to avoid false unhealthy status.
                            "healthcheck": {
                                "test": ["CMD-SHELL",
                                         "python3 -c \"import urllib.request; "
                                         "urllib.request.urlopen("
                                         "'http://localhost:4000/health/liveliness', timeout=3)"
                                         "\" 2>/dev/null || exit 1"],
                                "interval": "30s",
                                "timeout": "5s",
                                "retries": 3,
                                "start_period": "30s",
                            },
                        }
                    },
                    "networks": {"ai-resources": {"driver": "bridge"}},
                }
                compose_content = _yaml.safe_dump(compose_body, default_flow_style=False, sort_keys=False)
                tmp_compose_yaml.write_text(compose_content, encoding="utf-8")
                console.print(f"\n[bold cyan]── docker-compose.yaml  (would write → {state.compose_path()})[/]")
                for line in compose_content.splitlines():
                    console.print(f"  {line}")
            except Exception as e:
                ui.warn(f"Cannot render compose YAML: {e}")

        # ── settings.json patch ───────────────────────────────────────────────
        console.print(f"\n[bold cyan]── settings.json patch  (→ {_claude_cockpit.SETTINGS_PATH})[/]")
        patch = _claude_cockpit._build_settings_patch(
            executors_doc, master_key, gateway_url, ak_path, mode,
        )
        env_patch = patch.get("env", {})
        existing_env: dict = {}
        if _claude_cockpit.SETTINGS_PATH.is_file():
            try:
                existing_env = json.loads(
                    _claude_cockpit.SETTINGS_PATH.read_text(encoding="utf-8")
                ).get("env", {})
            except (json.JSONDecodeError, OSError):
                pass
        for k, v in env_patch.items():
            display_v = credentials.mask(v) if ("KEY" in k or "TOKEN" in k) else v
            if k not in existing_env:
                console.print(f"  [green]+ {k} = {display_v}  (new)[/]")
            elif existing_env[k] == v:
                console.print(f"  [dim]= {k} = {display_v}  (unchanged)[/]")
            else:
                console.print(f"  [yellow]~ {k} = {display_v}  (update)[/]")

        # ── start gateway for testing ─────────────────────────────────────────
        if s.mode == "multi-model" and tmp_litellm_yaml.is_file():
            console.print()
            ui.info("Starting gateway temporarily for smoke tests…")

            if runtime in ("pipx", "pip-venv"):
                # Resolve litellm binary
                bin_path = Path(s.litellm.local.binary_path or "")
                if not bin_path.is_file():
                    if runtime == "pip-venv":
                        bin_path = litellm.venv_dir() / "bin" / "litellm"
                    candidate = shutil.which("litellm")
                    if candidate and not bin_path.is_file():
                        bin_path = Path(candidate)
                if not bin_path.is_file():
                    ui.warn("litellm binary not found — skipping live gateway test")
                    ui.detail(f"Expected: {bin_path}")
                    ui.detail("Run `ai-resources setup` (without --dry-run) to install it first.")
                else:
                    env_for_proc = {**os.environ, **credentials.load_env()}
                    proc = subprocess.Popen(
                        [str(bin_path), "--config", str(tmp_litellm_yaml),
                         "--port", str(port), "--host", bind],
                        env=env_for_proc,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    with ui.spinner("Waiting for gateway health"):
                        healthy = litellm.wait_for_health(
                            f"{gateway_url}/health/liveliness",
                            max_attempts=60, delay=1.0,
                        )
                    if healthy:
                        ui.ok(f"Gateway healthy at {gateway_url}")
                    else:
                        ui.error("Gateway did not become healthy within 60 s")
                        ui.detail("Check logs above or try starting manually:")
                        ui.detail(f"  source {state.env_path()} && {bin_path} "
                                  f"--config {tmp_litellm_yaml} --port {port}")

            elif runtime in ("docker", "podman") and tmp_compose_yaml.is_file():
                with ui.spinner(f"docker compose up -d (dry-run container)"):
                    rc, _, err = litellm._run(
                        [runtime, "compose", "-f", str(tmp_compose_yaml), "up", "-d"],
                        timeout=120,
                    )
                if rc != 0:
                    ui.error(f"Container start failed: {err[:200]}")
                else:
                    started_docker = True
                    with ui.spinner("Waiting for gateway health"):
                        healthy = litellm.wait_for_health(
                            f"{gateway_url}/health/liveliness",
                            max_attempts=60, delay=1.0,
                        )
                    if healthy:
                        ui.ok(f"Gateway healthy at {gateway_url}")
                    else:
                        ui.error("Gateway did not become healthy within 60 s")
                        ui.detail(f"  {runtime} logs ai-resources-litellm-dryrun")

        elif s.mode == "multi-model" and s.litellm.deployment == "remote":
            with ui.spinner(f"Validating remote {s.litellm.remote.url}"):
                ok, msg = litellm.validate_remote(s.litellm.remote.url, master_key)
            if ok:
                ui.ok("Remote endpoint reachable")
            else:
                ui.error(f"Remote unreachable: {msg}")

        # ── smoke tests ───────────────────────────────────────────────────────
        if s.mode == "multi-model":
            console.print()
            ui.info("Running smoke tests…")
            results = smoke.run_all(executors_doc, gateway_url, master_key)
            for label, ok, msg in results:
                if ok:
                    ui.ok(label)
                else:
                    ui.warn(label)
                    for line in (msg or "(no detail)").splitlines():
                        ui.detail(f"  {line[:300]}")

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if started_docker and tmp_compose_yaml.is_file():
            litellm._run(
                [runtime, "compose", "-f", str(tmp_compose_yaml), "down"],
                timeout=60,
            )
        shutil.rmtree(tmp_dir, ignore_errors=True)

    console.print()
    ui.info("Dry run complete — no permanent changes made.")
    ui.detail("Remove --dry-run to apply.")
    return 0


# --- Completion banner -----------------------------------------------------------
def _print_completion(s: state.SetupState) -> None:
    # Multi-model + Claude Code OAuth session = gateway auth failures.
    # Surface this warning before the banner so users see it.
    if s.mode == "multi-model":
        claude_cs = s.cockpits.get("claude")
        if claude_cs and claude_cs.configured:
            from .cockpits import claude as _claude_cockpit
            if _claude_cockpit.is_logged_in_via_oauth():
                ui.console().print()
                ui.info("Claude Code is signed in via OAuth (claude.ai subscription).")
                ui.detail("The gateway runs with allow_requests_on_db_unavailable=true,")
                ui.detail("so your current setup works as-is.")
                ui.detail("")
                ui.detail("To switch to API key mode (cancel subscription):")
                ui.detail("  1. claude /logout")
                ui.detail("  2. Open a new terminal")
                ui.detail("  3. ai-resources doctor")

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
