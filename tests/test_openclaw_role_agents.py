"""One OpenClaw agent per kit role (ADR-0015 S8, acceptance 10): model and fallbacks of its row in the routing matrix."""
from __future__ import annotations

import copy
import json
import re

import pytest

from ai_resources import repo_root
from ai_resources.setup import effort, profiles, state
from ai_resources.setup.cockpits import openclaw_roles as R

EFF = {"explore": "low", "planner": "high", "implementer": "medium", "verifier": "high", "tester": "low"}
CATALOG = {"openai": ("gpt-5.5", "gpt-5.5-mini"), "anthropic": ("claude-sonnet-5-5", "claude-haiku-5-5")}
EFFECTIVE = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5",
             "fable": "claude-fable-5-1"}


def balanced():
    return profiles.to_executors(profiles.load_profile("balanced", "single-model"))


def build(enabled=("anthropic",), env=None, **kw):
    ex = balanced()
    return R.build_agents(repo_root(), enabled=enabled, catalog=CATALOG,
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


def run(s, host, tmp_path, *, enabled=("anthropic",), dry_run=False, methods=None, warns=None, env=None,
        cli_runtimes=(), infos=None, profile_doc=None, agy_ok=None):
    return R.configure(
        s, host.doc, config_root=tmp_path / "oc", profile_doc=profile_doc, by_persona={}, ak_path="/kit",
        kit_root=repo_root(), enabled=enabled, catalog=CATALOG, effort_table=effort.table_for_state(_balanced_state()),
        methods=methods or {"anthropic": "api_key"}, apply_patch=host.apply, register_agent=host.register,
        classify=lambda p: host.restart, change_records=lambda *a, **k: host.records.append(a) or [],
        run_changes=[], dry_run=dry_run, info=(infos.append if infos is not None else (lambda m: None)), warn=(warns.append if warns is not None else (lambda m: None)),
        error=lambda m: (_ for _ in ()).throw(AssertionError(m)), env=env if env is not None else {},
        cli_runtimes=cli_runtimes, agy_ok=agy_ok)


def test_fifteen_role_agents_carry_the_matrix_primary_and_fallbacks_and_the_mapped_thinking():
    agents, fell = build()
    assert len(agents) == 15 and fell == []
    by = {a["role"]: a for a in agents}
    assert by["planner"]["model"] == "anthropic/claude-sonnet-5-5" and by["planner"]["thinking"] == "high"
    assert by["planner"]["fallbacks"] == ["agy-cli-lane/gemini-3.8-flash-high"]
    # checkers: the dispatcher picks per run; the agent starts at the cheapest rung and keeps the rest as fallbacks
    assert by["verifier"]["model"] == "anthropic/claude-sonnet-5-5" and by["verifier"]["thinking"] == "high"
    assert by["verifier"]["fallbacks"] == ["anthropic/claude-opus-5-5", "agy-cli-lane/claude-opus-5-5"]
    # agy primaries (D4): the Antigravity slug first, the Max cell as the recorded fallback
    assert by["explore"]["model"] == "agy-cli-lane/gemini-3.8-flash-high" and by["explore"]["thinking"] == "low"
    assert by["explore"]["fallbacks"] == ["anthropic/claude-haiku-5-5"]
    assert by["tester"]["model"] == "agy-cli-lane/gemini-3.8-flash-high" and by["tester"]["thinking"] == "low"
    assert by["implementer"]["thinking"] == "medium"
    assert by["implementer"]["model"] == "anthropic/claude-sonnet-5-5"


def test_a_profile_routing_override_moves_the_agent_chain():
    agents, _ = build(profile_doc={"routing": {"explore": {"primary": "claude-code:opus", "fallback": ["claude-code:haiku"]}}})
    explore = next(a for a in agents if a["role"] == "explore")
    assert explore["model"] == "anthropic/claude-opus-5-5" and explore["fallbacks"] == ["anthropic/claude-haiku-5-5"]


def test_an_agy_cell_that_is_not_admitted_leaves_the_chain_and_is_reported():
    agents, fell = build(agy_ok=lambda role: False)
    by = {a["role"]: a for a in agents}
    assert by["explore"]["model"] == "anthropic/claude-haiku-5-5" and by["explore"]["fallbacks"] == []
    assert by["planner"]["model"] == "anthropic/claude-sonnet-5-5" and by["planner"]["fallbacks"] == []
    assert not any("agy-cli-lane" in ref for a in agents for ref in [a["model"], *a["fallbacks"]])
    assert {r for r, want, _t in fell} >= {"explore", "tester", "planner", "verifier"} and all(w == "agy" for _r, w, _t in fell)


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


def test_the_matrix_is_not_restricted_by_the_enabled_providers_only_personas_are():
    agents, fell = build(enabled=("openai",))
    assert fell == [] and {a["model"] for a in agents if a["role"] == "planner"} == {"anthropic/claude-sonnet-5-5"}


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
    host.doc["agents"]["entries"]["kit-role-verifier"]["model"]["primary"] = "anthropic/claude-opus-5-5"     # operator
    host.doc["agents"]["entries"]["kit-role-planner"]["thinkingDefault"] = "low"                          # operator
    warns: list[str] = []
    moved = {"routing": {"explore": {"primary": "claude-code:sonnet"}}}                                    # profile moved
    R_configure = R.configure
    R_configure(s, host.doc, config_root=tmp_path / "oc", profile_doc=moved, by_persona={}, ak_path="/kit",
                kit_root=repo_root(), enabled=("anthropic",), catalog=CATALOG,
                effort_table=effort.table_for_state(_balanced_state()), methods={"anthropic": "api_key"},
                apply_patch=host.apply, register_agent=host.register, classify=lambda p: [],
                change_records=lambda *a, **k: [], run_changes=[], info=lambda m: None, warn=warns.append,
                error=lambda m: (_ for _ in ()).throw(AssertionError(m)), env={})
    entries = host.doc["agents"]["entries"]
    assert entries["kit-role-explore"]["model"]["primary"].startswith("anthropic/claude-sonnet")      # followed the profile
    assert entries["kit-role-verifier"]["model"]["primary"] == "anthropic/claude-opus-5-5"            # kept
    assert entries["kit-role-planner"]["thinkingDefault"] == "low"
    assert any("kit-role-verifier" in w for w in warns) and any("kit-role-planner" in w for w in warns)


def _pins(doc):
    return {aid: e["models"] for aid, e in doc["agents"]["entries"].items() if "models" in e}


def test_a_cli_provider_gets_its_runtime_in_each_role_agent_and_never_in_the_shared_catalog(tmp_path):
    host = Host({"agents": {"defaults": {"models": {"anthropic/*": {"agentRuntime": {"id": "openclaw"}}}}}})
    run(state.SetupState(), host, tmp_path, methods={"anthropic": "cli"})
    assert host.doc["agents"]["defaults"]["models"] == {"anthropic/*": {"agentRuntime": {"id": "openclaw"}}}
    pins = _pins(host.doc)
    assert pins and all(aid.startswith("kit-role-") for aid in pins)
    for aid, models in pins.items():
        model = host.doc["agents"]["entries"][aid]["model"]
        chain = [model["primary"], *model.get("fallbacks", [])]
        # every Max cell of the chain runs through claude-cli; an Antigravity ref is never pinned
        assert models == {r: {"agentRuntime": {"id": "claude-cli"}} for r in chain if r.startswith("anthropic/")}, aid


def test_a_role_on_claude_runs_through_claude_code_when_it_is_signed_in_even_with_an_api_key_in_openclaw(tmp_path):
    host = Host()
    run(state.SetupState(), host, tmp_path, methods={"anthropic": "api_key"}, cli_runtimes=("claude-cli",))
    pins = _pins(host.doc)
    assert pins and {r["agentRuntime"]["id"] for m in pins.values() for r in m.values()} == {"claude-cli"}


def test_without_a_cli_sign_in_a_role_keeps_openclaws_own_route(tmp_path):
    host = Host()
    run(state.SetupState(), host, tmp_path, methods={"anthropic": "api_key"})
    assert _pins(host.doc) == {}


def test_an_operator_pin_on_a_role_agent_is_kept(tmp_path):
    host = Host()
    run(state.SetupState(), host, tmp_path, methods={"anthropic": "cli"})
    aid, models = next(iter(_pins(host.doc).items()))
    ref = next(iter(models))
    host.doc["agents"]["entries"][aid]["models"] = {ref: {"agentRuntime": {"id": "mine"}}}
    s = state.SetupState()
    run(s, host, tmp_path, methods={"anthropic": "cli"})
    assert host.doc["agents"]["entries"][aid]["models"] == {ref: {"agentRuntime": {"id": "mine"}}}


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


def test_report_role_agents_cannot_edit_files_and_writer_roles_can(tmp_path):
    host = Host()
    assert run(state.SetupState(), host, tmp_path) is True
    entries = host.doc["agents"]["entries"]
    for aid in ("kit-role-verifier", "kit-role-reviewer", "kit-role-security"):
        assert entries[aid]["tools"] == {"deny": ["write", "edit", "apply_patch", "bundle-mcp"]}, aid
    for aid in ("kit-role-planner", "kit-role-architect", "kit-role-explore"):
        assert entries[aid]["tools"] == {"deny": ["write", "edit", "apply_patch", "exec", "process", "bundle-mcp"]}, aid
    assert entries["kit-role-implementer"]["tools"] == {"deny": ["bundle-mcp"]}      # no MCP need: the resource guard only


def test_an_earlier_kit_deny_list_moves_to_the_no_exec_one_and_an_operator_list_is_kept(tmp_path):
    host = Host()
    assert run(state.SetupState(), host, tmp_path) is True
    entries = host.doc["agents"]["entries"]
    entries["kit-role-planner"]["tools"] = {"deny": ["write", "edit", "apply_patch"]}      # what 2.8.1 wrote
    entries["kit-role-architect"]["tools"] = {"deny": ["write"]}                           # the operator's
    entries["kit-role-reviewer"]["tools"] = {"deny": ["write", "edit", "apply_patch"]}     # already today's list
    assert run(state.SetupState(), host, tmp_path) is True
    assert entries["kit-role-planner"]["tools"] == {"deny": ["write", "edit", "apply_patch", "exec", "process", "bundle-mcp"]}
    assert entries["kit-role-architect"]["tools"] == {"deny": ["write", "bundle-mcp"]}          # the operator's token is kept
    assert entries["kit-role-reviewer"]["tools"] == {"deny": ["write", "edit", "apply_patch", "bundle-mcp"]}


def test_each_new_agent_is_announced_before_its_slow_registration_and_a_rerun_announces_none(tmp_path):
    s, host = _balanced_state(), Host()
    infos: list[str] = []
    assert run(s, host, tmp_path, infos=infos) is True
    progress = [m for m in infos if m.lstrip().startswith("(")]
    assert infos[0].startswith("Kit roles: adding 15 agent(s)")
    assert len(progress) == 15 and progress[0].strip().startswith("(1/15) kit-role-")
    again: list[str] = []
    run(s, host, tmp_path, infos=again)
    assert not [m for m in again if "adding" in m or m.lstrip().startswith("(")]


def test_the_written_entry_has_the_native_model_object_with_fallbacks(tmp_path):
    host = Host()
    run(state.SetupState(), host, tmp_path)
    entries = host.doc["agents"]["entries"]
    assert entries["kit-role-explore"]["model"] == {"primary": "agy-cli-lane/gemini-3.8-flash-high",
                                                    "fallbacks": ["anthropic/claude-haiku-5-5"]}
    assert entries["kit-role-implementer"]["model"] == {"primary": "anthropic/claude-sonnet-5-5",
                                                        "fallbacks": ["agy-cli-lane/gemini-3.8-flash-high"]}
    stub = host.doc["models"]["providers"]["agy-cli-lane"]
    assert {m["id"] for m in stub["models"]} == {"gemini-3.8-flash-high", "claude-opus-5-5"}


def test_an_agent_the_kit_wrote_without_fallbacks_gains_them_and_an_operator_chain_is_kept(tmp_path):
    host = Host()
    s = state.SetupState()
    run(s, host, tmp_path)
    entries = host.doc["agents"]["entries"]
    last = s.openclaw.role_agents_applied
    entries["kit-role-planner"]["model"] = {"primary": "anthropic/claude-sonnet-5-5"}          # what an older kit wrote
    last["fallbacks"].pop("kit-role-planner")
    entries["kit-role-reviewer"]["model"]["fallbacks"] = ["anthropic/claude-opus-5-5"]         # the operator's chain
    run(s, host, tmp_path)
    assert entries["kit-role-planner"]["model"]["fallbacks"] == ["agy-cli-lane/gemini-3.8-flash-high"]
    assert entries["kit-role-reviewer"]["model"]["fallbacks"] == ["anthropic/claude-opus-5-5"]


# --- the "kept" note: silent when nothing differs, actionable when it does ---------------------------------------------------

def _agent(model="anthropic/claude-haiku-5-5", thinking="low"):
    return R.RoleAgent(id="kit-role-explore", model=model, thinking=thinking, fallbacks=[])


def test_kept_note_is_silent_when_the_live_values_equal_what_the_kit_would_write():
    live = {"model": {"primary": "anthropic/claude-haiku-5-5"}, "thinkingDefault": "low"}
    assert R.kept_note("kit-role-explore", live, _agent()) == ""
    assert R.kept_note("kit-role-explore", {"model": "anthropic/claude-haiku-5-5", "thinkingDefault": "low"}, _agent()) == ""


def test_kept_note_names_both_values_the_spawn_model_and_the_commands():
    live = {"model": {"primary": "anthropic/claude-sonnet-5-5"}, "thinkingDefault": "low",
            "subagents": {"model": {"primary": "anthropic/claude-opus-5-5"}}}
    note = R.kept_note("kit-role-explore", live, _agent())
    assert "claude-sonnet-5-5" in note and "claude-haiku-5-5" in note
    assert "a spawn of it runs on anthropic/claude-opus-5-5" in note
    assert "openclaw config set agents.entries.kit-role-explore.model.primary anthropic/claude-haiku-5-5" in note
    assert "thinking" not in note.split("; to follow")[0].replace("(the kit changes only values it wrote)", "")


def test_kept_note_covers_a_thinking_only_difference():
    live = {"model": {"primary": "anthropic/claude-haiku-5-5"}, "thinkingDefault": "high"}
    note = R.kept_note("kit-role-explore", live, _agent())
    assert "thinking high (matrix row: low)" in note and "thinkingDefault low" in note and "model.primary" not in note


def test_configure_reconciles_an_unrecorded_kit_role_agent_and_shows_the_diff(tmp_path):
    """An agent setup did not create is moved to its matrix row (model fields only); a dry run shows the diff, writes nothing."""
    host = Host({"agents": {"entries": {"kit-role-explore": {"model": {"primary": "anthropic/claude-sonnet-5-5"},
                                                             "thinkingDefault": "xhigh"}}}})
    infos: list[str] = []
    run(_balanced_state(), host, tmp_path, dry_run=True, infos=infos)
    assert host.patches == [] and any("kit-role-explore: model.primary anthropic/claude-sonnet-5-5 ->" in m for m in infos)
    run(_balanced_state(), host, tmp_path)
    ent = host.doc["agents"]["entries"]["kit-role-explore"]
    assert ent["model"]["primary"] != "anthropic/claude-sonnet-5-5" and ent["thinkingDefault"] == "xhigh"


def test_a_bare_string_model_with_the_right_primary_but_missing_fallbacks_is_rewritten_on_an_unrecorded_agent(tmp_path):
    """An unrecorded kit-role agent whose model is the bare string `agents add` writes (right primary, no fallbacks)
    still gets the matrix chain: the primary alone is not the whole row."""
    host = Host({"agents": {"entries": {"kit-role-planner": {"model": "anthropic/claude-sonnet-5-5"}}}})
    run(_balanced_state(), host, tmp_path)
    assert host.doc["agents"]["entries"]["kit-role-planner"]["model"] == {
        "primary": "anthropic/claude-sonnet-5-5", "fallbacks": ["agy-cli-lane/gemini-3.8-flash-high"]}


def test_an_unrecorded_agent_already_on_the_matrix_chain_is_left_alone(tmp_path):
    chain = {"primary": "anthropic/claude-sonnet-5-5", "fallbacks": ["agy-cli-lane/gemini-3.8-flash-high"]}
    host = Host({"agents": {"entries": {"kit-role-planner": {"model": copy.deepcopy(chain), "thinkingDefault": "high"}}}})
    run(_balanced_state(), host, tmp_path)
    assert host.doc["agents"]["entries"]["kit-role-planner"]["model"] == chain
