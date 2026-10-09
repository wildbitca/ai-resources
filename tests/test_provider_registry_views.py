"""The adapter registry agrees with the legacy provider tables it sits beside (drift guard)."""
from __future__ import annotations

import pytest

from ai_resources import model_pins, model_providers as mp
from ai_resources.setup import compat, credentials, litellm, providers

# Snapshot of the values before the registry existed (S3d): a change here is a deliberate decision.
FROZEN_PROVIDER_IDS = ["anthropic", "google", "vertex", "openai", "ollama", "openrouter", "deepseek", "moonshot"]
FROZEN_PROVIDER_KEYS = {
    "anthropic": ["ANTHROPIC_API_KEY"], "google": ["GEMINI_API_KEY"], "openai": ["OPENAI_API_KEY"],
    "vertex": ["GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"],
    "openrouter": ["OPENROUTER_API_KEY"], "deepseek": ["DEEPSEEK_API_KEY"], "moonshot": ["MOONSHOT_API_KEY"],
}
FROZEN_UPSTREAM = {
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"), "google": ("gemini", "GEMINI_API_KEY"),
    "openai": ("openai", "OPENAI_API_KEY"), "deepseek": ("deepseek", "DEEPSEEK_API_KEY"),
    "moonshot": ("moonshot", "MOONSHOT_API_KEY"), "moonshotai": ("moonshot", "MOONSHOT_API_KEY"),
    "x-ai": ("xai", "XAI_API_KEY"),
}


def test_legacy_tables_still_hold_their_frozen_values():
    assert list(providers.PROVIDERS) == FROZEN_PROVIDER_IDS
    assert credentials.PROVIDER_KEYS == FROZEN_PROVIDER_KEYS
    assert litellm._VENDOR_UPSTREAM == FROZEN_UPSTREAM


def test_registry_ids_equal_provider_ids():
    assert mp.ids() == list(providers.PROVIDERS)


def test_compat_columns_are_registry_ids_not_literals():
    assert set(compat.PROVIDER_COLUMNS) == set(mp.model_families())
    assert set(compat.PROVIDER_COLUMNS) <= set(mp.REGISTRY)


@pytest.mark.parametrize("pid", FROZEN_PROVIDER_IDS)
def test_credential_env_vars_match_the_provider_table(pid):
    a, p = mp.get(pid), providers.PROVIDERS[pid]
    expected = ({p.primary_env_var} | set(p.extra_env_vars)) - {""}
    assert set(a.credential) == expected


@pytest.mark.parametrize("pid", sorted(set(FROZEN_UPSTREAM) & set(FROZEN_PROVIDER_IDS)))
def test_litellm_prefix_and_key_match_the_upstream_table(pid):
    a = mp.get(pid)
    assert (a.litellm_prefix, a.credential[0]) == litellm._VENDOR_UPSTREAM[pid]


def test_openrouter_namespaces_are_known_to_the_upstream_table():
    for a in mp.REGISTRY.values():
        if a.openrouter_ns and a.id not in ("vertex",):
            assert a.openrouter_ns in litellm._VENDOR_UPSTREAM or a.openrouter_ns == a.id


def test_anthropic_defaults_are_the_kit_pins():
    assert mp.get("anthropic").defaults == model_pins.DEFAULTS
    assert litellm._CLAUDE_PASSTHROUGH_MODELS == list(model_pins.DEFAULTS.values())


@pytest.mark.parametrize("pid", ["anthropic", "google", "vertex", "openai", "deepseek", "moonshot"])
def test_known_direct_models_parse_with_their_adapter(pid):
    for model_id in providers.KNOWN_MODELS[pid]:
        assert mp.get(pid).from_any_spelling(model_id), (pid, model_id)


def test_known_openrouter_models_parse_with_the_owning_adapter():
    for ref in providers.KNOWN_MODELS["openrouter"]:
        ns = ref.split("/", 1)[0]
        owner = next((a for a in mp.REGISTRY.values() if a.openrouter_ns == ns), None)
        if owner is None:      # x-ai is a gateway-only mapping, not selectable (OQ10)
            assert ns == "x-ai"
            continue
        assert owner.from_any_spelling(ref), ref


def test_kit_defaults_parse_and_are_stable():
    for a in mp.REGISTRY.values():
        for family, model_id in a.defaults.items():
            ref = a.from_any_spelling(model_id)
            assert ref and ref.family == family, (a.id, family, model_id)
