"""The openai KNOWN_MODELS list carries the GPT-6 ids that the pricing and Codex docs list as current.

Evidence: .agent-output/research-models/usage-openai-and-cross-benchmarks.md:165 (2026-10-13).
"""
from ai_resources.setup import providers


def test_openai_known_models_include_the_current_gpt6_ids():
    models = providers.models_for("openai")
    for model_id in ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"):
        assert model_id in models, model_id


def test_openai_known_models_keep_the_existing_ids():
    models = providers.models_for("openai")
    for model_id in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        assert model_id in models, model_id
    assert len(models) == len(set(models))
