"""Under OpenRouter, bare Anthropic model IDs must map to catalogue IDs.

Claude Code sends a `--model` value verbatim, and OpenRouter only accepts
namespaced IDs. OpenClaw's claude-cli runtime always launches Claude Code with
the bare ID (`--model claude-sonnet-5`), so without `modelOverrides` a chat bot
loses its main conversation as soon as setup switches to OpenRouter.

No network, no Claude Code install. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup.cockpits import claude  # noqa: E402

EXECUTORS = {"classes": {"sonnet": "anthropic/claude-sonnet-5"}}


def _patch(mode: str, backend: str) -> dict:
    return claude._build_settings_patch(EXECUTORS, "key", "https://gw", "/kit", mode, backend)


def test_openrouter_maps_bare_anthropic_ids_to_catalogue_ids():
    overrides = _patch("multi-model", "openrouter")["modelOverrides"]
    assert overrides["claude-sonnet-5"] == "anthropic/claude-sonnet-5"
    assert overrides["claude-opus-5"] == "anthropic/claude-opus-5"


def test_dotted_catalogue_versions_are_keyed_by_anthropics_dashed_ids():
    overrides = _patch("multi-model", "openrouter")["modelOverrides"]
    assert overrides["claude-haiku-4-5"] == "anthropic/claude-haiku-4.5"
    assert overrides["claude-fable-5-1"] == "anthropic/claude-fable-5.1"
    assert all("." not in key for key in overrides)


def test_other_routes_get_no_overrides():
    assert "modelOverrides" not in _patch("multi-model", "litellm")
    assert "modelOverrides" not in _patch("single-model", "openrouter")


def test_removal_keeps_the_users_own_overrides(tmp_path):
    settings = tmp_path / "settings.json"
    ours = claude._openrouter_model_overrides()
    settings.write_text(json.dumps({"modelOverrides": {
        **ours,
        "claude-opus-5": "my-own-opus-deployment",
    }}), encoding="utf-8")

    removed = claude._remove_kit_model_overrides(settings)

    left = json.loads(settings.read_text())["modelOverrides"]
    assert left == {"claude-opus-5": "my-own-opus-deployment"}
    assert "claude-opus-5" not in removed and "claude-sonnet-5" in removed


def test_removal_drops_an_emptied_block(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {}, "modelOverrides": claude._openrouter_model_overrides()}),
                        encoding="utf-8")
    claude._remove_kit_model_overrides(settings)
    assert "modelOverrides" not in json.loads(settings.read_text())
