"""Screen 4, the review: the answers by name and Apply / Change tools / Change how / Change team / More options /
Cancel (ADR-0009; it replaced "Edit a section..." and "Change models")."""
from __future__ import annotations

import argparse

import pytest

from ai_resources import selection as sel
from ai_resources.setup import credentials, model_selection as ms, profiles, state, ui, wizard
from wizard_fakes import SENTINEL, Script, Seq, host_env

SUB = "subscription-claude-55"


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


def _seed_openrouter():
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "openrouter"
    s.last_run = "2026-01-01T00:00:00+00:00"
    s.set_selection(sel.Selection(shape="single", providers={"anthropic": sel.ProviderSel()},
                                  slots={"anthropic:sonnet": sel.SlotSel("anthropic/claude-sonnet-5-5")},
                                  primary="anthropic:sonnet", smoke_path="direct"))
    state.config_root().mkdir(parents=True, exist_ok=True)
    state.save(s)


@pytest.fixture
def review_env(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    rows_seen = []
    monkeypatch.setattr(ui, "review_table", lambda rows, title="": rows_seen.append(dict(rows)))
    return rows_seen


def test_rows_come_from_the_state_and_never_show_a_secret(monkeypatch):
    monkeypatch.setattr(credentials, "get_key", lambda name, *a, **k: SENTINEL if name == "OPENROUTER_API_KEY" else "")
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "openrouter"
    rows = dict(wizard._review_rows(s))
    assert rows["How"] == "Advanced: openrouter gateway" and rows["Gateway"] == "openrouter (hosted)"
    assert rows["API keys"] == "OPENROUTER_API_KEY set"
    assert SENTINEL not in " ".join(f"{k} {v}" for k, v in rows.items())
    assert set(rows) == {"How", "Team", "Tools", "Gateway", "Providers", "API keys", "Will write"}
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "")
    assert dict(wizard._review_rows(s))["API keys"] == "OPENROUTER_API_KEY missing"


def test_a_path_without_a_gateway_has_no_gateway_rows():
    s = state.SetupState()
    s.mode = "single-model"
    rows = dict(wizard._review_rows(s))
    assert set(rows) == {"How", "Team", "Tools", "Will write"} and rows["How"] == "Each on its own (no gateway)"
    assert dict(wizard._review_rows(_team_state()))["How"] == "As a team, on your subscriptions (no gateway)"


def _team_state(*members):
    s = state.SetupState()
    s.mode, s.cli_team = "single-model", True
    s.profile.name = SUB
    s.profile.customizations = {"__targets__": {"selected": list(members or ("claude", "agy"))}}
    return s


def test_the_review_names_the_tools_and_the_team():
    rows = dict(wizard._review_rows(_team_state("claude", "codex")))
    assert rows["Tools"] == "Claude Code, Codex CLI" and rows["Team"].startswith("Balanced: ")
    assert rows["Will write"] == "executors.yaml, tool config files"
    custom = _team_state()
    custom.profile.customized = True
    assert dict(wizard._review_rows(custom))["Team"] == f"Custom: {SUB} (customized)"


def test_the_review_offers_the_three_changes_and_more_options_after_a_full_pass(review_env, monkeypatch):
    _seed_openrouter()
    seen = {}

    def apply_pick(values):
        seen["values"] = list(values)
        return ms.APPLY
    script = Script(**{"Use previous answers": True, "Choose mode": "multi-model:openrouter",
                       "Update OPENROUTER_API_KEY": False, "Model preset": "custom", "How do you want to choose models": "profile",
                       "Choose profile": lambda v: v[0], "Customize any role": False,
                       "Configure detected cockpits": lambda v: list(v), "Apply this?": apply_pick})
    script.install(monkeypatch)
    assert wizard.run(_args()) == 0
    values = seen["values"]
    assert values == [ms.APPLY, wizard.CHANGE_TOOLS, wizard.CHANGE_HOW, wizard.CHANGE_TEAM, wizard.MORE_OPTIONS,
                      ms.CANCEL]
    assert len(review_env) == 1 and review_env[0]["How"] == "Advanced: openrouter gateway"


def test_change_team_with_a_picked_team_asks_the_picker_again_and_returns_to_the_review(review_env, monkeypatch):
    _seed_openrouter()
    available = profiles.list_profiles(mode="multi-model", backend="openrouter")
    assert len(available) >= 2
    script = Script(**{"Use previous answers": True, "Choose mode": "multi-model:openrouter",
                       "Update OPENROUTER_API_KEY": False, "Model preset": "custom", "How do you want to choose models": "profile",
                       "Show how": False, "Choose profile": Seq(lambda v: v[0], lambda v: v[1]), "Customize any role": False,
                       "Configure detected cockpits": lambda v: list(v),
                       "Apply this?": Seq(wizard.CHANGE_TEAM, ms.APPLY)})
    script.install(monkeypatch)
    assert wizard.run(_args()) == 0
    asked = script.asked
    # Change team re-opens "Your team" (Custom, then the models question and the profile list); the gateway key
    # question belongs to an earlier screen and is not asked again
    assert len([m for m in asked if m.startswith("select: How do you want to choose models")]) == 2
    assert len([m for m in asked if m.startswith("confirm: Update OPENROUTER_API_KEY")]) == 1
    # this host recorded a team picked from its accounts, so Change team asks that picker again (not the profile list)
    assert len([m for m in asked if m.startswith("select: Choose profile")]) == 1
    assert len(review_env) == 2 and review_env[0]["Team"] != review_env[1]["Team"]


def test_cancel_at_the_review_is_130_and_the_apply_never_runs(review_env, monkeypatch):
    _seed_openrouter()
    applied = []
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: applied.append(1) or 0)
    script = Script(**{"Use previous answers": True, "Choose mode": "multi-model:openrouter",
                       "Update OPENROUTER_API_KEY": False, "Model preset": "custom", "How do you want to choose models": "profile",
                       "Choose profile": lambda v: v[0], "Customize any role": False,
                       "Configure detected cockpits": lambda v: list(v), "Apply this?": ms.CANCEL})
    script.install(monkeypatch)
    assert wizard.run(_args()) == 130
    assert applied == []


def test_unattended_prints_the_rows_and_offers_no_edit(review_env, monkeypatch):
    s = state.SetupState()
    s.mode = "single-model"
    s.cockpits["claude"] = state.CockpitState(installed=True)
    s.set_selection(sel.Selection(shape="single", providers={"google": sel.ProviderSel()},
                                  slots={"google:gemini-flash": sel.SlotSel("google/gemini-3.8-flash")},
                                  primary="google:gemini-flash", smoke_path="direct"))
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    monkeypatch.setattr(wizard, "_RUNNER", None)
    assert wizard._step_summary(s, _args(non_interactive=True)) == 0
    assert script.asked == [] and len(review_env) == 1


def test_parallel_runs_from_more_options_ask_the_acknowledgement_once(review_env, monkeypatch):
    s = state.SetupState()
    s.mode = "single-model"
    s.last_run = "2026-01-01T00:00:00+00:00"
    s.profile.name = SUB
    s.set_selection(sel.Selection(shape="single", providers={"anthropic": sel.ProviderSel()},
                                  slots={"anthropic:sonnet": sel.SlotSel("anthropic/claude-sonnet-5-5")},
                                  primary="anthropic:sonnet", smoke_path="claude-cli"))
    state.config_root().mkdir(parents=True, exist_ok=True)
    state.save(s)
    script = Script(**{"Use previous answers": True, "Choose mode": "multi-model:cli",
                       "Model preset": "custom", "How do you want to choose models": "profile",
                       "How should kit subagents pick": lambda v: SUB, "Orchestrator agent id": "main",
                       "Acknowledge for these roles?": True, "Configure detected cockpits": lambda v: list(v),
                       "Apply this?": Seq(wizard.MORE_OPTIONS, ms.APPLY), "More options": "parallel"})
    script.install(monkeypatch)
    assert wizard.run(_args(lanes=True)) == 0
    acks = [m for m in script.asked if "Acknowledge for these roles?" in m]
    assert len(acks) == 1
    proposals = [m for m in script.shown if m.startswith("info: Subscription lanes proposed")]
    assert len(proposals) == 2                       # the lanes step really ran again


def test_gateway_and_api_keys_rerun_step_5_and_the_row_moves_from_missing_to_set(review_env, monkeypatch):
    _seed_openrouter()
    saved = []
    monkeypatch.setattr(credentials, "get_key",
                        lambda name, *a, **k: SENTINEL if (saved and name == "OPENROUTER_API_KEY") else "")
    # The first pass finds no key and is answered with an empty value, which step 5 refuses (rc 1); to keep
    # the first pass alive the key is entered on the first ask and only becomes readable after the edit.
    monkeypatch.setattr(credentials, "update_env_tracked", lambda updates: ({}, []))
    script = Script(**{"Use previous answers": True, "Choose mode": "multi-model:openrouter",
                       "OPENROUTER_API_KEY": "entered", "Show how": False, "Model preset": "custom", "How do you want to choose models": "profile",
                       "Choose profile": lambda v: v[0], "Customize any role": False,
                       "Configure detected cockpits": lambda v: list(v),
                       "Apply this?": Seq(wizard.MORE_OPTIONS, ms.APPLY), "More options": "gateway"})
    script.install(monkeypatch)
    real_find = script._find

    def find(message, kind):
        if kind == "password" and "OPENROUTER_API_KEY" in message and len(
                [m for m in script.asked if m.startswith("password:")]) >= 1:
            saved.append(1)                       # from the second ask on the key reads back as set
        return real_find(message, kind)
    script._find = find
    assert wizard.run(_args()) == 0
    passwords = [m for m in script.asked if m.startswith("password:")]
    assert len(passwords) == 2
    assert len(review_env) == 2
    assert review_env[0]["API keys"] == "OPENROUTER_API_KEY missing"
    assert review_env[1]["API keys"] == "OPENROUTER_API_KEY set"


def test_cancel_at_the_review_prints_the_shared_cancel_block(review_env, monkeypatch, capsys):
    """Gap 5: same block as every other cancel, listing earlier real actions, not apply-scoped wording."""
    _seed_openrouter()
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: 0)
    real = wizard._step_summary

    def with_action(s, args):
        ui.record_action("installed something")
        return real(s, args)
    monkeypatch.setattr(wizard, "_step_summary", with_action)
    script = Script(**{"Use previous answers": True, "Choose mode": "multi-model:openrouter",
                       "Update OPENROUTER_API_KEY": False, "Model preset": "custom", "How do you want to choose models": "profile",
                       "Choose profile": lambda v: v[0], "Customize any role": False,
                       "Configure detected cockpits": lambda v: list(v), "Apply this?": ms.CANCEL})
    script.install(monkeypatch)
    assert wizard.run(_args()) == 130
    err = capsys.readouterr().err
    assert "Setup cancelled at Review" in err
    assert "Already done in this run: " in err and "installed something" in err
    assert "by the apply step" not in err
