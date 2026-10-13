"""Pins the corrections from the 2026-10-13 web re-check of the shipped scores database.

Source: .agent-output/research-models/recheck-2026-10-13.md. Values here are the verified board and pricing
figures, not illustrative fixtures."""
from __future__ import annotations

import pytest

from ai_resources.scoring import db as scores_db

SHIPPED = scores_db.load_shipped()
TB = "vals-terminal-bench-4.0"


def _proxy(model):
    return next(b for b in SHIPPED["models"][model]["benchmarks"] if b["board"] == TB)


def _pool(pid):
    return next(p for p in SHIPPED["pools"] if p["id"] == pid)


def _rows(model):
    return [r for r in SHIPPED["rows"] if r["model"] == model]


def test_every_tb4_proxy_carries_the_board_date_2026_10_13():
    for model in SHIPPED["models"]:
        assert _proxy(model)["as_of"] == "2026-10-13", model
    assert not any(r["as_of"] == "2026-10-12" for r in SHIPPED["rows"])
    assert not any("2026-10-12" in r.get("basis", "") for r in SHIPPED["rows"])


def test_haiku_proxy_and_derived_scores_follow_the_rechecked_board():
    b = _proxy("anthropic/claude-haiku-5.5")
    assert b["value"] == 36.36 and b["cost_per_task_usd"] == 3.12
    assert "35.35" not in str(b.get("notes", "")) and "3.05" not in str(b.get("notes", ""))
    rows = _rows("anthropic/claude-haiku-5.5")
    assert rows and all(r["score"] == 3.6 for r in rows)


def test_sonnet_uses_the_adjusted_value_and_the_unsupported_53_variant_is_gone():
    b = _proxy("anthropic/claude-sonnet-5.5")
    assert b["value"] == 62.63 and b["raw_value"] == 64.14
    assert "53.0" not in b["notes"]
    rows = _rows("anthropic/claude-sonnet-5.5")
    assert rows and all(r["score"] == 6.3 for r in rows)


def test_fable_uses_the_adjusted_value_under_the_same_policy_as_opus_and_sonnet():
    b = _proxy("anthropic/claude-fable-5.1")
    assert b["value"] == 50.0 and b["raw_value"] == 58.08
    rows = _rows("anthropic/claude-fable-5.1")
    assert rows and all(r["score"] == 5.0 for r in rows)
    for model in ("anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "anthropic/claude-fable-5.1"):
        assert _proxy(model)["value"] < _proxy(model)["raw_value"], model
        assert "provider-fallback" in _proxy(model)["notes"] or "provider fallback" in _proxy(model)["notes"]


def test_deepseek_value_is_14_14_and_the_per_row_date_is_stated_unknown():
    b = _proxy("deepseek/deepseek-v4-pro")
    assert b["value"] == 14.14 and b["as_of"] == "2026-10-13"
    assert "per-row date is not recorded" in b["notes"] and "inference" in b["notes"]
    rows = _rows("deepseek/deepseek-v4-pro")
    assert rows and all(r["as_of"] == "2026-10-13" for r in rows)
    assert all("per-row date is not recorded" in r["notes"] for r in rows)


def test_gemini_cache_read_prices_are_recorded():
    flash = SHIPPED["models"]["google/gemini-3.8-flash"]["prices"]
    assert flash[0]["cache_read"] == 0.075 and flash[1]["cache_read"] == 0.15
    pro = SHIPPED["models"]["google/gemini-3.1-pro-preview"]["prices"][0]
    assert pro["cache_read"] == 0.2 and pro["long_context_over"]["cache_read"] == 0.4
    db = scores_db.ScoresDB(SHIPPED)
    assert db.price_at("google/gemini-3.8-flash", "2026-10-13").cache_read == 0.075
    assert db.price_at("google/gemini-3.8-flash", "2027-01-01").cache_read == 0.15
    assert db.price_at("google/gemini-3.1-pro-preview", "2026-10-13", 200_001).cache_read == 0.4


@pytest.mark.parametrize("model", ["openai/gpt-6-astra", "openai/gpt-6.1-sol", "openai/gpt-6-sol",
                                    "openai/gpt-6-luna"])
def test_gpt6_long_context_tier_records_input_cache_and_output_multipliers(model):
    lc = SHIPPED["models"][model]["prices"][0]["long_context_over"]
    assert lc["tokens"] == 272000
    assert lc["in_multiplier"] == 2.0 and lc["cache_multiplier"] == 2.0 and lc["out_multiplier"] == 1.5
    # The engine prices only the input part of this tier; the output multiplier is data, not behaviour.
    p = scores_db.ScoresDB(SHIPPED).price_at(model, "2026-10-13", 300_000)
    assert p.tier == "long-context" and p.approximate and p.out_price is None


def test_gemini_37_flash_alias_is_a_vendor_labelled_redirect():
    a = next(x for x in SHIPPED["aliases"] if x["alias"] == "google/gemini-3.7-flash")
    assert a["label"] == "VENDOR" and a["target"] == "google/gemini-3.8-flash"
    assert "deprecations" in a["source_ref"] and "12.12" in a["notes"]


def test_usage_pool_facts_are_corrected():
    sub = _pool("claude-subscription")
    assert any("separate weekly limit for Fable" in f for f in sub["facts"])
    assert not any(f.startswith("Limits are plan-wide across models") for f in sub["facts"])
    codex = _pool("codex-subscription")
    assert any("weekly limit only" in f and "Pro" in f for f in codex["facts"])
    agy = _pool("agy-claude-gpt")
    assert any("UNVERIFIABLE" in f for f in agy["facts"])
    assert not any(f.startswith("Claude and GPT models share one Antigravity pool") for f in agy["facts"])


def test_shipped_document_still_validates_clean():
    assert scores_db.validate(SHIPPED) == []
