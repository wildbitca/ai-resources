"""Tests for the behaviour that was verified by hand while building it.

Everything here was checked once in a terminal during the 2026-09-16 session and
then had no gate: the CI ran `validate_kit.py` and `compileall`, so a change that
broke the gateway translation would have passed. These are the checks that
caught real defects, written down so they keep catching them.

No network, no credentials, no gateway. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import profiles, ui  # noqa: E402
from ai_resources.setup.cockpits import claude as claude_cockpit  # noqa: E402
from ai_resources.setup.litellm import (  # noqa: E402
    _CLAUDE_PASSTHROUGH_MODELS,
    _render_litellm_yaml,
    _split_model,
    _upstream_params,
)


# --- vendor -> upstream translation ---------------------------------------------

def test_split_model_prefers_the_canonical_vendor():
    assert _split_model("deepseek/deepseek-v4-pro", "ignored") == (
        "deepseek", "deepseek-v4-pro")


def test_split_model_falls_back_to_provider_for_legacy_bare_ids():
    assert _split_model("claude-sonnet-5", "anthropic") == ("anthropic", "claude-sonnet-5")


def test_anthropic_direct_route_converts_dots_to_dashes():
    """The catalogue spells it 4.5, Anthropic spells it 4-5. Without this the
    direct route 404s on a name that only exists on the gateway."""
    params = _upstream_params("anthropic/claude-haiku-4.5", "anthropic", "verifier")
    assert params["model"] == "anthropic/claude-haiku-4-5"


def test_gateway_route_preserves_the_catalogue_spelling():
    params = _upstream_params("openrouter/anthropic/claude-haiku-4.5", "openrouter", "verifier")
    assert params["model"] == "openrouter/anthropic/claude-haiku-4.5"


@pytest.mark.parametrize("model,expected_prefix", [
    ("google/gemini-3.7-flash", "gemini/"),
    ("deepseek/deepseek-v4-pro", "deepseek/"),
    ("moonshot/kimi-k2.7-code", "moonshot/"),
    ("openai/gpt-5.6-terra", "openai/"),
])
def test_each_vendor_gets_its_litellm_prefix(model, expected_prefix):
    assert _upstream_params(model, "", "role")["model"].startswith(expected_prefix)


def test_ollama_routes_to_localhost_without_a_key():
    params = _upstream_params("ollama/llama3.3:70b", "ollama", "explore")
    assert params["api_base"] == "http://127.0.0.1:11434"
    assert "api_key" not in params


def test_unknown_vendor_raises_instead_of_vanishing():
    """The old renderer hit `else: continue` and dropped the role in silence, so
    the gateway config was missing an entry nobody was told about."""
    with pytest.raises(RuntimeError) as err:
        _upstream_params("acme/foo-1", "acme", "planner")
    assert "planner" in str(err.value) and "acme" in str(err.value)


# --- the Claude-alias passthrough ----------------------------------------------

def test_passthrough_emits_aliases_without_the_anthropic_provider():
    """Gated on has_anthropic, this left the main conversation with no entry
    while every subagent worked."""
    executors = profiles.to_executors(profiles.load_profile("measured-best"), "litellm")
    doc = _render_litellm_yaml(executors, {"google": {"enabled": True}}, "LITELLM_MASTER_KEY")
    for alias in _CLAUDE_PASSTHROUGH_MODELS:
        assert f"model_name: {alias}" in doc


def test_passthrough_resolves_through_the_classes_block():
    executors = profiles.to_executors(profiles.load_profile("measured-best"), "litellm")
    assert executors["classes"], "measured-best must pin the aliases"
    doc = _render_litellm_yaml(executors, {}, "LITELLM_MASTER_KEY")
    # haiku is the case where the two spellings differ
    assert "anthropic/claude-haiku-4-5" in doc


def test_passthrough_carries_no_retired_model_ids():
    stale = ("4-7", "4-6", "20251001")
    assert not [m for m in _CLAUDE_PASSTHROUGH_MODELS if any(s in m for s in stale)]


# --- profile selection ----------------------------------------------------------

def test_gateway_agnostic_profiles_are_offered_under_both_backends():
    for backend in ("openrouter", "litellm"):
        offered = profiles.list_profiles(mode="multi-model", backend=backend)
        assert "measured-best" in offered, backend


def test_single_model_profiles_ignore_the_backend_filter():
    offered = profiles.list_profiles(mode="single-model", backend="openrouter")
    assert "claude-native" in offered


def test_to_executors_picks_the_gateway_per_backend():
    doc = profiles.load_profile("measured-best")
    assert "openrouter.ai" in profiles.to_executors(doc, "openrouter")["gateway"]["url"]
    assert "127.0.0.1" in profiles.to_executors(doc, "litellm")["gateway"]["url"]


def test_every_shipped_profile_renders():
    """A profile that names an unreachable vendor now fails loudly, so this also
    guards against shipping one."""
    enabled = {p: {"enabled": True} for p in
               ("anthropic", "google", "deepseek", "moonshot", "openrouter")}
    for name in profiles.list_profiles(mode="multi-model"):
        executors = profiles.to_executors(profiles.load_profile(name), "litellm")
        _render_litellm_yaml(executors, enabled, "LITELLM_MASTER_KEY")


def test_no_profile_carries_a_bare_model_id():
    for name in profiles.list_profiles(mode="multi-model"):
        for role, cfg in profiles.load_profile(name)["by_role"].items():
            assert "/" in cfg["model"], f"{name}:{role} is not canonical"


def test_known_personas_resolve_to_a_real_role():
    for persona, base in profiles.known_personas().items():
        assert base in profiles.KNOWN_ROLES
        assert persona.startswith(f"{base}-")


# --- non-interactive prompts ----------------------------------------------------

@pytest.fixture
def non_interactive():
    ui.set_non_interactive(True)
    yield
    ui.set_non_interactive(False)


def test_prompts_return_their_defaults(non_interactive):
    assert ui.select("m", ["a", "b"], default="b") == "b"
    assert ui.checkbox("m", ["a", "b"], default=["a"]) == ["a"]
    assert ui.checkbox("m", ["a", "b"]) == []
    assert ui.text("m", default="4000") == "4000"
    assert ui.confirm("m", default=False) is False
    assert ui.confirm("m", default=True) is True


def test_select_without_a_default_aborts(non_interactive):
    with pytest.raises(SystemExit):
        ui.select("profile?", ["a"])


def test_password_aborts_rather_than_returning_empty(non_interactive):
    """Returning "" would store an empty key and fail later at the gateway, far
    from the cause."""
    with pytest.raises(SystemExit):
        ui.password("OPENROUTER_API_KEY")


# --- subagent generation --------------------------------------------------------

@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_cockpit, "AGENTS_DIR", tmp_path)
    return tmp_path


def _executors(by_role=None, by_persona=None):
    return {"by_role": by_role or {}, "by_persona": by_persona or {}, "classes": {}}


def test_generates_one_subagent_per_role_and_persona(agents_dir):
    generated = claude_cockpit._generate_subagent_files(
        _executors(), str(REPO), "multi-model")
    expected = len(profiles.KNOWN_ROLES) + len(profiles.known_personas())
    assert len(generated) == expected


def _model_of(agents_dir, name):
    for line in (agents_dir / f"{name}.md").read_text().splitlines():
        if line.startswith("model:"):
            return line.split(":", 1)[1].strip()
    return None


def test_persona_inherits_its_base_role_model(agents_dir):
    claude_cockpit._generate_subagent_files(
        _executors({"implementer": {"model": "anthropic/claude-sonnet-5"}}),
        str(REPO), "multi-model")
    assert _model_of(agents_dir, "implementer-angular") == "anthropic/claude-sonnet-5"


def test_by_persona_overrides_the_role(agents_dir):
    claude_cockpit._generate_subagent_files(
        _executors({"implementer": {"model": "anthropic/claude-sonnet-5"}},
                   {"implementer-angular": {"model": "google/gemini-3.7-flash"}}),
        str(REPO), "multi-model")
    assert _model_of(agents_dir, "implementer-angular") == "google/gemini-3.7-flash"
    assert _model_of(agents_dir, "implementer-devops") == "anthropic/claude-sonnet-5"


def test_base_role_resolves_by_longest_prefix(agents_dir):
    """Both halves of `<role>-<domain>` contain hyphens in
    code-reviewer-api-platform, so it cannot be split on one."""
    claude_cockpit._generate_subagent_files(
        _executors({"code-reviewer": {"model": "anthropic/claude-opus-5"}}),
        str(REPO), "multi-model")
    assert _model_of(agents_dir, "code-reviewer-api-platform") == "anthropic/claude-opus-5"


def test_pruning_removes_tracked_agents_but_not_the_users(agents_dir):
    from ai_resources.setup import state
    tracking = state.InstallTracking()
    claude_cockpit._generate_subagent_files(_executors(), str(REPO), "multi-model", tracking)

    (agents_dir / "mine.md").write_text("---\nname: mine\n---\n")
    tracking.subagent_files_installed.append("retired-persona")
    (agents_dir / "retired-persona.md").write_text("---\nname: retired-persona\n---\n")

    before = {p.name for p in agents_dir.glob("*.md")}
    claude_cockpit._generate_subagent_files(_executors(), str(REPO), "multi-model", tracking)
    assert not (agents_dir / "retired-persona.md").exists()
    assert (agents_dir / "mine.md").exists()
    # The two witness files above both pass under a total wipe: the user's file
    # survives because it was never tracked, and the retired one is meant to go.
    # Assert the whole population instead — a second run with nothing changed
    # must leave every generated agent on disk, not delete the ones whose
    # content happened to be identical.
    assert {p.name for p in agents_dir.glob("*.md")} == before - {"retired-persona.md"}
    assert len(tracking.subagent_files_installed) > 10


def test_container_cli_drives_colima_with_docker_and_podman_with_itself():
    from ai_resources.setup import litellm as lite
    assert lite.container_cli("docker") == "docker"
    assert lite.container_cli("podman") == "podman"
    # Colima is a container runtime you drive with the docker CLI: it exposes
    # the docker socket and ships no `colima compose`.
    assert lite.container_cli("colima") == "docker"
    for rt in ("docker", "podman", "colima"):
        assert lite.is_container_mode(rt)
    for rt in ("pipx", "pip-venv", ""):
        assert not lite.is_container_mode(rt)


def test_service_control_works_for_every_container_runtime(monkeypatch):
    """Service control used to compare the runtime against the literal "docker".

    Podman and colima matched no branch, so start/stop/status/logs returned
    their empty defaults without running anything — while the login-time unit,
    which interpolates the runtime correctly, kept the gateway up. The gateway
    ran and the kit reported it absent.
    """
    from ai_resources.setup import litellm as lite

    for runtime, expected in (("docker", "docker"), ("podman", "podman"), ("colima", "docker")):
        calls = []

        def fake_run(cmd, *a, **kw):
            calls.append(cmd)
            return (0, "running", "")

        monkeypatch.setattr(lite, "runtime_mode", lambda r=runtime: r)
        monkeypatch.setattr(lite, "_run", fake_run)

        assert lite.start_service() is True, f"{runtime}: start did nothing"
        assert lite.stop_service() is True, f"{runtime}: stop did nothing"
        assert lite.service_status() == "running", f"{runtime}: status reported absent"
        assert lite.service_logs() == "running", f"{runtime}: logs came back empty"

        assert len(calls) == 4, f"{runtime}: expected 4 commands, got {calls}"
        assert all(c[0] == expected for c in calls), \
            f"{runtime}: should be driven by {expected}, got {[c[0] for c in calls]}"


def test_teardown_stops_the_container_for_every_container_runtime():
    """Teardown skipped colima, leaving the gateway running and the image pulled.

    `plan_multi_model_teardown` is pure, so the whole decision is checkable here:
    a container runtime must always produce the stop action, and a pip
    deployment must never produce it.
    """
    from ai_resources.setup import litellm as lite, state as st

    for runtime, should_stop in (("docker", True), ("podman", True),
                                 ("colima", True), ("pip-venv", False)):
        prev = st.SetupState()
        prev.litellm.local.runtime = runtime
        ids = [a[0] for a in lite.plan_multi_model_teardown(prev)]
        assert ("docker_down" in ids) is should_stop, \
            f"{runtime}: expected docker_down={should_stop}, plan was {ids}"


def test_an_explicitly_requested_profile_is_never_silently_substituted():
    """`--profile` used to be overwritten by the step-6 default without a word.

    A profile carried over in the saved state must still degrade quietly — that
    is what stops step 6 crashing on a state written under the other backend —
    so the two cases have to stay distinguishable.
    """
    from ai_resources.setup import wizard

    saved = wizard._REQUESTED_PROFILE
    try:
        # Nothing requested: the saved-state path, which must not be blocked.
        wizard._REQUESTED_PROFILE = ""
        assert wizard._reject_unavailable_requested_profile(["claude-native"], "x") is None

        # Requested and offered: proceed.
        wizard._REQUESTED_PROFILE = "measured-best"
        assert wizard._reject_unavailable_requested_profile(
            ["measured-best", "cost-optimized"], "x") is None

        # Requested and not offered: stop, rather than run under another profile.
        wizard._REQUESTED_PROFILE = "measured-best"
        assert wizard._reject_unavailable_requested_profile(
            ["claude-native"], "single-model mode") == 1
    finally:
        wizard._REQUESTED_PROFILE = saved
