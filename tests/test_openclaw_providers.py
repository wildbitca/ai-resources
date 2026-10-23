"""The OpenClaw provider catalog (ADR-0015 D2, acceptance 8): tolerant parsing, chat providers only, safe fallbacks."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ai_resources.setup import profiles
from ai_resources.setup.cockpits import _openclaw_providers as prov

FIX = Path(__file__).parent / "fixtures" / "openclaw"
LIST = (FIX / "providers_models_list.json").read_text(encoding="utf-8")
AUTH = (FIX / "models_auth_list.json").read_text(encoding="utf-8")


def runner(list_out=(0, LIST), auth_out=(0, AUTH)):
    calls = []

    def run(argv, timeout=120, **k):
        calls.append(list(argv))
        if argv[:2] == ["models", "list"]:
            return list_out
        if argv[:3] == ["models", "auth", "list"]:
            return auth_out
        return 1, "unexpected"
    run.calls = calls
    return run


def test_the_fixture_is_secret_free():
    for text in (LIST, AUTH):
        assert not re.search(r"sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|ya29\.|[A-Za-z0-9_-]{40,}", text)
        json.loads(text)


def test_twelve_providers_become_chat_providers_only():
    d = prov.discover_providers(runner())
    assert d.source == "cli"
    assert d.providers == ("anthropic", "openai", "google", "google-vertex", "openrouter", "deepseek", "mistral", "xai")
    assert "elevenlabs" not in d.providers and "fal" not in d.providers            # speech and image
    assert "claude-cli" not in d.providers and "agy-cli-lane" not in d.providers   # runtimes, not providers
    assert d.backends == frozenset({"anthropic", "google"})
    assert d.models["anthropic"][0] == "claude-sonnet-5-5"


def test_authenticated_providers_are_preselected_with_the_profile_ones():
    d = prov.discover_providers(runner())
    authed = prov.authenticated_providers(runner())
    assert authed == frozenset({"anthropic", "openai"})
    balanced = profiles.load_profile("balanced", "single-model")
    assert prov.profile_providers(balanced) == ["anthropic"]
    assert prov.preselected(d, authed, balanced) == ["anthropic", "openai"]
    assert prov.preselected(d, set(), None) == []


@pytest.mark.parametrize("out", [(1, "openclaw not found on PATH"), (127, ""), (0, "not json at all"), (0, "{"),
                                 (0, json.dumps({"models": "oops"})), (0, json.dumps({"models": [None, 3, {}]})),
                                 (0, json.dumps([]))])
def test_unavailable_or_malformed_output_degrades_to_the_fallback_without_raising(out, tmp_path):
    d = prov.discover_providers(runner(list_out=out), docs_dir=tmp_path / "missing")
    assert d.source == "fallback" and d.providers == prov.KIT_KNOWN and d.problems


def test_a_warning_line_before_the_json_is_tolerated():
    out = "Model discovery authentication was rejected for google.\n" + LIST
    assert prov.discover_providers(runner(list_out=(0, out))).source == "cli"


def test_the_docs_directory_is_the_second_fallback(tmp_path):
    for name in ("anthropic", "openai", "elevenlabs", "fal", "index", "models", "groq", "ollama"):
        (tmp_path / f"{name}.md").write_text("x")
    d = prov.discover_providers(runner(list_out=(1, "no")), docs_dir=tmp_path)
    assert d.source == "docs" and d.providers == ("anthropic", "groq", "ollama", "openai")


def test_a_runner_that_raises_is_a_failure_not_a_crash(tmp_path):
    def boom(argv, timeout=0):
        raise RuntimeError("down")
    d = prov.discover_providers(boom, docs_dir=tmp_path / "none")
    assert d.source == "fallback"
    assert prov.authenticated_providers(boom) == frozenset()


def test_authenticated_providers_survive_a_changed_shape():
    assert prov.authenticated_providers(runner(auth_out=(0, json.dumps({"x": [{"id": "a:b", "provider": "xai"}]})))) == {"xai"}
    assert prov.authenticated_providers(runner(auth_out=(0, "garbage"))) == frozenset()
    assert prov.authenticated_providers(runner(auth_out=(2, AUTH))) == frozenset()
    # a runtime id is the CLI backend of its provider
    assert prov.authenticated_providers(runner(auth_out=(0, json.dumps({"profiles": [{"id": "claude-cli:d", "provider": "claude-cli"}]})))) == {"anthropic"}


def test_discovering_providers_only_issues_read_only_commands():
    """ADR-0016 D1: sign-in belongs to OpenClaw (`openclaw configure --section model`). Reading the catalog and the
    signed-in providers issues list commands only: no key entry, no auth mutation, whatever the providers are."""
    run = runner()
    prov.discover_providers(run)
    prov.authenticated_providers(run)
    assert run.calls and all(c[:2] == ["models", "list"] or c[:3] == ["models", "auth", "list"] for c in run.calls), run.calls
    assert not any(w in " ".join(c) for c in run.calls for w in ("login", "paste", "logout", "set-key", "add", "remove"))


def test_the_real_cli_catalog_shape_is_read_through_the_same_parser():
    """The shape recorded from `openclaw models list --all --json` on 2026.10.5 (keys only, no values of interest)."""
    text = json.dumps({"count": 2, "models": [
        {"key": "anthropic/claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "input": "text+image", "available": True,
         "tags": ["default", "configured"]},
        {"key": "agy-cli-lane/gemini-3.8-flash-low", "name": "gemini-3.8-flash-low", "local": True}],
        "providerOutcomes": [{"provider": "google", "status": "auth-rejected"}]})
    models, backends = prov.parse_models_list(text)
    assert models == {"anthropic": ["claude-sonnet-5-5"]} and backends == frozenset({"google"})
