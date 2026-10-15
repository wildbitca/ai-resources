"""On a re-run every question of the model picker offers the saved answer as its default."""
from __future__ import annotations

from ai_resources import selection as sel
from ai_resources.setup import model_selection as ms, ui, wizard


def _cands():
    mk = ms.Candidate
    return {"anthropic": [mk("anthropic", "sonnet", "claude-sonnet-5-5"), mk("anthropic", "opus", "claude-opus-5")],
            "openai": [mk("openai", "gpt", "gpt-5"), mk("openai", "mini", "gpt-5-mini")]}


def _saved():
    return sel.Selection(shape=ms.SHAPE_MANY_PROVIDERS, providers={"anthropic": sel.ProviderSel(), "openai": sel.ProviderSel()},
                         slots={"anthropic:opus": sel.SlotSel("anthropic/claude-opus-5"),
                                "openai:mini": sel.SlotSel("openai/gpt-5-mini")},
                         primary="openai:mini", smoke_path="direct")


def test_the_saved_models_are_the_defaults_of_every_checkbox(monkeypatch):
    seen = []
    monkeypatch.setattr(ui, "checkbox", lambda msg, choices, default=None, **k: seen.append((msg, default)) or default)
    out = wizard._pick_candidates(ms.SHAPE_MANY_PROVIDERS, _cands(), _saved())
    assert seen[0] == ("Providers:", ["anthropic", "openai"])
    assert [c.slot for c in seen[1][1]] == ["anthropic:opus"] and [c.slot for c in seen[2][1]] == ["openai:mini"]
    assert [c.slot for c in out] == ["anthropic:opus", "openai:mini"]


def test_without_a_saved_selection_the_first_stable_model_is_the_default(monkeypatch):
    seen = []
    monkeypatch.setattr(ui, "select", lambda msg, choices, default=None, **k: seen.append(default) or "anthropic")
    monkeypatch.setattr(ui, "checkbox", lambda msg, choices, default=None, **k: default)
    out = wizard._pick_candidates(ms.SHAPE_ONE_PROVIDER, _cands(), None)
    assert seen == [None] and [c.slot for c in out] == ["anthropic:sonnet"]


def test_one_provider_and_single_defaults_come_from_the_saved_selection(monkeypatch):
    seen = []
    monkeypatch.setattr(ui, "select", lambda msg, choices, default=None, **k: seen.append((msg, default)) or default)
    monkeypatch.setattr(ui, "checkbox", lambda msg, choices, default=None, **k: default)
    wizard._pick_candidates(ms.SHAPE_ONE_PROVIDER, _cands(), _saved())
    assert seen[-1] == ("Which provider?", "anthropic")
    wizard._pick_candidates(ms.SHAPE_SINGLE, _cands(), _saved())
    assert seen[-1][0] == "Which model?" and seen[-1][1].slot == "anthropic:opus"
