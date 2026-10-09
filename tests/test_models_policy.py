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


# --- S7: non-Claude slots and the answer ladder ------------------------------------------------

def _google_disc(best="gemini-3.8-flash"):
    return models.Discovery(others={"google:gemini-flash": best})


def _eff(old="gemini-3.7-flash"):
    return {"google:gemini-flash": old}


def test_a_google_bump_with_unknown_price_needs_approval_even_when_the_answer_is_always():
    ov = {"policy": {"slots": {"google:gemini-flash": {"answer": "always", "max_bump": "minor"}}}}
    [p] = models.propose(_eff("gemini-9.1-flash"), _google_disc("gemini-9.2-flash"), ov)
    assert (p.slot, p.kind, p.decision) == ("google:gemini-flash", "minor", "needs_approval")
    assert "price unknown" in p.reasons


def test_a_known_equal_price_and_an_always_answer_is_auto(monkeypatch):
    monkeypatch.setattr(audit, "PRICES", {**audit.PRICES, "gemini-9.1-flash": (1.0, 2.0), "gemini-9.2-flash": (1.0, 2.0)})
    ov = {"policy": {"slots": {"google:gemini-flash": {"answer": "always", "max_bump": "minor"}}}}
    [p] = models.propose(_eff("gemini-9.1-flash"), _google_disc("gemini-9.2-flash"), ov)
    assert p.decision == "auto"


def test_a_new_non_claude_slot_defaults_to_ask(monkeypatch):
    monkeypatch.setattr(audit, "PRICES", {**audit.PRICES, "gemini-9.1-flash": (1.0, 2.0), "gemini-9.2-flash": (1.0, 2.0)})
    [p] = models.propose(_eff("gemini-9.1-flash"), _google_disc("gemini-9.2-flash"), {})
    assert p.decision == "needs_approval" and "mode approve" in p.reasons


def test_max_bump_minor_holds_a_major_jump_and_any_lets_it_through(monkeypatch):
    monkeypatch.setattr(audit, "PRICES", {**audit.PRICES, "gemini-3.7-flash": (1.0, 2.0), "gemini-4.0-flash": (1.0, 2.0)})
    base = {"google:gemini-flash": {"answer": "always", "max_bump": "minor"}}
    [held] = models.propose(_eff(), models.Discovery(others={"google:gemini-flash": "gemini-4.0-flash"}),
                            {"policy": {"slots": base}})
    assert held.decision == "needs_approval" and "major jump" in held.reasons
    [free] = models.propose(_eff(), models.Discovery(others={"google:gemini-flash": "gemini-4.0-flash"}),
                            {"policy": {"slots": {"google:gemini-flash": {"answer": "always", "max_bump": "any"}}}})
    assert free.decision == "auto"


def test_never_ids_suppress_one_id_and_the_family_never_suppresses_every_id():
    one = {"policy": {"slots": {"google:gemini-flash": {"never_ids": ["gemini-3.8-flash"]}}}}
    [p] = models.propose(_eff(), _google_disc(), one)
    assert p.decision == "suppressed"
    [other] = models.propose(_eff(), _google_disc("gemini-3.9-flash"), one)
    assert other.decision == "needs_approval"                       # only that exact id was refused
    fam = {"policy": {"families": {"google:gemini-flash": {"never": True}}}}
    for new in ("gemini-3.8-flash", "gemini-3.9-flash"):
        [q] = models.propose(_eff(), _google_disc(new), fam)
        assert q.decision == "suppressed", new


def test_precedence_exclude_and_frozen_beat_never_and_always():
    ov = {"policy": {"exclude": ["gemini-3.8-*"], "slots": {"google:gemini-flash": {"answer": "always", "never_ids": ["gemini-3.8-flash"]}}}}
    assert models.propose(_eff(), _google_disc(), ov)[0].decision == "excluded"
    ov = {"policy": {"slots": {"google:gemini-flash": {"answer": "always", "frozen": True, "never_ids": ["gemini-3.8-flash"]}}}}
    assert models.propose(_eff(), _google_disc(), ov)[0].decision == "frozen"


def test_an_approval_beats_an_always_that_would_wait():
    ov = {"approvals": {"google:gemini-flash": "gemini-3.8-flash"}}
    [p] = models.propose(_eff(), _google_disc(), ov)
    assert p.decision == "approved" and p.applicable


def test_a_preview_or_alias_is_never_a_candidate():
    disc = models.Discovery(previews={"google:gemini-flash": "gemini-9-flash-preview"})
    [p] = models.propose(_eff("gemini-3.8-flash"), disc, {})
    assert (p.kind, p.decision, p.applicable) == ("preview", "report", False)
    assert models.propose(_eff("gemini-3.8-flash"), models.Discovery(previews={"google:gemini-flash": "gemini-3.1-flash-preview"}), {}) == []


def test_no_smoke_path_adds_a_reason_when_the_path_cannot_probe_the_provider():
    sel = type("S", (), {"smoke_path": "claude-cli"})()
    [p] = models.propose(_eff(), _google_disc(), {}, sel, {})
    assert "no smoke path" in p.reasons


def test_proposals_carry_the_slot_and_provider_in_json():
    [p] = models.propose(_eff(), _google_disc(), {})
    d = p.as_dict()
    assert d["slot"] == "google:gemini-flash" and d["provider"] == "google" and d["cls"] == "gemini-flash"
