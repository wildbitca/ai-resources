"""Pre-refactor characterization of the Claude Code subagent renderer (ADR-0015 S2, acceptance 7).

The digests in tests/fixtures/role_render/claude_subagents.json were captured from `claude.render_subagent_files`
BEFORE the shared role renderer was extracted. The Claude adapter must keep producing the same bytes. Regenerate the
fixture only for a deliberate output change: `python tests/test_role_render_characterization.py --write`.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ai_resources.setup.cockpits import claude  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "role_render" / "claude_subagents.json"
EXECUTORS = {
    "by_role": {"implementer": {"model": "claude-sonnet-5-5"}, "planner": {"model": "claude-opus-5-5"},
                "explore": {"model": "claude-haiku-4-5"}, "verifier": {"model": "claude-opus-5-5"}},
    "by_persona": {"code-reviewer-angular": {"model": "claude-sonnet-5-5"}},
}
SCENARIOS = {"lean": "", "all": "all", "symfony": "symfony"}


def render(env_value: str, monkeypatch) -> dict[str, str]:
    if env_value:
        monkeypatch.setenv("AI_RESOURCES_SKILLS", env_value)
    else:
        monkeypatch.delenv("AI_RESOURCES_SKILLS", raising=False)
    return claude.render_subagent_files(EXECUTORS, "/kit", "multi-model")


def digests(files: dict[str, str]) -> dict[str, str]:
    return {k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in sorted(files.items())}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_claude_subagents_are_byte_identical_to_the_pre_refactor_output(scenario, monkeypatch):
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert digests(render(SCENARIOS[scenario], monkeypatch)) == golden[scenario]


def test_the_lean_scenario_has_fifteen_roles_and_no_persona(monkeypatch):
    names = set(render("", monkeypatch))
    assert len(names) == 15 and not any(n.endswith("-symfony") for n in names)


def test_the_domain_gate_adds_only_that_domains_personas(monkeypatch):
    lean, sym = set(render("", monkeypatch)), set(render("symfony", monkeypatch))
    assert sorted(sym - lean) == ["code-reviewer-symfony", "implementer-symfony", "planner-symfony",
                                  "software-architect-symfony", "tester-symfony"]


if __name__ == "__main__" and "--write" in sys.argv:       # pragma: no cover - fixture maintenance
    import os
    out = {}
    for name, value in SCENARIOS.items():
        os.environ.pop("AI_RESOURCES_SKILLS", None)
        if value:
            os.environ["AI_RESOURCES_SKILLS"] = value
        out[name] = digests(claude.render_subagent_files(EXECUTORS, "/kit", "multi-model"))
    FIXTURE.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
