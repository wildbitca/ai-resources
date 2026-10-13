"""A mixed-provider team, end to end on fixtures: dispatch fields, setup planning, `models status`, matrix.

A profile puts some roles on a non-Claude provider; the example workflow hands those roles to other OpenClaw
agents through `backend: openclaw-agent` + `agentId`. This walks that team through the real setup planning
(`model_selection.plan_table` / `wizard._step_summary`), the real `models status` verb and `compat`, with fixtures
and stubs only (no host, no gateway, no network), and asserts the pieces agree:

  * the roles the workflow dispatches are exactly the roles the profile puts on a provider the Claude cockpit
    cannot run verified;
  * the provider each dispatched agent runs on is in the selection, and the matrix has a verified OpenClaw engine
    cell for it;
  * setup's plan and `models status` name the same providers.

The helpers here are test-local on purpose: the kit has no code that joins the three (that is the point of the
test: it fails when one of them moves without the others).
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import pathlib

import pytest
import yaml

from ai_resources import cli, model_accounts as ma, model_pins, model_providers, models_cmd
from ai_resources import model_pins as mp
from ai_resources.selection import ProviderSel, Selection, SlotSel
from ai_resources.setup import compat, model_selection, state, wizard
from test_models_update import Env
from wizard_fakes import Script

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO / "workflows" / "feature-cross-provider.workflow.yaml"

# The mixed team. Roles run by another OpenClaw agent sit on google; the rest stay on Claude.
PROFILE = {
    "mode": "multi-model",
    "by_role": {
        "explore":            {"model": "google/gemini-3.1-pro-preview"},
        "planner":            {"model": "google/gemini-3.8-flash"},
        "code-reviewer":      {"model": "google/gemini-3.8-flash"},
        "software-architect": {"model": "anthropic/claude-opus-5.5"},
        "implementer":        {"model": "anthropic/claude-sonnet-5.5"},
        "tester":             {"model": "anthropic/claude-haiku-5.5"},
        "security-auditor":   {"model": "anthropic/claude-opus-5.5"},
        "verifier":           {"model": "anthropic/claude-sonnet-5.5"},
        "doc-writer":         {"model": "anthropic/claude-sonnet-5.5"},
    },
}

# What `agents.entries.<id>.model.primary` says on the (fixture) host: agy labels, never API ids.
AGENTS = {
    "agy-flash": {"model": {"primary": "agy-cli/gemini-3.8-flash-low"}},
    "agy-pro": {"model": {"primary": "agy-cli/gemini-3.1-pro-low"}},
}

DETECTED = ["claude", "openclaw", "agy"]


# --- the pieces, read from the real files -------------------------------------------------------------------

def workflow_steps() -> list[dict]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return list(doc["steps"])


def dispatched(steps: list[dict]) -> dict[str, str]:
    """role -> agentId for every step handed to another OpenClaw agent."""
    return {s["subagent_type"]: s["agentId"] for s in steps if s.get("backend") == "openclaw-agent"}


def agent_provider(model_ref: str) -> str:
    """The provider an agent's primary model belongs to. An agy label resolves by the kit's own label list."""
    bare = model_ref.split("/", 1)[1] if model_ref.startswith("agy-cli/") else model_ref
    if bare in model_pins.AGY_STATIC:
        return {"gemini": "google", "claude": "anthropic", "gpt": "openai"}[bare.split("-", 1)[0]]
    ref = model_providers.identify(model_ref)
    return ref.provider if ref else ""


def selection_for(profile: dict, agents: dict, smoke_path: str = "litellm", engine: str = "") -> Selection:
    """The Selection a wizard run records for this team: the profile's refs plus the dispatched agents' providers."""
    sel = Selection(shape="multi-provider", smoke_path=smoke_path, openclaw_engine=engine)
    refs = [cfg["model"] for cfg in profile["by_role"].values()]
    for ref in dict.fromkeys(refs):
        parsed = model_providers.identify(ref)
        sel.providers.setdefault(parsed.provider, ProviderSel())
        sel.slots.setdefault(f"{parsed.provider}:{parsed.family}", SlotSel(ref=ref))
    for cfg in agents.values():
        provider = agent_provider(cfg["model"]["primary"])
        sel.providers.setdefault(provider, ProviderSel())
    sel.primary = "anthropic:sonnet"
    return sel


def disagreements(profile: dict, steps: list[dict], agents: dict, selection: Selection) -> list[str]:
    """Every place where the dispatch fields, the profile and the compat matrix do not say the same thing."""
    found: list[str] = []
    claude_cell = lambda p: compat.cell("claude", p, compat.MULTI_LITELLM)           # noqa: E731
    dispatch = dispatched(steps)
    selected = set(selection.enabled_providers())
    for role, agent_id in dispatch.items():
        if agent_id not in agents:
            found.append(f"{role}: agent {agent_id!r} is not configured on the host")
            continue
        provider = agent_provider(agents[agent_id]["model"]["primary"])
        if provider not in selected:
            found.append(f"{role}: agent {agent_id} runs {provider!r}, which is not in the selection")
        engines = [e for e in compat.ENGINES if compat.ENGINE_CELLS[(e, provider)].action != compat.SKIP
                   and compat.ENGINE_CELLS[(e, provider)].verified]
        if not engines:
            found.append(f"{role}: no verified OpenClaw engine runs {provider!r} models")
        profile_provider = model_providers.identify(profile["by_role"][role]["model"]).provider \
            if role in profile["by_role"] else None
        if profile_provider and profile_provider != provider:
            found.append(f"{role}: the profile puts it on {profile_provider!r} but agent {agent_id} runs {provider!r}")
    for role, cfg in profile["by_role"].items():
        provider = model_providers.identify(cfg["model"]).provider
        c = claude_cell(provider)
        runnable_on_claude = c.action != compat.SKIP and c.verified
        if role in dispatch and runnable_on_claude and provider != "anthropic":
            found.append(f"{role}: dispatched although Claude Code runs {provider!r} verified")
        if role not in dispatch and not runnable_on_claude:
            found.append(f"{role}: on {provider!r}, which Claude Code cannot run verified, and not dispatched")
    return found


# --- fixtures -------------------------------------------------------------------------------------------------

@pytest.fixture
def vk():
    spec = importlib.util.spec_from_file_location("validate_kit_mixed_team", REPO / "scripts" / "validate_kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.failures.clear()
    if hasattr(mod, "warnings"):
        mod.warnings.clear()
    return mod


def _setup_state(selection: Selection) -> state.SetupState:
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "litellm"
    for cid in ("claude", "openclaw"):
        s.cockpits[cid] = state.CockpitState(installed=True)
    s.set_selection(selection)
    return s


def _status(env: Env, monkeypatch, capsys, selection: Selection, agents: dict) -> str:
    env.doc["agents"]["entries"].update(agents)
    accounts = {"anthropic": ma.Account(True, ["synthetic"]), "google": ma.Account(True, ["env-file"])}
    deps = dataclasses.replace(env.deps(), selection=lambda: selection, read_config=lambda: env.doc,
                               accounts=lambda: accounts)
    monkeypatch.setattr(models_cmd, "get_deps", lambda: deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: env.overlay)
    assert cli.main(["models", "status"]) == 0
    return capsys.readouterr().out


# --- the walk --------------------------------------------------------------------------------------------------

def test_the_example_workflow_is_accepted_by_the_validator(vk):
    """The dispatch fields are valid: the same check `validate_kit.py` runs in CI."""
    for step in workflow_steps():
        vk.check_step_backend(step, f"{WORKFLOW.name}:{step['id']}")
    assert vk.failures == []


def test_dispatched_roles_are_exactly_the_roles_on_a_provider_claude_code_cannot_run_verified():
    steps = workflow_steps()
    assert dispatched(steps) == {"explore": "agy-pro", "planner": "agy-flash", "code-reviewer": "agy-flash"}
    sel = selection_for(PROFILE, AGENTS)
    assert disagreements(PROFILE, steps, AGENTS, sel) == []
    off_claude = {role for role, cfg in PROFILE["by_role"].items()
                  if not compat.cell("claude", model_providers.identify(cfg["model"]).provider,
                                     compat.MULTI_LITELLM).verified}
    assert off_claude == set(dispatched(steps))


def test_setup_planning_serves_claude_roles_on_claude_code_and_google_through_the_antigravity_engine():
    sel = selection_for(PROFILE, AGENTS, engine="antigravity")
    assert set(sel.enabled_providers()) == {"anthropic", "google"}

    rows = {a.cockpit: a for a in model_selection.plan_table(sel, "multi-model", "litellm", DETECTED)}
    claude = rows["claude"]
    assert claude.action == compat.VIA_GATEWAY and claude.unverified is False
    assert claude.model_refs and all(r.startswith("anthropic/") for r in claude.model_refs)
    assert claude.notes and all(n.startswith("google/") and n.endswith("not verified yet") for n in claude.notes)

    by_engine = {e: {a.cockpit: a for a in model_selection.plan_table(sel, "multi-model", "litellm", DETECTED,
                                                                     engine=e)}["openclaw"]
                 for e in ("claude-code", "antigravity")}
    cc = by_engine["claude-code"]
    assert cc.action == compat.CONFIGURE and all(r.startswith("anthropic/") for r in cc.model_refs)
    assert cc.notes and all(n.startswith("google/") and "Claude refs only" in n for n in cc.notes)
    agy = by_engine["antigravity"]
    assert agy.action == compat.CONFIGURE and agy.unverified is False
    assert agy.model_refs == [f"{model_pins.AGY_STATIC[0]} (Antigravity id)"]
    assert any("unrestricted code execution" in n for n in agy.notes)

    engines = compat.engines_for(sel, DETECTED)
    assert "antigravity" in engines and "claude-code" in engines


def test_the_setup_summary_and_models_status_name_the_same_providers(tmp_path, monkeypatch, capsys):
    steps = workflow_steps()
    sel = selection_for(PROFILE, AGENTS, engine="antigravity")

    script = Script()
    script.install(monkeypatch, non_interactive=True)
    s = _setup_state(sel)
    ns = argparse.Namespace(models="", shape="", providers="", smoke_path="", backend="", cockpits="",
                            allow_unverified=False)
    assert wizard._step_summary(s, ns) == 0
    summary = script.transcript
    for slot, slot_sel in sel.slots.items():
        assert f"{slot} = {slot_sel.ref} [family]" in summary
    claude_line = next(ln for ln in summary.splitlines() if "Claude Code: will configure" in ln)
    assert "google/" not in claude_line and "through LiteLLM" in claude_line
    assert "OpenClaw: will configure" in summary and "(Antigravity id)" in summary

    env = Env(tmp_path, monkeypatch, previous_generation=False)
    out = _status(env, monkeypatch, capsys, sel, AGENTS)
    head, rest = out.split("Providers\n", 1)
    block = rest.split("\n\n", 1)[0]
    listed = {ln.split()[0] for ln in block.splitlines() if ln.split()[1:2] == ["credentialed"]}
    assert listed == {"anthropic", "google"} == set(sel.enabled_providers())
    for role, agent_id in dispatched(steps).items():
        assert agent_provider(AGENTS[agent_id]["model"]["primary"]) in listed
    for slot, slot_sel in sel.slots.items():
        row = next(ln for ln in head.splitlines() if ln.strip().startswith(slot + " "))
        provider, bare = slot_sel.ref.split("/", 1)
        assert (bare.replace(".", "-") if provider == "anthropic" else bare) in row    # the id the summary shows
    # The agents' agy labels are not model refs the pins govern: status reports no drift for them.
    assert not [ln for ln in out.splitlines() if "DRIFT" in ln and "agents.entries.agy-" in ln]


def test_a_dispatch_to_an_agent_on_a_provider_with_no_engine_is_flagged():
    steps = workflow_steps()
    bad_agents = {"agy-flash": {"model": {"primary": "deepseek/deepseek-v4-pro"}}, "agy-pro": AGENTS["agy-pro"]}
    sel = selection_for(PROFILE, bad_agents)
    found = disagreements(PROFILE, steps, bad_agents, sel)
    assert any("no verified OpenClaw engine runs 'deepseek'" in f for f in found), found
    assert any("planner" in f and "deepseek" in f for f in found)


def test_a_dispatch_to_an_agent_the_host_does_not_have_is_flagged():
    steps = workflow_steps()
    missing = {"agy-pro": AGENTS["agy-pro"]}
    found = disagreements(PROFILE, steps, missing, selection_for(PROFILE, missing))
    assert "planner: agent 'agy-flash' is not configured on the host" in found


def test_a_google_role_that_is_not_dispatched_is_flagged():
    profile = {"by_role": {**PROFILE["by_role"], "verifier": {"model": "google/gemini-3.8-flash"}}}
    found = disagreements(profile, workflow_steps(), AGENTS, selection_for(profile, AGENTS))
    assert any(f.startswith("verifier:") and "not dispatched" in f for f in found), found
