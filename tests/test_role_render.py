"""The shared role renderer (ADR-0015 S2): one spec per role, personas gated by domain, adapters stay thin."""
from __future__ import annotations

from ai_resources import repo_root
from ai_resources.setup import role_render


def test_fifteen_roles_and_no_persona_when_no_domain_is_active():
    specs = role_render.load_specs(repo_root(), env={})
    assert len(specs) == 15 and not any(s.is_persona for s in specs)
    assert role_render.role_names(repo_root()) == sorted(role_render.role_names(repo_root()))


def test_a_persona_is_a_spec_of_its_role_with_the_overlay():
    specs = {s.name: s for s in role_render.load_specs(repo_root(), env={"AI_RESOURCES_SKILLS": "symfony"})}
    persona = specs["implementer-symfony"]
    assert persona.is_persona and persona.base == "implementer" and persona.overlay
    assert persona.skills == specs["implementer"].skills
    assert persona.prompt().endswith(role_render.SEPARATOR + persona.overlay)
    assert persona.overlay not in specs["implementer"].prompt()


def test_prompt_applies_the_rewrite_and_the_context_last():
    spec = next(s for s in role_render.load_specs(repo_root(), env={}) if s.name == "planner")
    out = spec.prompt(lambda b: b.upper(), "\nTAIL")
    assert out.endswith("\nTAIL") and spec.body.upper() in out


def test_missing_kit_root_gives_no_specs(tmp_path):
    assert role_render.load_specs(tmp_path) == []


def test_agy_block_lists_each_role_once_with_its_prompt_path():
    specs = role_render.load_specs(repo_root(), env={"AI_RESOURCES_SKILLS": "all"})
    block = role_render.agy_role_block(specs, "/kit")
    assert block.count("\n- `") == 15 and "/kit/agents/roles/verifier.md" in block
    assert "-symfony`" not in block
