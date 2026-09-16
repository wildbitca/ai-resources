"""The dry-run preview must build the same settings.json patch as the real run.

`_step9_dry_run` used to call `_build_settings_patch(...)` with five positional
arguments, so `backend` silently fell back to its "litellm" default. Under the
openrouter backend the preview then omitted ANTHROPIC_AUTH_TOKEN, the blanked
ANTHROPIC_API_KEY, and the four ANTHROPIC_DEFAULT_*_MODEL pins that the real
`configure()` call writes — the dry run showed a different patch than the one
actually applied.

No network, no credentials, no gateway. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import credentials, state, ui, wizard  # noqa: E402
from ai_resources.setup.cockpits import claude as claude_cockpit  # noqa: E402


class _StoppedAfterPatch(Exception):
    """Raised once the patch is captured, to skip the gateway/smoke-test tail."""


class _FakeConsole:
    """Stand-in for `ui.console()`, which hard-requires rich (absent in CI)."""

    def print(self, *_a, **_k):
        pass


@pytest.fixture
def dry_run_env(monkeypatch):
    """Capture the `backend` `_step9_dry_run` forwards to `_build_settings_patch`.

    Stops execution right after the patch is built, before the gateway/smoke-test
    tail that would otherwise touch the network.
    """
    captured = {}

    def fake_build_settings_patch(executors, master_key, gateway_url, ak_path, mode,
                                   backend="litellm"):
        captured["backend"] = backend
        raise _StoppedAfterPatch()

    monkeypatch.setattr(claude_cockpit, "_build_settings_patch", fake_build_settings_patch)
    monkeypatch.setattr(credentials, "get_key", lambda *_a, **_k: "fake-key")
    # ui.console() hard-raises without rich, which CI doesn't install; every other
    # ui.* helper already degrades to plain print() on its own.
    monkeypatch.setattr(ui, "console", lambda: _FakeConsole())
    return captured


@pytest.mark.parametrize("backend", ["litellm", "openrouter"])
def test_dry_run_forwards_the_configured_backend_to_settings_patch(dry_run_env, backend):
    """Whatever backend the real `configure()` call would receive, the dry-run
    preview must receive the same one — not the "litellm" default."""
    s = state.SetupState()
    s.mode = "multi-model"
    s.backend = backend

    with pytest.raises(_StoppedAfterPatch):
        wizard._step9_dry_run(s)

    assert dry_run_env["backend"] == backend
