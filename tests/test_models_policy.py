"""Discovery and policy: pure functions over the live catalog fixture (tests/fixtures/models)."""
from __future__ import annotations

import json
import pathlib

import pytest

from ai_resources import audit, models
from ai_resources import model_pins as mp

CATALOG = (pathlib.Path(__file__).parent / "fixtures" / "models" / "claude-cli-catalog.json").read_text()


def runner_for(out=CATALOG, rc=0, refresh_rc=0):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if "refresh" in argv:
            return refresh_rc, "updated"
        return rc, out
    run.calls = calls
    return run


@pytest.fixture
def disc():
    return models.discover(runner_for())


def by_cls(props):
    return {p.cls: p for p in props}


def test_discover_reads_the_highest_per_class(disc):
    assert disc.best == {"haiku": "claude-haiku-5-5", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                         "fable": "claude-fable-5-1"}
    assert disc.new_families == ["claude-mythos-5"]


def test_discover_skips_refresh_when_asked():
    run = runner_for()
    models.discover(run, refresh=False)
    assert not any("refresh" in a for a in run.calls)


def test_refresh_failure_warns_and_continues():
    warnings = []
    models.discover(runner_for(refresh_rc=1), warn=warnings.append)
    assert warnings


@pytest.mark.parametrize("out,rc", [("not json", 0), ("{}", 0), ('{"models": 3}', 0), ("boom", 1)])
def test_discover_fails_closed(out, rc):
    with pytest.raises(models.DiscoveryError):
        models.discover(runner_for(out=out, rc=rc))


def test_unavailable_models_are_ignored():
    cat = json.loads(CATALOG)
    for m in cat["models"]:
        if m["key"].endswith("sonnet-5-5"):
            m["available"] = False
    assert models.discover(runner_for(json.dumps(cat))).best["sonnet"] == "claude-sonnet-5"


def test_todays_prices_make_bumps_wait_for_approval(disc):
    p = by_cls(models.propose(mp.effective({}), disc, {}))
    assert p["sonnet"].decision == "needs_approval" and "price unknown" in p["sonnet"].reasons
    assert p["opus"].decision == "needs_approval" and "price unknown" in p["opus"].reasons
    assert p["haiku"].decision == "needs_approval" and "major jump" in p["haiku"].reasons
    # S2: a class with nothing newer in the catalog is reported as "current", not left out.
    assert p["fable"].decision == "current" and not p["fable"].applicable


@pytest.fixture
def priced(monkeypatch):
    prices = dict(audit.PRICES)
    prices["claude-sonnet-5-5"] = audit.PRICES["claude-sonnet-5"]
    monkeypatch.setattr(audit, "PRICES", prices)
    return prices


def test_equal_known_price_is_auto(disc, priced):
    p = by_cls(models.propose(mp.effective({}), disc, {}))["sonnet"]
    assert (p.kind, p.price, p.decision) == ("minor", "equal", "auto")


def test_price_increase_needs_approval_unless_within_budget(disc, priced):
    s = priced["claude-sonnet-5"]
    priced["claude-sonnet-5-5"] = (s[0] * 1.1, s[1] * 1.1, *s[2:])
    p = by_cls(models.propose(mp.effective({}), disc, {}))["sonnet"]
    assert p.decision == "needs_approval" and "price increase 10%" in p.reasons
    p = by_cls(models.propose(mp.effective({}), disc, {"policy": {"max_cost_delta_pct": 15}}))["sonnet"]
    assert p.decision == "auto"


def test_exclude_frozen_and_approve_modes(disc, priced):
    ov = {"policy": {"exclude": ["claude-opus-5-5"], "classes": {"haiku": {"mode": "frozen"},
                                                                    "sonnet": {"mode": "approve"}}}}
    p = by_cls(models.propose(mp.effective({}), disc, ov))
    assert p["opus"].decision == "excluded"
    assert p["haiku"].decision == "frozen"
    assert p["sonnet"].decision == "needs_approval" and "mode approve" in p["sonnet"].reasons


def test_approval_matches_only_the_approved_id(disc):
    p = by_cls(models.propose(mp.effective({}), disc, {"approvals": {"haiku": "claude-haiku-5-5"}}))
    assert p["haiku"].decision == "approved" and p["haiku"].applicable
    p = by_cls(models.propose(mp.effective({}), disc, {"approvals": {"haiku": "claude-haiku-5"}}))
    assert p["haiku"].decision == "needs_approval"


def test_new_family_is_reported_and_never_applicable(disc):
    mythos = [p for p in models.propose(mp.effective({}), disc, {}) if p.kind == "new_family"]
    assert [p.new for p in mythos] == ["claude-mythos-5"] and not mythos[0].applicable


def test_a_downgrade_is_never_proposed(disc):
    eff = dict(mp.DEFAULTS, sonnet="claude-sonnet-5-5", opus="claude-opus-5-5", haiku="claude-haiku-5-5")
    # "current" rows (S2) report that nothing is newer; they are not proposals of a change.
    assert [p for p in models.propose(eff, disc, {}) if p.kind not in ("new_family", "current")] == []


def test_a_class_with_nothing_newer_is_reported_as_current(disc):
    row = by_cls(models.propose(mp.effective({}), disc, {}))["fable"]
    assert (row.old, row.new, row.kind, row.decision) == ("claude-fable-5-1", "claude-fable-5-1", "current", "current")


def test_a_pin_ahead_of_the_catalog_is_current_with_a_note(disc):
    eff = mp.effective({})
    eff["opus"] = "claude-opus-9-9"
    row = by_cls(models.propose(eff, disc, {}))["opus"]
    assert row.decision == "current" and row.reasons == ["catalog newest is claude-opus-5-5"]


def test_current_rows_never_make_a_run_applicable_or_pending(disc):
    props = [p for p in models.propose(mp.effective({}), disc, {}) if p.decision == "current"]
    assert props and not any(p.applicable for p in props)
