"""~/.aider.conf.yml: the gateway's OpenAI-compatible base, written only where the matrix wires Aider."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from ai_resources.setup import profiles, state
from ai_resources.setup.cockpits import _shared, aider

EXECUTORS = {"by_role": {"implementer": {"model": "anthropic/claude-sonnet-5"}}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(aider, "CONFIG_ROOT", tmp_path / ".aider")
    monkeypatch.setattr(aider, "CONVENTIONS_PATH", tmp_path / ".aider" / "CONVENTIONS.md")
    monkeypatch.setattr(aider, "CONF_PATH", tmp_path / ".aider.conf.yml")
    monkeypatch.setattr(_shared, "write_managed_block", lambda p, b, **k: True)
    return tmp_path


def _configure(mode="multi-model", backend="litellm", **ctx):
    s = state.SetupState()
    s.mode, s.backend = mode, backend
    return aider.configure({"state": s, "executors": EXECUTORS, "master_key": "k", **ctx})


def conf(home):
    return yaml.safe_load((home / ".aider.conf.yml").read_text())


def test_openrouter_uses_the_openai_compatible_base_with_api_v1(home):
    _configure(backend="openrouter", gateway_url="https://openrouter.ai/api",
               openai_base=profiles.OPENAI_BASE["openrouter"])
    assert conf(home)["openai-api-base"] == "https://openrouter.ai/api/v1"


def test_litellm_keeps_the_gateway_url_as_the_base(home):
    _configure(gateway_url="http://127.0.0.1:4000")
    assert conf(home)["openai-api-base"] == "http://127.0.0.1:4000"


def test_the_kit_never_embeds_the_key_value_beyond_what_it_always_wrote(home):
    _configure(gateway_url="http://127.0.0.1:4000")
    assert conf(home)["openai-api-key"] == "k"          # unchanged behaviour: the gateway master key, as before


def test_single_model_writes_no_conf(home):
    _configure(mode="single-model", gateway_url="http://127.0.0.1:4000")
    assert not (home / ".aider.conf.yml").exists()


def test_a_skip_route_writes_no_conf_and_says_why(home):
    route = SimpleNamespace(action="skip", reason="OpenRouter does not serve vertex models")
    _configure(gateway_url="http://127.0.0.1:4000", route=route)
    assert not (home / ".aider.conf.yml").exists()


def test_a_changed_base_keeps_the_old_file_next_to_it(home):
    (home / ".aider.conf.yml").write_text("openai-api-base: https://openrouter.ai/api\n")
    _configure(backend="openrouter", gateway_url="https://openrouter.ai/api", openai_base=profiles.OPENAI_BASE["openrouter"])
    assert (home / ".aider.conf.yml.kit-bak").read_text() == "openai-api-base: https://openrouter.ai/api\n"
    assert conf(home)["openai-api-base"] == "https://openrouter.ai/api/v1"


def test_the_matrix_wires_aider_under_openrouter_with_the_documented_base():
    from ai_resources.setup import compat
    c = compat.cell("aider", "google", compat.MULTI_OPENROUTER)
    assert c.action == compat.VIA_GATEWAY and c.verified and "api/v1" in c.source
    assert compat.cell("aider", "vertex", compat.MULTI_OPENROUTER).action == compat.SKIP


def test_the_executors_gateway_block_is_unchanged_for_existing_setups():
    assert profiles.GATEWAYS["openrouter"]["url"] == "https://openrouter.ai/api"
    assert "openai_base" not in profiles.GATEWAYS["openrouter"]
