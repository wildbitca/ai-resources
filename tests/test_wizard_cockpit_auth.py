"""Each selected cockpit asks its OWN sign-in and no global team/gateway question exists (ADR-0015 D1, acceptance 3)."""
from __future__ import annotations

import argparse
import itertools
import types

import pytest

from ai_resources.setup import cockpit_auth, detection, model_selection as ms, state, tools, ui, wizard
from ai_resources.setup.cockpits import ALL
from wizard_fakes import Script, fake_openclaw, host_env

ASKED_BY = {"claude": "claude", "openclaw": "openclaw", "agy": "gemini", "cursor": "cursor"}
FIVE = ("claude", "openclaw", "agy", "cursor", "codex")
SUBSETS = [c for n in range(1, 5) for c in itertools.combinations(FIVE, n)]
# "openrouter" may appear as ONE OF OpenClaw's own providers in its multiselect; it is not the kit gateway of ADR-0009.
FORBIDDEN = ("how should they work", "pick how they work", "advanced", "gateway", "litellm", "as a team",
             "each on its own")


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False, backend="", models="",
              shape="", providers="", smoke_path="", cockpits="")
    ns.update(kw)
    return argparse.Namespace(**ns)


def _state(*members):
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": list(members)}}
    return s


@pytest.mark.parametrize("members", SUBSETS, ids=lambda m: "+".join(m))
def test_only_the_selected_cockpits_ask_their_own_sign_in(monkeypatch, members):
    asked = []
    for cockpit, module in ASKED_BY.items():
        mod = ALL[module]
        monkeypatch.setattr(mod, "ask_auth", lambda ui_mod, s, detected=None, dry_run=False, _c=cockpit:
                            asked.append(_c) or 0)
    s = _state(*members)
    Script().install(monkeypatch)
    assert wizard._step_cockpit_auth(s, _args()) == 0
    assert asked == [c for c in ("claude", "openclaw", "agy", "cursor") if c in members]


def test_each_cockpit_records_its_own_method_name(monkeypatch):
    s = _state("claude", "openclaw", "agy", "cursor")
    fake_openclaw(monkeypatch)
    Script(**{"Claude Code sign-in": "keep", "OpenClaw sign-in": "keep"}).install(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert wizard._step_cockpit_auth(s, _args()) == 0
    got = {k: v["method"] for k, v in s.cockpit_auth.items()}
    assert s.cockpit_auth["claude"]["live"] == "api_key"          # the key is live (ANTHROPIC_API_KEY), so "keep" records it
    assert got["claude"] == "api_key" and got["agy"] == "google_account" and got["cursor"] == "managed"
    assert got["openclaw"] in cockpit_auth.OPENCLAW_PROVIDER_METHODS
    assert cockpit_auth.providers(s) == {"anthropic": "api_key", "openai": "api_key"}
    assert s.cli_team_api is True and s.cli_team is True and s.mode == "single-model"


def test_cursor_asks_no_question_and_agy_only_asks_keep_or_change(monkeypatch):
    s = _state("cursor", "agy")
    script = Script()
    script.install(monkeypatch)
    assert wizard._step_cockpit_auth(s, _args()) == 0
    assert script.asked == ["select: Antigravity sign-in:"]               # keep by default; Cursor asks nothing
    shown = " ".join(script.shown)
    assert "managed in Cursor" in shown and "Antigravity:" in shown


def test_a_cancel_inside_a_cockpit_question_stops_the_step(monkeypatch):
    s = _state("claude", "agy")
    monkeypatch.setattr(ALL["claude"], "ask_auth", lambda *a, **k: 130)
    Script().install(monkeypatch)
    assert wizard._step_cockpit_auth(s, _args()) == 130
    assert "agy" not in s.cockpit_auth


@pytest.mark.parametrize("members", [("claude",), ("claude", "openclaw"), ("openclaw",)],
                         ids=lambda m: "+".join(m))
def test_a_fresh_run_reaches_the_review_with_no_team_or_gateway_question(monkeypatch, tmp_path, members):
    host_env(monkeypatch, tmp_path)
    real_offer = tools.offer
    monkeypatch.setattr(tools, "offer", real_offer)
    present = {c: types.SimpleNamespace(installed=True, version="1.0", binary_path=f"/b/{c}", name=c) for c in members}
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: dict(present))
    monkeypatch.setattr(detection, "agy_installed", lambda: "agy" in members)
    found = {t: detection.Detected(t, t in members, "1.0", f"/b/{t}") for t in tools.TOOLS}
    monkeypatch.setattr(tools, "_detect_tools", lambda: found)
    monkeypatch.setattr(tools, "install_one", lambda *a, **k: (_ for _ in ()).throw(AssertionError("installed")))
    monkeypatch.setattr(wizard, "_PRESET", wizard._PRESET)
    monkeypatch.setattr(ui, "review_table", lambda rows, title="": None)
    # agy needs its terms check only when lanes are enabled; the OpenClaw questions are stubbed (their own tests)
    from ai_resources.setup.cockpits import openclaw as openclaw_cockpit
    for name in ("prompt", "prompt_cockpit", "prompt_model", "prompt_audio", "prompt_more", "prompt_image"):
        monkeypatch.setattr(openclaw_cockpit, name, lambda *a, **k: None)
    from ai_resources.setup.cockpits import _openclaw_channels
    monkeypatch.setattr(_openclaw_channels, "prompt", lambda *a, **k: None)
    fake_openclaw(monkeypatch)
    script = Script(**{"Tick the tools": list(members), "Sign in now": [], "Claude Code sign-in": "keep",
                       "OpenClaw sign-in": "keep",
                       "Starting team": "balanced", "Apply this?": ms.APPLY})
    script.install(monkeypatch)
    seen = {}
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: seen.setdefault("s", s) and 0)
    assert wizard.run(_args()) == 0
    text = " | ".join(script.asked + script.titles).lower()
    assert not any(w in text for w in FORBIDDEN), [w for w in FORBIDDEN if w in text]
    sign_in = [m for m in script.asked if "Claude Code sign-in" in m and m.startswith("select")]
    assert len(sign_in) == (1 if "claude" in members else 0)
    auth = seen["s"].cockpit_auth
    assert set(auth) == {c for c in members if c in ("claude", "openclaw", "agy")}
    assert cockpit_auth.secret_findings(auth) == []
