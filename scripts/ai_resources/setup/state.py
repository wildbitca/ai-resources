"""setup-state.yaml — persisted wizard answers + detection results."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # handled by ui.require_deps()

SCHEMA_VERSION = "1.0"


def config_root() -> Path:
    """Return ~/.config/ai-resources (or $XDG_CONFIG_HOME/ai-resources)."""
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "ai-resources"


def state_path() -> Path:
    return config_root() / "setup-state.yaml"


def env_path() -> Path:
    return config_root() / ".env"


def executors_path() -> Path:
    return config_root() / "executors.yaml"


def litellm_path() -> Path:
    return config_root() / "litellm.yaml"


def compose_path() -> Path:
    return config_root() / "docker-compose.yaml"


@dataclass
class CockpitState:
    installed: bool = False
    version: str = ""
    binary_path: str = ""
    config_root: str = ""
    configured: bool = False
    last_configured_at: str = ""


@dataclass
class LiteLLMLocal:
    runtime: str = "pip-venv"         # pip-venv | pipx | docker | podman
    binary_path: str = ""             # absolute path to litellm executable
    venv_path: str = ""               # for pip-venv mode
    python_path: str = ""             # python interpreter used for the install (3.10-3.13)
    image: str = "ghcr.io/berriai/litellm:main-stable"  # docker mode only
    bind_address: str = "127.0.0.1"
    port: int = 4000
    auto_start: bool = True
    lifecycle_path: str = ""
    log_dir: str = ""


@dataclass
class LiteLLMRemote:
    url: str = ""
    master_key_env: str = "LITELLM_MASTER_KEY"


@dataclass
class LiteLLMState:
    deployment: str = ""              # local | remote | skipped
    local: LiteLLMLocal = field(default_factory=LiteLLMLocal)
    remote: LiteLLMRemote = field(default_factory=LiteLLMRemote)
    health_check_at: str = ""


@dataclass
class ProviderState:
    enabled: bool = False
    auth_method: str = "api_key"      # api_key | vertex_adc | oauth
    env_var: str = ""
    last_validated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProfileState:
    name: str = "cost-optimized"
    customized: bool = False
    customizations: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class InstallTracking:
    """Audit trail of artifacts the wizard created — used to cleanly undo on mode switch.

    Each flag/path records something the wizard was responsible for installing.
    Pre-existing artifacts (e.g. a litellm already on PATH before setup) are NOT
    tracked here, so teardown only removes what we put there.
    """
    # LiteLLM Python package
    litellm_installed_by_us: bool = False
    litellm_install_method: str = ""           # "pipx" | "pip-venv" | ""
    # Docker image
    docker_image_pulled_by_us: bool = False
    docker_image: str = ""
    # Background runner (launchd plist / systemd unit)
    lifecycle_installed: bool = False
    lifecycle_path_written: str = ""
    # Wrapper script
    wrapper_path: str = ""
    # Generated YAMLs (litellm.yaml, docker-compose.yaml, executors.yaml, …)
    config_files_written: list[str] = field(default_factory=list)
    # .env keys added by the wizard (only those not pre-existing)
    env_keys_added: list[str] = field(default_factory=list)
    # Per-cockpit settings.json env keys we inserted
    cockpit_env_keys_added: dict[str, list[str]] = field(default_factory=dict)
    # Workflow script file names we copied into the cockpit's workflows dir (e.g. kit-plan.js).
    # Only these may be pruned later: a user's own kit-*.js is never touched.
    workflow_scripts_installed: list[str] = field(default_factory=list)
    # Subagent names we generated into the cockpit's agents dir. Same contract as
    # above: only these may be pruned, so an agent the user wrote by hand survives
    # even when its name matches one the kit used to ship. Without this record a
    # persona that is renamed or removed upstream would leave its subagent behind
    # forever, and the cockpit would keep offering an agent with no definition.
    subagent_files_installed: list[str] = field(default_factory=list)
    # External tools (claude, agy, openclaw) the wizard installed via their own official
    # installer — never a pre-existing binary. Teardown only ever touches these ids.
    tools_installed_by_us: list[str] = field(default_factory=list)
    # tool id -> the exact install command that was run, for audit / re-display.
    tool_install_methods: dict[str, str] = field(default_factory=dict)
    # tool id -> rc-file lines its installer appended (across ~/.bashrc, ~/.zshrc,
    # ~/.zprofile, ~/.profile), minus any that duplicated a pre-existing line. Only
    # these exact lines are ever removed on teardown.
    rc_lines_added: dict[str, list[str]] = field(default_factory=dict)
    # "yes" | "no" | None (never asked). Replayed under --non-interactive so a second
    # unattended run doesn't re-prompt for something already answered once.
    install_tools_answer: str | None = None


@dataclass
class OpenClawState:
    """Which engine the kit pointed OpenClaw's default agent at, and what it replaced."""
    engine: str = ""                  # antigravity | claude-code | codex | gemini-cli | keep
    model: str = ""                   # OpenClaw model ref (bare id for antigravity, e.g.
                                       # gemini-3.8-flash-low, claude-sonnet-4-6 or
                                       # gpt-oss-120b-medium — antigravity's two weekly
                                       # quota pools, see _agy_quota.py; namespaced
                                       # otherwise, e.g. anthropic/claude-sonnet-5)
    applied: bool = False             # the kit has written openclaw.json at least once
    config_path: str = ""
    # openclaw.json values before the kit's first write (model, models, engram,
    # extra_dirs, plus the antigravity-only keys), restored verbatim on teardown.
    # None means the key was absent. Also carries the one-off marker
    # "agy_mcp_bridge_preexisted" recorded when the antigravity engine registers the
    # agy MCP bridge, so teardown only removes an entry the kit itself created.
    previous: dict[str, Any] = field(default_factory=dict)
    # Voice notes. `voice` is the last answer (cloud | local | off | keep); `voice_applied`
    # is the mode the kit last wrote into tools.media, "" when it never did.
    voice: str = ""
    voice_language: str = ""          # ISO code passed to the transcriber, or auto
    voice_correction: str = ""        # local mode only: llm | offline
    voice_applied: str = ""
    # tools.media before the kit's first voice write, restored on teardown. None: absent.
    voice_previous: Any = None
    # What setup itself installed for voice notes, and only that, is removed on teardown:
    # Homebrew formulas that were missing before, and Whisper model files it downloaded.
    voice_formulas_installed: list[str] = field(default_factory=list)
    voice_models_downloaded: list[str] = field(default_factory=list)
    # MCP servers mirrored from Claude Code. `mcp` is the last answer (mirror | keep);
    # `mcp_skipped` the servers the user unticked. `mcp_mirrored` holds each server the kit
    # manages: {name: {"previous": definition before the kit (None: absent), "applied": ...}}.
    mcp: str = ""
    mcp_skipped: list[str] = field(default_factory=list)
    mcp_mirrored: dict[str, Any] = field(default_factory=dict)
    # antigravity engine only, below:
    orchestrator_model: str = "gemini-3.8-flash-low"
    # Bare model id: the ref is built as `claude-kit/<worker_model>`, and OpenClaw
    # only resolves two-segment refs (`claude-kit/anthropic/claude-sonnet-5` is
    # rejected by `config patch --dry-run`).
    worker_model: str = "claude-sonnet-5"
    plugin_linked: bool = False       # the kit ran `openclaw plugins install --link ...`
    risk_acknowledged: bool = False   # explicit consent to unrestricted code execution
    # True once `configure()` has successfully applied the antigravity engine's own
    # patch (agents.entries.claude, subagents.allowAgents, tools.media and
    # plugins.entries.ai-resources), independent of `engine` above.
    # `engine` reflects the *current* choice and is overwritten as soon as the wizard
    # picks a different one; this flag is what gates restoring antigravity-only keys
    # (in `configure()` when switching away, and in `teardown()`), so a switch from
    # antigravity to any other engine is never mistaken for "nothing to restore".
    antigravity_applied: bool = False
    # OpenClaw host section (cockpits/_openclaw_host.py). Every answer defaults to "no": a first
    # unattended run never changes how a live host behaves.
    host: bool = False                # the master answer: this machine runs the gateway
    host_narration: str = ""          # team narration into Telegram: "" (off) | milestones | every-step
    host_guard: bool = False          # the gateway guard hook (denies an undrained gateway stop)
    host_units: bool = False          # the ten openclaw-* systemd units (backup, watchdog, ...)
    host_config: bool = False         # the canonical config block (profiles/openclaw-host.json5)
    host_workboard: bool = True       # `openclaw plugins enable workboard` (asked; defaults to yes)
    host_agents_md: bool = False      # AGENTS.md templates for workspaces that have none
    host_check: bool = False          # report (and offer to fix) what `openclaw bootstrap` finds
    host_domain: str = ""             # public host name of the control UI (no scheme)
    host_pod_cidr: str = ""           # CIDR of the ingress that reaches the gateway
    host_operator_id: str = ""        # numeric Telegram id that failure notices go to
    host_backup_dir: str = ""         # where the backup tiers live
    # What the last configure() applied, so teardown removes exactly that and nothing else.
    host_hooks_applied: bool = False
    host_units_previous: dict[str, Any] = field(default_factory=dict)  # unit file -> text before (None: absent)
    host_units_applied: bool = False
    # Timers the kit enabled (they were not enabled before), so teardown disables exactly those.
    host_timers_enabled: list[str] = field(default_factory=list)
    # Hand-installed legacy team-hook registrations the kit replaced: [{"event", "entry"}], put back
    # by teardown.
    host_legacy_hooks: list[Any] = field(default_factory=list)
    host_config_changes: list[Any] = field(default_factory=list)       # leaves changed, with their old values
    host_workboard_applied: str = ""  # "" | enabled-by-kit | already-enabled
    host_agents_md_written: dict[str, str] = field(default_factory=dict)  # path -> sha256 of what was written
    host_env_previous: dict[str, Any] = field(default_factory=dict)    # kit-host.env key -> value before (None: absent)
    host_env_created: bool = False


@dataclass
class SetupState:
    schema_version: str = SCHEMA_VERSION
    last_run: str = ""
    mode: str = "single-model"        # single-model | multi-model
    # Which gateway serves multi-model routing. Meaningless in single-model.
    # litellm    = self-hosted gateway this kit installs and supervises
    # openrouter = hosted gateway; nothing to install, one key, no lifecycle
    backend: str = "litellm"          # litellm | openrouter
    cockpits: dict[str, CockpitState] = field(default_factory=dict)
    litellm: LiteLLMState = field(default_factory=LiteLLMState)
    providers: dict[str, ProviderState] = field(default_factory=dict)
    profile: ProfileState = field(default_factory=ProfileState)
    smoke_tests: dict[str, Any] = field(default_factory=lambda: {"last_run": "", "status": "unknown"})
    tracking: InstallTracking = field(default_factory=InstallTracking)
    openclaw: OpenClawState = field(default_factory=OpenClawState)


def _to_dict(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_dict(v) for v in obj]
    return obj


def _from_dict(cls: type, data: dict) -> Any:
    """Recursively rebuild a dataclass instance from a dict.

    Note: with `from __future__ import annotations`, fld.type is a string,
    not the actual class. We must resolve it via the dataclass's default
    factory, or hardcode known nested types. Below uses an explicit map
    for the known dataclasses — the simplest and most reliable approach.
    """
    if not isinstance(data, dict):
        return cls()

    NESTED: dict[type, dict[str, type]] = {
        SetupState: {
            "litellm": LiteLLMState,
            "profile": ProfileState,
            "tracking": InstallTracking,
            "openclaw": OpenClawState,
        },
        LiteLLMState: {
            "local": LiteLLMLocal,
            "remote": LiteLLMRemote,
        },
    }
    DICT_OF: dict[type, dict[str, type]] = {
        SetupState: {
            "cockpits": CockpitState,
            "providers": ProviderState,
        },
    }

    nested_map = NESTED.get(cls, {})
    dict_map = DICT_OF.get(cls, {})

    fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
    kwargs = {}
    for name in fields:
        if name not in data:
            continue
        val = data[name]
        if name in nested_map:
            kwargs[name] = _from_dict(nested_map[name], val) if isinstance(val, dict) else nested_map[name]()
        elif name in dict_map and isinstance(val, dict):
            sub_cls = dict_map[name]
            kwargs[name] = {k: _from_dict(sub_cls, v) for k, v in val.items()}
        else:
            kwargs[name] = val
    return cls(**kwargs)


def load() -> SetupState:
    """Load setup state from disk; return empty SetupState if absent."""
    path = state_path()
    if not path.is_file() or yaml is None:
        return SetupState()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return SetupState()
    return _from_dict(SetupState, data)


def save(state: SetupState) -> None:
    """Persist state to disk."""
    if yaml is None:
        return
    state.last_run = datetime.now(timezone.utc).isoformat()
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(_to_dict(state), default_flow_style=False, sort_keys=False)
    # The state records what the kit replaced on the host (previous config values, unit texts),
    # so it is owner-only: created 0600 (no window where it is world-readable) and tightened
    # when an older run left it wider.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def is_first_run() -> bool:
    return not state_path().is_file()
