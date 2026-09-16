"""Provider registry — known providers with their auth methods, env vars, models."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Provider:
    id: str
    name: str
    auth_methods: list[str]                # api_key | vertex_adc | oauth
    primary_env_var: str
    extra_env_vars: list[str] = field(default_factory=list)
    install_hint: str = ""
    docs_url: str = ""
    description: str = ""


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(
        id="anthropic",
        name="Anthropic (direct)",
        auth_methods=["api_key"],
        primary_env_var="ANTHROPIC_API_KEY",
        docs_url="https://console.anthropic.com/",
        description="claude-* models via Anthropic Console",
    ),
    "google": Provider(
        id="google",
        name="Google AI Studio",
        auth_methods=["api_key"],
        primary_env_var="GEMINI_API_KEY",
        docs_url="https://aistudio.google.com/apikey",
        description="gemini-* models via AI Studio",
    ),
    "vertex": Provider(
        id="vertex",
        name="Vertex AI (GCP)",
        auth_methods=["vertex_adc"],
        primary_env_var="GOOGLE_CLOUD_PROJECT",
        extra_env_vars=["GOOGLE_CLOUD_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS"],
        docs_url="https://cloud.google.com/vertex-ai/generative-ai/docs/overview",
        description="Claude + Gemini via GCP — enterprise audit & compliance",
    ),
    "openai": Provider(
        id="openai",
        name="OpenAI",
        auth_methods=["api_key"],
        primary_env_var="OPENAI_API_KEY",
        docs_url="https://platform.openai.com/api-keys",
        description="gpt-* models",
    ),
    "ollama": Provider(
        id="ollama",
        name="Local Ollama",
        auth_methods=["none"],
        primary_env_var="",
        docs_url="https://ollama.com/",
        description="Local models on localhost:11434 (offline-capable)",
    ),
    "openrouter": Provider(
        id="openrouter",
        name="OpenRouter (hosted gateway)",
        auth_methods=["api_key"],
        primary_env_var="OPENROUTER_API_KEY",
        docs_url="https://openrouter.ai/keys",
        description="400+ models behind one key — no local gateway to run",
    ),
    "deepseek": Provider(
        id="deepseek",
        name="DeepSeek",
        auth_methods=["api_key"],
        primary_env_var="DEEPSEEK_API_KEY",
        docs_url="https://platform.deepseek.com/api_keys",
        description="deepseek-* — best measured value for architecture work",
    ),
    "moonshot": Provider(
        id="moonshot",
        name="Moonshot AI (Kimi)",
        auth_methods=["api_key"],
        primary_env_var="MOONSHOT_API_KEY",
        docs_url="https://platform.moonshot.ai/console/api-keys",
        description="kimi-* — cheap long-context coding models",
    ),
}


KNOWN_MODELS: dict[str, list[str]] = {
    # Refreshed 2026-09-16 — the wizard offers these but allows free input.
    # Direct-provider IDs are bare; only the openrouter entry is namespaced.
    "anthropic": [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-fable-5-1",
    ],
    "google": [
        # gemini-3.1-pro-preview is the only Pro tier currently published; the
        # rest are stable. Verified against ai.google.dev 2026-09-16.
        "gemini-3.1-pro-preview",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
    ],
    "vertex": [
        # Verified against platform.claude.com 2026-09-16. The current generation
        # uses a bare alias; Haiku 4.5 still carries its @date suffix.
        # Note: regional endpoints (us-east5 and friends) serve Sonnet 4.6 and
        # earlier only — the models below need the global or a multi-region
        # endpoint, which is why GOOGLE_CLOUD_LOCATION defaults to "global".
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5@20251001",
        "claude-fable-5-1",
        "gemini-3.1-pro-preview",
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
    ],
    "openai": [
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ],
    "ollama": [
        "qwen2.5-coder:32b",
        "deepseek-coder-v2:16b",
        "llama3.3:70b",
    ],
    # Bare IDs for direct access. The namespaced spellings under "openrouter"
    # below reach the same models through that gateway instead.
    "deepseek": [
        "deepseek-v4-pro",      # 1.60 / 3.20
        "deepseek-v4-flash",    # 0.087 / 0.174
    ],
    "moonshot": [
        "kimi-k2.7-code",       # 0.71 / 3.21
    ],
    # OpenRouter IDs are namespaced <vendor>/<model>. Every entry below was
    # round-tripped against OpenRouter's Anthropic surface on 2026-09-16.
    # Use explicit IDs, not aliases: an alias such as claude-opus-latest needs a
    # "[1m]" suffix for the 1M window to be detected.
    "openrouter": [
        # $/1M in-out, resolved against the live catalogue on 2026-09-16.
        "anthropic/claude-opus-5",        # 5 / 25
        "anthropic/claude-sonnet-5",      # 2 / 10
        "anthropic/claude-haiku-4.5",     # 1 / 5   — dotted, "4-5" does not exist
        "anthropic/claude-fable-5.1",     # 10 / 50
        "google/gemini-3.1-pro-preview",  # 2 / 12  — only the -preview id exists
        "google/gemini-3.7-flash",        # 0.75 / 3.75
        "google/gemini-3.5-flash-lite",   # 0.30 / 2.50
        "deepseek/deepseek-v4-pro",       # 1.60 / 3.20
        "deepseek/deepseek-v4-flash",     # 0.087 / 0.174
        "moonshotai/kimi-k2.7-code",      # 0.71 / 3.21
        "x-ai/grok-4.6",                  # 2 / 6
        "openai/gpt-5.6-terra",           # 2 / 12
    ],
}


def get(provider_id: str) -> Provider:
    return PROVIDERS[provider_id]


def all_ids() -> list[str]:
    return list(PROVIDERS.keys())


def models_for(provider_id: str) -> list[str]:
    return KNOWN_MODELS.get(provider_id, [])
