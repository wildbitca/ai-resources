"""The kit block in every OpenClaw agent workspace AGENTS.md (v1.9.7).

Driven through `openclaw.configure` / `openclaw.teardown` against the simulated host of
test_openclaw_host_wizard: nothing here can reach the real ~/.openclaw or a gateway. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from test_openclaw_host_wizard import (  # noqa: F401  (fixtures are used by name)
    _configure, _state, outside_block, script, sim,
)
from ai_resources.setup import state
from ai_resources.setup.cockpits import _shared, openclaw

BEGIN = "<!-- BEGIN ai-resources"
HAND_SHAPED = "# Jarvis\n\nMy own rule.\n\n## Voice notes\n\nKeep it short.\n"


def _agents(sim, name: str):
    return sim.workspaces[name] / "AGENTS.md"


def _pairs(text: str) -> int:
    return text.count(BEGIN)


def _share_workspace(sim, aid: str, with_agent: str) -> None:
    """Add agent `aid` whose workspace is the same directory as `with_agent`'s."""
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["agents"]["entries"][aid] = {"workspace": doc["agents"]["entries"][with_agent]["workspace"]}
    sim.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")


# --- the builder ---------------------------------------------------------------------------------------

def test_the_block_teaches_the_orchestration_loop_and_the_delegation():
    text = _shared.openclaw_agent_kit_md("/kit")
    for needle in ("kit-orchestration", "workflow-", "sessions_spawn agentId=claude cwd=<project> thread=true",
                   "handoff.md", "verifier", "/kit/handoff.md.template"):
        assert needle in text
    assert "topic" not in text.lower()


@pytest.mark.parametrize("delegates", [True, False])
def test_the_long_running_rule_is_in_both_forms_once_and_before_the_worktrees(delegates):
    text = _shared.openclaw_agent_kit_md("/kit", delegates=delegates)
    heading = _shared.OPENCLAW_LONG_RUNNING_HEADING
    assert heading == "## Long-running commands never block a tool call"
    assert text.count(heading) == 1
    assert text.index("## Kit workflow") < text.index(heading) < text.index("## Worktrees")
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    for needle in ("T39", "background", "ask_user", "phase"):
        assert needle in section, needle


def test_the_long_running_rule_names_no_host_job():
    section = _shared.openclaw_agent_kit_md("/kit").split(_shared.OPENCLAW_LONG_RUNNING_HEADING, 1)[1]
    section = section.split("\n## ", 1)[0]
    for host_literal in ("security-scan", "wildbit", "devops", "bithome"):
        assert host_literal not in section


def test_the_worker_block_leaves_out_the_delegation_and_keeps_the_rest():
    worker = _shared.openclaw_agent_kit_md("/kit", delegates=False)
    assert "sessions_spawn" not in worker and "## Delegation" not in worker
    assert "kit-orchestration" in worker and "handoff.md" in worker


def test_the_block_is_agent_agnostic_so_a_shared_workspace_converges():
    """No agent id is a parameter at all: rendering 'for claude' then 'for security' cannot differ."""
    assert _shared.openclaw_agent_kit_md("/kit") == _shared.openclaw_agent_kit_md("/kit")
    assert "@AGENT" not in _shared.openclaw_agent_kit_md("/kit")


@pytest.mark.parametrize("model,engine", [("", "keep"), ("openrouter/auto", "direct"),
                                            ("anthropic/claude-sonnet-5", "claude-code")])
def test_the_common_block_is_identical_under_every_engine(sim, script, model, engine):
    s = state.SetupState()
    s.openclaw.applied = bool(model)
    s.openclaw.model = model
    target = sim.workspaces["infra"] / "AGENTS.md"
    openclaw._write_workspace_block(s, target, "/kit", "", kit=True, engine_part=False)
    assert outside_block(target.read_text(encoding="utf-8")) == ""
    body = target.read_text(encoding="utf-8")
    assert body == "\n".join([_shared.MANAGED_BEGIN, *_shared.openclaw_agent_kit_md("/kit").strip().splitlines(),
                              _shared.MANAGED_END]) + "\n"


def test_the_antigravity_text_is_kept_next_to_the_common_block(sim, script):
    s = state.SetupState()
    s.openclaw.applied = True
    s.openclaw.model = openclaw.ENGINES["antigravity"].models[0]
    target = sim.workspaces["main"] / "AGENTS.md"
    openclaw._write_workspace_block(s, target, "/kit", "", kit=True)
    text = target.read_text(encoding="utf-8")
    assert "You (agy) are the orchestrator" in text and "kit-orchestration" in text
    assert _pairs(text) == 1 and text.count("\n# ") == 1, "one block, one H1"


# --- configure ------------------------------------------------------------------------------------------

def test_an_unattended_keep_run_refreshes_the_block_in_every_workspace(sim, script):
    """The live shape: engine `keep`, the host section never answered."""
    s = _state()
    assert s.openclaw.host is False
    script.non_interactive = True
    _configure(s)
    for name in ("main", "app", "infra", "docs", "claude"):
        text = _agents(sim, name).read_text(encoding="utf-8")
        assert _pairs(text) == 1 and "kit-orchestration" in text
        assert ("sessions_spawn" in text) is (name != "claude")


def test_a_hand_shaped_file_keeps_every_byte_outside_the_markers(sim, script):
    _agents(sim, "app").write_text(HAND_SHAPED, encoding="utf-8")
    s = _state()
    _configure(s)
    text = _agents(sim, "app").read_text(encoding="utf-8")
    assert _pairs(text) == 1 and outside_block(text) == HAND_SHAPED
    assert "sessions_spawn" in text


def test_a_second_run_changes_no_file(sim, script):
    _agents(sim, "app").write_text(HAND_SHAPED, encoding="utf-8")
    s = _state()
    _configure(s)
    first = sim.files()
    first_state = json.dumps(state._to_dict(s.openclaw), sort_keys=True)
    _configure(s)
    assert sim.files() == first
    assert json.dumps(state._to_dict(s.openclaw), sort_keys=True) == first_state


def test_teardown_gives_back_the_pre_setup_bytes(sim, script):
    _agents(sim, "app").write_text(HAND_SHAPED, encoding="utf-8")
    s = _state()
    _configure(s)
    openclaw.teardown(s)
    assert _agents(sim, "app").read_text(encoding="utf-8") == HAND_SHAPED
    for name in ("main", "infra", "docs", "claude", "default"):
        assert not _agents(sim, name).exists(), f"{name}: a file the kit created is gone"
    assert s.openclaw.agent_kit_blocks == {}


def test_an_edited_kit_created_file_loses_only_the_block(sim, script):
    s = _state()
    _configure(s)
    infra = _agents(sim, "infra")
    infra.write_text(infra.read_text(encoding="utf-8") + "\nMy own rule.\n", encoding="utf-8")
    _configure(s)          # a refresh over the edit must not turn the file into "the kit's"
    openclaw.teardown(s)
    assert infra.read_text(encoding="utf-8").strip() == "My own rule."


def test_a_dry_run_writes_nothing_and_reports_each_file(sim, script):
    _agents(sim, "app").write_text(HAND_SHAPED, encoding="utf-8")
    before = sim.files()
    s = _state()
    _configure(s, dry_run=True)
    assert sim.files() == before
    assert s.openclaw.agent_kit_blocks == {}
    reported = " ".join(script.messages("detail"))
    for name in ("app", "infra", "docs", "claude"):
        assert str(_agents(sim, name)) in reported


def test_a_workspace_shared_by_two_agents_is_written_once(sim, script, monkeypatch):
    _share_workspace(sim, "security", "claude")
    calls: list[str] = []
    real = _shared.write_managed_block
    monkeypatch.setattr(_shared, "write_managed_block",
                        lambda path, body, **kw: (calls.append(str(path)), real(path, body, **kw))[1])
    s = _state()
    _configure(s)
    shared = str(_agents(sim, "claude"))
    assert calls.count(shared) == 1
    text = _agents(sim, "claude").read_text(encoding="utf-8")
    assert _pairs(text) == 1
    assert "sessions_spawn" in text, "security shares the tree and does delegate"


def test_the_shared_workspace_bytes_do_not_depend_on_the_entry_order(sim, script):
    _share_workspace(sim, "security", "claude")
    s = _state()
    _configure(s)
    forward = _agents(sim, "claude").read_bytes()
    _agents(sim, "claude").unlink()
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    entries = doc["agents"]["entries"]
    doc["agents"]["entries"] = {k: entries[k] for k in reversed(list(entries))}
    sim.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    _configure(_state())
    assert _agents(sim, "claude").read_bytes() == forward


def test_a_template_is_still_rendered_only_for_a_missing_file(sim, script):
    script.answers["Configure this machine as an OpenClaw host"] = True
    from test_openclaw_host_wizard import _run_wizard
    s = _state()
    _run_wizard(s)
    infra = _agents(sim, "infra").read_text(encoding="utf-8")
    assert "# AGENTS.md — infra" in infra and _pairs(infra) == 1
    assert len(s.openclaw.host_agents_md_written) == 3
    openclaw.teardown(s)
    assert not _agents(sim, "infra").exists(), "the template file with its block is the kit's: removed whole"


@pytest.mark.parametrize("shape", ["no newline at the end", "trailing blank lines\n\n\n", "crlf\r\nlines\r\n"])
def test_trailing_bytes_of_a_hand_shaped_file_survive_a_round_trip(sim, script, shape):
    _agents(sim, "infra").write_bytes(shape.encode("utf-8"))
    s = _state()
    _configure(s)
    assert outside_block(_agents(sim, "infra").read_bytes().decode("utf-8")).lstrip("\r\n") == shape
    openclaw.teardown(s)
    assert _agents(sim, "infra").read_bytes() == shape.encode("utf-8")


# --- the orchestrator's default workspace is main's (the live shape) ------------------------------------

def _default_is_main(sim) -> None:
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["agents"]["defaults"]["workspace"] = str(sim.workspaces["main"])
    sim.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def test_workspace_targets_keys_default_and_main_as_one_entry(sim):
    _default_is_main(sim)
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    main_ws = sim.workspaces["main"].resolve()
    targets = openclaw._workspace_targets(doc)
    assert list(targets).count(main_ws) == 1
    assert {"", "main"} <= set(targets[main_ws]["aids"])
    assert targets[main_ws]["engine"] is True


def test_orchestrator_default_workspace_gets_exactly_one_marker_pair(sim, script):
    _default_is_main(sim)
    s = _state()
    _configure(s)
    text = _agents(sim, "main").read_text(encoding="utf-8")
    assert _pairs(text) == 1 and text.count(_shared.MANAGED_END) == 1
    assert "kit-orchestration" in text and "sessions_spawn" in text
    before = {p: p.read_bytes() for p in sim.tmp.rglob("AGENTS.md")}
    _configure(s)
    assert {p: p.read_bytes() for p in sim.tmp.rglob("AGENTS.md")} == before


def test_a_workspace_directory_that_does_not_exist_is_reported_by_setup_not_created(sim, script):
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["agents"]["entries"]["docs"]["workspace"] = str(sim.tmp / "ws" / "missing")
    sim.cfg.write_text(json.dumps(doc), encoding="utf-8")
    s = _state()
    _configure(s)
    assert not (sim.tmp / "ws" / "missing").exists()
    assert any("workspace directory does not exist" in m and "docs" in m for m in script.messages("warn"))


def test_every_heading_of_the_rendered_kit_blocks_is_known_to_the_orphan_recovery():
    bodies = [
        _shared.openclaw_agent_kit_md("/kit", delegates=True),
        _shared.openclaw_agent_kit_md("/kit", delegates=False),
        _shared.kit_instructions_md("claude", "/kit", "http://gw", "single-model", native_skills=True),
        _shared.kit_instructions_md("claude", "/kit", "http://gw", "single-model", native_skills=False),
        _shared.multimodel_protocol_md("/kit", "http://gw"),
        # the engine part and the voice rule land between the same markers (_write_workspace_block)
        openclaw.MEMORY_MD,
        openclaw.voice.VOICE_MD,
        openclaw._antigravity_agents_md("/kit"),
    ]
    known = _shared.CURRENT_KIT_HEADINGS | _shared.LEGACY_KIT_HEADINGS
    for body in bodies:
        for heading in re.findall(r"^## (.+?)\s*$", body, re.MULTILINE):
            assert heading in known, f"{heading!r} would be stranded by the orphan-marker recovery"


def _orphaned(body: str, tail: str) -> str:
    """A file whose kit block lost its END marker, followed by hand-written text."""
    return "\n".join([_shared.MANAGED_BEGIN, *body.strip().splitlines()]) + "\n\n" + tail


_ORPHAN_BODIES = {
    "common": lambda: _shared.openclaw_agent_kit_md("/kit"),
    "antigravity+kit": lambda: openclaw._antigravity_agents_md("/kit") + "\n"
                               + _shared.openclaw_agent_kit_md("/kit").split("\n", 2)[2],
    "with-voice": lambda: _shared.openclaw_agent_kit_md("/kit") + "\n" + openclaw.voice.VOICE_MD,
    "direct": lambda: _shared.kit_instructions_md("OpenClaw", "/kit", "http://gw", "single-model",
                                                   native_skills=False) + "\n" + openclaw.MEMORY_MD,
}


@pytest.mark.parametrize("kind", sorted(_ORPHAN_BODIES))
def test_a_block_whose_end_marker_is_gone_is_rebuilt_without_stranding_kit_sections(tmp_path, kind):
    body = _ORPHAN_BODIES[kind]()
    tail = "# My notes\n\n## Mine\n\nHand-written rule.\n"
    path = tmp_path / "AGENTS.md"
    path.write_text(_orphaned(body, tail), encoding="utf-8")
    _shared.write_managed_block(path, body)
    text = path.read_text(encoding="utf-8")
    assert text.count(BEGIN) == 1 and text.count(_shared.MANAGED_END) == 1
    outside = outside_block(text)
    assert tail.strip() in outside
    for heading in _shared.CURRENT_KIT_HEADINGS:
        assert f"## {heading}\n" not in outside, f"{heading!r} stranded outside the block"


def test_two_spellings_of_one_workspace_are_written_once(sim, script, monkeypatch):
    # `security` reaches claude's tree through a symlink: one directory, two spellings.
    link = sim.tmp / "ws" / "claude-link"
    link.symlink_to(sim.workspaces["claude"])
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["agents"]["entries"]["security"] = {"workspace": str(link)}
    doc["agents"]["entries"]["claude"]["workspace"] = str(
        sim.workspaces["claude"].parent / ".." / "ws" / "claude")   # a `..` detour, same dir
    # the orchestrator's default workspace spells the same directory a third way
    doc["agents"]["defaults"]["workspace"] = str(sim.workspaces["claude"]) + "/../claude"
    sim.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    calls: list[str] = []
    real = _shared.write_managed_block
    monkeypatch.setattr(_shared, "write_managed_block",
                        lambda path, body, **kw: (calls.append(str(path)), real(path, body, **kw))[1])
    _configure(_state())
    shared = str(_agents(sim, "claude"))
    assert sum(Path(c).resolve() == Path(shared).resolve() for c in calls) == 1
    assert _pairs(_agents(sim, "claude").read_text(encoding="utf-8")) == 1


def test_workspace_targets_resolve_spellings_to_one_key(sim):
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    real = sim.workspaces["claude"]
    # agent_workspaces() resolves entries itself, so the defaults path is the one that needs
    # `_workspace_targets` to resolve: spell it with a `..` detour and a trailing slash.
    doc["agents"]["defaults"]["workspace"] = str(real.parent / ".." / "ws" / real.name) + "/"
    doc["agents"]["entries"]["security"] = {"workspace": str(real)}
    doc["agents"]["entries"]["claude"]["workspace"] = str(real) + "/"
    keys = [k for k in openclaw._workspace_targets(doc) if k == real.resolve()]
    assert len(keys) == 1
    assert sorted(openclaw._workspace_targets(doc)[real.resolve()]["aids"]) == ["", "claude", "security"]
