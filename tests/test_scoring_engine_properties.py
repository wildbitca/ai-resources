"""Property tests for the composition engine: hand-written seeded generators, 200 seeds each (no Hypothesis).

Fixture databases vary scores, labels, expiry and enabled providers. The scores are generated, not evidence.
"""
from __future__ import annotations

import random

import pytest

from ai_resources.scoring import db as scores_db
from ai_resources.scoring import headroom as hr
from ai_resources.scoring.compose import ComposeInput, compose, optimum
from scoring_fakes import (DEEPSEEK, GEMINI, HAIKU, OPUS, ROUTE_OF, SONNET, SOL, fixture_db, make_input, row,
                           shipped_copy)

SEEDS = range(200)
MODELS = (SONNET, OPUS, HAIKU, GEMINI, SOL, DEEPSEEK)
PROVIDERS = ("anthropic", "google", "openai", "deepseek")
ROLES = tuple(shipped_copy()["policy"]["importance"])
POOLS = ("claude-subscription", "gemini-api", "openai-api", "deepseek-api", "anthropic-api")


def gen_rows(rnd: random.Random):
    rows = []
    for role in ROLES:
        for model in MODELS:
            if rnd.random() < 0.2:
                continue
            roll = rnd.random()
            if roll < 0.1:
                rows.append(row(role, model, None, label="UNKNOWN"))
            else:
                rows.append(row(role, model, round(rnd.uniform(0, 10), 1),
                                label=rnd.choice(["INFERRED", "VENDOR", "MEASURED-3P"]),
                                as_of=rnd.choice(["2026-10-12", "2026-10-12", "2026-01-01"]),
                                **({"basis": "gen"} if True else {})))
    return rows


def gen_input(seed: int, mode=None, **over):
    rnd = random.Random(seed)
    db = fixture_db(gen_rows(rnd))
    enabled = tuple(p for p in PROVIDERS if rnd.random() < 0.7) or ("anthropic",)
    kw = dict(mode=mode or rnd.choice(["uniform", "single-provider", "mixed"]), enabled=enabled,
              allow_unverified=rnd.random() < 0.5,
              static_weights={p: rnd.choice([1.0, 0.7, 0.3]) for p in POOLS if rnd.random() < 0.6})
    kw.update(over)
    return db, make_input(db, **kw)


@pytest.mark.parametrize("seed", SEEDS)
def test_p1_unverified_cells_only_with_the_flag_and_always_marked(seed):
    _, inp = gen_input(seed)
    comp = compose(inp)
    for a in comp.assignments:
        if not a.verified:
            assert inp.allow_unverified and "unverified" in a.flags and a.grade == "UNVERIFIED"
    if comp.assignments and any(not a.verified for a in comp.assignments):
        assert "unverified-cells" in comp.flags


@pytest.mark.parametrize("seed", SEEDS)
def test_p2_every_assignment_uses_an_enabled_provider(seed):
    db, inp = gen_input(seed)
    comp = compose(inp)
    for a in comp.assignments:
        assert db.required_provider(a.model, a.route) in inp.enabled


@pytest.mark.parametrize("seed", SEEDS)
def test_p3_each_pick_is_in_band_or_flagged_and_loss_is_capped(seed):
    db, inp = gen_input(seed, mode="mixed")
    comp = compose(inp)
    if comp.refused:
        return
    assert comp.weighted_loss <= comp.loss_cap + 1e-6
    for a in comp.assignments:
        in_band = a.score >= a.s_star - a.tolerance - 1e-9
        assert in_band or "diversity" in a.flags or "below-band" in a.flags
        if "diversity" in a.flags:
            assert a.score >= a.s_star - db.policy["diversity_tolerance"] - 1e-9
        if a.tolerance_class == "critical":
            assert in_band or "below-band" in a.flags


@pytest.mark.parametrize("seed", SEEDS)
def test_p4_result_does_not_depend_on_input_order(seed):
    db, inp = gen_input(seed)
    first = compose(inp)
    rnd = random.Random(seed + 1)
    doc = db.data
    shuffled = dict(doc)
    shuffled["rows"] = list(doc["rows"])
    rnd.shuffle(shuffled["rows"])
    db2 = scores_db.ScoresDB(shuffled)
    enabled = list(inp.enabled)
    rnd.shuffle(enabled)
    again = compose(make_input(db2, mode=inp.mode, enabled=enabled, allow_unverified=inp.allow_unverified,
                               static_weights=dict(reversed(list(inp.static_weights.items())))))
    assert again.assignments == first.assignments
    assert again.input_digest == first.input_digest and again.refusal == first.refusal


@pytest.mark.parametrize("seed", SEEDS)
def test_p5_adding_a_provider_never_lowers_the_score_only_optimum(seed):
    db, inp = gen_input(seed)
    base = optimum(inp)
    for extra in PROVIDERS:
        if extra in inp.enabled:
            continue
        more = ComposeInput(db=inp.db, mode=inp.mode, enabled=tuple(inp.enabled) + (extra,), as_of=inp.as_of,
                            gate=inp.gate, allow_unverified=inp.allow_unverified)
        assert optimum(more) >= base - 1e-9


@pytest.mark.parametrize("seed", SEEDS)
def test_p7_uniform_uses_one_model_and_single_provider_uses_one_provider(seed):
    db, inp = gen_input(seed, mode="uniform")
    comp = compose(inp)
    assert len({a.model for a in comp.assignments}) <= 1
    db, inp = gen_input(seed, mode="single-provider")
    comp = compose(inp)
    assert len({db.required_provider(a.model, a.route) for a in comp.assignments}) <= 1


@pytest.mark.parametrize("seed", SEEDS)
def test_p8_no_backend_or_agentid_and_fallbacks_stay_in_vendor(seed):
    db, inp = gen_input(seed)
    comp = compose(inp)
    for role, entry in comp.by_role.items():
        assert set(entry) <= {"model", "fallbacks"}
        vendor = entry["model"].split("/", 1)[0]
        assert all(f.split("/", 1)[0] == vendor for f in entry.get("fallbacks", []))
        assert entry["model"] != ""


@pytest.mark.parametrize("seed", SEEDS)
def test_p10_empty_enabled_set_is_a_refusal_not_an_exception(seed):
    _, inp = gen_input(seed, enabled=())
    comp = compose(inp)
    assert comp.refused and not comp.assignments and "no provider" in comp.refusal


@pytest.mark.parametrize("seed", SEEDS)
def test_p12_all_unknown_readings_equal_no_readings(seed):
    db, inp = gen_input(seed, mode="mixed")
    readings = {p: hr.unknown_reading(p) for p in POOLS}
    with_readings = compose(make_input(db, mode="mixed", enabled=inp.enabled, allow_unverified=inp.allow_unverified,
                                       static_weights=inp.static_weights, readings=readings))
    assert with_readings.assignments == compose(inp).assignments


@pytest.mark.parametrize("seed", SEEDS)
def test_p13_readings_never_widen_a_band_or_admit_a_candidate(seed):
    db, inp = gen_input(seed, mode="mixed")
    rnd = random.Random(seed + 7)
    readings = {p: hr.HeadroomReading(p, hr.KNOWN, round(rnd.uniform(0, 1), 2), None, "fixture") for p in POOLS}
    live = compose(make_input(db, mode="mixed", enabled=inp.enabled, allow_unverified=inp.allow_unverified,
                              static_weights=inp.static_weights, readings=readings))
    base = compose(inp)
    assert live.refused == base.refused
    live_rows = {w.role: w for w in live.why_rows}
    for w in base.why_rows:
        assert live_rows[w.role].band == w.band
        a = live_rows[w.role].chosen
        if a is not None:
            assert f"{a.model}@{a.route}" in w.band or "diversity" in a.flags
    assert {(r.model, r.route) for r in live.rejected} == {(r.model, r.route) for r in base.rejected}


def test_the_engine_module_is_pure():
    """compose.py imports no I/O, clock, randomness, wizard or state module."""
    import ast
    import pathlib
    import ai_resources.scoring.compose as mod
    tree = ast.parse(pathlib.Path(mod.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0] or "." + (node.module or ""))
            if node.level:
                imported |= {a.name for a in node.names if node.level >= 2}
    forbidden = {"os", "time", "random", "datetime", "subprocess", "socket", "pathlib", "yaml", "wizard", "state"}
    assert not imported & forbidden, imported & forbidden
