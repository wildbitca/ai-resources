"""ADR-0016 D1 (agy): the Antigravity sign-in step shows whether a Google account state is present (presence only, nothing
read) and hands the terminal to `agy` only on an explicit change."""
from __future__ import annotations

import pytest

from ai_resources.setup import cockpit_auth, live_auth, state, ui
from ai_resources.setup.cockpits import gemini
from wizard_fakes import Script


@pytest.fixture
def agy(monkeypatch, tmp_path):
    home = tmp_path / ".gemini"
    home.mkdir()
    monkeypatch.setattr(live_auth, "agy_home", lambda: home)
    handed = []
    monkeypatch.setattr(live_auth, "handoff", lambda argv: handed.append(list(argv)) or 0)
    return type("A", (), {"home": home, "handed": handed})


def ask(monkeypatch, answers, *, dry_run=False, non_interactive=False):
    script = Script(**answers)
    script.install(monkeypatch, non_interactive=non_interactive)
    s = state.SetupState()
    return gemini.ask_auth(ui, s, None, dry_run), script, s


def test_a_signed_in_account_is_shown_by_presence_only(agy, monkeypatch):
    (agy.home / "google_accounts.json").write_text('{"active": "someone@example.invalid", "old": []}', encoding="utf-8")
    rc, script, s = ask(monkeypatch, {"Antigravity sign-in": "keep"})
    assert rc == 0 and agy.handed == []
    assert cockpit_auth.live_kind(s, "agy") == "oauth_google" and cockpit_auth.method(s, "agy") == "google_account"
    shown = " ".join(script.shown) + " ".join(script.asked)
    assert "Google account signed in" in shown and "someone" not in shown and "example.invalid" not in shown
    assert "someone" not in repr(s.cockpit_auth)


def test_no_account_state_reads_not_signed_in(agy, monkeypatch):
    rc, script, s = ask(monkeypatch, {"Antigravity sign-in": "keep"})
    assert cockpit_auth.live_kind(s, "agy") == "none" and any("not signed in" in m for m in script.shown)


def test_change_hands_the_terminal_to_agy_once_and_reads_again(agy, monkeypatch):
    script = Script(**{"Antigravity sign-in": "change"})
    script.install(monkeypatch)
    s = state.SetupState()

    def handoff(argv):
        agy.handed.append(list(argv))
        (agy.home / "google_accounts.json").write_text("{}", encoding="utf-8")        # the sign-in created the state
        return 0
    monkeypatch.setattr(live_auth, "handoff", handoff)
    assert gemini.ask_auth(ui, s, None, False) == 0
    assert agy.handed == [["agy"]] and cockpit_auth.live_kind(s, "agy") == "oauth_google"


def test_keep_a_dry_run_and_a_non_interactive_run_never_hand_over(agy, monkeypatch):
    ask(monkeypatch, {"Antigravity sign-in": "keep"})
    ask(monkeypatch, {"Antigravity sign-in": "change"}, dry_run=True)
    ask(monkeypatch, {}, non_interactive=True)
    assert agy.handed == []
