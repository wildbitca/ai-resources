"""ADR-0011 (plan I7): the image question in phase 2.1 asks, shows every route with its labels, recommends nothing, never
asks for a key and never writes. Every UI call is scripted; the only subprocess seam (`_image_accounts`) is stubbed."""
from __future__ import annotations

import json
import pathlib

import pytest

from ai_resources import model_accounts as ma
from ai_resources.setup import state, ui, wizard
from ai_resources.setup.cockpits import _openclaw_image as img
from ai_resources.setup.cockpits import openclaw as cockpit
from wizard_fakes import Script

REAL_IMAGE_ACCOUNTS = cockpit._image_accounts            # captured before conftest stubs it for every test
STATUS = pathlib.Path(__file__).parent / "fixtures" / "openclaw_models_status"
Q = "Which image generation"


def _state(*, openclaw=True, claude=True):
    s = state.SetupState()
    s.cockpits["claude"] = state.CockpitState(installed=claude)
    s.cockpits["openclaw"] = state.CockpitState(installed=openclaw)
    s.profile.customizations["__targets__"] = {"selected": (["claude"] if claude else []) + (["openclaw"] if openclaw else [])}
    s.openclaw.engine = "keep"
    return s


@pytest.fixture
def no_other_prompts(monkeypatch, tmp_path):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg))
    return cfg


def status_accounts(name):
    return ma.allowlist_status(json.loads((STATUS / name).read_text()))


# --- AC-Q1b -----------------------------------------------------------------------------------------------------------

def test_the_picker_preselects_keep_and_lists_disable_and_every_route(monkeypatch, no_other_prompts):
    s = _state()
    script = Script(**{Q: "keep"})
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s)
    assert script.defaults[next(k for k in script.defaults if Q in k)] == ["keep"]
    titles = "\n".join(script.titles)
    assert "Keep what OpenClaw does now" in titles and "turn image generation off" in titles
    for route in img.CATALOG:
        assert img.choice_label(route) in titles, route.id
    assert "recommend" not in titles.lower() and "LiteLLM" not in titles
    assert s.openclaw.image_mode == "keep"


def test_comfyui_is_a_disabled_choice_with_its_reason_until_a_workflow_is_configured():
    choices = img.route_choices({}, {})
    comfy = [c for c in choices if getattr(c, "value", None) == "comfyui"][0]
    assert img.effective_route(img.ROUTES["comfyui"], {}).selectable_without_provider_config is False
    assert "[" not in comfy.title.rsplit(" - ", 1)[-1]                       # no visibility marker on a greyed row
    if hasattr(comfy, "disabled"):
        assert comfy.disabled == "configure a ComfyUI workflow in OpenClaw first"
    configured = {"models": {"providers": {"comfy": {"baseUrl": "http://127.0.0.1:8188"}}}}
    assert img.effective_route(img.ROUTES["comfyui"], configured).selectable_without_provider_config is True
    enabled = [c for c in img.route_choices(configured, {}) if getattr(c, "value", None) == "comfyui"][0]
    assert not getattr(enabled, "disabled", None)


def test_every_enabled_row_carries_a_visibility_marker():
    titles = [c.title for c in img.route_choices({}, status_accounts("openai_profile.json"), {"FAL_KEY"})]
    assert any(t.endswith("[openclaw-profile]") for t in titles)
    assert any(t.endswith("[kit-env-only]") for t in titles) and any(t.endswith("[missing]") for t in titles)


# --- AC-7: defaults, never-answered, saved answers -----------------------------------------------------------------------

def test_a_never_answered_state_resolves_to_keep_and_a_saved_answer_is_the_default():
    s = state.SetupState()
    assert img.default_mode(s) == "keep"
    s.openclaw.image_mode = "openrouter"
    assert img.default_mode(s) == "openrouter"
    s.openclaw.image_mode = "not-a-route"
    assert img.default_mode(s) == "keep"


def test_a_non_interactive_run_asks_nothing_runs_nothing_and_leaves_the_state(monkeypatch):
    s = _state()
    before = state._to_dict(s)
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    called = []
    monkeypatch.setattr(cockpit, "_image_accounts", lambda: called.append(1) or ({}, []))
    wizard._step_cockpit_choice(s)
    assert script.asked == [] and called == [] and state._to_dict(s) == before
    assert s.openclaw.image_mode == ""


def test_an_answered_question_is_not_asked_again_but_more_options_asks_it_again(monkeypatch, no_other_prompts):
    s = _state()
    s.openclaw.image_mode = "disable"
    script = Script()
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s)
    assert script.asked == []
    script = Script(**{Q: "keep"})
    script.install(monkeypatch)
    cockpit.prompt_image(s)
    assert script.defaults[next(iter(script.defaults))] == ["disable"] and s.openclaw.image_mode == "keep"


# --- AC-6: never without OpenClaw --------------------------------------------------------------------------------------

def test_claude_code_alone_never_reaches_the_image_prompt(monkeypatch):
    s = _state(openclaw=False)
    script = Script()
    script.install(monkeypatch)
    monkeypatch.setattr(cockpit, "prompt_image", lambda *a, **k: pytest.fail("image prompt called"))
    wizard._step_cockpit_choice(s)
    assert script.asked == []


# --- AC-8: no secret prompt, no write, only the status read ----------------------------------------------------------------

def test_picking_a_route_asks_model_and_account_and_never_a_secret(monkeypatch, no_other_prompts):
    s = _state()
    script = Script(**{Q: "openai-key", "Model for": "openai/gpt-image-2"})
    script.install(monkeypatch)
    monkeypatch.setattr(cockpit, "_image_accounts", lambda: (status_accounts("openai_profile.json"), []))
    before = no_other_prompts.read_bytes()
    wizard._step_cockpit_choice(s)
    assert s.openclaw.image_mode == "openai-key" and s.openclaw.image_model == "openai/gpt-image-2"
    assert not any(a.startswith("password") for a in script.asked)
    assert no_other_prompts.read_bytes() == before
    assert any("does not fall back to OPENAI_API_KEY" in m for m in script.shown)       # F7
    assert s.openclaw.image_account == "openai:*"


def test_a_route_without_a_sign_in_says_it_signs_in_at_apply(monkeypatch, no_other_prompts):
    s = _state()
    script = Script(**{Q: "fal", "Model for": "fal/krea/v2/medium/text-to-image"})
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s)
    assert s.openclaw.image_account == img.SIGN_IN_AT_APPLY
    assert any("never sees the key" in m for m in script.shown)


def test_other_model_ref_is_validated_and_a_secret_looking_ref_is_refused(monkeypatch, no_other_prompts):
    s = _state()
    script = Script(**{Q: "fal", "Model for": img.OTHER_REF, "model ref": "fal/fal-ai/flux/dev"})
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s)
    assert s.openclaw.image_model == "fal/fal-ai/flux/dev"
    s2 = _state()
    script = Script(**{Q: "fal", "Model for": img.OTHER_REF, "model ref": "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"})
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s2)
    assert "sk-proj" not in s2.openclaw.image_model and "sk-proj" not in json.dumps(state._to_dict(s2))


def test_foundry_asks_a_validated_deployment_name_with_ui_text(monkeypatch, no_other_prompts):
    s = _state()
    script = Script(**{Q: "microsoft-foundry", "model ref": "microsoft-foundry/mai-image-2"})
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s)
    assert s.openclaw.image_model == "microsoft-foundry/mai-image-2"
    assert any(a.startswith("text: ") for a in script.asked)


def test_minimax_asks_the_sign_in_method(monkeypatch, no_other_prompts):
    s = _state()
    script = Script(**{Q: "minimax", "Model for": "minimax/image-01", "How do you sign in": "oauth"})
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s)
    assert s.openclaw.image_account == "oauth"


def test_disable_warns_that_it_affects_all_agents_and_notes_an_operator_entry(monkeypatch, no_other_prompts):
    s = _state()
    script = Script(**{Q: "disable"})
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s)
    assert any("ALL agents" in m and "NOT PROVEN" in m for m in script.shown)
    no_other_prompts.write_text(json.dumps({"tools": {"deny": ["group:media"]}}))
    s2 = _state()
    script = Script(**{Q: "disable"})
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s2)
    assert any("already denied by your tools.deny entry 'group:media'" in m for m in script.shown)


def test_a_route_with_an_operator_covering_entry_and_an_existing_model_warns(monkeypatch, no_other_prompts):
    no_other_prompts.write_text(json.dumps({
        "tools": {"deny": ["image*"]},
        "agents": {"defaults": {"mediaModels": {"image": {"primary": "fal/fal-ai/flux/dev"}}}}}))
    s = _state()
    script = Script(**{Q: "openrouter", "Model for": "openrouter/google/gemini-3.1-flash-image-preview"})
    script.install(monkeypatch)
    wizard._step_cockpit_choice(s)
    text = "\n".join(script.shown)
    assert "still blocks image_generate; setup does not remove it" in text
    assert "already has an image model set (fal/fal-ai/flux/dev)" in text


def test_keep_names_existing_sign_ins_without_calling_it_advice(monkeypatch, no_other_prompts):
    s = _state()
    script = Script(**{Q: "keep"})
    script.install(monkeypatch)
    monkeypatch.setattr(cockpit, "_image_accounts", lambda: (status_accounts("openrouter_profile.json"), []))
    wizard._step_cockpit_choice(s)
    text = "\n".join(script.shown)
    assert "leaves image generation as it is" in text and "openrouter" in text and "recommend" not in text.lower()


def test_the_only_subprocesses_the_prompt_makes_are_the_two_read_only_models_reads(monkeypatch):
    from ai_resources.setup import credentials
    calls = []
    monkeypatch.setattr(cockpit, "_image_accounts", REAL_IMAGE_ACCOUNTS)
    monkeypatch.setattr(cockpit, "_openclaw", lambda args, stdin=None, timeout=120: calls.append(list(args)) or (0, "{}"))
    monkeypatch.setattr(credentials, "load_env", lambda: {"FAL_KEY": "value-never-returned", "EMPTY": ""})
    accounts, names = cockpit._image_accounts()
    assert calls == [["models", "status", "--json"], ["models", "auth", "list", "--json"]] and accounts == {}
    assert names == ["FAL_KEY"] and "value-never-returned" not in repr((accounts, names))


def test_image_accounts_survive_a_failing_or_hostile_status_read(monkeypatch):
    from ai_resources.setup import credentials
    monkeypatch.setattr(cockpit, "_image_accounts", REAL_IMAGE_ACCOUNTS)
    monkeypatch.setattr(credentials, "load_env", lambda: {})
    monkeypatch.setattr(cockpit, "_openclaw", lambda *a, **k: (1, "boom"))
    assert cockpit._image_accounts()[0] == {}
    hostile = (STATUS / "hostile.json").read_text()
    monkeypatch.setattr(cockpit, "_openclaw", lambda *a, **k: (0, hostile))
    accounts, _names = cockpit._image_accounts()
    assert "FAKEKEY-DO-NOT-LEAK" not in json.dumps(accounts)


def test_auth_list_ids_are_merged_as_names_and_nothing_else(monkeypatch):
    from ai_resources.setup import credentials
    monkeypatch.setattr(cockpit, "_image_accounts", REAL_IMAGE_ACCOUNTS)
    monkeypatch.setattr(credentials, "load_env", lambda: {})
    listing = json.dumps({"profiles": [{"provider": "openrouter", "id": "openrouter:default", "key": "FAKEKEY-DO-NOT-LEAK"}]})

    def fake(args, stdin=None, timeout=120):
        return (0, listing) if args[:3] == ["models", "auth", "list"] else (0, "{}")
    monkeypatch.setattr(cockpit, "_openclaw", fake)
    accounts, _names = cockpit._image_accounts()
    assert accounts == {"openrouter": {"ids": ["openrouter:default"]}}
