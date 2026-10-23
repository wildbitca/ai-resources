"""One OpenClaw agent per kit role (ADR-0015 S8, acceptance 10): the profile's models, enabled providers only."""
from __future__ import annotations

import copy
import json
import re

import pytest

from ai_resources import repo_root
from ai_resources.setup import effort, profiles, state
from ai_resources.setup.cockpits import _openclaw_providers as P
from ai_resources.setup.cockpits import openclaw_roles as R

EFF = {"explore": "low", "planner": "high", "implementer": "medium", "verifier": "high", "tester": "low"}
CATALOG = {"openai": ("gpt-5.5", "gpt-5.5-mini"), "anthropic": ("claude-sonnet-5-5", "claude-haiku-5-5")}
EFFECTIVE = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5",
             "fable": "claude-fable-5-1"}


def balanced():
    return profiles.to_executors(profiles.load_profile("balanced", "single-model"))


def build(enabled=("anthropic",), env=None, **kw):
    ex = balanced()
    return R.build_agents(repo_root(), ex["by_role"], enabled=enabled, catalog=CATALOG,
                          effort_table=effort.table_for_state(_balanced_state()), ak_path="/kit",
                          by_persona=ex.get("by_persona"), env=env if env is not None else {}, effective=EFFECTIVE, **kw)


def _balanced_state():
    s = state.SetupState()
    s.profile.name = "balanced"
    return s


class Host:
    """A fake OpenClaw: records patches and registered agents; the config is whatever the test hands in."""

    def __init__(self, doc=None):
        self.doc = doc or {}
        self.patches: list[dict] = []
        self.registered: list[tuple] = []
        self.restart: list[str] = []
        self.records: list = []

    def apply(self, patch, dry_run=False, **k):
        if not dry_run:
            self.patches.append(copy.deepcopy(patch))
            _merge(self.doc, patch)
        return True, "ok"

    def register(self, aid, ws, model):
        self.registered.append((aid, str(ws), model))
        self.doc.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {"name": aid})
        return "added"


def _merge(dst, src):
    for k, v in src.items():
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


def run(s, host, tmp_path, *, enabled=("anthropic",), dry_run=False, methods=None, warns=None, env=None):
    ex = balanced()
    return R.configure(
        s, host.doc, config_root=tmp_path / "oc", by_role=ex["by_role"], by_persona={}, ak_path="/kit",
        kit_root=repo_root(), enabled=enabled, catalog=CATALOG, effort_table=effort.table_for_state(_balanced_state()),
        methods=methods or {"anthropic": "api_key"}, apply_patch=host.apply, register_agent=host.register,
        classify=lambda p: host.restart, change_records=lambda *a, **k: host.records.append(a) or [],
        run_changes=[], dry_run=dry_run, info=lambda m: None, warn=(warns.append if warns is not None else (lambda m: None)),
        error=lambda m: (_ for _ in ()).throw(AssertionError(m)), env=env if env is not None else {})


def test_fifteen_role_agents_carry_the_profile_models_and_the_mapped_thinking():
    agents, fell = build()
    assert len(agents) == 15 and fell == []
    by = {a["role"]: a for a in agents}
    assert by["planner"]["model"] == "anthropic/claude-sonnet-5-5" and by["planner"]["thinking"] == "high"
    assert by["verifier"]["model"] == "anthropic/claude-opus-5-5" and by["verifier"]["thinking"] == "high"
    assert by["explore"]["model"] == "anthropic/claude-haiku-5-5" and by["explore"]["thinking"] == "low"
    assert by["implementer"]["thinking"] == "medium"
    prof = balanced()["by_role"]
    for role, agent in by.items():
        assert P.role_provider(prof[role]) == "anthropic" and agent["model"].startswith("anthropic/")


def test_ids_are_valid_openclaw_agent_ids_and_unique():
    agents, _ = build(env={"AI_RESOURCES_SKILLS": "all"})
    ids = [a.id for a in agents]
    assert len(ids) == len(set(ids)) == 40
    assert all(re.fullmatch(r"[a-z][a-z0-9-]{0,30}", i) and i.startswith("kit-role-") for i in ids)
    assert R.agent_id_for("software-architect", "api-platform") == "kit-role-architect-api"
    assert R.agent_id_for("verifier") == "kit-role-verifier"


def test_persona_variants_exist_only_for_active_domains():
    lean = {a.id for a in build()[0]}
    sym = {a.id for a in build(env={"AI_RESOURCES_SKILLS": "symfony"})[0]}
    assert sorted(sym - lean) == ["kit-role-architect-symfony", "kit-role-implementer-symfony",
                                  "kit-role-planner-symfony", "kit-role-reviewer-symfony", "kit-role-tester-symfony"]


def test_each_agent_has_scoped_skills_and_its_prompt():
    by = {a["role"]: a for a in build()[0]}
    assert "kit-orchestration" in by["verifier"]["skills"] and "common-protocol-enforcement" in by["verifier"]["skills"]
    assert "/kit" in by["verifier"]["prompt"] and "$AGENT_KIT/" not in by["verifier"]["prompt"]


def test_a_role_whose_provider_is_not_enabled_runs_on_the_cheapest_enabled_one():
    agents, fell = build(enabled=("openai",))
    assert {a["model"] for a in agents} == {"openai/gpt-5.5-mini"}
    assert len(fell) == 15 and all(want == "anthropic" for _r, want, _t in fell)


def test_setup_writes_fifteen_agents_once_and_a_rerun_is_a_zero_diff(tmp_path):
    host = Host()
    s = state.SetupState()
    assert run(s, host, tmp_path) is True
    assert len(host.registered) == 15
    ids = {a for a, _, _ in host.registered}
    assert ids == {a.id for a in build()[0]}
    for aid, ws, model in host.registered:
        text = (tmp_path / "oc" / "kit-roles" / aid / "AGENTS.md").read_text()
        assert "Kit context" in text
    entries = host.doc["agents"]["entries"]
    assert entries["kit-role-planner"]["thinkingDefault"] == "high"
    assert entries["kit-role-planner"]["model"]["primary"] == "anthropic/claude-sonnet-5-5"
    assert "kit-orchestration" in entries["kit-role-planner"]["skills"]
    before = (copy.deepcopy(host.doc), copy.deepcopy(s.openclaw.role_agents_applied))
    n_patches = len(host.patches)
    assert run(s, host, tmp_path) is False
    assert (host.doc, s.openclaw.role_agents_applied) == before and len(host.patches) == n_patches


def test_a_profile_change_moves_the_kit_value_but_an_operator_edit_is_kept(tmp_path):
    host = Host()
    s = state.SetupState()
    run(s, host, tmp_path)
    host.doc["agents"]["entries"]["kit-role-verifier"]["model"]["primary"] = "anthropic/claude-sonnet-5-5"   # operator
    host.doc["agents"]["entries"]["kit-role-planner"]["thinkingDefault"] = "low"                          # operator
    warns: list[str] = []
    ex = balanced()
    ex["by_role"]["explore"]["model"] = "sonnet"                                                           # profile moved
    R_configure = R.configure
    R_configure(s, host.doc, config_root=tmp_path / "oc", by_role=ex["by_role"], by_persona={}, ak_path="/kit",
                kit_root=repo_root(), enabled=("anthropic",), catalog=CATALOG,
                effort_table=effort.table_for_state(_balanced_state()), methods={"anthropic": "api_key"},
                apply_patch=host.apply, register_agent=host.register, classify=lambda p: [],
                change_records=lambda *a, **k: [], run_changes=[], info=lambda m: None, warn=warns.append,
                error=lambda m: (_ for _ in ()).throw(AssertionError(m)), env={})
    entries = host.doc["agents"]["entries"]
    assert entries["kit-role-explore"]["model"]["primary"].startswith("anthropic/claude-sonnet")      # followed the profile
    assert entries["kit-role-verifier"]["model"]["primary"] == "anthropic/claude-sonnet-5-5"          # kept
    assert entries["kit-role-planner"]["thinkingDefault"] == "low"
    assert any("kit-role-verifier" in w for w in warns) and any("kit-role-planner" in w for w in warns)


def test_a_cli_provider_gets_its_runtime_in_the_catalog_fill_only(tmp_path):
    host = Host()
    s = state.SetupState()
    run(s, host, tmp_path, methods={"anthropic": "cli"})
    models = host.doc["agents"]["defaults"]["models"]
    assert models["anthropic/claude-sonnet-5-5"] == {"agentRuntime": {"id": "claude-cli"}}
    host2 = Host({"agents": {"defaults": {"models": {"anthropic/claude-sonnet-5-5": {"agentRuntime": {"id": "mine"}}}}}})
    run(state.SetupState(), host2, tmp_path / "b", methods={"anthropic": "cli"})
    assert host2.doc["agents"]["defaults"]["models"]["anthropic/claude-sonnet-5-5"] == {"agentRuntime": {"id": "mine"}}


def test_without_a_provider_answer_nothing_is_written(tmp_path):
    host = Host()
    assert run(state.SetupState(), host, tmp_path, enabled=None) is False
    assert host.registered == [] and host.patches == []


def test_a_dry_run_writes_nothing(tmp_path):
    host = Host()
    assert run(state.SetupState(), host, tmp_path, dry_run=True) is False
    assert host.registered == [] and host.patches == [] and not (tmp_path / "oc").exists()


def test_a_restart_class_key_is_deferred_never_applied(tmp_path):
    host = Host()
    host.restart = ["agents.entries.kit-role-planner"]
    warns: list[str] = []
    s = state.SetupState()
    assert run(s, host, tmp_path, warns=warns) is False
    assert host.patches == [] and s.openclaw.role_agents_applied["deferred"]
    assert any("NOT written" in w for w in warns)


def test_an_edited_agents_md_is_kept(tmp_path):
    host = Host()
    s = state.SetupState()
    run(s, host, tmp_path)
    path = tmp_path / "oc" / "kit-roles" / "kit-role-planner" / "AGENTS.md"
    path.write_text("mine\n")
    warns: list[str] = []
    run(s, host, tmp_path, warns=warns)
    assert path.read_text() == "mine\n" and any("AGENTS.md is kept" in w for w in warns)


def test_teardown_removes_only_kit_role_ids_after_confirmation(tmp_path):
    host = Host()
    s = state.SetupState()
    run(s, host, tmp_path, methods={"anthropic": "cli"})
    deleted: list[str] = []
    s.openclaw.role_agents_applied["created_agent_ids"].append("main")          # never a kit id: must be skipped
    assert R.teardown(s, host.doc, apply_patch=host.apply, oc=lambda argv: deleted.append(argv[2]) or (0, ""),
                      confirm=lambda m: True) is True
    assert "main" not in deleted and len(deleted) == 15 and all(d.startswith("kit-role-") for d in deleted)
    assert s.openclaw.role_agents_applied == {}
    assert not (tmp_path / "oc" / "kit-roles" / "kit-role-planner").exists()


def test_a_declined_delete_keeps_the_record(tmp_path):
    host = Host()
    s = state.SetupState()
    run(s, host, tmp_path)
    assert R.teardown(s, host.doc, apply_patch=host.apply, oc=lambda argv: (0, ""), confirm=lambda m: False) is True
    assert len(s.openclaw.role_agents_applied["created_agent_ids"]) == 15


def test_the_state_record_is_additive_and_secret_free():
    s = state.SetupState()
    assert "role_agents_applied" not in state._to_dict(s)["openclaw"]
    s.openclaw.role_agents_applied = {"created_agent_ids": ["kit-role-planner"]}
    assert state._to_dict(s)["openclaw"]["role_agents_applied"]["created_agent_ids"] == ["kit-role-planner"]


def test_the_engine_model_rewrite_skips_role_agents():
    doc = {"agents": {"entries": {"kit-role-planner": {"model": {"primary": "anthropic/claude-sonnet-5-5"}},
                                  "kit-lane-cli-x": {"model": {"primary": "x"}},
                                  "chat": {"model": {"primary": "old"}}}}}
    from ai_resources.setup.cockpits import openclaw as oc
    patch = oc.build_patch(oc.ENGINES["claude-code"], "claude-sonnet-5-5", doc, "/s", "engram", fallbacks=False)
    assert set(patch["agents"].get("entries", {})) == {"chat"}
    assert oc._is_kit_agent("kit-role-verifier") and oc._is_kit_agent("kit-lane-agy-x") and not oc._is_kit_agent("main")
