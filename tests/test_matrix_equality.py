"""ADR-0019 D2: the routing matrix is the one table. Every artifact generated from it equals it, and a profile
`routing:` override reaches all of them.

Artifacts: `lanes.yaml`, the `model:` block of `agents/roles/*.md`, the `kit-role-*` entries of the OpenClaw config
(`build_patch`) and the setup table rows."""
from __future__ import annotations

import pytest
import yaml

from ai_resources import generate, repo_root
from ai_resources.scoring import routing
from ai_resources.setup import lanes_render, profiles, state
from ai_resources.setup.cockpits import openclaw_roles as R

EFFECTIVE = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
OVERRIDE = {"routing": {"tester": {"primary": "claude-code:opus", "fallback": ["claude-code:sonnet"]},
                        "implementer": {"primary": "agy:gemini-4-argon-high", "fallback": ["claude-code:sonnet"]},
                        "verifier": {"primary": "claude-code:opus"}}}
TEAM = sorted(r.name for r in routing.ROLES.values() if r.kind == "team")


def _lanes_yaml(profile_doc=None):
    s = state.SetupState()
    s.mode = "single-model"
    s.profile.lanes = lanes_render.lanes_state(profile_doc or {}, orchestrators=["main"])
    return yaml.safe_load(lanes_render.expected_text(s))


def _agents(profile_doc=None):
    ex = profiles.to_executors(profiles.load_profile("balanced", "single-model"))
    agents, _ = R.build_agents(repo_root(), profile_doc=profile_doc, enabled=("anthropic",), catalog={},
                               effort_table={}, ak_path="/kit", by_persona=ex.get("by_persona"), env={},
                               effective=EFFECTIVE)
    return {a["role"]: a for a in agents if not a["domain"]}


def _patch_entries(profile_doc=None):
    agents = list(_agents(profile_doc).values())
    ws = {a.id: __import__("pathlib").Path("/ws") / a.id for a in agents}
    patch, kept = R.build_patch(agents, {}, ws, {}, {"anthropic": "cli"}, ())
    assert kept == []
    return patch["agents"]["entries"]


def _expected_chain(role, profile_doc=None):
    return routing.openclaw_chain(routing.merged(profile_doc)[role], EFFECTIVE)


# --- the four artifacts equal the matrix --------------------------------------------------------------------------------

def test_lanes_yaml_equals_the_matrix():
    doc = _lanes_yaml()
    assert doc["routing"] == routing.render_section()
    for name, role in routing.ROLES.items():
        row = doc["routing"]["matrix"][name]
        assert row["primary"]["cell"] == role.chain[0]
        assert [f["cell"] for f in row["fallback"]] == list(role.chain[1:])
        assert row["class"] == role.klass and all(c["pool"] == routing.pool_of(c["cell"]) for c in [row["primary"], *row["fallback"]])


@pytest.mark.parametrize("role", TEAM)
def test_the_claude_agent_definition_equals_the_matrix(role):
    path = repo_root() / "agents" / "roles" / f"{role}.md"
    text = path.read_text(encoding="utf-8")
    block = generate.role_model_block(role)
    assert block in text, f"{path.name}: run `ai-resources generate` (the routing block is generated, never hand edited)"
    assert generate.apply_role_model_block(text, block) == text           # idempotent: nothing left to sync
    meta, _ = generate.parse_simple_frontmatter(text)
    row = routing.ROLES[role]
    first_max = next((c for c in row.chain if routing.runtime_of_cell(c)), "claude-code")
    assert meta["model"] == routing.claude_alias(first_max)
    assert f"# route: {'checker ladder: ' if row.checker else ''}" in text


def test_every_team_role_has_a_claude_agent_file_and_no_other_role_has_one():
    files = sorted(p.stem for p in (repo_root() / "agents" / "roles").glob("*.md"))
    assert files == TEAM


def test_a_hand_edited_routing_block_is_detected():
    role = "explore"
    text = (repo_root() / "agents" / "roles" / f"{role}.md").read_text(encoding="utf-8")
    edited = text.replace("model: haiku", "model: opus")
    assert edited != text and generate.apply_role_model_block(edited, generate.role_model_block(role)) == text


@pytest.mark.parametrize("role", TEAM)
def test_the_kit_role_entry_equals_the_matrix(role):
    entries = _patch_entries()
    agent = _agents()[role]
    primary, fallbacks = _expected_chain(role)
    entry = entries[agent.id]
    assert entry["model"]["primary"] == primary
    assert entry["model"].get("fallbacks", []) == list(fallbacks)
    # a Max cell is pinned to the claude-cli runtime, an Antigravity cell never is
    chain = [primary, *fallbacks]
    assert entry.get("models", {}) == {r: {"agentRuntime": {"id": "claude-cli"}} for r in chain if r.startswith("anthropic/")}


def test_agy_cells_map_to_the_agy_provider_and_max_cells_to_anthropic():
    assert routing.cell_to_openclaw_ref("agy:gemini-4-argon-high") == "agy-cli-lane/gemini-4-argon-high"
    assert routing.cell_to_openclaw_ref("claude-code:sonnet") == "anthropic/claude-sonnet-5-5"
    assert routing.cell_to_openclaw_ref("claude-code:sonnet", {"sonnet": "claude-sonnet-5-6"}) == "anthropic/claude-sonnet-5-6"
    assert routing.runtime_of_cell("claude-code:haiku") == "claude-cli" and routing.runtime_of_cell("agy:x") == ""


def test_the_dynamic_checkers_start_at_the_first_ladder_rung():
    for role in ("verifier", "security-auditor"):
        primary, fallbacks = _expected_chain(role)
        ladder = [routing.cell_to_openclaw_ref(c, EFFECTIVE) for c in routing.CHECKER_LADDER]
        assert [primary, *fallbacks] == ladder


def test_the_setup_table_rows_equal_the_matrix():
    rows = routing.table_rows()
    assert sorted(r for r, _p, _f in rows) == sorted(routing.ROLES)
    for role, primary, fallback in rows:
        row = routing.ROLES[role]
        if row.checker and row.primary is None:
            assert primary == "per-run checker ladder"
            assert fallback == " -> ".join(f"{c} [{routing.pool_of(c)}]" for c in row.chain)
        else:
            assert primary == f"{row.chain[0]} [{routing.pool_of(row.chain[0])}]"
            assert fallback.startswith(" -> ".join(f"{c} [{routing.pool_of(c)}]" for c in row.chain[1:]) or "-")


def test_the_d4_assignments():
    on_agy = {"explore", "code-reviewer", "doc-writer", "tester", "package-upgrade", "generalPurpose"}
    on_claude = {"planner", "software-architect", "implementer", "crashlytics-fixer", "sentry-fixer",
                 "terraform-maintainer", "crossplane-upjet-maintainer"}
    for r in on_agy:
        assert routing.ROLES[r].primary.startswith("agy:"), r
    for r in on_claude:
        assert routing.ROLES[r].primary.startswith("claude-code:"), r
    assert routing.ROLES["tester"].primary == routing.ROLES["generalPurpose"].primary == "agy:gemini-4-argon-high"
    assert all(routing.ROLES[r].checker and routing.ROLES[r].primary is None for r in ("verifier", "security-auditor"))


# --- a profile override flows to every artifact ------------------------------------------------------------------------

def test_a_profile_override_reaches_lanes_yaml_the_agents_the_claude_files_and_the_tables():
    merged = routing.merged(OVERRIDE)
    assert merged["tester"].primary == "claude-code:opus"
    # lanes.yaml
    assert _lanes_yaml(OVERRIDE)["routing"] == routing.render_section(OVERRIDE) != routing.render_section()
    # OpenClaw kit-role entries
    entries, agents = _patch_entries(OVERRIDE), _agents(OVERRIDE)
    for role in ("tester", "implementer", "verifier"):
        primary, fallbacks = _expected_chain(role, OVERRIDE)
        entry = entries[agents[role].id]["model"]
        assert entry["primary"] == primary and entry.get("fallbacks", []) == list(fallbacks)
    assert entries[agents["tester"].id]["model"]["primary"] == "anthropic/claude-opus-5-5"
    assert entries[agents["implementer"].id]["model"]["primary"] == "agy-cli-lane/gemini-4-argon-high"
    # Claude agent definitions
    assert "model: opus" in generate.role_model_block("tester", OVERRIDE)
    assert generate.role_model_block("tester", OVERRIDE) != generate.role_model_block("tester")
    # setup table rows
    rows = {r: (p, f) for r, p, f in routing.table_rows(OVERRIDE)}
    assert rows["tester"][0].startswith("claude-code:opus") and rows["implementer"][0].startswith("agy:gemini-4-argon-high")


def test_the_override_keeps_the_team_rule_no_api_credit_pool_on_a_team_role():
    with pytest.raises(routing.RoutingError):
        routing.merged({"routing": {"tester": {"primary": "anthropic-api:claude-haiku-5-5"}}})


def test_routing_doc_reads_the_lanes_record_then_the_saved_profile():
    s = state.SetupState()
    s.mode, s.profile.name = "single-model", "balanced"
    assert lanes_render.routing_doc(s) == {}
    s.profile.lanes = lanes_render.lanes_state(OVERRIDE)
    assert lanes_render.routing_doc(s) == {"routing": OVERRIDE["routing"]}
