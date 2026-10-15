"""Inline validation at the wizard prompts (plan S9): the answer is checked while typing, not after."""
from __future__ import annotations

import pytest

from ai_resources.setup import state, ui, wizard


@pytest.mark.parametrize("value,expected", [
    ("4000", True), ("", True), (" 80 ", True), ("65535", True),
    ("abc", "Enter a port between 1 and 65535"), ("0", "Enter a port between 1 and 65535"),
    ("65536", "Enter a port between 1 and 65535"), ("-1", "Enter a port between 1 and 65535"),
    ("40.5", "Enter a port between 1 and 65535"),
])
def test_port_validator(value, expected):
    assert wizard._validate_port(value) == expected


@pytest.mark.parametrize("value,ok", [
    ("https://litellm.example.com", True), ("http://localhost:4000", True),
    ("ftp://host", False), ("litellm.example.com", False), ("http://", False), ("", False), ("httpx", False),
])
def test_url_validator(value, ok):
    assert (wizard._validate_url(value) is True) == ok


@pytest.mark.parametrize("value,ok", [("claude-sonnet-5-5", True), ("", False), ("   ", False), ("a b", False)])
def test_model_id_validator(value, ok):
    assert (wizard._validate_model_id(value) is True) == ok


def test_required_validator_names_the_thing():
    check = wizard._required("A master key")
    assert check("x") is True and check("  ") == "A master key is required"


class _Recorder:
    """Stands in for ui.text/ui.password: records the validator and answers with the default."""

    def __init__(self, answers=None):
        self.validators: dict[str, object] = {}
        self.answers = answers or {}

    def text(self, message, default="", validate=None):
        self.validators[message] = validate
        return self.answers.get(message, default)

    def password(self, message, validate=None):
        self.validators[message] = validate
        return self.answers.get(message, "secret")


def test_the_port_prompt_passes_its_validator_and_a_bad_value_never_reaches_int(monkeypatch):
    rec = _Recorder({"Port": "4100"})
    monkeypatch.setattr(ui, "text", rec.text)
    monkeypatch.setattr(ui, "select", lambda m, c, default=None, **k: default)
    s = state.SetupState()
    wizard._ask_bind_and_port(s)
    assert s.litellm.local.port == 4100
    check = rec.validators["Port"]
    assert check("abc") == "Enter a port between 1 and 65535"      # shown inline; no TypeError, no crash
    assert check("4000") is True


def test_the_remote_url_prompt_is_validated(monkeypatch):
    rec = _Recorder({"Remote LiteLLM URL (e.g. https://litellm.example.com)": "https://gw.example.com"})
    monkeypatch.setattr(ui, "text", rec.text)
    monkeypatch.setattr(ui, "password", rec.password)
    monkeypatch.setattr(ui, "console", lambda: type("C", (), {"print": lambda *a, **k: None})())
    monkeypatch.setattr(wizard.credentials, "update_env_tracked", lambda u: (None, []))
    monkeypatch.setattr(wizard.litellm, "validate_remote", lambda url, key: (True, "ok"))
    s = state.SetupState()
    assert wizard._step3_remote(s, dry_run=False) == 0
    url_check = rec.validators["Remote LiteLLM URL (e.g. https://litellm.example.com)"]
    assert url_check("ftp://x") != True and url_check("https://x.example") is True      # noqa: E712
    assert rec.validators["Master key (will be stored in .env)"]("") == "A master key is required"


def test_the_custom_model_prompt_is_validated(monkeypatch):
    seen = {}

    def text(message, default="", validate=None):
        seen[message] = validate
        return "claude-sonnet-5-5"

    monkeypatch.setattr(ui, "text", text)
    monkeypatch.setattr(ui, "console", lambda: type("C", (), {"print": lambda *a, **k: None})())
    monkeypatch.setattr(ui, "role_table", lambda *a, **k: None)
    monkeypatch.setattr(ui, "detail", lambda m: None)
    monkeypatch.setattr(ui, "info", lambda m: None)
    monkeypatch.setattr(ui, "section", lambda *a: None)
    monkeypatch.setattr(ui, "confirm", lambda m, default=True, **k: True if m.startswith("Customize") else default)

    def select(message, choices, default=None, **k):
        values = [getattr(c, "value", c) for c in choices]
        if message.startswith("Choose profile"):
            return default
        if message.startswith("Pick a role"):
            return "__done__" if seen else values[0]
        if message.startswith("Vendor for"):
            return values[0]
        if message.startswith("Model for"):
            return "__custom__"
        return default
    monkeypatch.setattr(ui, "select", select)
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "litellm"
    s.providers["anthropic"] = state.ProviderState(enabled=True)
    wizard._step6_profile(s)
    (message, check), = [(m, v) for m, v in seen.items() if m.startswith("Model id for")]
    assert check("") != True and check("claude-opus-5-5") is True      # noqa: E712
