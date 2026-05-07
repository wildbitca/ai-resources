"""Shared helpers for cockpit configurators."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .. import state


def deep_merge_json(path: Path, patch: dict, *, dry_run: bool = False) -> bool:
    """Merge patch into JSON at path. Returns True if written."""
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    def _merge(dst: dict, src: dict) -> bool:
        changed = False
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                if _merge(dst[k], v):
                    changed = True
            elif dst.get(k) != v:
                dst[k] = v
                changed = True
        return changed

    changed = _merge(existing, patch)
    if not changed:
        return False
    if dry_run:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def env_keys_added_by_patch(path: Path, patch_env: dict[str, str]) -> list[str]:
    """Return env keys from patch_env that aren't already present at path['env'].

    Used by cockpit configurators to record exactly which keys they're introducing
    (so we can later remove only those, not user-set values).
    """
    if not path.is_file():
        return list(patch_env.keys())
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return list(patch_env.keys())
    existing_env = existing.get("env") or {}
    return [k for k in patch_env if k not in existing_env]


def remove_env_keys_from_settings(path: Path, keys: list[str]) -> list[str]:
    """Remove the given keys from the settings.json `env` block. Returns keys actually removed."""
    if not path.is_file() or not keys:
        return []
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    env_block = cur.get("env")
    if not isinstance(env_block, dict):
        return []
    removed = [k for k in keys if k in env_block]
    for k in removed:
        env_block.pop(k, None)
    if not env_block:
        cur.pop("env", None)
    path.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return removed


def write_text(path: Path, content: str, *, dry_run: bool = False) -> bool:
    """Write text file, creating parent dirs. Returns True if written/changed."""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                return False
        except OSError:
            pass
    if dry_run:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def ensure_symlink(link: Path, target: Path, *, dry_run: bool = False) -> bool:
    if dry_run:
        return False
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return False
        link.unlink()
    elif link.exists():
        return False
    link.symlink_to(target)
    return True


def multimodel_protocol_md(ak_path: str, gateway_url: str, mode: str = "multi-model") -> str:
    """Return the multi-model protocol section to inject into cockpit instruction files."""
    if mode == "single-model":
        return ""

    return f"""## Multi-Model Routing Protocol (ai-resources)

**Mode:** multi-model via LiteLLM gateway at `{gateway_url}`

This environment uses per-role model routing. The kit ships with N subagent
definitions at `~/.claude/agents/*.md` (or equivalent for your cockpit). Each
agent declares a `model:` in its frontmatter — that string is sent verbatim
to the configured LiteLLM gateway, which routes to the actual provider.

### How it works

1. **Gateway (LiteLLM):** local container at `{gateway_url}` exposes an
   Anthropic-compatible `/v1/messages` endpoint. It auto-translates calls to
   the right provider (Anthropic, Google, OpenAI, Vertex) based on the
   `model` field.
2. **Cockpit env:** `ANTHROPIC_BASE_URL` is set to the gateway. Your cockpit
   sends every API call to the gateway instead of Anthropic directly.
3. **Per-subagent model:** subagent frontmatter `model:` is passthrough — set
   it to `claude-sonnet-4-6`, `gemini-2.5-pro`, `gpt-5`, etc. The gateway
   handles translation.
4. **Routing config:** see `~/.config/ai-resources/executors.yaml` for the
   role → model mapping. Edit there or run `ai-resources setup` to change.

### When you spawn a subagent

- Read the agent's frontmatter `model:` field — that's the model that role uses
- Tools, MCP, skills, file access all work normally — the gateway only changes
  which model receives the request
- Engram MCP for persistent memory works for ALL subagents regardless of model

### Limits to know

- Anthropic prompt caching is preserved when routing to Claude. Lost when
  routing to Gemini/OpenAI (their own caching applies but format-specific).
- Some Anthropic-only beta features (computer-use, code-execution) won't work
  cross-provider. Use Claude models for those subagents.
- Gateway must be running: `ai-resources daemon status` to verify.

### Manage

```sh
ai-resources doctor          # full health check
ai-resources executors       # show role → model mapping
ai-resources daemon status   # gateway health
ai-resources daemon logs     # gateway logs
ai-resources audit           # cost report per role
```

Kit root: `{ak_path}`.
"""


def kit_context_block(ak_path: str) -> str:
    """Return the standard kit-context footer used in subagent files."""
    return (
        f"\n\n---\n\n## Kit context\n\n"
        f"- **Skills index:** `{ak_path}/skills-index.json`\n"
        f"- **Skills root:** `{ak_path}/skills/`\n"
        f"- **Workflows:** `{ak_path}/workflows/`\n"
        f"- **Routing:** `~/.config/ai-resources/executors.yaml`\n"
    )


def mcp_engram_block() -> dict:
    """Standard Engram MCP server block (matches Claude Desktop / Code / Gemini schema)."""
    return {
        "engram": {
            "command": "engram",
            "args": ["mcp"],
        }
    }
