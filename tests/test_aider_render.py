"""~/.aider.conf.yml: the gateway's OpenAI-compatible base, written only where the matrix wires Aider."""
from __future__ import annotations

from types import SimpleNamespace

import os
import stat

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


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.mark.parametrize("src_mode", [0o600, 0o644, 0o666])
def test_the_backup_of_the_key_file_is_never_group_or_other_readable(home, src_mode):
    conf_path = home / ".aider.conf.yml"
    conf_path.write_text("openai-api-key: secret\nopenai-api-base: https://openrouter.ai/api\n")
    os.chmod(conf_path, src_mode)
    _configure(backend="openrouter", gateway_url="https://openrouter.ai/api", openai_base=profiles.OPENAI_BASE["openrouter"])
    bak = home / ".aider.conf.yml.kit-bak"
    assert "secret" in bak.read_text()
    assert _mode(bak) == 0o600
    assert _mode(bak) & 0o077 == 0


def test_a_stale_wider_backup_is_tightened_not_reused(home):
    bak = home / ".aider.conf.yml.kit-bak"
    bak.write_text("old")
    os.chmod(bak, 0o644)
    (home / ".aider.conf.yml").write_text("openai-api-base: x\n")
    _configure(backend="openrouter", gateway_url="https://openrouter.ai/api", openai_base=profiles.OPENAI_BASE["openrouter"])
    assert _mode(bak) == 0o600


def test_a_new_conf_with_the_key_is_created_0600(home):
    _configure(backend="litellm", gateway_url="http://127.0.0.1:4000")
    path = home / ".aider.conf.yml"
    assert "openai-api-key" in path.read_text()
    assert _mode(path) == 0o600


@pytest.mark.parametrize("old_mode", [0o644, 0o666])
def test_an_existing_conf_is_tightened_to_0600_when_rewritten(home, old_mode):
    path = home / ".aider.conf.yml"
    path.write_text("openai-api-base: http://old\n")
    os.chmod(path, old_mode)
    _configure(backend="litellm", gateway_url="http://127.0.0.1:4000")
    assert "openai-api-key" in path.read_text()
    assert _mode(path) == 0o600


def test_an_unchanged_wider_conf_is_still_tightened(home):
    _configure(backend="litellm", gateway_url="http://127.0.0.1:4000")
    path = home / ".aider.conf.yml"
    os.chmod(path, 0o644)
    _configure(backend="litellm", gateway_url="http://127.0.0.1:4000")
    assert _mode(path) == 0o600
