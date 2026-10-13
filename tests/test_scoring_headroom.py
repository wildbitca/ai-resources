"""The headroom model: static weights, the unknown state and the effective weight (AC-21)."""
from __future__ import annotations

import pytest

from ai_resources.scoring import db as scores_db
from ai_resources.scoring import headroom as hr
from ai_resources.setup.cockpits import _agy_quota

DB = scores_db.ScoresDB(scores_db.load_shipped())


def reading(state=hr.KNOWN, used=0.25, resets_at=None, read_at=None, pool="claude-subscription"):
    return hr.HeadroomReading(pool_id=pool, state=state, used_fraction=used, resets_at=resets_at,
                              source="fixture", read_at=read_at)


def test_static_levels_are_ample_normal_scarce():
    assert (hr.level_weight("ample"), hr.level_weight("normal"), hr.level_weight("scarce")) == (1.0, 0.7, 0.3)
    assert hr.level_weight(None) == 1.0 and hr.level_weight("unknown-level") == 1.0


def test_known_reading_scales_the_static_weight_down():
    ew = hr.effective_weight(0.7, reading(used=0.25))
    assert ew.basis == "live" and ew.weight == pytest.approx(0.7 * 0.75)


def test_fully_used_pool_is_floored_not_zero():
    assert hr.effective_weight(1.0, reading(used=1.0)).weight == hr.EPSILON


def test_unknown_or_missing_reading_uses_the_static_weight():
    for r in (None, hr.unknown_reading("p"), hr.HeadroomReading("p", hr.KNOWN, None)):
        ew = hr.effective_weight(0.3, r)
        assert (ew.weight, ew.basis) == (0.3, "static")


def test_stale_reading_uses_the_static_weight():
    assert hr.effective_weight(0.7, reading(state=hr.STALE)).basis == "static"
    past_reset = reading(resets_at=1_000)
    assert hr.effective_weight(0.7, past_reset, now_epoch=1_000).basis == "static"
    assert hr.effective_weight(0.7, past_reset, now_epoch=999).basis == "live"
    old = reading(read_at=0)
    assert hr.effective_weight(0.7, old, now_epoch=hr.DEFAULT_MAX_AGE_S + 1).basis == "static"


def test_pressure_is_load_over_weight():
    assert hr.pressure(2.0, 1.0, 0.5) == 6.0
    assert hr.pressure(0.0, 1.0, 0.0) == 1.0 / hr.EPSILON


@pytest.mark.parametrize("model", ["gemini-3.8-flash-low", "claude-sonnet-4-6", "gpt-oss-120b-medium", "mystery"])
def test_agy_pool_rule_is_the_one_in_agy_quota(model):
    assert hr.agy_reporting_pool(model) == _agy_quota.pool_for_model(model)


def test_pool_for_prefers_an_enabled_pool_on_a_shared_route():
    assert DB.pool_for("anthropic", "anthropic-direct") == "claude-subscription"
    assert DB.pool_for("anthropic", "anthropic-direct", ("anthropic-api",)) == "anthropic-api"
    assert DB.pool_for("openai", "litellm") == "openai-api"
    assert DB.pool_for("deepseek", "litellm") == "deepseek-api"
    assert DB.pool_for("anthropic", "nowhere") == ""
