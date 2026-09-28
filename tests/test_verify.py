"""ai-resources verify: the framework, the generic check and the two front ends (v1.10.0).

Cockpits are replaced by tiny fakes or pointed at tmp_path: nothing here reads the operator's
~/.claude, ~/.openclaw or the live host. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import json
import types

import pytest

from ai_resources import verify
from ai_resources.setup import state
from ai_resources.setup.cockpits import ALL, _shared

BEGIN, END = _shared.MANAGED_BEGIN, _shared.MANAGED_END


def _fake(verify_fn=None, **attrs):
    return types.SimpleNamespace(**({"verify": verify_fn} if verify_fn else {}), **attrs)


@pytest.fixture
def fakes(monkeypatch):
    from ai_resources.setup import cockpits
    registry: dict = {}
    monkeypatch.setattr(cockpits, "ALL", registry)
    return registry


def _state(*configured, **extra) -> state.SetupState:
    s = state.SetupState()
    for cid in configured:
        s.cockpits[cid] = state.CockpitState(installed=True, configured=True, **extra)
    return s


# --- run_all ----------------------------------------------------------------------------------------------

def test_a_cockpit_whose_verify_raises_is_one_error_and_the_others_still_run(fakes):
    def boom(ctx):
        raise RuntimeError("kaput")
    fakes["a"] = _fake(lambda ctx: [verify.Finding("ok", "a", "fine")])
    fakes["b"] = _fake(boom)
    fakes["c"] = _fake(lambda ctx: [verify.Finding("warn", "c", "meh", "do x")])
    found = verify.run_all(_state())
    assert [(f.cockpit, f.level) for f in found] == [("a", "ok"), ("b", "error"), ("c", "warn")]
    assert "b" in found[1].message and "kaput" in found[1].message


def test_registry_order_is_kept_errors_first_inside_a_cockpit(fakes):
    fakes["first"] = _fake(lambda ctx: [verify.Finding("ok", "first", "1"), verify.Finding("error", "first", "2"),
                                        verify.Finding("warn", "first", "3")])
    fakes["second"] = _fake(lambda ctx: [verify.Finding("warn", "second", "4")])
    found = verify.ordered(verify.run_all(_state()))
    assert [(f.cockpit, f.level) for f in found] == [("first", "error"), ("first", "warn"), ("first", "ok"),
                                                      ("second", "warn")]


def test_an_explicit_id_list_visits_only_those(fakes):
    seen = []
    for cid in ("a", "b", "c"):
        fakes[cid] = _fake(lambda ctx, _c=cid: seen.append(_c) or [])
    verify.run_all(_state(), ["c", "a", "nope"])
    assert seen == ["a", "c"]   # registry order, unknown ids ignored


def test_a_finding_rejects_an_unknown_level():
    with pytest.raises(ValueError):
        verify.Finding("fatal", "x", "y")


def test_the_real_registry_keeps_openclaw_last():
    assert list(ALL)[-1] == "openclaw"


def test_verify_imports_alone_without_a_cycle():
    import subprocess
    import sys
    from pathlib import Path
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    code = ("import sys; sys.path.insert(0, %r); import ai_resources.verify; "
            "assert not any(m.startswith('ai_resources.setup.cockpits') for m in sys.modules)" % str(scripts))
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).returncode == 0


# --- the generic check ----------------------------------------------------------------------------------------

def test_a_configured_cockpit_with_its_block_is_ok(fakes, tmp_path):
    md = tmp_path / "AGENT_KIT.md"
    md.write_text(f"mine\n{BEGIN}\nbody\n{END}\n", encoding="utf-8")
    fakes["cursor"] = _fake(INSTRUCTIONS_PATH=md)
    found = verify.run_all(_state("cursor", config_root=str(tmp_path)))
    assert [f.level for f in found] == ["ok"] and str(md) in found[0].message


def test_a_dropped_block_is_an_error_and_two_blocks_are_an_error(fakes, tmp_path):
    md = tmp_path / "AGENT_KIT.md"
    fakes["cursor"] = _fake(INSTRUCTIONS_PATH=md)
    md.write_text("# a hand-written body\n", encoding="utf-8")
    [f] = verify.run_all(_state("cursor", config_root=str(tmp_path)))
    assert f.level == "error" and "re-run `ai-resources setup`" in f.remedy
    md.write_text(f"{BEGIN}\na\n{END}\n{BEGIN}\nb\n{END}\n", encoding="utf-8")
    [f] = verify.run_all(_state("cursor", config_root=str(tmp_path)))
    assert f.level == "error" and "2 kit block" in f.message


def test_a_missing_instruction_file_is_a_warning_not_an_error(fakes, tmp_path):
    fakes["cursor"] = _fake(INSTRUCTIONS_PATH=tmp_path / "gone.md")
    [f] = verify.run_all(_state("cursor", config_root=str(tmp_path)))
    assert f.level == "warn"


def test_a_record_of_an_unknown_shape_reports_nothing(fakes):
    s = state.SetupState()
    s.cockpits["cursor"] = types.SimpleNamespace(configured="yes", config_root=42)   # not a CockpitState
    fakes["cursor"] = _fake(INSTRUCTIONS_PATH=None)
    assert verify.run_all(s) == []
    s.cockpits = ["not", "a", "dict"]
    assert verify.run_all(s) == []
    s2 = state.SetupState()   # no record at all for the selected cockpit
    assert verify.run_all(s2) == []


def test_every_selected_cockpit_produces_at_least_one_finding(tmp_path, monkeypatch):
    """The real nine non-openclaw cockpits, each configured against tmp_path."""
    from ai_resources.setup import cockpits
    selected = [cid for cid in cockpits.ALL if cid not in ("openclaw",)]
    for cid in selected:
        mod = cockpits.ALL[cid]
        for attr in verify._INSTRUCTION_ATTRS:
            if isinstance(getattr(mod, attr, None), type(tmp_path)):
                monkeypatch.setattr(mod, attr, tmp_path / cid / "instructions.md", raising=False)
                (tmp_path / cid).mkdir(exist_ok=True)
                (tmp_path / cid / "instructions.md").write_text(f"{BEGIN}\nx\n{END}\n", encoding="utf-8")
    s = _state(*selected, config_root=str(tmp_path))
    _claude_home(monkeypatch, tmp_path / "claude", with_block=True)
    found = verify.run_all(s, selected)
    assert {f.cockpit for f in found} == set(selected)
    assert not [f for f in found if f.level == "error"], verify.render(found)


def test_verify_writes_nothing(fakes, tmp_path):
    md = tmp_path / "AGENT_KIT.md"
    md.write_text("plain\n", encoding="utf-8")
    fakes["cursor"] = _fake(INSTRUCTIONS_PATH=md)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    verify.run_all(_state("cursor", config_root=str(tmp_path)))
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()} == before


# --- rendering -----------------------------------------------------------------------------------------------------

def test_render_and_summary_are_stable_for_a_fixed_list():
    fs = [verify.Finding("ok", "a", "fine"), verify.Finding("error", "a", "broken", "fix it"),
          verify.Finding("warn", "b", "hmm")]
    assert verify.render(fs) == verify.render(list(fs))
    assert verify.render(fs).splitlines()[0] == "[error] a: broken"
    assert "-> fix it" in verify.render(fs)
    assert verify.summary(fs) == "1 of 2 selected toolings did not reach the expected state (1 error(s), 1 warning(s), 1 ok)"
    assert verify.summary([verify.Finding("ok", "a", "x")]).startswith("All 1 selected toolings")


# --- the claude cockpit's own verify ---------------------------------------------------------------------------

def _claude_home(monkeypatch, root, *, with_block: bool):
    from ai_resources.setup.cockpits import claude
    root.mkdir(parents=True, exist_ok=True)
    for name, value in (("CONFIG_ROOT", root), ("SETTINGS_PATH", root / "settings.json"),
                        ("CLAUDE_MD_PATH", root / "CLAUDE.md"), ("AGENTS_DIR", root / "agents"),
                        ("WORKFLOWS_DIR", root / "workflows")):
        monkeypatch.setattr(claude, name, value)
    (root / "settings.json").write_text('{"env": {}}', encoding="utf-8")
    (root / "skills").mkdir(exist_ok=True)
    (root / "CLAUDE.md").write_text((f"{BEGIN}\nx\n{END}\n" if with_block else "# mine\n"), encoding="utf-8")
    return claude


def test_claude_verify_is_ok_on_a_healthy_home_and_writes_nothing(tmp_path, monkeypatch):
    _claude_home(monkeypatch, tmp_path / "c", with_block=True)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    found = verify.run_all(_state("claude"), ["claude"])
    assert found and not [f for f in found if f.level != "ok"], verify.render(found)
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()} == before


def test_claude_verify_flags_a_dropped_block_and_missing_recorded_files(tmp_path, monkeypatch):
    _claude_home(monkeypatch, tmp_path / "c", with_block=False)
    s = _state("claude")
    s.tracking.workflow_scripts_installed = ["kit-plan.js"]
    s.tracking.subagent_files_installed = ["implementer"]
    found = verify.run_all(s, ["claude"])
    assert [f.level for f in found if f.level == "error"] == ["error"]
    assert any(f.level == "warn" and "kit-plan.js" in f.message and "agents/implementer.md" in f.message for f in found)


def test_claude_verify_survives_a_tracking_record_of_the_wrong_shape(tmp_path, monkeypatch):
    _claude_home(monkeypatch, tmp_path / "c", with_block=True)
    s = _state("claude")
    s.tracking.workflow_scripts_installed = {"kit-plan.js": 1}
    s.tracking.subagent_files_installed = None
    assert not [f for f in verify.run_all(s, ["claude"]) if f.level != "ok"]


def test_claude_verify_never_reads_or_reports_model_or_permission_settings(tmp_path, monkeypatch):
    claude = _claude_home(monkeypatch, tmp_path / "c", with_block=True)
    (tmp_path / "c" / "settings.json").write_text(json.dumps({"bundleMcp": False, "env": {}}), encoding="utf-8")
    text = " ".join(f.message + f.remedy for f in verify.run_all(_state("claude"), ["claude"]))
    for word in ("bundleMcp", "dangerously-skip-permissions", "FORBIDDEN_CLAUDE_FLAGS"):
        assert word not in text
    import inspect
    src = inspect.getsource(claude.verify)
    assert not any(w in src for w in ("bundleMcp", "dangerously", "FORBIDDEN_CLAUDE_FLAGS", "isolatesInstructions"))
