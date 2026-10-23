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

    def rule(self, *_a, **_k):
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


def test_dry_run_announces_that_verification_is_skipped_and_probes_nothing(dry_run_env, monkeypatch):
    """Step 10 in a dry run is a header and a notice: run_all is never called, so no live host is probed."""
    from ai_resources import verify

    calls = []
    monkeypatch.setattr(verify, "run_all", lambda *a, **k: calls.append(a) or [])
    sections, lines = [], []
    monkeypatch.setattr(ui, "section", lambda n, total, title: sections.append((n, total, title)))
    monkeypatch.setattr(ui, "info", lambda msg: lines.append(msg))
    monkeypatch.setattr(ui, "role_table", lambda *a, **k: None)
    monkeypatch.setattr(claude_cockpit, "_build_settings_patch", lambda *a, **k: {})
    monkeypatch.setattr(claude_cockpit, "_kit_hooks", lambda *a, **k: {}, raising=False)
    s = state.SetupState()
    s.mode = "single-model"
    try:
        wizard._step9_dry_run(s)
    except _StoppedAfterPatch:
        pytest.skip("the preview stops before its tail in this harness")
    assert (wizard._screen_of(10), wizard.SCREENS, "Verification") in sections
    assert any("verification" in m.lower() and "dry run" in m.lower() for m in lines)
    assert calls == []


# --- image generation under --dry-run (ADR-0011, plan I10, AC-9, AC-16) -------------------------------------------------------

import hashlib
import json as _json

from openclaw_image_fakes import FakeOC
from ai_resources.setup.cockpits import openclaw as _oc_cockpit
from ai_resources.setup.cockpits import _openclaw_image as _img


def _image_dry_run(monkeypatch, tmp_path, mode, model="", doc=None, **kw):
    cfg = tmp_path / "openclaw.json"
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg))
    fake = FakeOC(cfg, doc if doc is not None else {"tools": {"deny": ["bundle-mcp"]}})
    monkeypatch.setattr(_oc_cockpit, "_openclaw", fake)
    monkeypatch.setattr(_oc_cockpit, "_before_first_write", None)
    s = state.SetupState()
    s.mode = "single-model"
    s.profile.customizations["__targets__"] = {"selected": ["claude", "openclaw"]}
    s.openclaw.image_mode, s.openclaw.image_model = mode, model
    for k, v in kw.items():
        setattr(s.openclaw, k, v)
    shown = []
    for level in ("info", "warn", "error", "ok", "detail"):
        monkeypatch.setattr(ui, level, lambda m, *a, _l=level, **k: shown.append(f"{_l}: {m}"))
    monkeypatch.setattr(ui, "console", lambda: _FakeConsole())
    before = hashlib.sha256(cfg.read_bytes()).hexdigest()
    wizard._apply_image_dry_run(s)
    assert hashlib.sha256(cfg.read_bytes()).hexdigest() == before, "a dry run must not write the config"
    return fake, "\n".join(shown), s


def test_image_dry_run_disable_shows_the_merged_list_and_sends_only_dry_run_patches(monkeypatch, tmp_path):
    fake, text, s = _image_dry_run(monkeypatch, tmp_path, "disable")
    assert "tools.deny before: bundle-mcp" in text and "tools.deny after:  bundle-mcp, image_generate" in text
    assert "nothing written" in text
    assert fake.live_patches() == [] and fake.patches(dry=True)
    assert all(a[:2] == ["config", "patch"] and "--dry-run" in a for a in fake.argvs())
    assert s.openclaw.image_deny_added is False


def test_image_dry_run_route_shows_the_ref_and_the_native_flow_without_running_it(monkeypatch, tmp_path):
    monkeypatch.setattr(_oc_cockpit, "_image_accounts", lambda: ({}, []))
    fake, text, s = _image_dry_run(monkeypatch, tmp_path, "fal", "fal/krea/v2/medium/text-to-image", doc={})
    assert "agents.defaults.mediaModels.image to fal/krea/v2/medium/text-to-image" in text
    assert "not written: OpenClaw reports no usable fal sign-in; run `openclaw configure --section model` yourself" in text
    assert "paste-api-key" not in text
    assert fake.live_patches() == [] and s.openclaw.image_applied is False
    assert all("--dry-run" in a for a in fake.argvs())


def test_image_dry_run_keep_prints_one_line_and_runs_nothing(monkeypatch, tmp_path):
    fake, text, _s = _image_dry_run(monkeypatch, tmp_path, "keep")
    assert "image generation: keep (no change)" in text and fake.calls == []


def test_image_dry_run_with_a_seeded_secret_shows_no_secret_for_any_route(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-SECRET")
    monkeypatch.setattr(_oc_cockpit, "_image_accounts", lambda: ({}, ["OPENAI_API_KEY"]))
    for mode in ("disable", "openai-key", "fal", "openrouter"):
        model = (_img.ROUTES[mode].models or ("",))[0]
        fake, text, _s = _image_dry_run(monkeypatch, tmp_path / mode if False else tmp_path, mode, model)
        blob = text + _json.dumps(fake.calls)
        assert "sk-test-SECRET" not in blob and "SECRET" not in blob, mode


def test_image_dry_run_not_for_a_claude_only_run(monkeypatch, tmp_path):
    s = state.SetupState()
    s.mode = "single-model"
    s.profile.customizations["__targets__"] = {"selected": ["claude"]}
    s.openclaw.image_mode = "disable"
    monkeypatch.setattr(_oc_cockpit, "_openclaw", lambda *a, **k: pytest.fail("openclaw was called"))
    monkeypatch.setattr(ui, "console", lambda: _FakeConsole())
    wizard._apply_image_dry_run(s)
