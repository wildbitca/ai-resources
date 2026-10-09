"""Price rules: exact audit prices, then the operator's own, then OpenRouter's public list; never invented."""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone

import httpx

from ai_resources import models

FIX = pathlib.Path(__file__).parent / "fixtures" / "models"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def test_an_unknown_price_stays_unknown():
    assert models._price_delta("gemini-3.7-flash", "gemini-3.8-flash", {}) == ("unknown", None)


def test_the_operators_own_prices_are_used_when_both_ids_have_one():
    ov = {"prices": {"gemini-9.1-flash": [0.75, 3.75], "gemini-9.2-flash": [0.75, 3.75]}}
    assert models._price_delta("gemini-9.1-flash", "gemini-9.2-flash", ov)[0] == "equal"
    ov["prices"]["gemini-9.2-flash"] = [1.5, 7.5]
    assert models._price_delta("gemini-9.1-flash", "gemini-9.2-flash", ov) == ("higher(100%)", 100.0)


def test_the_audit_table_wins_over_the_operators_prices(monkeypatch):
    from ai_resources import audit
    monkeypatch.setattr(audit, "PRICES", {**audit.PRICES, "gemini-9.1-flash": (1.0, 2.0), "gemini-9.2-flash": (1.0, 2.0)})
    ov = {"prices": {"gemini-9.2-flash": [50.0, 50.0]}}
    assert models._price_delta("gemini-9.1-flash", "gemini-9.2-flash", ov)[0] == "equal"


def test_an_incomplete_manual_entry_is_not_a_price():
    assert models._price_delta("a", "b", {"prices": {"a": ["x", 1], "b": [1]}}) == ("unknown", None)


def test_openrouter_public_pricing_is_converted_to_per_million():
    prices = models.openrouter_price_map(json.loads((FIX / "openrouter-pricing.json").read_text()))
    assert prices["gemini-3.8-flash"][0] > 0 and prices["gemini-3.8-flash"][1] > 0
    assert "claude-sonnet-5" in prices


class Deps:
    def __init__(self, http):
        self.http = http
        self.now = lambda: NOW


def test_the_price_list_is_fetched_once_cached_with_a_timestamp_and_never_overrides_manual_prices():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, text=(FIX / "openrouter-pricing.json").read_text())

    sel = type("S", (), {"smoke_path": "openrouter"})()
    deps = Deps(models.make_http(httpx.MockTransport(handler)))
    ov = {"prices": {"gemini-3.8-flash": [9.0, 9.0]}}
    merged = models._with_prices(deps, ov, sel)
    assert merged["prices"]["gemini-3.8-flash"] == [9.0, 9.0]
    assert "gemini-3.7-flash" in merged["prices"]
    assert ov["price_cache"]["fetched_at"] == NOW.isoformat()
    models._with_prices(deps, ov, sel)
    assert len(calls) == 1                       # fresh cache: no second request


def test_other_backends_do_not_fetch_prices():
    sel = type("S", (), {"smoke_path": "litellm"})()
    deps = Deps(lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    assert models._with_prices(deps, {}, sel) == {}
