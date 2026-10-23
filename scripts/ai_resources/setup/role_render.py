"""One neutral description of the kit roles for every cockpit (ADR-0015 S2, item a).

`load_specs()` reads `agents/roles/*.md` and the ACTIVE `agents/personas/*.md` once and returns `RoleSpec` objects:
the role prompt, the persona overlay, the domain gate (`domains.persona_is_active`) and the scoped skills
(`skill_scope.ROLE_SKILLS`). Each cockpit has a small adapter that turns a spec into its own format:

* Claude Code subagent frontmatter: `setup/cockpits/claude.py` (byte-identical to the pre-refactor output),
* an OpenClaw agent entry: `setup/cockpits/openclaw_roles.py`,
* a managed `GEMINI.md` role block for agy: `agy_role_block` below.

Pure: no write, no network. The model of a role comes from the profile (`by_role`), never from here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .. import domains, skill_scope
from ..generate import parse_simple_frontmatter

SEPARATOR = "\n\n---\n\n"


@dataclass(frozen=True)
class RoleSpec:
    name: str                       # agent name: the role, or `<role>-<domain>` for a persona
    description: str
    body: str                       # the role prompt (raw: the adapter rewrites cockpit-isms)
    base: str                       # the role whose tools, skills and model apply (== name for a role)
    overlay: str = ""               # the persona overlay (raw), empty for a role
    meta: Mapping = field(default_factory=dict)

    @property
    def is_persona(self) -> bool:
        return self.name != self.base

    @property
    def skills(self) -> tuple[str, ...]:
        return tuple(skill_scope.ROLE_SKILLS.get(self.base, ()))

    @property
    def mcp_servers(self) -> tuple[str, ...]:
        return tuple(skill_scope.ROLE_MCP_SERVERS.get(self.base, ()))

    def prompt(self, rewrite: Callable[[str], str] = lambda b: b, context: str = "") -> str:
        """Role prompt (+ persona overlay) as an adapter wants it; `context` is appended last."""
        if self.is_persona:
            return rewrite(self.body) + SEPARATOR + rewrite(self.overlay) + context
        return rewrite(self.body) + context


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def load_specs(kit_root: Path, env: dict | None = None) -> list[RoleSpec]:
    """Roles (sorted by file name), then the personas of the active domains (sorted), as the Claude renderer
    always ordered them."""
    roles_dir = kit_root / "agents" / "roles"
    if not roles_dir.is_dir():
        return []
    specs: list[RoleSpec] = []
    bodies: dict[str, str] = {}
    for role_md in sorted(roles_dir.glob("*.md")):
        text = _read(role_md)
        if text is None:
            continue
        meta, body = parse_simple_frontmatter(text)
        name = str(meta.get("name", role_md.stem))
        bodies[name] = body
        specs.append(RoleSpec(name, str(meta.get("description", "")), body, name, "", meta))
    personas_dir = kit_root / "agents" / "personas"
    for persona_md in sorted(personas_dir.glob("*.md")) if personas_dir.is_dir() else []:
        name = persona_md.stem
        # Longest match wins: both halves of `<role>-<domain>` can contain hyphens.
        candidates = [r for r in bodies if name.startswith(f"{r}-")]
        if not candidates:
            continue
        if not domains.persona_is_active(name, bodies, env):
            continue
        base = max(candidates, key=len)
        text = _read(persona_md)
        if text is None:
            continue
        meta, overlay = parse_simple_frontmatter(text)
        specs.append(RoleSpec(name, str(meta.get("description", "")), bodies[base], base, overlay, meta))
    return specs


def role_names(kit_root: Path) -> list[str]:
    """The role agents only (no persona): what `kit-role-<role>` agents are built from."""
    return [s.name for s in load_specs(kit_root, env={domains.ENV_VAR: ""}) if not s.is_persona]


def agy_role_block(specs: list[RoleSpec], kit_path: str) -> str:
    """The managed `GEMINI.md` block that lists the roles agy can play (ADR-0015 S9): one line per role with the
    description, the skills it reads and the path of its prompt. Short on purpose: agy loads it every session."""
    lines = ["Kit roles (read the role file before playing one; never sign off your own work):", ""]
    for spec in specs:
        if spec.is_persona:
            continue
        skills = ", ".join(spec.skills) or "-"
        desc = " ".join(spec.description.split())
        lines.append(f"- `{spec.name}`: {desc} Skills: {skills}. Prompt: `{kit_path}/agents/roles/{spec.name}.md`")
    return "\n".join(lines) + "\n"
