"""Kit agents run through Claude Code without moving the operator's agents (2.6.2).

OpenClaw resolves an agent's own exact `models` entry before `agents.defaults.models`, so the kit pins `claude-cli` on
the kit's agents (roles, claude-cli lanes) and, on a confirmed question, on the `claude` worker. The operator's agents
keep their route: the Anthropic API, where the Claude Platform credits of a Max plan apply.
"""
from __future__ import annotations

import copy

import pytest

from ai_resources.setup import state
from ai_resources.setup.cockpits import openclaw as cockpit
from ai_resources.setup.cockpits import openclaw_roles

SONNET, OPUS, HAIKU = "anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5", "anthropic/claude-haiku-5-5"
API = {"agentRuntime": {"id": "openclaw"}}
CLI = {"agentRuntime": {"id": "claude-cli"}}


def _doc(worker=None):
    return {"agents": {
        "defaults": {"models": {"anthropic/*": copy.deepcopy(API)}},
        "entries": {
            "main": {"model": {"primary": SONNET, "fallbacks": [OPUS]}},
            "claude": worker if worker is not None else {"model": {"primary": SONNET, "fallbacks": [OPUS, HAIKU]}},
        },
    }}


def _state(installed=True, live="subscription"):
    s = state.SetupState()
    s.cockpits["claude"] = state.CockpitState(installed=installed)
    if live:
        s.cockpit_auth = {"claude": {"live": live}}
    return s


# --- which agents may run on a CLI -------------------------------------------------------------------------------------

def test_claude_cli_is_offered_only_when_claude_code_is_installed_and_not_signed_out():
    assert cockpit.kit_cli_runtimes(_state()) == ("claude-cli",)
    assert cockpit.kit_cli_runtimes(_state(live="")) == ("claude-cli",)          # never read: claude-cli fails loudly
    assert cockpit.kit_cli_runtimes(_state(live="none")) == ()
    assert cockpit.kit_cli_runtimes(_state(installed=False)) == ()


# --- the worker --------------------------------------------------------------------------------------------------------

def test_the_worker_lists_every_anthropic_ref_on_openclaws_own_route():
    assert cockpit.worker_api_refs(_doc()) == [SONNET, OPUS, HAIKU]


def test_a_ref_the_operator_pinned_on_the_worker_or_already_on_claude_cli_is_left_out():
    worker = {"model": {"primary": SONNET, "fallbacks": [OPUS, HAIKU, "agy-cli/gemini-4-argon-high"]},
              "models": {OPUS: {"agentRuntime": {"id": "openclaw"}}}}
    doc = _doc(worker)
    doc["agents"]["defaults"]["models"][HAIKU] = copy.deepcopy(CLI)
    assert cockpit.worker_api_refs(doc) == [SONNET]


def test_no_worker_means_nothing_to_offer():
    doc = _doc()
    del doc["agents"]["entries"]["claude"]
    assert cockpit.worker_api_refs(doc) == []


def test_the_worker_patch_touches_only_the_workers_own_models_map():
    patch = cockpit.worker_runtime_patch([SONNET, OPUS])
    assert patch == {"agents": {"entries": {"claude": {"models": {SONNET: CLI, OPUS: CLI}}}}}


class _Oc:
    def __init__(self):
        self.applied: list[dict] = []

    def apply(self, patch, dry_run=False, **k):
        if not dry_run:
            self.applied.append(copy.deepcopy(patch))
        return True, "ok"


@pytest.fixture
def oc(monkeypatch):
    fake = _Oc()
    monkeypatch.setattr(cockpit, "_lanes_apply", fake.apply)
    monkeypatch.setattr(cockpit, "_lanes_classify", lambda p: [])
    monkeypatch.setattr(cockpit.ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(cockpit.ui, "info", lambda m: None)
    return fake


def test_a_yes_pins_the_worker_and_records_the_change(oc, monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(cockpit.ui, "confirm", lambda m, default=False, **k: asked.append(m) or True)
    changes: list[dict] = []
    assert cockpit._configure_worker_runtime(_state(), _doc(), dry_run=False, run_changes=changes) is True
    assert oc.applied == [cockpit.worker_runtime_patch([SONNET, OPUS, HAIKU])]
    assert changes and all(c["action"] == "worker" for c in changes)
    assert len(asked) == 1


def test_the_default_answer_changes_nothing(oc, monkeypatch):
    monkeypatch.setattr(cockpit.ui, "confirm", lambda m, default=False, **k: default)
    assert cockpit._configure_worker_runtime(_state(), _doc(), dry_run=False, run_changes=[]) is False
    assert oc.applied == []


@pytest.mark.parametrize("mode", ["dry_run", "non_interactive", "no_claude_code"])
def test_the_worker_is_never_moved_unasked(oc, monkeypatch, mode):
    monkeypatch.setattr(cockpit.ui, "confirm", lambda m, default=False, **k: pytest.fail("asked"))
    if mode == "non_interactive":
        monkeypatch.setattr(cockpit.ui, "is_non_interactive", lambda: True)
    s = _state(installed=mode != "no_claude_code")
    assert cockpit._configure_worker_runtime(s, _doc(), dry_run=mode == "dry_run", run_changes=[]) is False
    assert oc.applied == []


def test_a_restart_class_key_is_deferred_not_written(oc, monkeypatch):
    monkeypatch.setattr(cockpit.ui, "confirm", lambda m, default=False, **k: True)
    monkeypatch.setattr(cockpit, "_lanes_classify", lambda p: ["agents.entries.claude.models"])
    monkeypatch.setattr(cockpit.ui, "warn", lambda m: None)
    assert cockpit._configure_worker_runtime(_state(), _doc(), dry_run=False, run_changes=[]) is False
    assert oc.applied == []


# --- roles: a model the kit moves away from loses the kit's pin --------------------------------------------------------

def test_a_role_that_changes_model_drops_the_old_pin_and_pins_the_new_one(tmp_path):
    agent = openclaw_roles.RoleAgent(id="kit-role-planner", role="planner", name="planner", domain="",
                                     description="d", model=OPUS, thinking="", skills=[], prompt="p",
                                     prompt_sha256="x")
    doc = {"agents": {"entries": {"kit-role-planner": {"model": {"primary": SONNET}, "models": {SONNET: CLI}}}}}
    recorded = {"created_agent_ids": ["kit-role-planner"], "models": {"kit-role-planner": SONNET}, "thinking": {}}
    patch, kept = openclaw_roles.build_patch([agent], doc, {"kit-role-planner": tmp_path}, recorded,
                                             {"anthropic": "api_key"}, ("claude-cli",))
    assert kept == []
    ent = patch["agents"]["entries"]["kit-role-planner"]
    assert ent["model"] == {"primary": OPUS}
    assert ent["models"] == {SONNET: None, OPUS: CLI}
    assert "defaults" not in patch["agents"]


# --- the one question pins the worker; kit-lane agents are no longer touched (ADR-0019) ---------------------------

def test_the_one_question_pins_the_worker_and_leaves_kit_lane_agents_alone(oc, monkeypatch):
    monkeypatch.setattr(cockpit.ui, "confirm", lambda m, default=False, **k: True)
    doc = _doc()
    doc["agents"]["entries"]["kit-lane-cli-haiku55"] = {"model": {"primary": HAIKU}}
    assert cockpit._configure_worker_runtime(_state(), doc, dry_run=False, run_changes=[]) is True
    assert oc.applied == [cockpit.kit_runtime_patch({"claude": [SONNET, OPUS, HAIKU]})]
    assert not hasattr(cockpit, "kit_api_agents") and not hasattr(cockpit, "lane_primary_repairs")


def test_the_recommended_run_moves_the_worker_without_asking(oc, monkeypatch):
    monkeypatch.setattr(cockpit.ui, "detail", lambda m: None)
    cockpit.ui.set_auto(True)
    try:
        assert cockpit._configure_worker_runtime(_state(), _doc(), dry_run=False, run_changes=[]) is True
    finally:
        cockpit.ui.set_auto(False)
    assert oc.applied == [cockpit.worker_runtime_patch([SONNET, OPUS, HAIKU])]


# --- 2.6.5: a role spelled as a bare model string is the kit's, not the operator's ---------------------------------

def _role(model=SONNET, thinking=""):
    return openclaw_roles.RoleAgent(id="kit-role-reviewer", role="code-reviewer", name="reviewer", domain="",
                                    description="d", model=model, thinking=thinking, skills=[], prompt="p",
                                    prompt_sha256="x")


def test_a_role_whose_model_is_a_bare_string_still_gets_its_claude_cli_pin(tmp_path):
    doc = {"agents": {"entries": {"kit-role-reviewer": {"model": SONNET}}}}
    recorded = {"created_agent_ids": ["kit-role-reviewer"], "models": {"kit-role-reviewer": SONNET}, "thinking": {}}
    patch, kept = openclaw_roles.build_patch([_role()], doc, {"kit-role-reviewer": tmp_path}, recorded,
                                             {"anthropic": "api_key"}, ("claude-cli",))
    assert kept == []
    # the bare string is also respelled as the object form, which clears verify's warning
    assert patch == {"agents": {"entries": {"kit-role-reviewer": {"model": {"primary": SONNET}, "models": {SONNET: CLI},
                                                                  "tools": {"deny": ["bundle-mcp"]},
                                                                  "subagents": {"model": {"primary": SONNET}}}}}}


def test_a_spawned_kit_role_runs_its_own_matrix_chain_not_the_hosts_default_subagent_model(tmp_path):
    """`agents.defaults.subagents.model` outranks the target agent's own `model` for a sessions_spawn; the role's entry pins
    its chain in `subagents.model`, which outranks the default. Fill-only: an operator's own value stays."""
    role = _role(OPUS)
    doc = {"agents": {"defaults": {"subagents": {"model": SONNET}},
                      "entries": {"kit-role-reviewer": {"model": {"primary": OPUS}, "workspace": str(tmp_path),
                                                        "tools": {"deny": ["bundle-mcp"]}, "models": {OPUS: CLI}}}}}
    recorded = {"created_agent_ids": ["kit-role-reviewer"], "models": {"kit-role-reviewer": OPUS}, "thinking": {}}
    patch, _ = openclaw_roles.build_patch([role], doc, {"kit-role-reviewer": tmp_path}, recorded, {"anthropic": "api_key"},
                                          ("claude-cli",))
    assert patch["agents"]["entries"]["kit-role-reviewer"]["subagents"] == {"model": {"primary": OPUS}}
    # already pinned to the chain: nothing to send; pinned by the operator to something else: left alone
    doc["agents"]["entries"]["kit-role-reviewer"]["subagents"] = {"model": {"primary": OPUS}}
    assert "subagents" not in (openclaw_roles.build_patch([role], doc, {"kit-role-reviewer": tmp_path}, recorded,
                                                          {"anthropic": "api_key"}, ("claude-cli",))[0]
                               .get("agents", {}).get("entries", {}).get("kit-role-reviewer", {}))
    doc["agents"]["entries"]["kit-role-reviewer"]["subagents"] = {"model": "anthropic/claude-haiku-5-5"}
    assert "subagents" not in (openclaw_roles.build_patch([role], doc, {"kit-role-reviewer": tmp_path}, recorded,
                                                          {"anthropic": "api_key"}, ("claude-cli",))[0]
                               .get("agents", {}).get("entries", {}).get("kit-role-reviewer", {}))


def test_a_bare_string_role_the_kit_wrote_follows_a_profile_change(tmp_path):
    doc = {"agents": {"entries": {"kit-role-reviewer": {"model": SONNET}}}}
    recorded = {"created_agent_ids": ["kit-role-reviewer"], "models": {"kit-role-reviewer": SONNET}, "thinking": {}}
    patch, kept = openclaw_roles.build_patch([_role(OPUS, "high")], doc, {"kit-role-reviewer": tmp_path}, recorded,
                                             {"anthropic": "api_key"}, ("claude-cli",))
    assert kept == []
    ent = patch["agents"]["entries"]["kit-role-reviewer"]
    assert ent["model"] == {"primary": OPUS} and ent["thinkingDefault"] == "high"
    assert ent["models"] == {OPUS: CLI}


def test_the_bare_entry_agents_add_left_behind_gets_the_kits_fields(tmp_path):
    shell = {"name": "kit-role-reviewer", "identity": {"name": "kit-role-reviewer"}, "workspace": str(tmp_path),
             "agentDir": "/x/agent", "model": SONNET}
    doc = {"agents": {"entries": {"kit-role-reviewer": shell}}}
    patch, kept = openclaw_roles.build_patch([_role(SONNET, "medium")], doc, {"kit-role-reviewer": tmp_path},
                                             {}, {"anthropic": "api_key"}, ("claude-cli",))
    assert kept == []
    ent = patch["agents"]["entries"]["kit-role-reviewer"]
    assert ent["thinkingDefault"] == "medium" and ent["models"] == {SONNET: CLI} and ent["description"] == "d"
    assert ent["model"] == {"primary": SONNET} and "name" not in ent


def test_an_entry_the_operator_shaped_is_not_a_shell(tmp_path):
    live = {"name": "kit-role-reviewer", "workspace": str(tmp_path), "model": OPUS}
    doc = {"agents": {"entries": {"kit-role-reviewer": live}}}
    patch, kept = openclaw_roles.build_patch([_role(SONNET, "medium")], doc, {"kit-role-reviewer": tmp_path},
                                             {}, {"anthropic": "api_key"}, ("claude-cli",))
    assert kept == ["kit-role-reviewer"] and patch == {}


def test_an_identity_the_operator_set_keeps_the_entry_theirs(tmp_path):
    live = {"name": "kit-role-reviewer", "identity": {"name": "Rev", "emoji": "x"}, "workspace": str(tmp_path),
            "model": SONNET}
    doc = {"agents": {"entries": {"kit-role-reviewer": live}}}
    _, kept = openclaw_roles.build_patch([_role(SONNET, "medium")], doc, {"kit-role-reviewer": tmp_path},
                                         {}, {"anthropic": "api_key"}, ("claude-cli",))
    assert kept == ["kit-role-reviewer"]


def test_an_operator_owned_role_gets_a_spawn_pin_that_follows_its_own_model(tmp_path):
    doc = {"agents": {"defaults": {"subagents": {"model": SONNET}},
                      "entries": {"kit-role-reviewer": {"model": {"primary": SONNET}, "workspace": str(tmp_path),
                                                        "tools": {"deny": ["bundle-mcp"]}, "models": {SONNET: CLI}}}}}
    recorded = {"created_agent_ids": ["kit-role-reviewer"], "models": {}, "thinking": {}}
    patch, kept = openclaw_roles.build_patch([_role(OPUS)], doc, {"kit-role-reviewer": tmp_path}, recorded,
                                             {"anthropic": "api_key"}, ("claude-cli",))
    assert kept == ["kit-role-reviewer"]
    assert patch["agents"]["entries"]["kit-role-reviewer"]["subagents"] == {"model": {"primary": SONNET}}
