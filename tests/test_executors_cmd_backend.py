"""`ai-resources executors set/tune` must offer IDs the active backend accepts.

Two defects fixed here:

1. `_select_provider_and_model` (used by `executors set`/`tune`) always offered
   `KNOWN_MODELS[provider]` regardless of the configured backend. Under
   OpenRouter that menu is full of bare IDs OpenRouter rejects
   (e.g. "claude-opus-5"); under LiteLLM it is full of OpenRouter-namespaced
   IDs LiteLLM's vendor table cannot map (e.g. "moonshotai/kimi-k2.7-code").
   Both catalogues are wrong for the other backend because they disagree on
   spelling, not just presence (dashes vs dots for Anthropic).

2. `executors test` picked its round-trip result with `run_all(...)[-1][1:]`,
   a purely positional read. `run_all` can front-load a credential or
   gateway-health probe ahead of the per-model round-trip and return early —
   with no round-trip entry at all — the moment one of those fails, so the
   "last" result is not reliably the model probe.

No network, no credentials, no gateway, no rich/questionary (CI has neither).
Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import executors_cmd as ec  # noqa: E402
from ai_resources.setup import ui  # noqa: E402


# --- backend-aware model catalogues ---------------------------------------------

def test_openrouter_offers_only_namespaced_ids():
    known = ec._known_models_for_backend("openrouter")
    for vendor, models in known.items():
        for model in models:
            assert "/" in model, f"{vendor}: {model!r} is not namespaced for OpenRouter"


def test_litellm_offers_each_vendors_own_direct_spelling():
    known = ec._known_models_for_backend("litellm")
    # LiteLLM never routes through the "openrouter" pseudo-vendor.
    assert "openrouter" not in known
    for vendor, models in known.items():
        for model in models:
            assert model.startswith(f"{vendor}/"), f"{model!r} not namespaced under {vendor}"


def test_anthropic_spelling_diverges_between_backends():
    """The catalogue spells Haiku with a dot, Anthropic itself with a dash —
    offering the wrong one under either backend produces a rejected model ID."""
    openrouter = ec._known_models_for_backend("openrouter")["anthropic"]
    litellm = ec._known_models_for_backend("litellm")["anthropic"]

    assert "anthropic/claude-haiku-4.5" in openrouter
    assert "anthropic/claude-haiku-4-5" not in openrouter

    assert "anthropic/claude-haiku-4-5" in litellm
    assert "anthropic/claude-haiku-4.5" not in litellm


def test_moonshot_vendor_prefix_diverges_between_backends():
    """OpenRouter namespaces Kimi as `moonshotai`; the direct API is `moonshot`.
    Offering the OpenRouter spelling to LiteLLM produced an ID its vendor table
    could not map, and vice versa."""
    openrouter = ec._known_models_for_backend("openrouter")
    litellm = ec._known_models_for_backend("litellm")

    assert "moonshotai" in openrouter and "moonshot" not in openrouter
    assert "moonshot" in litellm and "moonshotai" not in litellm
    assert "moonshotai/kimi-k2.7-code" in openrouter["moonshotai"]
    assert "moonshot/kimi-k2.7-code" in litellm["moonshot"]


# --- interactive selection, backend-threaded ------------------------------------

@pytest.fixture
def non_interactive():
    ui.set_non_interactive(True)
    yield
    ui.set_non_interactive(False)


def test_select_under_openrouter_returns_the_namespaced_default(non_interactive):
    """A vendor with no PROVIDERS registry entry (moonshotai, x-ai) must still be
    selectable — building the provider menu from PROVIDERS directly raised."""
    provider, model = ec._select_provider_and_model(
        current_provider="moonshotai",
        current_model="moonshotai/kimi-k2.7-code",
        backend="openrouter",
    )
    assert provider == "moonshotai"
    assert model == "moonshotai/kimi-k2.7-code"


def test_select_under_litellm_canonicalises_a_legacy_bare_default(non_interactive):
    """A pre-canonical by_role entry stored `provider` and a bare `model`
    separately; the menu must still resolve it to today's canonical ID."""
    provider, model = ec._select_provider_and_model(
        current_provider="anthropic",
        current_model="claude-opus-5",
        backend="litellm",
    )
    assert provider == "anthropic"
    assert model == "anthropic/claude-opus-5"


def test_select_never_hands_the_openrouter_spelling_to_litellm(non_interactive):
    """Regression for the exact reported defect: under litellm, the dotted
    catalogue spelling of haiku is not a valid choice at all — it is not a
    member of the litellm-side list, so the prompt has no matching default
    and (non-interactively) aborts rather than silently keeping the
    wrong-backend spelling as if it had been offered and accepted."""
    with pytest.raises(SystemExit):
        ec._select_provider_and_model(
            current_provider="anthropic", current_model="anthropic/claude-haiku-4.5",
            backend="litellm",
        )


# --- vendor / label helpers used by set, tune, and the diff table ---------------

def test_vendor_of_prefers_the_canonical_prefix():
    assert ec._vendor_of({"model": "google/gemini-3.7-flash"}) == "google"


def test_vendor_of_falls_back_to_legacy_provider_field():
    assert ec._vendor_of({"provider": "anthropic", "model": "claude-sonnet-5"}) == "anthropic"


def test_model_label_does_not_double_the_vendor_prefix():
    """A canonical model already names its vendor; joining it with a leftover
    `provider` field produced "anthropic/anthropic/claude-opus-5"."""
    cfg = {"provider": "anthropic", "model": "anthropic/claude-opus-5"}
    assert ec._model_label(cfg) == "anthropic/claude-opus-5"


def test_model_label_joins_legacy_bare_entries():
    cfg = {"provider": "anthropic", "model": "claude-opus-5"}
    assert ec._model_label(cfg) == "anthropic/claude-opus-5"


# --- smoke-result selection for `executors test` --------------------------------

def test_pick_smoke_result_uses_the_round_trip_entry_when_all_pass():
    results = [
        ("Gateway health  http://x", True, ""),
        ("Model round-trip  anthropic/claude-sonnet-5", True, "served by anthropic/claude-sonnet-5"),
    ]
    ok, msg = ec._pick_smoke_result(results)
    assert ok is True
    assert "served by" in msg


def test_pick_smoke_result_fails_on_an_earlier_health_failure():
    """Positional `[-1]` treated this single-entry list as a pass: the only
    result was the health probe, and its own message happened to be empty."""
    results = [("Gateway health  http://x", False, "endpoint unreachable")]
    ok, msg = ec._pick_smoke_result(results)
    assert ok is False
    assert msg == "endpoint unreachable"


def test_pick_smoke_result_fails_on_a_missing_credential():
    results = [("Gateway credential present", False, "LITELLM_MASTER_KEY missing")]
    ok, msg = ec._pick_smoke_result(results)
    assert ok is False
    assert "missing" in msg


def test_pick_smoke_result_fails_when_no_round_trip_ran_at_all():
    """Every probe reported ok, but none of them was the model round-trip — the
    old code would have reported this as success."""
    results = [("Gateway health  http://x", True, "")]
    ok, _msg = ec._pick_smoke_result(results)
    assert ok is False


def test_pick_smoke_result_fails_on_an_empty_list():
    """`results[-1]` on an empty list raises IndexError instead of failing the role."""
    ok, msg = ec._pick_smoke_result([])
    assert ok is False
    assert msg
