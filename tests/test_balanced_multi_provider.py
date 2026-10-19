"""ADR-0010 S6: Balanced is multi-provider (Claude and Gemini), per row, and never moves a writer onto Antigravity.

Offline: no network, no agy, no real home. The gates stay closed except where a test opens them on purpose through
a monkeypatched `EVIDENCE`; no shipped constant changes."""
from __future__ import annotations

import dataclasses
import pathlib

import pytest

from ai_resources import agy_lane_gates as gates_mod
from ai_resources import model_accounts as ma
from ai_resources import roles_policy
from ai_resources.setup import compat, lanes_render
from ai_resources.setup import profiles, state, team_view, ui, wizard
from ai_resources.setup.cockpits import openclaw_lanes

IDS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
GEMINI_ROLES = {"explore", "code-reviewer", "planner", "software-architect"}
WRITERS = ("implementer", "tester", "doc-writer", "generalPurpose", "package-upgrade",
           "crashlytics-fixer", "sentry-fixer", "terraform-maintainer", "crossplane-upjet-maintainer")
PRESETS = ("balanced", "performance", "low-cost")


def _doc(name="balanced"):
    return profiles.load_profile(name, "single-model")


def _all_cands(block):
    for key in ("roles", "second"):
        for role, cands in (block.get(key) or {}).items():
            for c in cands:
                yield key, role, c


def _gemini(block):
    return {(key, role) for key, role, c in _all_cands(block) if c.startswith("agy:gemini-")}


def _by_role(doc):
    return profiles.to_executors(doc)["by_role"]


def _open_gates(monkeypatch):
    monkeypatch.setattr(gates_mod, "EVIDENCE", {g.id: dataclasses.replace(g, status=gates_mod.PROVEN, evidence="fixture")
                                                for g in gates_mod.GATES})


def _google_ack(roles):
    return {"google": lanes_render.make_ack("google", list(roles), "2026-10-19T00:00:00+00:00", account="a@example.com")}


# --- AC-S6a: the cli Balanced declares Gemini on exactly four places ------------------------------------------------

def test_cli_balanced_validates_and_declares_gemini_on_exactly_the_four_places():
    block = _doc()["lanes"]
    assert lanes_render.validate_block(block) == []
    assert _gemini(block) == {("roles", "explore"), ("second", "code-reviewer"), ("second", "planner"),
                              ("second", "software-architect")}
    assert set(block["pins"]) == GEMINI_ROLES
    assert all(c.startswith("agy:gemini-") for cands in block["pins"].values() for c in cands)
    assert not set(block.get("second") or {}) & set(roles_policy.CHECKER_ROLES)


def test_cli_balanced_resolves_without_a_lane_conflict_closed_and_open(monkeypatch):
    doc = _doc()
    by_role = _by_role(doc)
    closed = lanes_render.propose(doc, by_role, "single-model", claude_ids=IDS)
    assert closed["held"] and all(h["candidate"].startswith("agy:") for h in closed["held"])
    _open_gates(monkeypatch)
    roles = sorted(set(roles_policy.LANE_ELIGIBILITY) - {"implementer"})
    opened = lanes_render.propose(doc, by_role, "single-model", claude_ids=IDS, acks=_google_ack(roles),
                                  gates={"antigravity": True})
    assert not opened.get("held") or all("Antigravity" not in h["reason"] for h in opened["held"])


# --- AC-S6b: held while the gates are closed ----------------------------------------------------------------------

def test_with_agy_acknowledged_and_no_record_every_gemini_member_is_held_with_the_gate_refusal():
    from ai_resources import agy_lane_gates
    assert not agy_lane_gates.all_proven()
    block = _doc()["lanes"]
    frag = lanes_render.state_from_block(
        block, claude_ids=IDS, gates={"antigravity": True},
        acks=_google_ack(["explore", "code-reviewer", "planner", "software-architect", "verifier", "security-auditor"]))
    held_gemini = {(h["role"], h["candidate"]) for h in frag["held"] if "gemini-" in h["candidate"]}
    assert {r for r, _c in held_gemini} == GEMINI_ROLES
    gemini_reasons = {h["reason"] for h in frag["held"] if "gemini-" in h["candidate"]}
    assert gemini_reasons == {agy_lane_gates.refusal()}
    assert all(a["backend"] == "claude-cli" for a in frag["agents"].values())
    assert openclaw_lanes.writable_backends() == {"claude-cli"}


# --- AC-S6c: no preset carries an agy writer ----------------------------------------------------------------------

@pytest.mark.parametrize("preset", PRESETS)
def test_no_writer_role_lists_an_agy_candidate(preset):
    block = _doc(preset)["lanes"]
    offenders = [(key, role, c) for key, role, c in _all_cands(block) if role in WRITERS and c.startswith("agy:")]
    assert offenders == []


# --- AC-S6d: the planner second is held when the acknowledgement does not list planner ------------------------------

def test_a_google_acknowledgement_without_planner_holds_the_planner_second_with_the_reason(monkeypatch):
    _open_gates(monkeypatch)
    ack = _google_ack(["explore", "code-reviewer", "software-architect"])
    frag = lanes_render.state_from_block(_doc()["lanes"], claude_ids=IDS, gates={"antigravity": True}, acks=ack)
    held = {(h["role"], h["candidate"]): h["reason"] for h in frag["held"]}
    assert held[("planner", "agy:gemini-3.1-pro-low")] == "held: the Google acknowledgement does not list planner"
    assert ("explore", "agy:gemini-3.8-flash-high") not in held
    assert ("code-reviewer", "agy:gemini-3.8-flash-high") not in held


# --- AC-S6e: OpenRouter Balanced, the opt-in ------------------------------------------------------------------------

def _or_state():
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "openrouter"
    s.profile.name = "gateway/balanced"
    s.cockpits["claude"] = state.CockpitState(installed=True)
    return s


def test_gateway_balanced_declares_gemini_on_explore_tester_doc_writer_and_keeps_opus_checkers():
    doc = profiles.load_profile("gateway/balanced", "multi-model")
    by_role = doc["by_role"]
    google = sorted(r for r, c in by_role.items() if c["model"].startswith("google/"))
    assert google == ["doc-writer", "explore", "tester"]
    for r in ("planner", "software-architect", "code-reviewer", "verifier", "security-auditor"):
        assert by_role[r]["model"] == "anthropic/claude-opus-5.5" and not by_role[r].get("fallbacks"), r
    assert doc["defaults"]["fallbacks"] == []
    assert wizard._google_roles(_or_state()) == ["explore", "tester", "doc-writer"]


def test_declined_optin_marks_the_gemini_refs_skip_and_review_names_the_roles(monkeypatch):
    s = _or_state()
    infos = []
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda msg, default=False: (infos.append(("ask", msg, default)) or False))
    monkeypatch.setattr(ui, "info", lambda m: infos.append(("info", m)))
    wizard._GEMINI_ASKED.clear()
    wizard._openrouter_gemini_optin(s)
    ask = [i for i in infos if i[0] == "ask"]
    assert len(ask) == 1 and ask[0][2] is False
    assert ask[0][1] == ("Use Gemini on explore, tester and doc-writer through OpenRouter "
                         "(not yet verified with Claude Code)?")
    assert ("info", "Gemini held on OpenRouter: Claude Code x Google not verified; "
                    "explore, tester, doc-writer use the session model") in infos
    assert s.profile.allow_unverified is False
    from ai_resources.selection import Selection, SlotSel
    sel = Selection(shape="multi-provider", slots={
        "google:flash": SlotSel(ref="google/gemini-3.7-flash"), "anthropic:opus": SlotSel(ref="anthropic/claude-opus-5.5")})
    act = compat.plan(sel, "multi-model", "openrouter", ["claude"], allow_unverified=False)[0]
    assert act.model_refs == ["anthropic/claude-opus-5.5"]
    assert any("gemini-3.7-flash" in n for n in act.notes)


def test_accepted_optin_sets_allow_unverified_and_it_is_saved(monkeypatch, tmp_path):
    s = _or_state()
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda msg, default=False: True)
    wizard._GEMINI_ASKED.clear()
    wizard._openrouter_gemini_optin(s)
    assert s.profile.allow_unverified is True
    assert state._to_dict(s)["profile"]["allow_unverified"] is True


def test_the_optin_is_never_answered_for_the_operator(monkeypatch):
    s = _or_state()
    asked = []
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: asked.append(a) or True)
    monkeypatch.setattr(ui, "info", lambda m: None)
    wizard._GEMINI_ASKED.clear()
    wizard._openrouter_gemini_optin(s)
    assert asked == [] and s.profile.allow_unverified is False


def test_the_optin_is_skipped_when_the_cell_is_verified_or_there_is_no_google_role(monkeypatch):
    s = _or_state()
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    asked = []
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: asked.append(a) or True)
    monkeypatch.setattr(compat, "claude_google_openrouter_verified", lambda: True)
    wizard._GEMINI_ASKED.clear()
    wizard._openrouter_gemini_optin(s)
    monkeypatch.undo()
    other = _or_state()
    other.profile.name = "gateway/performance"
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: asked.append(a) or True)
    wizard._openrouter_gemini_optin(other)
    assert asked == []


def test_gateway_balanced_only_unverified_refs_are_google():
    doc = profiles.load_profile("gateway/balanced", "multi-model")
    from test_profile_validity import profile_refs, selection_from_profile
    sel = selection_from_profile(doc)
    plan = compat.plan(sel, "multi-model", "openrouter", ["claude"], allow_unverified=False)[0]
    skipped = {n.split(":", 1)[0] for n in plan.notes}
    assert skipped == {"google/gemini-3.7-flash"}
    assert {r.split("/", 1)[0] for r in plan.model_refs} == {"anthropic"}
    assert profile_refs(doc)


# --- AC-S6f: per-row Google routing ----------------------------------------------------------------------------------

def test_an_openrouter_key_alone_is_not_google_credentials_on_the_litellm_row():
    accounts = ma.detect(ma.Deps(env=lambda: {"OPENROUTER_API_KEY": "x"}, status=lambda: None))
    assert accounts["openrouter"].credentialed
    assert ma.routed_credentialed(accounts, "google", "openrouter") is True
    assert ma.routed_credentialed(accounts, "google", "litellm") is False
    with_key = ma.detect(ma.Deps(env=lambda: {"GEMINI_API_KEY": "x"}, status=lambda: None))
    assert ma.routed_credentialed(with_key, "google", "litellm") is True
    assert ma.routed_credentialed(with_key, "google", "openrouter") is False


def _litellm_state(with_google):
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "litellm"
    s.providers = {"anthropic": state.ProviderState(enabled=True)}
    if with_google:
        s.providers["google"] = state.ProviderState(enabled=True)
    return s


def test_a_legacy_litellm_state_without_google_renders_no_gemini_deployment_and_one_line():
    import yaml
    from ai_resources.setup import litellm
    doc = profiles.load_profile("gateway/balanced", "multi-model")
    executors = profiles.to_executors(doc, "litellm")
    s = _litellm_state(False)
    routed, lines = team_view.degrade_unrouted(executors, s.providers)
    assert len(lines) == 1 and lines[0].startswith("Gemini not active:")
    assert "explore, tester, doc-writer" in lines[0]
    text = litellm._render_litellm_yaml(routed, {k: v.__dict__ for k, v in s.providers.items()}, "LITELLM_MASTER_KEY")
    assert "gemini/" not in text
    for cfg in routed["by_role"].values():
        assert not cfg["model"].startswith("google/")
    # with the provider on, nothing is degraded and the deployment is rendered
    kept, none = team_view.degrade_unrouted(executors, _litellm_state(True).providers)
    assert none == [] and kept == executors
    assert "gemini/" in litellm._render_litellm_yaml(kept, {"anthropic": {"enabled": True}, "google": {"enabled": True}},
                                                     "LITELLM_MASTER_KEY")
    assert yaml.safe_load(text)["model_list"]


def test_degrade_leaves_a_google_role_with_no_claude_fallback_and_says_so():
    executors = {"by_role": {"explore": {"model": "google/gemini-3.7-flash"}}, "defaults": {"fallbacks": []}}
    out, lines = team_view.degrade_unrouted(executors, {})
    assert out["by_role"]["explore"]["model"] == "google/gemini-3.7-flash"
    assert lines and "no Claude fallback" in lines[0]


def test_the_wizard_degrades_only_the_litellm_row():
    executors = profiles.to_executors(profiles.load_profile("gateway/balanced", "multi-model"), "openrouter")
    s = _or_state()
    assert wizard._routed_executors(s, executors) is executors
    legacy = _litellm_state(False)
    assert wizard._routed_executors(legacy, executors)["by_role"]["explore"]["model"].startswith("anthropic/")


# --- AC-S6g: every Balanced row has both providers or says why not ---------------------------------------------------

DEGRADED_ROWS = {"single": "Gemini cannot run on 'Each on its own'",
                 "litellm": "LiteLLM is legacy; choose OpenRouter for Gemini"}


def _declared_providers(row):
    from ai_resources import model_providers
    name = wizard.PRESET_PROFILES[row]["balanced"]
    doc = profiles.load_profile(name, "single-model" if row in ("cli", "single") else "multi-model")
    refs = [c["model"] for c in doc["by_role"].values()]
    refs += [c[4:] for _k, _r, c in _all_cands(doc.get("lanes") or {}) if c.startswith("agy:")]
    out = set()
    for ref in refs:
        if ref.startswith("gemini-"):
            out.add("google")
        else:
            parsed = model_providers.identify(ref)
            out.add(parsed.provider if parsed else ("anthropic" if ref in IDS else ref))
    return out


@pytest.mark.parametrize("row", ["cli", "single", "openrouter", "litellm"])
def test_each_balanced_row_declares_anthropic_and_google_or_is_a_listed_degraded_row(row):
    declared = _declared_providers(row)
    if row in DEGRADED_ROWS:
        return
    assert {"anthropic", "google"} <= declared, (row, declared)


def test_the_legacy_litellm_balanced_is_claude_only_and_checker_fixed():
    assert _declared_providers("litellm") == {"anthropic"}
    by_role = _by_role(profiles.load_profile("balanced-claude-55", "multi-model"))
    assert by_role["verifier"]["model"] == "anthropic/claude-opus-5.5"
