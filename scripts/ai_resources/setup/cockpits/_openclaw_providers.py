"""The OpenClaw provider catalog and each provider's auth options (ADR-0015 D2).

The providers come from OpenClaw itself, read-only:

* `openclaw models list --all --json`  -> the catalog (`models[].key` is `<provider>/<model>`),
* `openclaw models auth list --json`   -> the providers that already hold a saved sign-in (ids only; no key field is read).

Everything is tolerant: a missing CLI, a non-zero exit, text around the JSON or a shape OpenClaw changed never raises. The
fallbacks, in order, are the provider documentation shipped inside the OpenClaw package (`docs/providers/*.md`) and then the
providers the kit knows. Speech, image, video and music providers are dropped: they are not chat providers.

No key, token or secret is read, returned or logged here. Pure functions plus one injected `runner(argv, timeout=)`.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

# The eight providers the kit ships routing for (profiles and the compat matrix): the last-resort list.
KIT_KNOWN: tuple[str, ...] = ("anthropic", "openai", "google", "google-vertex", "openrouter", "deepseek", "mistral",
                              "xai")

# Providers OpenClaw lists for speech, image, video and music. They never run a chat turn.
NON_CHAT: frozenset[str] = frozenset({
    "azure-speech", "comfy", "deepgram", "elevenlabs", "fal", "fish-audio", "gradium", "inworld", "kie", "pixverse",
    "runway", "senseaudio", "vydra", "assemblyai", "stability", "ideogram", "recraft", "midjourney", "luma", "pika",
    "suno", "udio", "openai-image", "openai-tts",
})

# Runtime ids that appear in the catalog as a "provider" but are CLI backends of another provider.
BACKEND_RUNTIMES: dict[str, str] = {
    "claude-cli": "anthropic",
    "codex-cli": "openai",
    "codex": "openai",
    "agy-cli": "google",
    "agy-cli-lane": "google",
    "google-gemini-cli": "google",
}

# provider -> (OpenClaw runtime id, tool that must be installed, label)
CLI_BACKENDS: dict[str, tuple[str, str, str]] = {
    "anthropic": ("claude-cli", "claude", "Claude Code CLI backend (your subscription, no key)"),
    "openai": ("codex", "codex", "Codex CLI backend (your subscription, no key)"),
    "google": ("agy-cli", "agy", "Antigravity CLI backend (Google terms acknowledgement required)"),
}

Runner = Callable[..., "tuple[int, str]"]


@dataclass(frozen=True)
class Discovery:
    """Result of reading the catalog. `source` is where the provider list came from."""
    providers: tuple[str, ...]
    source: str                                   # cli | docs | fallback
    models: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    backends: frozenset[str] = frozenset()        # providers that expose a CLI runtime in the catalog
    problems: tuple[str, ...] = ()


def _json_from(text: str) -> Any:
    """The first JSON document in `text` (OpenClaw prints a warning line before it), or None."""
    if not isinstance(text, str):
        return None
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return None
    decoder = json.JSONDecoder()
    try:
        return decoder.raw_decode(text[min(starts):])[0]
    except ValueError:
        return None


def parse_models_list(text: str) -> tuple[dict[str, list[str]], frozenset[str]]:
    """({provider: [model ids]}, providers that are CLI backends) from `models list --all --json`. Never raises."""
    doc = _json_from(text)
    rows = doc.get("models") if isinstance(doc, dict) else doc
    models: dict[str, list[str]] = {}
    backends: set[str] = set()
    if not isinstance(rows, list):
        return models, frozenset()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("key") if isinstance(row.get("key"), str) else ""
        provider, _, model = key.partition("/")
        if not provider:
            provider = str(row.get("provider") or "")
            model = str(row.get("id") or row.get("name") or "")
        if not provider or not provider.replace("-", "").replace("_", "").replace(".", "").isalnum():
            continue
        if provider in BACKEND_RUNTIMES:
            backends.add(BACKEND_RUNTIMES[provider])
            continue
        models.setdefault(provider, [])
        if model and model not in models[provider]:
            models[provider].append(model)
    return models, frozenset(backends)


def docs_providers_dir(binary: str | None = None) -> Path | None:
    """`docs/providers` of the installed OpenClaw package, found from the binary's real path (None when absent)."""
    binary = binary if binary is not None else shutil.which("openclaw")
    if not binary:
        return None
    here = Path(os.path.realpath(binary)).parent
    for parent in (here, *here.parents):
        cand = parent / "docs" / "providers"
        if cand.is_dir():
            return cand
        if parent == parent.parent:
            break
    return None


def _from_docs(directory: Path | None) -> list[str]:
    if directory is None:
        return []
    try:
        names = sorted(p.stem for p in directory.glob("*.md"))
    except OSError:
        return []
    return [n for n in names if n not in ("index", "models")]


def _chat_only(names: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for n in names:
        if n and n not in NON_CHAT and n not in BACKEND_RUNTIMES and n not in seen:
            seen.append(n)
    return seen


def discover_providers(runner: Runner, *, docs_dir: Path | None = None, docs_lookup: bool = True) -> Discovery:
    """Chat providers from OpenClaw: the catalog, else the shipped docs, else the kit's own list."""
    problems: list[str] = []
    try:
        rc, out = runner(["models", "list", "--all", "--json"], timeout=60)
    except Exception as e:  # noqa: BLE001 - a broken runner must not take setup down
        rc, out = 1, str(type(e).__name__)
    if rc == 0:
        models, backends = parse_models_list(out)
        names = _chat_only(models)
        if names:
            return Discovery(tuple(names), "cli", {k: tuple(models[k]) for k in names}, backends)
        problems.append("the model catalog listed no chat provider")
    else:
        problems.append("`openclaw models list` is not available")
    if docs_dir is None and docs_lookup:
        docs_dir = docs_providers_dir()
    names = _chat_only(_from_docs(docs_dir))
    if names:
        return Discovery(tuple(names), "docs", {}, frozenset(), tuple(problems))
    return Discovery(KIT_KNOWN, "fallback", {}, frozenset(), tuple(problems))


def authenticated_providers(runner: Runner) -> frozenset[str]:
    """Providers that hold a saved sign-in, by id only. Empty on any failure."""
    try:
        rc, out = runner(["models", "auth", "list", "--json"], timeout=60)
    except Exception:  # noqa: BLE001
        return frozenset()
    if rc != 0:
        return frozenset()
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            prov = node.get("provider")
            if isinstance(prov, str) and prov and isinstance(node.get("id"), str):
                found.add(BACKEND_RUNTIMES.get(prov, prov))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(_json_from(out))
    return frozenset(found)


def profile_providers(profile: Mapping[str, Any] | None) -> list[str]:
    """Providers a kit profile routes roles to (the prefix of each `by_role` model), in order of first use."""
    out: list[str] = []
    for spec in ((profile or {}).get("by_role") or {}).values():
        model = spec.get("model") if isinstance(spec, dict) else spec
        if not isinstance(model, str) or not model:
            continue
        # A single-model profile names Claude aliases and bare ids (`sonnet`, `claude-opus-5-5`); a gateway one `p/m`.
        if "/" in model:
            prov = model.split("/", 1)[0]
        elif model in ("haiku", "sonnet", "opus", "fable", "inherit") or model.startswith("claude"):
            prov = "anthropic"
        elif model.startswith("gpt"):
            prov = "openai"
        elif model.startswith("gemini"):
            prov = "google"
        else:
            continue
        if prov and prov not in out:
            out.append(prov)
    return out


def preselected(discovery: Discovery, authenticated: Iterable[str], profile: Mapping[str, Any] | None = None) -> list[str]:
    """The providers ticked when the multiselect opens: the authenticated ones, then those the profile uses."""
    wanted = set(authenticated) | set(profile_providers(profile))
    return [p for p in discovery.providers if p in wanted]


def auth_options(provider: str, *, installed: Mapping[str, bool] | None = None,
                 backends: Iterable[str] = ()) -> list[tuple[str, str]]:
    """[(method, label)] for a provider. `api_key` is always there (OpenClaw's own store, entered on the operator's terminal).
    A CLI backend is offered only where the kit has an engine for it AND the tool is installed or the catalog lists it."""
    options = [("api_key", "An API key (saved by OpenClaw in its own store; the kit never sees it)")]
    spec = CLI_BACKENDS.get(provider)
    if spec:
        _method, tool, label = spec
        if (installed or {}).get(tool) or provider in set(backends):
            options.append(("cli", label))
    return options


def auth_command(provider: str) -> list[str]:
    """argv that stores an API key in OpenClaw's own store. It runs on the operator's terminal: the kit passes no secret."""
    return ["models", "auth", "paste-api-key", "--provider", provider]


# --- routing restricted to the enabled providers (ADR-0015 S7) ----------------------------------------------------------

# The first segment of an OpenClaw model ref -> the provider it bills. A runtime prefix is the CLI backend of its provider.
REF_PROVIDER: dict[str, str] = {
    "anthropic": "anthropic", "claude-cli": "anthropic", "claude-kit": "anthropic",
    "openai": "openai", "codex": "openai", "codex-cli": "openai",
    "google": "google", "google-vertex": "google-vertex", "agy-cli": "google", "agy-cli-lane": "google",
    "google-gemini-cli": "google",
}
# The provider each kit engine runs on: the engine's own provider is always enabled with its CLI backend.
ENGINE_PROVIDER: dict[str, str] = {"antigravity": "google", "claude-code": "anthropic", "codex": "openai"}

_CHEAP_HINTS = ("haiku", "mini", "flash", "lite", "small", "nano", "low")


def provider_of_ref(ref: str) -> str:
    """The provider a model ref bills (`agy-cli/gemini-x` -> google), or the first segment when unknown."""
    head = str(ref).split("/", 1)[0]
    return REF_PROVIDER.get(head, head)


def restrict_refs(refs: Iterable[str], enabled: Iterable[str] | None) -> list[str]:
    """Keep the refs whose provider is enabled. `enabled=None` means no restriction (the sign-in was never asked)."""
    if enabled is None:
        return list(refs)
    allowed = set(enabled)
    return [r for r in refs if provider_of_ref(r) in allowed]


def cheapest_model(provider: str, models: Iterable[str]) -> str:
    """The cheapest-looking model id of a provider: one named like a small tier, else the last listed."""
    listed = [m for m in models if m]
    for hint in _CHEAP_HINTS:
        for m in listed:
            if hint in m.lower():
                return m
    return listed[-1] if listed else ""


def role_provider(model: Any) -> str:
    """The provider of a profile role's model (an alias, a bare id or `provider/model`); '' when unknown."""
    if isinstance(model, dict):
        model = model.get("model")
    if not isinstance(model, str) or not model:
        return ""
    if "/" in model:
        return provider_of_ref(model)
    if model in ("haiku", "sonnet", "opus", "fable", "inherit") or model.startswith("claude"):
        return "anthropic"
    if model.startswith("gpt"):
        return "openai"
    if model.startswith("gemini"):
        return "google"
    return ""


def route_roles(by_role: Mapping[str, Any], enabled: Iterable[str] | None,
                catalog: Mapping[str, Iterable[str]] | None = None) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """({role: model ref}, [(role, wanted provider, fallback ref)]) restricted to the enabled providers.

    A role whose model's provider is enabled keeps it (as `provider/model` when the profile gave a bare id). A role whose
    provider is NOT enabled falls back to the cheapest model of the first enabled provider; those roles come back in the
    second list so the review can name them. `enabled=None` restricts nothing."""
    allowed = list(enabled) if enabled is not None else None
    catalog = catalog or {}
    routed: dict[str, str] = {}
    fell_back: list[tuple[str, str, str]] = []
    for role, spec in by_role.items():
        model = spec.get("model") if isinstance(spec, dict) else spec
        if not isinstance(model, str) or not model:
            continue
        want = role_provider(model)
        ref = model if "/" in model else f"{want}/{model}" if want else model
        if allowed is None or not want or want in allowed:
            routed[role] = ref
            continue
        for alt in allowed:
            cheap = cheapest_model(alt, catalog.get(alt, ()))
            if cheap:
                routed[role] = f"{alt}/{cheap}"
                fell_back.append((role, want, routed[role]))
                break
        else:
            routed[role] = ref                       # nothing to fall back on: keep it and let the review say so
            fell_back.append((role, want, ref))
    return routed, fell_back
