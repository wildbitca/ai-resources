"""ADR-0016 D6 (AC-13): hygiene, the agy MCP file and the CLAUDE.md migration only ask while the wizard runs; Apply writes.

Ctrl-C at Review leaves the scratch HOME untouched; Apply produces exactly the bytes the old immediate steps produced."""
from __future__ import annotations

import argparse
import json
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_resources.setup import agy_mcp, claude_md_migrate as cmm, deferred, detection, hygiene, state, ui, wizard
from ai_resources.setup import credentials, tools
from ai_resources.setup.cockpits import claude
from wizard_fakes import ABORT, Script, Seq

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _section(title, n, word="regla"):
    body = (f"{word} importante: no traducir, mantener acentos y eñes. " * 40)[: n * 4]
    return f"## {title}\n\n{body}\n\n"


OPERATOR = _section("IDIOMA - regla global", 600) + _section("Nota breve", 40) + _section("CLUSTERS - solo wildbit", 450)


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
            and not p.name.endswith(".lock")}


def mk_state():
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    return s


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch ~/.claude with a CLAUDE.md holding two heavy blocks, and a scratch agy MCP file location."""
    md = tmp_path / "home" / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text(OPERATOR, encoding="utf-8")
    monkeypatch.setattr(claude, "CLAUDE_MD_PATH", md)
    monkeypatch.setattr(claude, "CONFIG_ROOT", md.parent)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    mcp = tmp_path / "home" / ".gemini" / "config" / "mcp_config.json"
    monkeypatch.setattr(agy_mcp, "mcp_path", lambda: mcp)
    monkeypatch.setattr(agy_mcp, "portable_candidates", lambda *a, **k: {})
    return types.SimpleNamespace(root=tmp_path / "home", md=md, mcp=mcp, tmp=tmp_path)


def answers_move_first_block(monkeypatch):
    def select(message, choices, default=None, instruction=""):
        return "skill" if "IDIOMA" in message else "keep"
    monkeypatch.setattr(ui, "select", select)
    monkeypatch.setattr(ui, "text", lambda message, default="", validate=None: "Operator language rules")
    monkeypatch.setattr(ui, "confirm", lambda message, default=True: "standing rules" not in message)
    monkeypatch.setattr(ui, "checkbox", lambda message, choices, default=None, **k: [])


# --- the queue ---------------------------------------------------------------------------------------------------------

def test_actions_run_in_queue_order_once_and_a_failure_does_not_stop_the_rest():
    s = state.SetupState()
    ran = []
    deferred.enqueue(s, deferred.Action("a", "first", run=lambda: ran.append("a")))
    deferred.enqueue(s, deferred.Action("b", "second", run=lambda: (_ for _ in ()).throw(OSError("disk"))))
    deferred.enqueue(s, deferred.Action("c", "third", run=lambda: ran.append("c")))
    warned = []
    results = deferred.run_all(s, warn=warned.append)
    assert ran == ["a", "c"] and results == [("a", True), ("b", False), ("c", True)]
    assert warned and "second" in warned[0]
    assert deferred.run_all(s) == []                       # each action runs once


def test_queueing_a_key_again_replaces_it_and_discard_removes_it():
    s = state.SetupState()
    deferred.enqueue(s, deferred.Action("k", "old"))
    deferred.enqueue(s, deferred.Action("k", "new"))
    assert [a.label for a in deferred.queue(s)] == ["new"]
    deferred.discard(s, "k")
    assert deferred.queue(s) == []


def test_an_applies_now_action_is_listed_but_never_run():
    s = state.SetupState()
    ran = []
    deferred.enqueue(s, deferred.Action("login", "claude auth login", applies_now=True, run=lambda: ran.append(1)))
    deferred.enqueue(s, deferred.Action("w", "write a file", run=lambda: ran.append(2)))
    assert deferred.review_labels(s) == ["write a file", "claude auth login (applies now, already done)"]
    deferred.run_all(s)
    assert ran == [2]


def test_dry_run_lines_list_without_executing():
    s = state.SetupState()
    ran = []
    deferred.enqueue(s, deferred.Action("w", "write a file", plan_text="write ~/x", run=lambda: ran.append(1)))
    assert deferred.dry_run_lines(s) == ["would run: write ~/x"] and ran == []


def test_the_queue_is_never_part_of_the_saved_state(home):
    s = mk_state()
    deferred.enqueue(s, deferred.Action("w", "write"))
    assert "_deferred_writes" not in json.dumps(state._to_dict(s))


# --- the CLAUDE.md migration -------------------------------------------------------------------------------------------

def test_the_claude_md_step_changes_nothing_until_apply_and_apply_equals_the_old_immediate_bytes(home, monkeypatch):
    before = snapshot(home.root)
    answers_move_first_block(monkeypatch)
    s = mk_state()
    assert wizard._step_claude_md(s) == 0
    assert snapshot(home.root) == before, "the step wrote something before Apply"
    assert s.tracking.claude_md_migrations == []
    assert [a.key for a in deferred.pending(s)] == ["claude_md"]
    assert "IDIOMA" in deferred.review_labels(s)[0]
    deferred.run_all(s)
    after = snapshot(home.root)
    assert after != before and len(s.tracking.claude_md_migrations) == 1

    # the same answers through the old one-shot form on an identical twin tree give the same bytes
    twin = home.tmp / "twin"
    md2 = twin / ".claude" / "CLAUDE.md"
    md2.parent.mkdir(parents=True)
    md2.write_text(OPERATOR, encoding="utf-8")
    monkeypatch.setattr(claude, "CLAUDE_MD_PATH", md2)
    monkeypatch.setattr(claude, "CONFIG_ROOT", md2.parent)
    s2 = mk_state()
    import os
    for k in ("HOME",):
        monkeypatch.setenv(k, str(twin))
    cmm.run_step(s2, now=NOW)
    assert md2.read_text(encoding="utf-8") == (home.root / ".claude" / "CLAUDE.md").read_text(encoding="utf-8").replace(
        str(home.root), str(twin))


def test_a_declined_migration_queues_nothing(home, monkeypatch):
    monkeypatch.setattr(ui, "select", lambda *a, **k: "keep")
    s = mk_state()
    wizard._step_claude_md(s)
    assert deferred.pending(s) == []


def test_a_dry_run_asks_nothing_and_queues_nothing(home, monkeypatch):
    monkeypatch.setattr(ui, "select", lambda *a, **k: pytest.fail("asked in a dry run"))
    s = mk_state()
    before = snapshot(home.root)
    wizard._step_claude_md(s, dry_run=True)
    assert deferred.pending(s) == [] and snapshot(home.root) == before


# --- the agy MCP file --------------------------------------------------------------------------------------------------

def test_the_agy_mcp_step_changes_nothing_until_apply(home, monkeypatch):
    monkeypatch.setattr(ui, "confirm", lambda message, default=True: True)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: [])
    s = mk_state()
    s.profile.customizations["__targets__"]["selected"] = ["claude", "agy"]
    before = snapshot(home.root)
    wizard._step_agy_mcp(s)
    assert snapshot(home.root) == before and not home.mcp.exists()
    assert [a.key for a in deferred.pending(s)] == ["agy_mcp"]
    deferred.run_all(s)
    assert "engram" in json.loads(home.mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert s.tracking.agy_mcp_added


# --- hygiene -----------------------------------------------------------------------------------------------------------

def test_the_hygiene_step_queues_the_chosen_tools_and_disables_them_only_at_apply(home, monkeypatch):
    cand = hygiene.Candidate("mcp", "grafana", {"command": "g"}, "unused") if "unused" in hygiene.Candidate.__dataclass_fields__ \
        else None
    if cand is None:
        fields = list(hygiene.Candidate.__dataclass_fields__)
        cand = hygiene.Candidate(**{f: ("mcp" if f == "kind" else "grafana" if f == "name" else {} if f == "config" else "")
                                    for f in fields})
    disabled = []
    monkeypatch.setattr(hygiene, "plan", lambda s, **k: [cand])
    monkeypatch.setattr(hygiene, "apply", lambda s, chosen, **k: disabled.append([c.name for c in chosen]) or 1)
    s = mk_state()
    wizard._step_hygiene(s)
    assert disabled == [] and [a.key for a in deferred.pending(s)] == ["hygiene"]
    deferred.run_all(s)
    assert disabled == [["grafana"]]


# --- Review, dry run, Apply ---------------------------------------------------------------------------------------------

def test_review_lists_the_queued_writes_under_will_write(home, monkeypatch):
    s = mk_state()
    deferred.enqueue(s, deferred.Action("claude_md", "move CLAUDE.md blocks: 'IDIOMA' -> skill"))
    rows = dict(wizard._review_rows(s))
    assert "also at Apply: move CLAUDE.md blocks" in rows["Will write"]


def test_apply_runs_the_queue_before_it_configures_anything(monkeypatch):
    order = []
    s = mk_state()
    deferred.enqueue(s, deferred.Action("w", "write", run=lambda: order.append("deferred")))
    monkeypatch.setattr(wizard, "_single_model_executors", lambda s: order.append("executors") or {})
    monkeypatch.setattr(wizard, "_apply_lanes_file", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_apply_channel_changes", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_note_profile_table_change", lambda *a, **k: None)
    monkeypatch.setattr(state, "save", lambda s: None)
    monkeypatch.setattr(wizard, "_print_completion", lambda s: None, raising=False)
    try:
        wizard._step9_apply(s)
    except Exception:
        pass
    assert order[:2] == ["deferred", "executors"]


# --- the whole wizard: Ctrl-C at Review leaves a zero diff -------------------------------------------------------------

def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_ctrl_c_at_review_leaves_the_scratch_home_untouched(home, monkeypatch, capsys):
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "detected_table", lambda rows: None)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: {
        "claude": types.SimpleNamespace(installed=True, version="2.0", binary_path="/opt/claude", name="Claude Code")})
    monkeypatch.setattr(tools, "offer", lambda *a, **k: [])
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "stored-secret")
    monkeypatch.setattr(hygiene, "plan", lambda s, **k: [])
    saves = []
    monkeypatch.setattr(state, "save", saves.append)
    script = Script(**{"Claude Code sign-in": "keep", "Starting team": "custom", "How do you want to choose models": "profile",
                       "How should kit subagents pick": lambda v: v[0], "Customize any role": False,
                       "Configure detected cockpits": lambda v: list(v),
                       "what should happen to this block": lambda v: "skill" if True else "keep",
                       "When should Claude load": "Operator language rules", "standing rules": False,
                       "Create ": True, "Apply this?": ABORT})
    script.install(monkeypatch)
    before = snapshot(home.root)
    rc = wizard.run(_args())
    err = capsys.readouterr().err
    assert rc == 130 and "Traceback" not in err
    assert snapshot(home.root) == before, "Ctrl-C at Review changed the disk"
    assert saves == []
    from ai_resources.setup import operator_rules
    assert not operator_rules.path().exists(), "the operator-rules file was created before Apply"
    assert any("what should happen to this block" in m for m in script.asked)      # it really asked, and queued
    assert any(m.startswith("confirm: Create ") for m in script.asked)
