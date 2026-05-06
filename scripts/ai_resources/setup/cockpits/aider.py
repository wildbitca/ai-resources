"""Aider cockpit configurator — uses LiteLLM for multi-provider support."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from .. import state
from ..detection import detect_aider
from ... import repo_root
from . import _shared


NAME = "Aider"
ID = "aider"
CONFIG_ROOT = Path.home() / ".aider"
CONVENTIONS_PATH = CONFIG_ROOT / "CONVENTIONS.md"
CONF_PATH = Path.home() / ".aider.conf.yml"


def detect():
    return detect_aider()


def _conf_yaml(executors: dict, gateway_url: str, master_key: str) -> str:
    """Generate ~/.aider.conf.yml with LiteLLM endpoint and per-role models.

    Aider supports 3 roles: architect (planner/thinker), editor (default model),
    weak (commits/summaries). We pick the most appropriate from executors.
    """
    by_role = executors.get("by_role", {})

    architect_model = by_role.get("software-architect", {}).get("model") \
        or by_role.get("planner", {}).get("model") \
        or "claude-opus-4-7"
    editor_model = by_role.get("implementer", {}).get("model") \
        or "claude-sonnet-4-6"
    weak_model = by_role.get("verifier", {}).get("model") \
        or "claude-haiku-4-5"

    conf = {
        "openai-api-base": gateway_url,
        "openai-api-key": master_key,
        "model": editor_model,
        "architect-model": architect_model,
        "weak-model": weak_model,
        "read": [str(CONVENTIONS_PATH)],
        "auto-commits": True,
        "dirty-commits": False,
        "no-stream": False,
    }
    if yaml is None:
        # Minimal hand-written fallback
        return "\n".join(f"{k}: {v}" for k, v in conf.items())
    return yaml.safe_dump(conf, default_flow_style=False, sort_keys=False)


def configure(ctx: dict) -> list[Path]:
    s = ctx["state"]
    ak_path = str(repo_root())
    gateway_url = ctx.get("gateway_url", "http://127.0.0.1:4000")
    master_key = ctx.get("master_key", "")
    executors = ctx.get("executors", {})
    written: list[Path] = []

    # Conventions file
    multimodel = _shared.multimodel_protocol_md(ak_path, gateway_url, s.mode)
    md = (
        f"# ai-resources (Aider)\n\n"
        f"Loaded automatically via `~/.aider.conf.yml` (`read:` directive).\n\n"
        f"**Refresh:** After updating the kit, run `ai-resources generate`.\n\n"
        f"{multimodel}"
        f"## Skill Discovery\n\n"
        f"- Skills index: `{ak_path}/skills-index.json`\n"
        f"- Workflows: `{ak_path}/workflows/`\n"
    )
    if _shared.write_text(CONVENTIONS_PATH, md):
        written.append(CONVENTIONS_PATH)

    # ~/.aider.conf.yml only when multi-model
    if s.mode == "multi-model" and master_key:
        if _shared.write_text(CONF_PATH, _conf_yaml(executors, gateway_url, master_key)):
            written.append(CONF_PATH)

    cs = s.cockpits.get(ID) or state.CockpitState()
    cs.installed = True
    cs.version = ctx.get("detected_version", cs.version)
    cs.binary_path = ctx.get("detected_path", cs.binary_path)
    cs.config_root = str(CONFIG_ROOT)
    cs.configured = True
    cs.last_configured_at = datetime.now(timezone.utc).isoformat()
    s.cockpits[ID] = cs

    return written
