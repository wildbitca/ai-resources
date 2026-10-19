"""ADR-0011 (plan I5): the image route catalog and the mediaModels builder. Pure: no process, no file."""
from __future__ import annotations

import dataclasses
import json

import pytest

from ai_resources.setup.cockpits import _openclaw_image as img


# --- AC-Q1a: every route has labels, none is recommended -----------------------------------------------------------

def test_every_route_has_a_billing_and_a_proof_label():
    for r in img.ROUTES.values():
        assert r.billing_label.strip() and r.proof_label.strip(), r.id


def test_no_route_carries_a_recommended_flag_or_wording():
    assert not any("recommend" in f.name.lower() for f in dataclasses.fields(img.Route))
    text = json.dumps([dataclasses.asdict(r) for r in img.ROUTES.values()]).lower()
    assert "recommend" not in text and "best" not in text and "default choice" not in text
    assert not any("recommend" in img.choice_label(r).lower() for r in img.ROUTES.values())


def test_pay_per_use_needs_a_source_line_and_the_rest_read_not_verified():
    for r in img.CATALOG:
        if "pay per use" in r.billing_label:
            assert r.billing_source and ":" in r.billing_source, r.id
        else:
            assert not r.billing_source, r.id
            assert any(w in r.billing_label for w in ("not verified", "NOT PROVEN")), r.id
    assert img.ROUTES["openai-key"].billing_label == "pay per use"
    assert img.ROUTES["openrouter"].billing_label == img.BILLING_UNVERIFIED


def test_every_route_generation_is_docs_only():
    for r in img.CATALOG:
        assert "docs only, not tested on this host" in r.proof_label, r.id


def test_litellm_is_not_a_route_and_the_pseudo_routes_exist():
    assert "litellm" not in " ".join(img.route_ids()).lower()
    assert not any("litellm" in m for r in img.CATALOG for m in r.models)
    assert img.route_ids()[:2] == ["keep", "disable"]
    assert img.KEEP.kind == "keep" and img.DISABLE.kind == "disable"
    assert "affects all agents" in img.DISABLE.label and "tools.deny" in img.DISABLE.label


def test_the_documented_routes_are_all_present():
    assert {r.id for r in img.CATALOG} == {
        "openai-codex", "openai-key", "google-key", "openrouter", "xai", "fal", "deepinfra", "minimax",
        "microsoft-foundry", "vydra", "comfyui"}


def test_comfyui_is_not_selectable_without_a_workflow_and_says_why():
    r = img.ROUTES["comfyui"]
    assert r.selectable_without_provider_config is False and "workflow" in r.requires
    with pytest.raises(ValueError):
        img.build_patch("comfyui", "comfy/workflow")
    assert all(x.selectable_without_provider_config for x in img.CATALOG if x.id != "comfyui")


def test_routes_name_env_vars_never_values():
    for r in img.CATALOG:
        for name in r.env:
            assert name.isupper() and "=" not in name and " " not in name
    assert img.ROUTES["google-key"].env == ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def test_choice_labels_have_one_shape_for_every_route():
    for r in img.CATALOG:
        label = img.choice_label(r)
        assert label.count(" - ") >= 2 and " via " in label and r.billing_label in label and r.proof_label in label


def test_no_route_has_an_unsourced_antigravity_or_gemini_oauth_path():
    assert not [r for r in img.CATALOG if "antigravity" in r.id or "oauth" in r.auth and r.provider == "google"]


# --- AC-4: build_patch --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("route", [r for r in img.CATALOG if r.selectable_without_provider_config and r.models])
def test_build_patch_touches_only_the_media_model_image_path_and_holds_no_env_value(route):
    ref = route.models[0] if not route.free_text_model else "microsoft-foundry/mai-image-2"
    patch = img.build_patch(route.id, ref)
    assert patch == {"agents": {"defaults": {"mediaModels": {"image": {"primary": ref, "timeoutMs": 180000}}}}}
    assert img.REPLACE_PATHS == ["agents.defaults.mediaModels.image"]
    text = json.dumps(patch)
    assert all(name not in text for name in route.env)


def test_build_patch_is_none_for_keep_and_disable():
    assert img.build_patch("keep", "") is None and img.build_patch("disable", "") is None


@pytest.mark.parametrize("bad", ["", "gpt-image-2", "openai/", "/gpt", "openai/gpt image", "openai/gpt\n2", "openai//x",
                                 "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "openai/" + "A" * 40, 5, None,
                                 "openai/x" + "y" * 200, "other/gpt-image-2", "openai/ghp_abc"])
def test_an_invalid_ref_raises_before_any_patch(bad):
    with pytest.raises(ValueError):
        img.build_patch("openai-key", bad)


def test_an_unknown_route_raises():
    with pytest.raises(ValueError):
        img.build_patch("nope", "openai/gpt-image-2")


def test_other_model_refs_are_accepted_when_they_fit_the_route_prefix():
    assert img.validate_model_ref("fal/fal-ai/flux/dev", img.ROUTES["fal"]) == "fal/fal-ai/flux/dev"
    assert img.validate_model_ref("  openrouter/x/y  ", img.ROUTES["openrouter"]) == "openrouter/x/y"
    with pytest.raises(ValueError):
        img.validate_model_ref("fal/x", img.ROUTES["openrouter"])


def test_foundry_takes_a_free_text_deployment_checked_like_any_ref():
    assert img.build_patch("microsoft-foundry", "microsoft-foundry/my-mai-deployment")
    with pytest.raises(ValueError):
        img.build_patch("microsoft-foundry", "microsoft-foundry/my deployment")


# --- restore_patch --------------------------------------------------------------------------------------------------------

def test_restore_patch_uses_null_for_an_absent_key_and_a_replace_path_for_a_value():
    patch, paths = img.restore_patch(None, True)
    assert patch == {"agents": {"defaults": {"mediaModels": {"image": None}}}} and paths == []
    prev = {"primary": "openai/gpt-image-2", "fallbacks": ["fal/x/y"], "timeoutMs": 1}
    patch, paths = img.restore_patch(prev, False)
    assert patch["agents"]["defaults"]["mediaModels"]["image"] == prev and paths == img.REPLACE_PATHS
    patch, paths = img.restore_patch("openai/gpt-image-2", False)
    assert patch["agents"]["defaults"]["mediaModels"]["image"] == "openai/gpt-image-2"


# --- read-only account detection (plan I6, AC-5) --------------------------------------------------------------------------

import pathlib

from ai_resources import model_accounts as ma

STATUS = pathlib.Path(__file__).parent / "fixtures" / "openclaw_models_status"
SENTINEL = "FAKEKEY-DO-NOT-LEAK"


def accounts_from(name):
    return ma.allowlist_status(json.loads((STATUS / name).read_text()))


def test_account_a_gemini_key_only_in_the_kit_env_is_kit_env_only_and_names_the_fix_without_reading_a_value():
    view = img.provider_account(img.ROUTES["google-key"], accounts_from("empty.json"), {"GEMINI_API_KEY"})
    assert view.source_label == "kit-env-only" and view.gateway_sees_it is False
    assert view.name == "GEMINI_API_KEY"
    assert view.fix == "openclaw models auth paste-api-key --provider google"


def test_account_the_gateway_env_and_profiles_count_as_seen():
    env = img.provider_account(img.ROUTES["google-key"], accounts_from("google_env.json"))
    assert (env.gateway_sees_it, env.source_label) == (True, "openclaw-env") and "GEMINI_API_KEY" in env.name
    prof = img.provider_account(img.ROUTES["openrouter"], accounts_from("openrouter_profile.json"))
    assert (prof.gateway_sees_it, prof.source_label, prof.name) == (True, "openclaw-profile", "openrouter:*")
    openai = accounts_from("openai_profile.json")
    for rid in ("openai-key", "openai-codex"):
        assert img.provider_account(img.ROUTES[rid], openai).source_label == "openclaw-profile"


def test_account_missing_names_the_env_vars_and_the_sign_in_command():
    view = img.provider_account(img.ROUTES["fal"], accounts_from("empty.json"))
    assert (view.gateway_sees_it, view.source_label, view.name) == (False, "missing", "FAL_KEY")
    assert view.fix == "openclaw models auth paste-api-key --provider fal"
    assert img.provider_account(img.ROUTES["fal"], None).source_label == "missing"


def test_account_comfyui_needs_provider_config_and_foundry_has_no_established_command():
    view = img.provider_account(img.ROUTES["comfyui"], accounts_from("openai_profile.json"))
    assert view.source_label == "provider-config-needed" and "workflow" in view.name
    assert img.sign_in_command(img.ROUTES["microsoft-foundry"]) == ""
    assert img.provider_account(img.ROUTES["microsoft-foundry"], None).fix == ""


def test_sign_in_commands_carry_the_provider_id_only():
    assert img.sign_in_command(img.ROUTES["openai-codex"]) == "openclaw models auth login --provider openai"
    assert img.sign_in_command(img.ROUTES["openai-key"]) == "openclaw models auth paste-api-key --provider openai"
    assert img.sign_in_command(img.ROUTES["minimax"]) == "openclaw models auth paste-api-key --provider minimax"
    for r in img.CATALOG:
        cmd = img.sign_in_command(r)
        assert "=" not in cmd and "sk-" not in cmd and not any(name in cmd for name in r.env)


def test_account_never_reads_or_returns_a_secret_value():
    hostile = accounts_from("hostile.json")
    assert SENTINEL not in json.dumps(hostile)
    view = img.provider_account(img.ROUTES["fal"], hostile)
    assert SENTINEL not in repr(view) and view.source_label == "openclaw-env"
    for fixture in STATUS.glob("*.json"):
        for r in img.CATALOG:
            assert SENTINEL not in repr(img.provider_account(r, accounts_from(fixture.name), {"FAL_KEY"}))


def test_account_keep_and_disable_have_no_account():
    assert img.provider_account(img.KEEP, None).source_label == "missing"
