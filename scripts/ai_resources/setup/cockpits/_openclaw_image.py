"""OpenClaw image generation: the route catalog and the patch builders (ADR-0011, plan I5). Pure: no I/O, no prompt.

OpenClaw has no "off" for image generation: with `agents.defaults.mediaModels.image` unset it auto-detects any provider
that has credentials, and `image_generate` appears as soon as one does. Setup therefore offers three kinds of answer:

    keep      change nothing (the default; never described as advice)
    disable   deny the `image_generate` tool through tools.deny (ADR-0011; written by `cockpits/openclaw.py`)
    <route>   write `agents.defaults.mediaModels.image` for one documented provider

The catalog recommends nothing. Every route carries a plain billing label and a proof label: "pay per use" is printed only
for a route whose installed OpenClaw docs say so (`billing_source` names the file and line); every other route reads
"billing per provider, not verified". Every route's generation is "docs only, not tested on this host".

The kit never holds an image credential. Signing in goes through `openclaw models auth login --provider <id>` or
`openclaw models auth paste-api-key --provider <p>` on the operator's terminal (plan I9); this module only names the
provider ids and the env var NAMES a route uses.

Facts read from OpenClaw 2026.10.5 docs (docs/tools/image-generation.md:84-124, docs/cli/models.md:392,
docs/providers/*.md). The ids below are documented, not run on this host.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

TIMEOUT_MS = 180000                               # docs/tools/image-generation.md:269-277
MEDIA_PATH = "agents.defaults.mediaModels.image"
REPLACE_PATHS = [MEDIA_PATH]
PROOF_DOCS = "docs only, not tested on this host"
BILLING_UNVERIFIED = "billing per provider, not verified"

KEEP_ID, DISABLE_ID = "keep", "disable"


@dataclass(frozen=True)
class Route:
    """One answer of the image question. `kind` is `keep`, `disable` or `route`. No field says "recommended"."""
    id: str
    label: str
    kind: str = "route"
    provider: str = ""                            # the model-ref prefix: `openai/gpt-image-2` -> `openai`
    auth: tuple[str, ...] = ()                    # oauth | api-key
    models: tuple[str, ...] = ()
    env: tuple[str, ...] = ()                     # env var NAMES the gateway reads (never values)
    profile_prefixes: tuple[str, ...] = ()        # auth profile id prefixes, e.g. `openai:`
    billing_label: str = BILLING_UNVERIFIED
    billing_source: str = ""                      # the installed-docs line behind a "pay per use" label, else ""
    proof_label: str = PROOF_DOCS
    requires: str = ""                            # what must exist before the route is selectable
    selectable_without_provider_config: bool = True
    login_provider: str = ""                      # `models auth login --provider` (OAuth); "" = print the docs, run nothing
    key_provider: str = ""                        # `models auth paste-api-key --provider`; "" = print only
    note: str = ""
    free_text_model: bool = False                 # the model is a free-text deployment name (Foundry)


KEEP = Route(KEEP_ID, "Keep what OpenClaw does now - change nothing", kind="keep", billing_label="writes nothing",
             proof_label="n/a")
DISABLE = Route(DISABLE_ID, "No: turn image generation off (adds image_generate to tools.deny; affects all agents)",
                kind="disable", billing_label="n/a", proof_label="live effect on running sessions: NOT PROVEN")

CATALOG: tuple[Route, ...] = (
    Route("openai-codex", "OpenAI", provider="openai", auth=("oauth",),
          models=("openai/gpt-image-2", "openai/gpt-image-1.5"), profile_prefixes=("openai:",),
          billing_label="subscription, account access NOT PROVEN",
          proof_label=PROOF_DOCS + "; quota NOT PROVEN", login_provider="openai",
          note="ChatGPT/Codex sign-in; GPT Image 2.5 subscription access is not established"),
    Route("openai-key", "OpenAI", provider="openai", auth=("api-key",),
          models=("openai/gpt-image-2", "openai/gpt-image-2.5-flare", "openai/gpt-image-2.5-sunburst",
                  "openai/gpt-image-1.5"),
          env=("OPENAI_API_KEY",), profile_prefixes=("openai:",), billing_label="pay per use",
          billing_source="docs/providers/openai.md:21", key_provider="openai",
          note="with an OpenAI OAuth profile present OpenClaw does not fall back to OPENAI_API_KEY"),
    Route("google-key", "Google Gemini", provider="google", auth=("api-key",),
          models=("google/gemini-3.1-flash-image",), env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
          profile_prefixes=("google:",), key_provider="google",
          note="API key only; Antigravity sign-in is not an image route (Antigravity terms)"),
    Route("openrouter", "OpenRouter", provider="openrouter", auth=("api-key",),
          models=("openrouter/google/gemini-3.1-flash-image-preview",), env=("OPENROUTER_API_KEY",),
          profile_prefixes=("openrouter:",), key_provider="openrouter"),
    Route("xai", "xAI", provider="xai", auth=("api-key",), models=("xai/grok-imagine-image",
                                                                     "xai/grok-imagine-image-quality"),
          env=("XAI_API_KEY",), profile_prefixes=("xai:",), key_provider="xai"),
    Route("fal", "fal", provider="fal", auth=("api-key",),
          models=("fal/krea/v2/medium/text-to-image", "fal/openai/gpt-image-2.5/flare/text-to-image",
                  "fal/openai/gpt-image-2.5/sunburst/text-to-image"),
          env=("FAL_KEY",), profile_prefixes=("fal:",), key_provider="fal"),
    Route("deepinfra", "DeepInfra", provider="deepinfra", auth=("api-key",),
          models=("deepinfra/black-forest-labs/FLUX-1-schnell",), env=("DEEPINFRA_API_KEY",),
          profile_prefixes=("deepinfra:",), key_provider="deepinfra"),
    Route("minimax", "MiniMax", provider="minimax", auth=("api-key", "oauth"), models=("minimax/image-01",),
          env=("MINIMAX_API_KEY",), profile_prefixes=("minimax:", "minimax-portal:"),
          billing_label=BILLING_UNVERIFIED + " (OAuth: NOT PROVEN)", key_provider="minimax",
          login_provider="minimax-portal", note="API key or MiniMax OAuth (minimax-portal)"),
    Route("microsoft-foundry", "Microsoft Foundry", provider="microsoft-foundry", auth=("api-key",),
          models=("microsoft-foundry/<deployment>",), env=("AZURE_OPENAI_API_KEY",),
          profile_prefixes=("microsoft-foundry:",), billing_label="billing per Azure subscription, not verified",
          requires="needs a Foundry deployment configured in OpenClaw", free_text_model=True,
          note="Entra ID (`az login`) is also documented; the kit does not run it"),
    Route("vydra", "Vydra", provider="vydra", auth=("api-key",), models=("vydra/grok-imagine",),
          env=("VYDRA_API_KEY",), profile_prefixes=("vydra:",), key_provider="vydra"),
    Route("comfyui", "ComfyUI", provider="comfy", auth=("api-key",), models=(), env=("COMFY_API_KEY",
                                                                                      "COMFY_CLOUD_API_KEY"),
          profile_prefixes=("comfy:",), billing_label="billing NOT PROVEN",
          requires="configure a ComfyUI workflow in OpenClaw first", selectable_without_provider_config=False,
          note="workflow-configured; the kit writes no models.providers entry"),
)

ROUTES: Mapping[str, Route] = {r.id: r for r in (KEEP, DISABLE, *CATALOG)}


def get(route_id: str) -> Route | None:
    return ROUTES.get(route_id)


def route_ids() -> list[str]:
    return [r.id for r in ROUTES.values()]


def _auth_label(route: Route) -> str:
    names = {"oauth": "OAuth sign-in", "api-key": "API key"}
    return " or ".join(names[a] for a in route.auth) or "none"


def choice_label(route: Route) -> str:
    """`<provider> via <auth> - <billing label> - <proof label>`. Same shape for every route: no row is advised."""
    if route.kind != "route":
        return route.label
    return f"{route.label} via {_auth_label(route)} - {route.billing_label} - {route.proof_label}"


# --- model refs -----------------------------------------------------------------------------------------------------------

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_OPAQUE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
MAX_REF = 160


def validate_model_ref(ref: object, route: Route | None = None) -> str:
    """The ref as a clean `provider/model[/...]` string, or ValueError. At least two plain segments, no whitespace, no
    control character, no secret-looking text (a pasted key must never reach a patch, a log or a screen). With `route` the
    prefix must be that route's provider."""
    if not isinstance(ref, str):
        raise ValueError("a model ref is text")
    text = ref.strip()
    if not text or len(text) > MAX_REF:
        raise ValueError("a model ref is 1 to 160 characters")
    segments = text.split("/")
    if len(segments) < 2 or not all(_SEGMENT.fullmatch(p) for p in segments):
        raise ValueError("a model ref looks like provider/model (letters, digits, . _ - and / only)")
    if text.lower().startswith("sk-") or any(_OPAQUE.fullmatch(p) or p.lower().startswith(("sk-", "ghp_", "xox"))
                                             for p in segments):
        raise ValueError("that does not look like a model ref")
    if route is not None and route.kind == "route" and segments[0] != route.provider:
        raise ValueError(f"a {route.label} model ref starts with {route.provider}/")
    return text


# --- patches --------------------------------------------------------------------------------------------------------------

def build_patch(route_id: str, model: str, *, timeout_ms: int = TIMEOUT_MS) -> dict | None:
    """The `config patch` body for `route_id` and `model`: None for keep and disable (they write no model), else exactly
    `agents.defaults.mediaModels.image = {primary, timeoutMs}`. ValueError for an unknown route, a route that cannot be
    selected without provider config, or an invalid ref, before anything is built."""
    route = ROUTES.get(route_id)
    if route is None:
        raise ValueError(f"unknown image route: {route_id!r}")
    if route.kind != "route":
        return None
    if not route.selectable_without_provider_config:
        raise ValueError(f"{route.label}: {route.requires}")
    ref = validate_model_ref(model, route)
    return {"agents": {"defaults": {"mediaModels": {"image": {"primary": ref, "timeoutMs": int(timeout_ms)}}}}}


def restore_patch(previous: Any, absent: bool) -> tuple[dict, list[str]]:
    """What puts `mediaModels.image` back: null (a delete, no replace path) when the key did not exist before the kit's
    first write, else the previous value sent whole with `--replace-path`. Never conflates the two."""
    if absent or previous is None:
        return {"agents": {"defaults": {"mediaModels": {"image": None}}}}, []
    return {"agents": {"defaults": {"mediaModels": {"image": previous}}}}, list(REPLACE_PATHS)
