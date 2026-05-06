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
}


KNOWN_MODELS: dict[str, list[str]] = {
    # Latest as of 2026-Q2 — wizard offers these but allows free input
    "anthropic": [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ],
    "google": [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ],
    "vertex": [
        "claude-opus-4-7@20260101",
        "claude-sonnet-4-6@20260101",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ],
    "openai": [
        "gpt-5",
        "gpt-5-mini",
        "gpt-4o",
    ],
    "ollama": [
        "qwen2.5-coder:32b",
        "deepseek-coder-v2:16b",
        "llama3.3:70b",
    ],
}


def get(provider_id: str) -> Provider:
    return PROVIDERS[provider_id]


def all_ids() -> list[str]:
    return list(PROVIDERS.keys())


def models_for(provider_id: str) -> list[str]:
    return KNOWN_MODELS.get(provider_id, [])
