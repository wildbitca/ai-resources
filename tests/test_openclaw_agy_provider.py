"""`openclaw_agy`: the agy-cli-lane provider plumbing a kit-role agent with an `agy:` cell needs (ADR-0019)."""
from __future__ import annotations

from ai_resources.setup.cockpits import openclaw_agy as A
from ai_resources.setup.cockpits import openclaw_lanes as L
from ai_resources.setup.cockpits import openclaw_roles as R


def test_the_stub_is_created_whole_when_the_provider_is_absent():
    stub = A.stub_patch_for_slugs(["b", "a", "a"], {})
    assert stub == {"baseUrl": A.STUB_BASE_URL, "api": A.STUB_API, "apiKey": A.STUB_API_KEY,
                    "models": [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}]}
    assert A.stub_patch_for_slugs([], {}) is None


def test_an_existing_provider_only_gains_the_missing_models_and_keeps_operator_values():
    doc = {"models": {"providers": {"agy-cli-lane": {"baseUrl": "mine", "models": [{"id": "a", "name": "A"}]}}}}
    assert A.stub_patch_for_slugs(["a"], doc) is None
    assert A.stub_patch_for_slugs(["a", "b"], doc) == {"models": [{"id": "a", "name": "A"}, {"id": "b", "name": "b"}]}


def test_agy_refs_are_recognised_and_slugs_read_back():
    assert A.is_agy_ref("agy-cli-lane/gemini-4-argon-high") and not A.is_agy_ref("anthropic/claude-sonnet-5-5")
    assert A.agy_slug({"model": "agy-cli-lane/x"}) == "x" and A.agy_slug({"model": "y"}) == "y"


def test_the_role_writer_adds_the_stub_for_the_slugs_of_every_chain():
    agents = [R.RoleAgent(model="agy-cli-lane/s1", fallbacks=["anthropic/claude-haiku-5-5", "agy-cli-lane/s2"]),
              R.RoleAgent(model="anthropic/claude-sonnet-5-5", fallbacks=[])]
    stub = R.agy_stub_patch(agents, {})
    assert [m["id"] for m in stub["models"]] == ["s1", "s2"]


def test_the_kit_lane_module_still_exposes_the_moved_names_until_it_goes():
    assert L.STUB_PROVIDER == A.STUB_PROVIDER and L.AGY_TOOL_DENY == A.AGY_TOOL_DENY
    assert L.LanesWriteError is A.LanesWriteError and L.ensure_agy_home is A.ensure_agy_home
