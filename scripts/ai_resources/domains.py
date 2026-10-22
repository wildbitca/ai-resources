"""Persona domains and which of them are active (ADR-0014 D6).

`agents/personas/<role>-<domain>.md` overlays exist for five domains. Their subagents are rendered only for the active
domains, the same opt-in that widens the native skill list: `AI_RESOURCES_SKILLS=flutter,angular` (a comma list of
skill-id prefixes) or `all`. With the variable unset every domain is inactive and no persona subagent is written; the
role subagent still reads the overlay itself by path (kit-orchestration), so nothing a workflow needs is lost.

One map for both consumers (`skill_scope` parses the same tokens; `claude._generate_subagent_files` filters personas).
Standard library only.
"""
from __future__ import annotations

import os

ENV_VAR = "AI_RESOURCES_SKILLS"

# persona domain -> the token an operator writes in AI_RESOURCES_SKILLS (the skill-id prefix of that domain).
DOMAINS: dict[str, str] = {
    "dart-flutter": "flutter",
    "devops": "devops",
    "angular": "angular",
    "symfony": "symfony",
    "api-platform": "api-platform",
}

# Extra spellings that also switch a domain on (the persona domain's own name, and `dart` for the dart-* skills).
_EXTRA_TOKENS: dict[str, tuple[str, ...]] = {"dart-flutter": ("dart-flutter", "dart")}


def tokens(raw: str | None = None, env: dict | None = None) -> list[str]:
    """The comma-separated tokens of AI_RESOURCES_SKILLS, lower-cased, without empty items and without `lean`."""
    if raw is None:
        raw = (env if env is not None else os.environ).get(ENV_VAR) or ""
    return [x for x in (p.strip().lower() for p in raw.split(",")) if x and x != "lean"]


def active_domains(env: dict | None = None) -> set[str]:
    """Persona domains switched on by AI_RESOURCES_SKILLS: all of them for `all`, none when unset."""
    toks = tokens(env=env)
    if "all" in toks:
        return set(DOMAINS)
    active: set[str] = set()
    for domain, token in DOMAINS.items():
        spellings = {token, domain, *(_EXTRA_TOKENS.get(domain, ()))}
        if any(t.rstrip("-") in spellings for t in toks):
            active.add(domain)
    return active


def persona_domain(persona: str, roles) -> str:
    """The domain part of `<role>-<domain>` (longest matching role wins), or "" when no role prefixes the name."""
    matches = [r for r in roles if persona.startswith(f"{r}-")]
    return persona[len(max(matches, key=len)) + 1:] if matches else ""


def persona_is_active(persona: str, roles, env: dict | None = None) -> bool:
    """Whether the persona's subagent is rendered. A domain the map does not know is rendered (never hide what the
    parity test would have flagged); a name no role prefixes is not a persona."""
    domain = persona_domain(persona, roles)
    if not domain:
        return False
    if domain not in DOMAINS:
        return True
    return domain in active_domains(env)
