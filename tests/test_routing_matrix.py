"""The single routing matrix, its resolver and the balancing rule (ADR-0018, ADR-0019). Pure and offline."""
from __future__ import annotations

import pytest

from ai_resources.scoring import routing
from ai_resources.setup import lanes_render

HOT = routing.POOL_MAX
CAUSES = {"pool-hot", "pool-exhausted", "agy-missing", "timeout", "5xx", "empty", "key-invalid"}


def test_every_cell_is_valid_with_a_known_pool_and_class():
    for r in routing.merged().values():
        assert r.kind in ("team", "chat") and r.klass in routing.CLASS_POOLS
        for cell in r.chain:
            assert routing.validate_cell(cell) is None
            assert routing.pool_of(cell) in routing.POOLS


def test_one_primary_and_one_fallback_chain_per_role_and_no_removed_concepts():
    for name, r in routing.ROLES.items():
        assert r.name == name
        assert isinstance(r.fallback, tuple)
        assert (r.primary is None) == r.checker
        if r.primary:
            assert r.fallback and r.primary not in r.fallback
    assert not {"second", "secondary", "candidates"} & set(routing.Role.__dataclass_fields__)


def test_matrix_values_follow_the_role_class_split():
    roles = routing.ROLES
    for r in ("explore", "code-reviewer", "doc-writer", "tester", "package-upgrade", "generalPurpose"):
        assert roles[r].primary.startswith("agy:"), r
    for r in ("planner", "software-architect", "implementer", "crashlytics-fixer", "sentry-fixer",
              "terraform-maintainer", "crossplane-upjet-maintainer"):
        assert roles[r].primary == "claude-code:sonnet", r
    assert roles["verifier"].checker and roles["security-auditor"].checker
    assert routing.pool_of(roles["main"].primary) == routing.POOL_ANTHROPIC_API
    for r in ("utility", "summarization", "explore-bulk"):
        assert routing.pool_of(roles[r].primary) == routing.POOL_GEMINI_API
    assert roles["heartbeat"].note            # stays on Haiku until Gemini tool-calling is proven


def test_team_fallbacks_never_land_on_an_api_credit_pool():
    for r in routing.ROLES.values():
        if r.kind == "team":
            assert all(routing.pool_of(c) not in routing.CREDIT_POOLS for c in r.chain), r.name
    with pytest.raises(routing.RoutingError, match="API-credit"):
        routing.merged({"routing": {"implementer": {"fallback": "anthropic-api:claude-haiku-5-5"}}})


def test_profile_override_replaces_cells_and_rejects_bad_ones():
    doc = {"routing": {"explore": {"primary": "claude-cli:haiku", "fallback": ["claude-code:sonnet"]}}}
    m = routing.merged(doc)
    assert m["explore"].primary == "claude-cli:haiku" and m["explore"].fallback == ("claude-code:sonnet",)
    assert m["planner"] == routing.ROLES["planner"]
    for bad in ({"explore": {"primary": "gpt:x"}}, {"nope": {}}, {"explore": "x"}, {"explore": {"class": "z"}}):
        with pytest.raises(routing.RoutingError):
            routing.merged({"routing": bad})
    with pytest.raises(routing.RoutingError):
        routing.merged({"routing": ["explore"]})


def test_choose_pool_spills_to_the_next_pool_and_unknown_is_available():
    assert routing.choose_pool("team-read", {})[0] == routing.POOL_AGY_GEMINI
    pool, why = routing.choose_pool("team-read", {routing.POOL_AGY_GEMINI: 0.9, routing.POOL_MAX: 0.3})
    assert pool == routing.POOL_MAX and "spill" in why
    pool, why = routing.choose_pool("team-read", {routing.POOL_AGY_GEMINI: 0.95, routing.POOL_MAX: 0.9})
    assert pool == routing.POOL_MAX and "least used" in why
    with pytest.raises(routing.RoutingError):
        routing.choose_pool("nope", {})


def test_balancing_distributes_work_across_all_four_pools():
    drained = {routing.choose_pool(c, {})[0] for c in routing.CLASS_POOLS}
    assert drained == {routing.POOL_MAX, routing.POOL_AGY_GEMINI, routing.POOL_ANTHROPIC_API, routing.POOL_GEMINI_API}


# --- resolver --------------------------------------------------------------------------------------------

def test_resolve_takes_the_primary_with_no_cause_when_the_pool_is_available():
    r = routing.resolve("implementer", {HOT: 0.2})
    assert (r.cell, r.cause, r.blocked, r.backend, r.model) == ("claude-code:sonnet", None, False, "claude-code", "sonnet")
    assert r.fallbacks == ("agy:gemini-4-argon-high",)
    assert routing.resolve("implementer", {}).cause is None          # unknown usage is not exhaustion


@pytest.mark.parametrize("used,cause", [(0.85, "pool-hot"), (0.99, "pool-hot"), (1.0, "pool-exhausted")])
def test_resolve_spills_to_the_fallback_when_the_pool_is_hot(used, cause):
    r = routing.resolve("implementer", {HOT: used})
    assert r.cell == "agy:gemini-4-argon-high" and r.cause == cause and cause in routing.FALLBACK_CAUSES


@pytest.mark.parametrize("cause", sorted(CAUSES))
def test_resolve_records_the_cause_of_every_fallback_trigger(cause):
    r = routing.resolve("explore", unavailable={"agy": cause})
    assert r.cell == "claude-code:haiku" and r.cause == cause and not r.blocked and cause in r.reason


def test_resolve_blocks_instead_of_silently_falling_back_past_the_chain():
    r = routing.resolve("explore", {routing.POOL_AGY_GEMINI: 0.9, HOT: 0.9})
    assert r.blocked and r.cell is None and r.cause == "pool-hot"


def test_cell_level_unavailability_skips_only_that_cell():
    r = routing.resolve("tester", unavailable={"agy:gemini-4-argon-high": "timeout"})
    assert r.cell == "claude-code:sonnet" and r.cause == "timeout"


def test_checker_takes_the_cheapest_rung_of_a_family_that_wrote_nothing():
    assert routing.resolve("verifier").cell == "claude-code:sonnet"
    assert routing.resolve("security-auditor", writers=["gemini-3.8-flash", "haiku"]).cell == "claude-code:sonnet"
    r = routing.resolve("verifier", writers=["sonnet"])
    assert r.cell == "claude-code:opus" and r.cause is None
    r = routing.resolve("verifier", writers=["claude-sonnet-5-5", "opus"])
    assert r.cell is None and r.blocked          # agy:claude-opus is also opus: never silently weaker


def test_a_writer_of_unknown_family_fails_the_checker_closed():
    for unknown in ("mystery-model", routing.UNKNOWN_FAMILY):
        r = routing.resolve("verifier", writers=["gemini-3.8-flash", unknown])
        assert r.blocked and r.cell is None and r.cause == "no-independent-checker"
    assert routing.resolve("tester", writers=["mystery-model"]).cell == routing.ROLES["tester"].primary   # not a checker


def test_a_down_rung_does_not_mask_the_missing_independence():
    r = routing.resolve("verifier", writers=["sonnet", "opus"], unavailable={"claude-code": "timeout"})
    assert r.blocked and r.cause == "no-independent-checker"
    r = routing.resolve("verifier", {HOT: 0.9, "claude-subscription": 1.0}, writers=["gemini-3.8-flash"])
    assert r.cause == "pool-hot" or r.cell      # a pool cause stays when independence is not what failed


def test_checker_spills_with_a_cause_when_its_rung_is_hot():
    r = routing.resolve("verifier", {HOT: 0.9}, writers=["gemini-3.8-flash"])
    assert r.cell == "agy:claude-opus-5-5" and r.cause == "pool-hot"


def test_resolve_rejects_an_unknown_role_and_honours_a_profile_override():
    with pytest.raises(routing.RoutingError):
        routing.resolve("nope")
    doc = {"routing": {"explore": {"primary": "claude-code:haiku"}}}
    assert routing.resolve("explore", profile_doc=doc).cell == "claude-code:haiku"


# --- quota text ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("pool", sorted(routing.POOLS))
def test_classify_quota_recognises_quota_text_for_every_pool(pool):
    assert routing.classify_quota("Error: You hit your limit, retry after 5h", pool)
    assert routing.classify_quota("HTTP 429 Too Many Requests", pool)
    assert routing.classify_quota("a normal answer", pool) is None
    assert routing.classify_quota("hit your limit", "no-such-pool") is None


# --- rendering ---------------------------------------------------------------------------------------------

def test_table_rows_cover_every_role_once_with_pools():
    rows = routing.table_rows()
    assert sorted(r[0] for r in rows) == sorted(routing.ROLES)
    by = {r[0]: r for r in rows}
    assert "[agy-gemini]" in by["explore"][1] and "[claude-subscription]" in by["explore"][2]
    assert by["verifier"][1] == "per-run checker ladder"
    assert len(routing.table_lines()) == len(rows)


@pytest.mark.parametrize("cell,ref", [("claude-code:sonnet", "anthropic/claude-sonnet-5-5"),
                                      ("claude-cli:haiku", "anthropic/claude-haiku-5-5"),
                                      ("agy:gemini-4-argon-high", "agy-cli-lane/gemini-4-argon-high"),
                                      ("gemini-api:gemini-3.8-flash", "google/gemini-3.8-flash"),
                                      ("anthropic-api:claude-haiku-5-5", "anthropic/claude-haiku-5-5")])
def test_cell_to_openclaw_ref(cell, ref):
    assert routing.cell_to_openclaw_ref(cell) == ref
    with pytest.raises(routing.RoutingError):
        routing.cell_to_openclaw_ref("gpt:x")


def test_lanes_yaml_carries_the_routing_section_and_it_moves_the_digest():
    lanes = {"orchestrators": []}
    a = lanes_render.render_doc(lanes, "single-model")
    m = a["routing"]["matrix"]
    assert m["explore"]["primary"] == {"cell": "agy:gemini-3.8-flash-high", "pool": routing.POOL_AGY_GEMINI}
    assert m["explore"]["fallback"] == [{"cell": "claude-code:haiku", "pool": routing.POOL_MAX}]
    assert m["verifier"]["checker"] and m["verifier"]["primary"]["cell"] == routing.CHECKER_LADDER[0]
    assert a["routing"]["balancing"]["spill_at"] == routing.SPILL_AT
    b = lanes_render.render_doc({**lanes, "routing": {"explore": {"primary": "claude-cli:haiku"}}}, "single-model")
    assert a["digest"] != b["digest"]
    assert lanes_render.doc_digest(dict(a)) == a["digest"]
