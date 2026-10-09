"""The cockpit x provider x mode matrix (setup/compat.py) is complete, cited and truthful."""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from ai_resources.setup import compat, state
from ai_resources.setup.cockpits import ALL as COCKPITS_ALL
from ai_resources.setup.providers import PROVIDERS

DETECTED = list(COCKPITS_ALL)
MODE_ARGS = {
    compat.SINGLE: ("single-model", None),
    compat.MULTI_LITELLM: ("multi-model", "litellm"),
    compat.MULTI_OPENROUTER: ("multi-model", "openrouter"),
}


def _selection(*keys):
    return SimpleNamespace(slots={k: {"ref": k.replace(":", "/")} for k in keys})


def test_columns_are_the_provider_registry_without_the_gateway_backend():
    assert set(compat.PROVIDER_COLUMNS) == set(PROVIDERS) - {"openrouter"}


def test_every_cockpit_provider_mode_has_a_cited_cell():
    assert set(compat.COCKPITS) == set(COCKPITS_ALL)
    for cid in list(compat.COCKPITS) + list(compat.REPORT_ONLY):
        for provider in compat.PROVIDER_COLUMNS:
            for mk in compat.MODES:
                c = compat.CELLS[(cid, provider, mk)]
                assert c.source.strip(), (cid, provider, mk)
                assert c.action in compat.ACTIONS


def test_agy_and_claude_desktop_are_report_only_rows():
    for cid in ("agy", "claude-desktop"):
        for provider in compat.PROVIDER_COLUMNS:
            c = compat.CELLS[(cid, provider, compat.SINGLE)]
            assert c.report_only and c.action == compat.SKIP and c.reason
    out = compat.plan(None, "single-model", None, ["agy", "claude-desktop"])
    assert [a.cockpit for a in out] == ["agy", "claude-desktop"]
    assert all(a.report_only and not a.kit_content for a in out)


@pytest.mark.parametrize("cid", compat.COCKPITS)
@pytest.mark.parametrize("mk", compat.MODES)
def test_unverified_cells_behave_as_skip_unless_opted_in(cid, mk):
    mode, backend = MODE_ARGS[mk]
    for provider in compat.PROVIDER_COLUMNS:
        c = compat.CELLS[(cid, provider, mk)]
        if c.verified or c.action == compat.SKIP:
            continue
        sel = _selection(f"{provider}:model")
        [act] = compat.plan(sel, mode, backend, [cid])
        assert act.action == compat.SKIP and "not verified yet" in act.reason
        [opt] = compat.plan(sel, mode, backend, [cid], allow_unverified=True)
        assert opt.action == c.action and opt.unverified


# AC-S1b-3: a snapshot of what today's wizard configures when no selection exists. Only claude, aider and
# openclaw have a model setting to write (aider.py:79-82 writes ~/.aider.conf.yml only in multi-model through
# the gateway; claude.py configures directly in single-model and through the gateway otherwise); every other
# cockpit gets kit instructions only.
_INSTRUCTIONS_ONLY = {c: compat.INSTRUCTIONS_ONLY for c in
                      ("gemini", "cursor", "codex", "copilot", "windsurf", "continue", "opencode")}
IMPLICIT_ACTIONS = {
    compat.SINGLE: {"claude": compat.CONFIGURE, "aider": compat.SKIP, "openclaw": compat.CONFIGURE,
                    **_INSTRUCTIONS_ONLY},
    compat.MULTI_LITELLM: {"claude": compat.VIA_GATEWAY, "aider": compat.VIA_GATEWAY, "openclaw": compat.CONFIGURE,
                           **_INSTRUCTIONS_ONLY},
    compat.MULTI_OPENROUTER: {"claude": compat.VIA_GATEWAY, "aider": compat.VIA_GATEWAY, "openclaw": compat.CONFIGURE,
                              **_INSTRUCTIONS_ONLY},
}


def test_implicit_selection_reproduces_todays_configure_set_for_every_cockpit_in_every_mode():
    assert set(IMPLICIT_ACTIONS) == set(MODE_ARGS)
    for mk, (mode, backend) in MODE_ARGS.items():
        acts = compat.plan(None, mode, backend, DETECTED)
        assert [a.cockpit for a in acts] == list(compat.COCKPITS)
        assert all(a.kit_content for a in acts), mk
        assert {a.cockpit: a.action for a in acts} == IMPLICIT_ACTIONS[mk], mk
        assert not any(a.unverified or a.report_only for a in acts), mk


def test_single_gemini_flash_example():
    sel = _selection("google:gemini-flash")
    acts = {a.cockpit: a for a in compat.plan(sel, "single-model", None,
                                                ["claude", "gemini", "cursor", "codex", "copilot",
                                                 "windsurf", "continue", "opencode", "aider", "openclaw"])}
    assert acts["claude"].action == compat.SKIP
    assert "runs only Claude models without a gateway" in acts["claude"].reason
    assert acts["aider"].action == compat.SKIP
    assert acts["gemini"].action == compat.CONFIGURE
    for cid in ("codex", "cursor", "copilot", "windsurf", "continue", "opencode"):
        assert acts[cid].action == compat.INSTRUCTIONS_ONLY, cid
    assert acts["openclaw"].action == compat.SKIP
    assert "no verified OpenClaw runtime" in acts["openclaw"].reason


def test_gateway_and_antigravity_cells_name_their_prerequisites():
    for (cid, provider, mk), c in compat.CELLS.items():
        if c.action == compat.VIA_GATEWAY:
            assert c.prerequisite, (cid, provider, mk)
    for provider in ("anthropic", "google", "openai"):
        c = compat.ENGINE_CELLS[("antigravity", provider)]
        assert "agy" in c.prerequisite and "risk" in c.prerequisite


def test_engines_follow_the_picked_providers():
    assert compat.engines_for(_selection("anthropic:opus")) == ["claude-code", "antigravity"]
    assert "codex" in compat.engines_for(_selection("openai:gpt"))
    # a native Google engine is not offered until it is verified
    assert "native" not in compat.engines_for(_selection("google:gemini-flash"))
    assert "native" in compat.engines_for(_selection("google:gemini-flash"), allow_unverified=True)
    assert "antigravity" not in compat.engines_for(_selection("anthropic:opus"), detected=["claude"])


def test_needs_backend_only_when_a_gateway_cockpit_is_kept():
    assert not compat.needs_backend(_selection("google:gemini-flash"), ["gemini", "cursor"])
    assert compat.needs_backend(_selection("google:gemini-flash"), ["claude"])
    assert compat.needs_backend(_selection("anthropic:opus"), ["aider"])
    assert not compat.needs_backend(_selection("anthropic:opus"), ["claude"])


def test_markdown_table_lists_every_cockpit_and_engine():
    md = compat.markdown_table()
    for cid in list(compat.COCKPITS) + list(compat.REPORT_ONLY) + list(compat.ENGINES):
        assert f"| {cid} |" in md
    assert md.endswith("\n")


# --- AC-S1b-5: the matrix against the real configure() output -----------------------------------
FORBIDDEN = ("ANTHROPIC_BASE_URL", "openai-api-base", "ANTHROPIC_DEFAULT_", "modelOverrides",
             "OPENAI_API_BASE", "GOOGLE_GEMINI_BASE_URL")


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Capture everything a cockpit would write, without touching HOME."""
    from ai_resources.setup.cockpits import _shared
    writes: list[tuple[str, str]] = []

    def w_text(path, content, **kw):
        writes.append((str(path), content))
        return True

    def w_block(path, body, **kw):
        writes.append((str(path), body))
        return True

    def merge(path, patch, **kw):
        writes.append((str(path), json.dumps(patch)))
        return True

    monkeypatch.setattr(_shared, "write_text", w_text)
    monkeypatch.setattr(_shared, "write_managed_block", w_block)
    monkeypatch.setattr(_shared, "deep_merge_json", merge)
    monkeypatch.setattr(_shared, "link_agents_skills", lambda *a, **k: None)
    return writes


@pytest.mark.parametrize("cid", [c for c in compat.COCKPITS if c not in ("claude", "aider", "openclaw")])
@pytest.mark.parametrize("mk", compat.MODES)
def test_instruction_only_cockpits_write_no_model_or_endpoint_key(cid, mk, captured):
    mode, backend = MODE_ARGS[mk]
    s = state.SetupState()
    s.mode, s.backend = mode, backend or "litellm"
    COCKPITS_ALL[cid].configure({"state": s, "gateway_url": "http://gw.invalid", "master_key": "k",
                                 "executors": {"by_role": {}}})
    assert captured, cid
    for path, content in captured:
        if path.endswith(".json"):
            keys = json.loads(content)
            assert "model" not in keys, (cid, path)
        for bad in FORBIDDEN:
            assert bad not in content, (cid, path, bad)


def test_claude_single_model_writes_no_gateway_endpoint():
    from ai_resources.setup.cockpits import claude
    patch = claude._build_settings_patch({}, "k", "http://gw.invalid", "/kit", "single-model", "litellm")
    assert "ANTHROPIC_BASE_URL" not in patch["env"]
    assert "modelOverrides" not in patch
