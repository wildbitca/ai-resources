"""Ctrl-C at the real wizard's prompts: rc 130, no traceback, nothing saved, no file touched, lock released.

The REAL `wizard.run` is driven (real `state.locked_run`, real `_run` and real step functions) through
`wizard_fakes.Script`; only the host probes and the prompts are faked. A cancel is `ABORT` (what a ui wrapper
raises inside the wizard scope) or `INTERRUPT` (a bare KeyboardInterrupt that no wrapper converted)."""
from __future__ import annotations

import argparse
import types
from pathlib import Path

import pytest

from ai_resources.setup import credentials, detection, state, tools, ui, wizard

pytestmark = pytest.mark.usefixtures("extra_tools_present")   # the selector now knows more CLIs
from wizard_fakes import ABORT, INTERRUPT, Script

_REAL_OFFER = tools.offer          # the `host` fixture replaces tools.offer with a stub

_COCKPITS = {"claude": types.SimpleNamespace(installed=True, version="2.0", binary_path="/opt/claude",
                                             name="Claude Code")}


def _snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.endswith(".lock"):
            out[str(p)] = (p.stat().st_mtime_ns, p.read_bytes())
    return out


@pytest.fixture
def host(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "xdg").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "detected_table", lambda rows: None)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: dict(_COCKPITS))
    monkeypatch.setattr(tools, "offer", lambda *a, **k: [])
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "stored-secret")
    saves = []
    real_save = state.save

    def counting_save(s):
        saves.append(s)
        return real_save(s)
    monkeypatch.setattr(state, "save", counting_save)
    return types.SimpleNamespace(home=home, saves=saves, monkeypatch=monkeypatch)


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


def _drive(host, answers, args=None):
    script = Script(**answers)
    script.install(host.monkeypatch)
    return wizard.run(args or _args()), script


def _assert_clean_cancel(rc, capsys, host, before, step=None):
    assert rc == 130
    err = capsys.readouterr().err
    assert "Traceback" not in err and "cancelled" in err.lower()
    assert "Nothing was applied" in err
    if step:
        assert f"at {step}" in err
    assert host.saves == []
    assert _snapshot(host.home) == before
    # The lock was released: a second locked run gets in.
    assert state.locked_run(lambda: 0) == 0
    return err


def test_cancel_at_resume_confirm(host, capsys):
    s = state.SetupState()
    s.last_run = "2026-01-01T00:00:00+00:00"
    host.monkeypatch.setattr(state, "save", host.saves.append)   # seed the file without counting it
    import yaml
    state.state_path().parent.mkdir(parents=True, exist_ok=True)
    state.state_path().write_text(yaml.safe_dump(state._to_dict(s)), encoding="utf-8")
    before = _snapshot(host.home)
    rc, _ = _drive(host, {"Use previous answers": ABORT})
    _assert_clean_cancel(rc, capsys, host, before)


def test_cancel_at_port_text(host, capsys):
    host.monkeypatch.setattr(detection, "detect_container_runtime",
                             lambda: ("docker", types.SimpleNamespace(installed=True, version="1", binary_path="/d")))
    host.monkeypatch.setattr(detection, "detect_compose", lambda cli: True)
    before = _snapshot(host.home)
    rc, script = _drive(host, {"Choose mode": "multi-model:litellm", "Where is your gateway": "local-docker",
                               "Bind interface": "127.0.0.1", "Port": ABORT})
    _assert_clean_cancel(rc, capsys, host, before, step="LiteLLM gateway")
    assert any(m.startswith("text: Port") for m in script.asked)


def test_cancel_at_providers_checkbox(host, capsys):
    before = _snapshot(host.home)
    rc, _ = _drive(host, {"Choose mode": "multi-model:litellm", "Where is your gateway": "skipped",
                          "Select providers": ABORT})
    _assert_clean_cancel(rc, capsys, host, before, step="Provider backends")


def test_cancel_at_profile_confirm(host, capsys):
    from ai_resources.scoring import db as scores_db, detect as scoring_detect, explain, flow, profile_out
    mp = host.monkeypatch
    mp.setattr(scores_db, "load", lambda: types.SimpleNamespace(problems=[]))
    mp.setattr(scoring_detect, "default_deps", lambda: object())
    mp.setattr(scoring_detect, "inventory", lambda deps, db: {})
    mp.setattr(flow, "enabled_providers", lambda s, inv: ["openrouter"])
    mp.setattr(flow, "askable_pools", lambda inv, enabled, db: [])
    mp.setattr(flow, "run", lambda *a, **k: (types.SimpleNamespace(refused=False, kept_default=True),
                                             types.SimpleNamespace(readings={})))
    mp.setattr(profile_out, "to_customizations", lambda comp: {})
    mp.setattr(explain, "render_text", lambda comp, lanes: "")
    before = _snapshot(host.home)
    rc, script = _drive(host, {"Choose mode": "multi-model:openrouter", "Model preset": "custom", "How do you want to choose models": "profile",
                               "Update OPENROUTER_API_KEY": False, "Choose profile": "__compose__",
                               "Composition mode": "uniform",
                               "Use this team?": ABORT})
    _assert_clean_cancel(rc, capsys, host, before, step="Models and roles - 6b Role assignment")
    assert any(m.startswith("confirm: Use this team?") for m in script.asked)


def test_cancel_at_lanes_acknowledgement(host, capsys):
    before = _snapshot(host.home)
    rc, script = _drive(host, {"Choose mode": "single-model", "Model preset": "custom", "How do you want to choose models": "profile",
                               "How should kit subagents pick": "subscription-claude-55",
                               "Acknowledge for these roles?": ABORT, "Orchestrator agent id": "main"},
                        _args(lanes=True))
    err = _assert_clean_cancel(rc, capsys, host, before, step="Models and roles - 6c Subscription lanes")
    assert any(m.startswith("confirm: Acknowledge") for m in script.asked)
    assert not (state.config_root() / "backups" / "lanes").exists()
    assert "text_sha256" not in err
    assert not state.lanes_path().exists()


def test_bare_keyboard_interrupt_names_the_step(host, capsys):
    """prompt_scope restores the section on exit, so the name must be read before the scope ends."""
    before = _snapshot(host.home)
    rc, _ = _drive(host, {"Choose mode": "multi-model:litellm", "Where is your gateway": "skipped",
                          "Select providers": INTERRUPT})
    _assert_clean_cancel(rc, capsys, host, before, step="Provider backends")


def test_cancel_after_the_gateway_save_lists_what_was_done(host, capsys):
    host.monkeypatch.setattr(detection, "detect_container_runtime",
                             lambda: ("docker", types.SimpleNamespace(installed=True, version="1", binary_path="/d")))
    host.monkeypatch.setattr(detection, "detect_compose", lambda cli: True)
    rc, _ = _drive(host, {"Choose mode": "multi-model:litellm", "Where is your gateway": "local-docker",
                          "Bind interface": "127.0.0.1", "Port": "4000", "Container image": "",
                          "Pull": ABORT})
    err = capsys.readouterr().err
    assert rc == 130 and "Traceback" not in err
    assert len(host.saves) == 1
    assert "Already done in this run" in err and "saved gateway state" in err
    assert "Nothing was applied" not in err
    assert state.locked_run(lambda: 0) == 0


def test_cancel_after_the_tools_offer_installed_something_lists_it(host, capsys):
    """The real tools.offer records its install, so the cancel message does not say 'Nothing was applied'."""
    mp = host.monkeypatch
    tool_id = next(iter(tools.TOOLS))
    mp.setattr(tools, "offer", _REAL_OFFER)
    # offer() now takes ONE detection pass (versions for the single list), so the fake is that pass, not missing_tools
    mp.setattr(tools, "_detect_tools", lambda: {t: detection.Detected(t, t != tool_id, "1", "/bin/" + t)
                                                for t in tools.TOOLS})
    mp.setattr(tools, "_run_install", lambda cmd, timeout=300: (True, ""))
    rc, script = _drive(host, {"Tick the tools the kit should use": [tool_id], "Sign in now": [], "Choose mode": ABORT})
    err = capsys.readouterr().err
    assert rc == 130 and "Traceback" not in err
    assert any(m.startswith("checkbox: Tick the tools the kit should use") for m in script.asked)
    assert f"installed {tools.TOOLS[tool_id].label}" in err
    assert "Nothing was applied" not in err


def test_cancel_in_the_middle_of_step9_lists_the_files_already_written(host, capsys):
    """Real _step9_apply: executors.yaml is written, then a Ctrl-C hits before the rest of the apply."""
    from ai_resources import selection as sel
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "openrouter"
    s.last_run = "2026-01-01T00:00:00+00:00"
    s.set_selection(sel.Selection(shape="single", providers={"anthropic": sel.ProviderSel()},
                                  slots={"anthropic:sonnet": sel.SlotSel("anthropic/claude-sonnet-5-5")},
                                  primary="anthropic:sonnet", smoke_path="direct"))
    state.config_root().mkdir(parents=True, exist_ok=True)
    state.save(s)
    host.saves.clear()

    def interrupt(*a, **k):
        raise KeyboardInterrupt
    host.monkeypatch.setattr(wizard, "_apply_lanes_file", interrupt)
    from ai_resources.setup import model_selection as ms
    rc, _ = _drive(host, {"Use previous answers": True, "Choose mode": "multi-model:openrouter",
                          "Update OPENROUTER_API_KEY": False, "Model preset": "custom", "How do you want to choose models": "profile",
                          "Choose profile": lambda v: v[0], "Customize any role": False,
                          "Configure detected cockpits": lambda v: list(v), "Apply this?": ms.APPLY})
    err = capsys.readouterr().err
    assert rc == 130 and "Traceback" not in err
    assert state.executors_path().exists()
    assert "Already done in this run" in err and "wrote executors.yaml" in err
    assert "Nothing was applied" not in err


def test_cancel_inside_a_cockpit_configure_names_the_partial_write(host, capsys):
    """Gap 4: Ctrl-C inside mod.configure leaves a partial write; the cancel block says so."""
    from ai_resources import selection as sel
    from ai_resources.setup import model_selection as ms
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "openrouter"
    s.last_run = "2026-01-01T00:00:00+00:00"
    s.set_selection(sel.Selection(shape="single", providers={"anthropic": sel.ProviderSel()},
                                  slots={"anthropic:sonnet": sel.SlotSel("anthropic/claude-sonnet-5-5")},
                                  primary="anthropic:sonnet", smoke_path="direct"))
    state.config_root().mkdir(parents=True, exist_ok=True)
    state.save(s)
    host.saves.clear()

    def interrupt(ctx):
        raise KeyboardInterrupt
    host.monkeypatch.setattr(wizard.ALL_COCKPITS["claude"], "configure", interrupt)
    rc, _ = _drive(host, {"Use previous answers": True, "Choose mode": "multi-model:openrouter",
                          "Update OPENROUTER_API_KEY": False, "Model preset": "custom", "How do you want to choose models": "profile",
                          "Choose profile": lambda v: v[0], "Customize any role": False,
                          "Configure detected cockpits": lambda v: list(v), "Apply this?": ms.APPLY})
    err = capsys.readouterr().err
    assert rc == 130 and "Traceback" not in err
    assert "may be partially written: claude" in err
    assert "Nothing was applied" not in err
