"""Provider adapters: naming, ordering and channels. Pure; the google cases use the live-catalog fixture."""
from __future__ import annotations

import json
import pathlib

import pytest

from ai_resources import model_providers as mp

FIX = pathlib.Path(__file__).parent / "fixtures" / "models"


def _ids(name: str, provider: str) -> list[str]:
    data = json.loads((FIX / name).read_text())
    return [m["key"].split("/", 1)[1] for m in data["models"] if m["key"].startswith(provider + "/")]


@pytest.mark.parametrize("spelling", ["claude-haiku-4-5", "anthropic/claude-haiku-4.5", "claude-haiku-4-5@20251001"])
def test_every_spelling_of_a_claude_id_yields_the_same_ref(spelling):
    ref = mp.get("anthropic").from_any_spelling(spelling)
    assert (ref.provider, ref.family, ref.version) == ("anthropic", "haiku", (4, 5))


def test_the_newest_stable_google_flash_in_the_live_catalog():
    g = mp.get("google")
    refs = [g.parse(i) for i in _ids("catalog-google.json", "google")]
    flash = [r for r in refs if r and r.family == "gemini-flash" and r.channel == mp.STABLE]
    assert max(flash, key=lambda r: r.version).id == "gemini-3.8-flash"


def test_channels_and_families_of_the_google_catalog():
    g = mp.get("google")
    assert g.parse("gemini-flash-latest").channel == mp.ALIAS
    assert g.parse("gemini-3.1-pro-preview").channel == mp.PREVIEW
    lite = g.parse("gemini-3.5-flash-lite")
    assert lite.family == "gemini-flash-lite" and lite.channel == mp.STABLE
    assert g.parse("gemini-3.5-flash").family == "gemini-flash"


def test_an_id_no_family_matches_is_not_parsed():
    assert mp.get("google").parse("gemma-4-31b-it") is None
    assert mp.get("anthropic").parse("claude-mythos-5") is None
    assert mp.get("ollama").parse("qwen2.5-coder:32b") is None


def test_newer_compares_versions_inside_one_family_only():
    a = mp.get("google")
    assert a.newer(a.parse("gemini-3.8-flash"), a.parse("gemini-3.7-flash"))
    assert not a.newer(a.parse("gemini-3.7-flash"), a.parse("gemini-3.8-flash"))
    assert not a.newer(a.parse("gemini-3.8-flash"), a.parse("gemini-3.5-flash-lite"))


def test_other_vendors_name_their_families():
    assert mp.get("openai").parse("gpt-5.6-terra").slot == "openai:gpt-terra"
    assert mp.get("deepseek").parse("deepseek-v4-flash").slot == "deepseek:deepseek-flash"
    assert mp.get("moonshot").parse("kimi-k2.7-code").version == (2, 7)


def test_namespaced_ids_resolve_through_the_openrouter_namespace():
    assert mp.get("moonshot").from_any_spelling("moonshotai/kimi-k2.7-code").family == "kimi-code"
    assert mp.get("google").from_any_spelling("google/gemini-3.7-flash").version == (3, 7)


def test_identify_finds_the_owning_adapter():
    assert mp.identify("gemini-3.8-flash").provider == "google"
    assert mp.identify("claude-opus-5").provider == "anthropic"
    assert mp.identify("nonsense") is None


def test_vertex_and_ollama_have_no_smoke_path_or_no_family():
    assert mp.get("vertex").smoke_kinds == ()
    assert mp.get("ollama").families == ()
