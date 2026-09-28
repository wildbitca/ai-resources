"""The openclaw cockpit's verify(): E1 block drift, stale routing docs, E4 identity, E2 model form,
the bindings invariant (S8). Read-only: every test asserts nothing on disk changed.

Driven against the simulated host of test_openclaw_host_wizard with a canned host runner. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import json

import pytest

from test_openclaw_host_wizard import _configure, _state, script, sim  # noqa: F401  (fixtures by name)
from test_verify_openclaw_host import HostRunner
from ai_resources.setup.cockpits import _shared, openclaw

BEGIN, END = _shared.MANAGED_BEGIN, _shared.MANAGED_END
CATCH_ALL = {"agentId": "main", "comment": "catch-all", "match": {"channel": "telegram", "accountId": "*"}}


def _edit(sim, fn):
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    fn(doc)
    sim.cfg.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _snapshot(sim):
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in sim.tmp.rglob("*") if p.is_file()}


def _verify(s=None, runner=None):
    return openclaw.verify({"state": s or _state(), "runner": runner or HostRunner()})


def _applied(sim, script):
    """A state and disk as setup leaves them: every workspace has its block, and it is recorded."""
    s = _state()
    _configure(s)
    return s


def _errors(found):
    return [f for f in found if f.level == "error"]


def _by(found, level, needle):
    return [f for f in found if f.level == level and needle in f.message]


def test_a_clean_applied_host_has_no_error_and_no_block_warning(sim, script):
    s = _applied(sim, script)
    found = _verify(s)
    assert not _errors(found), [f.message for f in _errors(found)]
    assert any(f.level == "ok" and "kit block is present exactly once" in f.message for f in found)


def test_a_recorded_block_that_was_replaced_wholesale_is_one_error_naming_the_file(sim, script):
    s = _applied(sim, script)
    main_md = sim.workspaces["main"] / "AGENTS.md"
    assert str(main_md) in s.openclaw.agent_kit_blocks
    main_md.write_text("# AGENTS.md - Jarvis\n\nMy hand-written body.\n", encoding="utf-8")   # the drift
    before = _snapshot(sim)
    errors = _errors(_verify(s))
    assert len(errors) == 1
    assert str(main_md) in errors[0].message and "after setup wrote it" in errors[0].message
    assert "re-run `ai-resources setup`" in errors[0].remedy
    assert _snapshot(sim) == before   # nothing written, mtimes included


def test_two_marker_pairs_are_an_error(sim, script):
    s = _applied(sim, script)
    md = sim.workspaces["infra"] / "AGENTS.md"
    text = md.read_text(encoding="utf-8")
    md.write_text(text + "\n" + text, encoding="utf-8")
    errors = _errors(_verify(s))
    assert len(errors) == 1 and "2 kit block" in errors[0].message


def test_a_blockless_file_the_kit_never_recorded_is_only_a_warn(sim, script):
    s = _applied(sim, script)
    md = sim.workspaces["docs"] / "AGENTS.md"
    md.write_text("# plain\n", encoding="utf-8")
    s.openclaw.agent_kit_blocks.pop(str(md), None)
    found = _verify(s)
    assert not _errors(found) and _by(found, "warn", "has no kit block")


def test_the_kit_block_never_reads_as_stale_documentation(sim, script):
    s = _applied(sim, script)
    assert not _by(_verify(s), "warn", "documents routing")


def test_a_dead_chat_id_outside_the_block_warns_and_a_live_one_does_not(sim, script):
    s = _applied(sim, script)
    _edit(sim, lambda d: d.update(bindings=[{"type": "route", "agentId": "app",
                                             "match": {"channel": "telegram", "peer": {"kind": "group", "id": "-5217865131"}}}]))
    md = sim.workspaces["app"] / "AGENTS.md"
    md.write_text(md.read_text(encoding="utf-8") + "\nForum -1003678125825 topic 7 is where I live.\n"
                  "Group -5217865131 is the live one.\n", encoding="utf-8")
    stale = _by(_verify(s), "warn", "documents routing")
    assert len(stale) == 1
    assert "-1003678125825" in stale[0].message and "topic 7" in stale[0].message
    assert "-5217865131" not in stale[0].message
    assert "by hand" in stale[0].remedy


def test_a_string_form_model_is_a_warn_naming_the_agent_with_the_remedy(sim, script):
    s = _applied(sim, script)
    _edit(sim, lambda d: d["agents"]["entries"]["app"].update(model="anthropic/claude-sonnet-5"))
    warns = _by(_verify(s), "warn", "agent app spells its model as a string")
    assert len(warns) == 1
    assert warns[0].remedy == "openclaw config set agents.entries.app.model.primary anthropic/claude-sonnet-5"
    assert not _by(_verify(s), "warn", "agent infra spells")


def test_two_agents_sharing_a_workspace_whose_identity_names_one_is_one_warn_naming_both(sim, script):
    s = _applied(sim, script)
    shared = sim.workspaces["claude"]
    (shared / "IDENTITY.md").write_text("# IDENTITY\n\n- Name: security\n- Emoji: x\n", encoding="utf-8")
    _edit(sim, lambda d: d["agents"]["entries"].update(
        security={"workspace": str(shared), "identity": {"name": "security"}}))
    before = _snapshot(sim)
    warns = _by(_verify(s), "warn", "share")
    assert len(warns) == 1
    msg = warns[0].message
    assert "claude" in msg and "security" in msg and str(shared) in msg and "Name: security" in msg
    assert "openclaw config set agents.entries.claude.identity.name claude --dry-run" in warns[0].remedy
    assert _snapshot(sim) == before and not _by(_verify(s), "warn", "agents infra")


def test_a_shared_workspace_where_every_agent_has_a_name_is_not_a_warn(sim, script):
    s = _applied(sim, script)
    shared = sim.workspaces["claude"]
    (shared / "IDENTITY.md").write_text("- Name: security\n", encoding="utf-8")
    _edit(sim, lambda d: (d["agents"]["entries"]["claude"].update(identity={"name": "claude"}),
                          d["agents"]["entries"].update(security={"workspace": str(shared), "identity": {"name": "security"}})))
    assert not _by(_verify(s), "warn", "share")


def test_a_catch_all_that_is_not_last_is_an_error(sim, script):
    s = _applied(sim, script)
    route = {"type": "route", "agentId": "app", "match": {"channel": "telegram", "peer": {"kind": "group", "id": "-5217865131"}}}
    _edit(sim, lambda d: d.update(bindings=[CATCH_ALL, route]))
    errors = _errors(_verify(s))
    assert len(errors) == 1 and "not last" in errors[0].message
    _edit(sim, lambda d: d.update(bindings=[route, CATCH_ALL]))
    assert not _errors(_verify(s))


def test_verify_never_mentions_claude_kit_flags(sim, script):
    s = _applied(sim, script)
    text = " ".join(f.message + f.remedy for f in _verify(s))
    for word in ("bundleMcp", "dangerously-skip-permissions", "FORBIDDEN_CLAUDE_FLAGS", "isolatesInstructions"):
        assert word not in text


def test_an_unreadable_config_is_a_warn_and_the_host_checks_still_run(sim, script):
    s = _applied(sim, script)
    sim.cfg.write_text("{ not json", encoding="utf-8")
    found = _verify(s)
    assert _by(found, "warn", "could not be read")
    assert any("gateway" in f.message for f in found)

