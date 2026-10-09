"""Generated instruction text never claims gateway routing for a cockpit that is not wired to one."""
from __future__ import annotations

import pytest

from ai_resources.setup import state
from ai_resources.setup.cockpits import ALL, _shared, claude

I_COCKPITS = ["gemini", "cursor", "codex", "copilot", "windsurf", "continue", "opencode"]


@pytest.fixture
def written(monkeypatch):
    out: dict[str, str] = {}
    monkeypatch.setattr(_shared, "write_managed_block", lambda p, body, **k: out.__setitem__(str(p), body) or True)
    monkeypatch.setattr(_shared, "write_text", lambda p, c, **k: out.__setitem__(str(p), c) or True)
    monkeypatch.setattr(_shared, "deep_merge_json", lambda p, patch, **k: True)
    monkeypatch.setattr(_shared, "link_agents_skills", lambda *a, **k: None)
    return out


def _configure(cid: str, mode: str, backend: str = "litellm", master_key: str = "k") -> None:
    s = state.SetupState()
    s.mode, s.backend = mode, backend
    ALL[cid].configure({"state": s, "gateway_url": "http://127.0.0.1:4000", "master_key": master_key,
                        "executors": {"by_role": {}}})


@pytest.mark.parametrize("cid", I_COCKPITS)
def test_instruction_only_cockpits_make_no_gateway_claim_in_multi_model(cid, written):
    _configure(cid, "multi-model")
    body = "\n".join(written.values())
    assert "go through" not in body
    assert "uses its own model settings; ai-resources does not choose" in body
    assert "Anthropic does not support" not in body


def test_aider_in_multi_model_says_calls_go_through_the_gateway(written):
    _configure("aider", "multi-model")
    assert any("Model calls go through LiteLLM gateway" in v for v in written.values())


def test_claude_block_in_multi_model_says_calls_go_through_the_gateway():
    md = claude._claude_md("/kit", "http://127.0.0.1:4000", "multi-model")
    assert "Model calls go through LiteLLM gateway" in md
    assert "Anthropic does not support" not in md


@pytest.mark.parametrize("cid", I_COCKPITS + ["aider"])
def test_single_model_text_has_no_multi_model_section(cid, written):
    _configure(cid, "single-model", master_key="")
    assert all("Multi-model routing" not in v for v in written.values())


def test_single_model_protocol_is_empty():
    assert _shared.multimodel_protocol_md("/kit", "http://gw", "single-model") == ""
    assert _shared.multimodel_protocol_md("/kit", "http://gw", "single-model", "instructions_only") == ""


def test_heading_stays_known_to_the_orphan_recovery():
    assert "Multi-model routing" in _shared.CURRENT_KIT_HEADINGS
