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


@dataclass
class SetupState:
    schema_version: str = SCHEMA_VERSION
    last_run: str = ""
    mode: str = "single-model"        # single-model | multi-model
    cockpits: dict[str, CockpitState] = field(default_factory=dict)
    litellm: LiteLLMState = field(default_factory=LiteLLMState)
    providers: dict[str, ProviderState] = field(default_factory=dict)
    profile: ProfileState = field(default_factory=ProfileState)
    smoke_tests: dict[str, Any] = field(default_factory=lambda: {"last_run": "", "status": "unknown"})
    tracking: InstallTracking = field(default_factory=InstallTracking)


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
    path.write_text(
        yaml.safe_dump(_to_dict(state), default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def is_first_run() -> bool:
    return not state_path().is_file()
