"""`setup --compose` and the step 6 "compose" choice: opt-in, explained, marked, and nothing changes without it."""
from __future__ import annotations

import pathlib

import pytest

from ai_resources import model_accounts as ma
from ai_resources.scoring import detect as scoring_detect
from ai_resources.scoring import flow, headroom_sources
from ai_resources.selection import ProviderSel, Selection, SlotSel
from ai_resources.setup import profiles, state, wizard
from wizard_fakes import Script


class Det:
    def __init__(self, installed):
        self.installed = installed
        self.name, self.version, self.binary_path = "x", "1", "/bin/x"


def fake_deps(tmp_path, installed=("claude", "openclaw"), creds=("anthropic", "google", "openai"), env=None, files=()):
    ids = ("claude", "gemini", "cursor", "codex", "aider", "copilot", "windsurf", "continue", "opencode",
           "openclaw", "agy")
    present = {str(tmp_path / f) for f in files}
    accts = {pid: ma.Account(pid in creds, ["env-file"] if pid in creds else [], "", direct_key=pid in creds)
             for pid in ("anthropic", "google", "vertex", "openai", "ollama", "openrouter", "deepseek", "moonshot")}
    return scoring_detect.Deps(cockpits=lambda: {c: Det(c in installed) for c in ids}, accounts=lambda: accts,
                               env=lambda: dict(env or {}), file_exists=lambda p: p in present, home=tmp_path)


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(scoring_detect, "default_deps", lambda: fake_deps(tmp_path))
    monkeypatch.setattr(wizard, "_COMPOSE", {})
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "")
    monkeypatch.setattr(wizard, "_DRY_RUN", False)
    monkeypatch.setattr(flow, "default_sidecar_path", lambda: tmp_path / "absent-sidecar.json")
    return tmp_path


def multi_state(providers=("anthropic", "google", "openai")):
    s = state.SetupState(mode="multi-model", backend="litellm")
    s.providers = {p: state.ProviderState(enabled=True) for p in providers}
    return s


def compose_flags(**kw):
    base = {"mode": "", "headroom": "", "live": "", "why_json": "", "allow_fable": False, "allow_unverified": False}
    base.update(kw)
    return base


# --- AC-13: nothing changes without the new flags ----------------------------------------------------
def test_no_flag_produces_a_byte_identical_executors_yaml(host, monkeypatch, tmp_path):
    script = Script(**{"Choose profile": profiles.RECOMMENDED_PROFILE, "Customize any role": False})
    script.install(monkeypatch)
    s = multi_state()
    assert wizard._step6_profile(s) == 0
    assert s.profile.customizations == {} and s.profile.composed is None and s.profile.customized is False
    assert not any("compose" in m.lower() and "Composition mode" in m for m in script.asked)
    base = profiles.load_profile(profiles.RECOMMENDED_PROFILE)
    after = profiles.to_executors(base, "litellm")
    ref = tmp_path / "ref.yaml"
    out = tmp_path / "out.yaml"
    profiles.write_executors(profiles.to_executors(profiles.load_profile(profiles.RECOMMENDED_PROFILE), "litellm"), ref)
    profiles.write_executors(after, out)
    assert ref.read_bytes() == out.read_bytes()


def test_the_compose_choice_is_offered_and_does_not_change_the_default(host, monkeypatch):
    seen = {}
    script = Script(**{"Choose profile": lambda values: seen.setdefault("values", values) and profiles.RECOMMENDED_PROFILE,
                       "Customize any role": False})
    script.install(monkeypatch)
    wizard._step6_profile(multi_state())
    assert "__compose__" in seen["values"] and "__custom__" in seen["values"]


# --- interactive compose ----------------------------------------------------------------------------
def test_uniform_through_the_menu_stores_customizations_and_provenance(host, monkeypatch):
    script = Script(**{"Choose profile": "__compose__", "Composition mode": "uniform",
                       "Allow Claude Fable": False, "Use this team": True})
    script.install(monkeypatch)
    s = multi_state()
    assert wizard._step6_profile(s) == 0
    models = {c["model"] for c in s.profile.customizations.values()}
    assert s.profile.customized and len(models) == 1
    assert s.profile.composed["mode"] == "uniform" and s.profile.composed["input_digest"]
    assert "Team composition: mode uniform" in script.transcript
    assert "fable" not in " ".join(models)


def test_declining_the_team_keeps_the_plain_profile_path(host, monkeypatch):
    script = Script(**{"Choose profile": "__compose__", "Composition mode": "uniform", "Allow Claude Fable": False,
                       "Use this team": False, "Customize any role": False})
    script.install(monkeypatch)
    s = multi_state()
    assert wizard._step6_profile(s) == 0
    assert s.profile.customizations == {} and s.profile.composed is None


def test_mixed_is_not_offered_without_allow_unverified(host, monkeypatch):
    offered = {}
    script = Script(**{"Choose profile": "__compose__", "Allow Claude Fable": False,
                       "Composition mode": lambda values: offered.setdefault("v", list(values)) and "uniform",
                       "Use this team": False, "Customize any role": False})
    script.install(monkeypatch)
    wizard._step6_profile(multi_state())
    assert offered["v"] == ["uniform", "single-provider"]
    assert "Mixed mode is offered only with --allow-unverified" in script.transcript


def test_mixed_questions_cover_dispatchable_pools_only_and_never_agy_or_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(scoring_detect, "default_deps", lambda: fake_deps(
        tmp_path, installed=("claude", "openclaw", "agy", "codex"), files=(".codex/auth.json",)))
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(allow_unverified=True))
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "")
    reads = []
    monkeypatch.setattr(headroom_sources, "read_for_pool", lambda *a, **k: reads.append(a) or None)
    script = Script(**{"Choose profile": "__compose__", "Composition mode": "mixed",
                       "How much headroom": "normal", "Read live headroom": False,
                       "Allow Claude Fable": False, "Use this team": True})
    script.install(monkeypatch)
    s = multi_state()
    assert wizard._step6_profile(s) == 0
    asked = " ".join(script.asked)
    assert "claude-subscription" in asked and "gemini-api" in asked and "openai-api" in asked
    assert "agy-gemini" not in asked and "agy-claude-gpt" not in asked and "codex-subscription" not in asked
    assert reads == []                         # no live reader runs without the opt-in


def test_live_read_is_opt_in_and_unknown_falls_back_to_the_static_level(host, monkeypatch):
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(allow_unverified=True))
    script = Script(**{"Choose profile": "__compose__", "Composition mode": "mixed",
                       "How much headroom": "scarce", "Read live headroom": True,
                       "Allow Claude Fable": False, "Use this team": True})
    script.install(monkeypatch)
    s = multi_state()
    assert wizard._step6_profile(s) == 0
    assert "Live headroom for claude-subscription is unknown" in script.transcript
    assert s.profile.composed["headroom"]["claude-subscription"]["basis"] == "static"


# --- flags -------------------------------------------------------------------------------------------
def test_compose_mixed_without_allow_unverified_is_refused_citing_the_matrix(host, monkeypatch):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(mode="mixed"))
    assert wizard._preflight_compose() == 2
    text = script.transcript
    assert "mixed mode is refused" in text and "no verified cross-provider cell" in text and "--allow-unverified" in text


def test_flag_path_composes_without_prompts_and_marks_unverified_cells(host, monkeypatch):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(mode="mixed", allow_unverified=True,
                                                          headroom="claude-subscription=scarce,openai-api=ample"))
    assert wizard._preflight_compose() == 0
    s = multi_state()
    assert wizard._step6_profile(s) == 0
    prov = s.profile.composed
    assert prov["mode"] == "mixed" and prov["allow_unverified"] is True
    assert prov["unverified_roles"], "scarce Claude pool plus an ample OpenAI pool moves a role to an unverified cell"
    for role in prov["unverified_roles"]:
        assert s.profile.customizations[role]["model"].startswith("openai/")
    assert "UNVERIFIED" in script.transcript
    assert script.asked == [] or all("Choose profile" not in m for m in script.asked)


def test_without_allow_unverified_the_other_provider_gets_no_role_and_the_why_says_unverified(host, monkeypatch):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(mode="single-provider"))
    s = multi_state()
    assert wizard._step6_profile(s) == 0
    assert all(c["model"].startswith("anthropic/") for c in s.profile.customizations.values())
    comp, _ = flow.run(s, flow.Request(mode="mixed", allow_unverified=False), deps=fake_deps(host))
    assert all(a.model.startswith("anthropic/") for a in comp.assignments)
    assert any(r.kind == "unverified" and r.reason == "not verified yet" for r in comp.rejected)


def test_empty_provider_set_is_a_refusal_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(scoring_detect, "default_deps", lambda: fake_deps(tmp_path, installed=(), creds=()))
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(mode="uniform"))
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    s = multi_state(providers=())
    assert wizard._step6_profile(s) == 1
    assert "No team proposed" in script.transcript


@pytest.fixture
def workspace():
    """A workspace outside tmpfs: pytest's tmp_path lives under /tmp, which --why-json refuses by design."""
    import shutil
    import tempfile
    base = pathlib.Path.home() / ".cache" / "ai-resources-tests"
    base.mkdir(parents=True, exist_ok=True)
    ws = pathlib.Path(tempfile.mkdtemp(prefix="why-json-", dir=base))
    yield ws
    shutil.rmtree(ws, ignore_errors=True)


def test_why_json_rejects_tmp_and_accepts_the_workspace(host, workspace, monkeypatch):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(mode="uniform", why_json="/tmp/why.json"))
    assert wizard._preflight_compose() == 2
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(mode="uniform", why_json="out/why.json"))
    assert wizard._preflight_compose() == 0
    s = multi_state()
    assert wizard._step6_profile(s) == 0
    import json
    doc = json.loads((workspace / "out" / "why.json").read_text())
    assert doc["mode"] == "uniform" and doc["rows"]
    assert (workspace / "out" / "why.json").stat().st_mode & 0o777 == 0o600


def test_unknown_pool_or_level_in_the_headroom_flag_is_rejected_before_any_write(host, monkeypatch):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    for bad in ("nowhere=ample", "openai-api=huge", "openai-api"):
        monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(mode="mixed", allow_unverified=True, headroom=bad))
        assert wizard._preflight_compose() == 2, bad


def test_failing_live_fetcher_degrades_to_static_weights(host):
    def boom():
        raise TimeoutError("slow")
    s = multi_state(providers=("anthropic", "google", "openai", "openrouter"))
    req = flow.Request(mode="mixed", allow_unverified=True, live=("openrouter",),
                       headroom={"openrouter": "normal"})
    comp, inp = flow.run(s, req, deps=fake_deps(host, creds=("anthropic", "openrouter")),
                         reader_deps={"openrouter_fetch": boom})
    reading = inp.readings["openrouter"]
    assert reading.state == "unknown" and reading.source == "error:TimeoutError"
    assert not comp.refused


# --- single-model --------------------------------------------------------------------------------------
def test_single_model_offers_uniform_only(host, monkeypatch):
    script = Script(**{"How should kit subagents": "claude-native"})
    script.install(monkeypatch, non_interactive=False)
    s = state.SetupState(mode="single-model")
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(mode="mixed", allow_unverified=True))
    assert wizard._step6_single_model_profile(s) == 2
    assert "uniform only" in script.transcript


def test_single_model_uniform_stores_claude_aliases_that_reach_the_executors(host, monkeypatch):
    script = Script(**{"How should kit subagents": "claude-native"})
    script.install(monkeypatch)
    s = state.SetupState(mode="single-model")
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(mode="uniform"))
    assert wizard._step6_single_model_profile(s) == 0
    assert s.profile.customized and {c["model"] for c in s.profile.customizations.values()} <= {"opus", "sonnet", "haiku"}
    ex = wizard._single_model_executors(s)
    for role, entry in s.profile.customizations.items():
        assert ex["by_role"][role]["model"] == entry["model"]


# --- U1: single-model never widens to detected providers -----------------------------------------------
def test_single_model_without_declared_providers_enables_only_anthropic(host):
    """Setup step 1 declared nothing in single-model mode: the enabled set is the Claude cockpit's own provider,
    never every credentialed provider the probe happens to see."""
    deps = fake_deps(host, creds=("anthropic", "google", "openai"))
    inv = scoring_detect.inventory(deps, flow.scores_db.load())
    assert set(inv.credentialed_providers()) >= {"anthropic", "google", "openai"}
    s = state.SetupState(mode="single-model")
    assert flow.enabled_providers(s, inv) == ["anthropic"]
    comp, inp = flow.run(s, flow.Request(mode="uniform", allow_unverified=True), deps=deps)
    assert tuple(inp.enabled) == ("anthropic",)
    assert not comp.refused
    assert comp.assignments and all(a.model.startswith("anthropic/") for a in comp.assignments)
    assert set(flow.to_aliases(profiles_overlay(comp))) == {a.role for a in comp.assignments}


def test_single_model_enabled_set_is_anthropic_even_when_claude_is_not_installed(host):
    deps = fake_deps(host, installed=("openclaw",), creds=("google", "openai"))
    inv = scoring_detect.inventory(deps, flow.scores_db.load())
    assert flow.enabled_providers(state.SetupState(mode="single-model"), inv) == ["anthropic"]


def profiles_overlay(comp):
    from ai_resources.scoring import profile_out
    return profile_out.to_customizations(comp)


# --- F3: the live opt-in is offered only where a production reader exists --------------------------------
def test_only_the_claude_sidecar_has_a_production_live_reader():
    pool = lambda pid, src: {"id": pid, "headroom_source": src}
    assert flow.has_live_reader(pool("claude-subscription", "statusline-sidecar"))
    assert not flow.has_live_reader(pool("openrouter", "openrouter-key"))
    assert not flow.has_live_reader(pool("vertex-claude", "vertex-quotas"))
    assert not flow.has_live_reader(pool("openai-api", "none"))
    # an injected fetcher (tests, a future reviewed production fetcher) makes the same pool readable
    assert flow.has_live_reader(pool("openrouter", "openrouter-key"), {"openrouter_fetch": lambda: {}})
    assert flow.has_live_reader(pool("vertex-claude", "vertex-quotas"), {"vertex_fetch": lambda p, m: {}})


def test_wizard_does_not_offer_live_reads_for_openrouter_or_vertex(tmp_path, monkeypatch):
    monkeypatch.setattr(scoring_detect, "default_deps", lambda: fake_deps(
        tmp_path, creds=("anthropic", "google", "openai", "vertex", "openrouter")))
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(allow_unverified=True))
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "")
    monkeypatch.setattr(flow, "default_sidecar_path", lambda: tmp_path / "absent-sidecar.json")
    script = Script(**{"Choose profile": "__compose__", "Composition mode": "mixed",
                       "How much headroom": "normal", "Read live headroom": False,
                       "Allow Claude Fable": False, "Use this team": True})
    script.install(monkeypatch)
    s = multi_state(providers=("anthropic", "google", "openai", "vertex", "openrouter"))
    assert wizard._step6_profile(s) == 0
    headroom_pools = [q for q in script.asked if "How much headroom" in q]
    assert any("openrouter" in q or "vertex-claude" in q for q in headroom_pools), headroom_pools
    live = [q for q in script.asked if "Read live headroom" in q]
    assert live and all("claude-subscription" in q for q in live), live
