"""The scores database: schema, loader, validator, evidence labels, prices and the seeded evidence (AC-1, 2, 24-26)."""
from __future__ import annotations

import copy
import pathlib

import pytest

from ai_resources.scoring import db as scores_db
from ai_resources.scoring import evidence
from ai_resources.scoring.evidence import Label
from ai_resources.setup import profiles
from scoring_fakes import fixture_doc, row, shipped_copy

FIX = pathlib.Path(__file__).parent / "fixtures"
SHIPPED = scores_db.load_shipped()


# --- AC-1: the shipped document ------------------------------------------------------------------
def test_shipped_document_validates_clean():
    assert scores_db.validate(SHIPPED) == []


def test_every_row_has_label_date_and_source_and_scores_are_in_range():
    rows = SHIPPED["rows"]
    assert rows
    for r in rows:
        assert r["label"] in evidence.ROW_LABELS
        assert scores_db.parse_date(r["as_of"])
        assert r["source_ref"]
        assert r["score"] is None or 0 <= r["score"] <= 10
        if r["score"] is not None:
            assert ".agent-output/research-models/" in r["source_ref"] and ":" in r["source_ref"].split(".md")[-1]


def test_policy_covers_exactly_the_known_roles_and_aliases_resolve():
    assert sorted(SHIPPED["policy"]["importance"]) == sorted(profiles.KNOWN_ROLES)
    for a in SHIPPED["aliases"]:
        assert a["target"] in SHIPPED["models"]


def test_loaded_database_exposes_rows_expanded_per_route():
    db = scores_db.ScoresDB(SHIPPED)
    sonnet = [r for r in db.rows_for("implementer") if r.model == "anthropic/claude-sonnet-5.5"]
    assert sorted(r.route for r in sonnet) == ["anthropic-direct", "openrouter", "vertex-global"]
    assert {r.effort for r in sonnet} == {"default"}


# --- AC-2: no `verified`, labels are required ----------------------------------------------------
def test_a_verified_key_anywhere_is_rejected():
    doc = shipped_copy()
    doc["rows"][0]["verified"] = True
    assert any("verified" in e for e in scores_db.validate(doc))
    doc = shipped_copy()
    doc["pools"][0]["verified"] = False
    assert any("verified" in e for e in scores_db.validate(doc))


def test_a_row_without_a_label_fails_validation():
    doc = fixture_doc([row("tester", "anthropic/claude-haiku-5.5", 3.5)])
    del doc["rows"][0]["label"]
    assert any("label" in e for e in scores_db.validate(doc))


def test_inferred_rows_need_a_basis_and_measured_here_rows_need_run_fields():
    doc = fixture_doc([row("tester", "anthropic/claude-haiku-5.5", 3.5)])
    del doc["rows"][0]["basis"]
    assert any("basis" in e for e in scores_db.validate(doc))
    doc = fixture_doc([row("tester", "anthropic/claude-haiku-5.5", 3.5, label="MEASURED-HERE")])
    errs = scores_db.validate(doc)
    for f in scores_db.MEASURED_HERE_FIELDS:
        assert any(f in e for e in errs), f


def test_score_out_of_range_and_duplicate_keys_fail():
    doc = fixture_doc([row("tester", "anthropic/claude-haiku-5.5", 11.0)])
    assert any("score" in e for e in scores_db.validate(doc))
    doc = fixture_doc([row("tester", "anthropic/claude-haiku-5.5", 3.0), row("tester", "anthropic/claude-haiku-5.5", 3.1)])
    assert any("duplicate" in e for e in scores_db.validate(doc))


def test_routable_is_computed_never_stored():
    doc = shipped_copy()
    doc["models"]["openai/gpt-6.1-sol"]["routable"] = True
    assert any("routable" in e for e in scores_db.validate(doc))


def test_price_windows_must_not_overlap():
    doc = shipped_copy()
    doc["models"]["google/gemini-3.8-flash"]["prices"].append(
        {"valid_from": "2026-12-01", "in": 1.0, "out": 2.0, "price_source": "x", "price_as_of": "2026-10-13"})
    assert any("overlap" in e for e in scores_db.validate(doc))


# --- evidence labels -----------------------------------------------------------------------------
@pytest.mark.parametrize("spelling,label", [
    ("MEASURED-REPO", Label.MEASURED_HERE), ("THIRD-PARTY", Label.MEASURED_3P), ("MEASURED", Label.MEASURED_3P),
    ("vendor-reported", Label.VENDOR), ("VENDOR-DOC", Label.VENDOR), ("UNCONFIRMED", Label.UNKNOWN),
    ("NOT CONFIRMED", Label.UNKNOWN), ("CANNOT CONFIRM", Label.UNKNOWN), ("USER-REPORTED", Label.UNKNOWN),
    ("[?]", Label.UNKNOWN), ("INFERRED", Label.INFERRED), ("[I]", Label.INFERRED), ("nonsense", Label.UNKNOWN)])
def test_label_normalisation(spelling, label):
    assert evidence.normalise(spelling) is label


def test_weakest_label_wins_and_an_empty_set_is_unknown():
    assert evidence.weakest("MEASURED-3P", "INFERRED", "VENDOR") is Label.INFERRED
    assert evidence.weakest("MEASURED-HERE") is Label.MEASURED_HERE
    assert evidence.weakest() is Label.UNKNOWN


# --- overrides -----------------------------------------------------------------------------------
def test_missing_override_file_means_no_overrides(tmp_path):
    assert scores_db.load_overrides(tmp_path / "absent.yaml") is None
    doc, problems = scores_db.merge(SHIPPED, None)
    assert doc["rows"] == SHIPPED["rows"] and problems == []


def test_override_row_shadows_by_key_and_is_flagged():
    over = scores_db.load_overrides(FIX / "scores_override_valid.yaml")
    doc, problems = scores_db.merge(SHIPPED, over)
    assert problems == []
    db = scores_db.ScoresDB(doc)
    hit = [r for r in db.rows_for("tester") if r.model == "anthropic/claude-haiku-5.5" and r.route == "anthropic-direct"]
    assert len(hit) == 1 and hit[0].score == 5.0 and hit[0].override and hit[0].label is Label.MEASURED_HERE
    # the sibling routes of the same shipped row are kept
    others = [r for r in db.rows_for("tester") if r.model == "anthropic/claude-haiku-5.5" and r.route != "anthropic-direct"]
    assert {r.route for r in others} == {"openrouter", "vertex-global"} and not any(r.override for r in others)


def test_override_without_reason_or_with_verified_is_dropped():
    over = scores_db.load_overrides(FIX / "scores_override_invalid.yaml")
    doc, problems = scores_db.merge(SHIPPED, over)
    assert len(problems) == 3
    assert doc["rows"] == SHIPPED["rows"]


def test_numeric_claude_weights_only_through_an_override_with_a_reason():
    assert not any("claude-subscription" in e for e in scores_db.validate(SHIPPED))
    over = scores_db.load_overrides(FIX / "scores_override_valid.yaml")
    doc, _ = scores_db.merge(SHIPPED, over)
    pool = next(p for p in doc["pools"] if p["id"] == "claude-subscription")
    assert pool["model_weights"]["claude-sonnet-5.5"] == 2.0 and pool["weights_override_reason"]


# --- staleness and prices (AC-25) ----------------------------------------------------------------
def test_staleness_uses_expires_after_days_and_an_explicit_date():
    r = scores_db.Row("tester", "m", "anthropic-direct", "default", 5.0, Label.INFERRED, "2026-10-12", "x",
                      expires_after_days=90)
    assert not scores_db.is_stale(r, "2027-01-10")
    assert scores_db.is_stale(r, "2027-01-11")
    assert scores_db.is_stale(scores_db.Row("t", "m", "r", "e", 1.0, Label.INFERRED, "", "x"), "2026-10-13")


def test_gemini_flash_price_doubles_on_2027_01_01():
    db = scores_db.ScoresDB(SHIPPED)
    before = db.price_at("google/gemini-3.8-flash", "2026-12-31")
    after = db.price_at("google/gemini-3.8-flash", "2027-01-01")
    assert (before.in_price, before.out_price) == (0.75, 3.75)
    assert (after.in_price, after.out_price) == (1.50, 7.50)


def test_haiku_price_has_a_long_context_tier_above_100k_tokens():
    db = scores_db.ScoresDB(SHIPPED)
    low = db.price_at("anthropic/claude-haiku-5.5", "2026-10-13", 100_000)
    high = db.price_at("anthropic/claude-haiku-5.5", "2026-10-13", 100_001)
    assert (low.in_price, low.out_price, low.tier) == (0.10, 0.50, "standard")
    assert (high.in_price, high.out_price, high.tier) == (0.50, 2.50, "long-context")


def test_openrouter_haiku_price_is_flagged_verify_and_direct_is_not():
    db = scores_db.ScoresDB(SHIPPED)
    assert db.price_at("anthropic/claude-haiku-5.5", "2026-10-13", 0, "openrouter").verify is True
    assert db.price_at("anthropic/claude-haiku-5.5", "2026-10-13", 0, "anthropic-direct").verify is False


def test_a_price_is_never_invented():
    db = scores_db.ScoresDB(SHIPPED)
    assert db.price_at("google/gemini-4-argon", "2026-10-13") is None
    sol = db.price_at("openai/gpt-6.1-sol", "2026-10-13", 300_000)
    assert sol.tier == "long-context" and sol.approximate and sol.out_price is None and sol.in_price == 4.0


# --- routability is computed from the kit's lists --------------------------------------------------
def test_routable_follows_the_kit_lists():
    db = scores_db.ScoresDB(SHIPPED)
    assert db.routable("anthropic/claude-sonnet-5.5", "anthropic-direct")
    assert db.routable("openai/gpt-6.1-sol", "litellm")                 # added to KNOWN_MODELS (decision Q15)
    assert not db.routable("google/gemini-3.8-flash", "openrouter")      # not in the kit's OpenRouter list
    assert not db.routable("anthropic/claude-haiku-5.5", "vertex-global")   # the kit lists the 4.5 id on Vertex
    assert not db.routable("google/gemini-4-argon", "litellm")


# --- AC-24: the seeded evidence -------------------------------------------------------------------
def _proxy(model):
    return next(b for b in SHIPPED["models"][model]["benchmarks"] if b["board"] == "vals-terminal-bench-4.0")


def test_tb4_proxies_are_measured_3p_role_agnostic_and_cited():
    for model, entry in SHIPPED["models"].items():
        b = _proxy(model)
        assert b["role_agnostic"] is True
        assert b["as_of"] == "2026-10-13"   # the Vals board header read 10/13/2026 on the 2026-10-13 re-check
        if b["label"] == "MEASURED-3P":
            assert b["value"] is not None
            assert ("usage-openai-and-cross-benchmarks.md" in b["source_ref"]) or ("reasoning.md" in b["source_ref"])


def test_opus_uses_the_adjusted_value_and_keeps_the_raw_one():
    b = _proxy("anthropic/claude-opus-5.5")
    assert b["value"] == 58.08 and b["raw_value"] == 65.15


def test_gemini_31_pro_proxy_is_not_used_as_a_score():
    b = _proxy("google/gemini-3.1-pro-preview")
    assert b["value"] is None and b["raw_value"] == 2.52 and b["label"] == "UNKNOWN"
    assert not [r for r in SHIPPED["rows"] if r["model"] == "google/gemini-3.1-pro-preview"]


def test_no_review_planning_or_security_row_is_measured_and_proxy_rows_are_inferred_with_a_basis():
    for r in SHIPPED["rows"]:
        if r["role"] in ("planner", "software-architect", "code-reviewer", "verifier", "security-auditor"):
            assert not r["label"].startswith("MEASURED"), r
        if r["score"] is not None:
            assert r["label"] == "INFERRED" and r["basis"], r


def test_proxy_rows_equal_accuracy_over_ten_with_one_decimal():
    for r in SHIPPED["rows"]:
        if r["score"] is None:
            continue
        b = _proxy(r["model"])
        assert r["score"] == round(b["value"] / 10.0 + 1e-9, 1), r


def test_the_operator_example_numbers_are_not_seed_data():
    illustrative = {7.9, 8.0, 7.0, 9.0, 8.5, 8.7}
    assert not illustrative.intersection(r["score"] for r in SHIPPED["rows"] if r["score"] is not None)


def test_new_openai_models_have_entries_with_dated_prices():
    for model in ("openai/gpt-6.1-sol", "openai/gpt-6-sol", "openai/gpt-6-luna", "openai/gpt-6-astra",
                  "anthropic/claude-fable-5.1"):
        assert SHIPPED["models"][model]["prices"], model


def test_5_5_hazards_are_recorded_as_vendor_evidence():
    for model in ("anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-5.5"):
        hz = SHIPPED["models"][model]["hazards"]
        assert any("tool_choice" in h["text"] for h in hz)
        assert all(h["label"] == "VENDOR" and h["as_of"] == "2026-10-13" for h in hz)


# --- AC-26: pools ---------------------------------------------------------------------------------
def _pool(pid):
    return next(p for p in SHIPPED["pools"] if p["id"] == pid)


def test_claude_subscription_pool_facts():
    p = _pool("claude-subscription")
    assert p["model_weights"]["kind"] == "qualitative"
    assert not any(isinstance(v, (int, float)) for v in p["model_weights"]["tiers"].values())
    assert p["headless_draws_plan"] is True and p["credits_cache_ttl_minutes"] == 5
    assert p["headroom_source"] == "statusline-sidecar" and p["plan_wide"] is True


def test_anthropic_api_pool_lists_the_spend_cap_as_a_hard_stop():
    p = _pool("anthropic-api")
    stop = next(h for h in p["hard_stop_errors"] if h["code"] == "enforced_spend_limit_reached")
    assert stop["retry_after"] is False and stop["http"] == 429


def test_codex_pool_is_unknown_where_the_vendor_is_silent_and_never_dispatchable():
    p = _pool("codex-subscription")
    assert p["weekly_cap"] == "UNKNOWN" and p["headless_draws_plan"] == "UNKNOWN"
    assert p["usage"] == "instructions-only"
    for pid in ("agy-gemini", "agy-claude-gpt"):
        assert _pool(pid)["usage"] == "direct-only" and _pool(pid)["routes"] == []


def test_fable_is_an_opt_in_candidate_only():
    assert SHIPPED["models"]["anthropic/claude-fable-5.1"]["candidate_policy"] == "opt-in"
