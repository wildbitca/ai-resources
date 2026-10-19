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
from dataclasses import dataclass, field, replace
from typing import Any, Collection, Mapping, NamedTuple, Sequence

from .. import state, ui
from ... import openclaw_tool_policy

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

def build_patch(route_id: str, model: str, *, timeout_ms: int = TIMEOUT_MS, route: Route | None = None) -> dict | None:
    """The `config patch` body for `route_id` and `model`: None for keep and disable (they write no model), else exactly
    `agents.defaults.mediaModels.image = {primary, timeoutMs}`. ValueError for an unknown route, a route that cannot be
    selected without provider config, or an invalid ref, before anything is built."""
    route = route or ROUTES.get(route_id)
    if route is None or route.id != route_id:
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


# --- read-only account detection (plan I6) --------------------------------------------------------------------------------

OPENCLAW_PROFILE, OPENCLAW_ENV, KIT_ENV_ONLY, MISSING, PROVIDER_CONFIG_NEEDED = (
    "openclaw-profile", "openclaw-env", "kit-env-only", "missing", "provider-config-needed")


class AccountView(NamedTuple):
    """Whether the gateway can already authenticate a route. Names only: never a value, never a mask of one."""
    gateway_sees_it: bool
    source_label: str                         # one of the five labels above
    name: str                                 # profile prefix or env var NAME(S)
    fix: str = ""                             # the command the operator runs, never run by the kit here


def sign_in_command(route: Route) -> str:
    """The OpenClaw command that signs `route` in, with the provider id only. "" when no id is established."""
    if "oauth" in route.auth and route.login_provider and "api-key" not in route.auth:
        return f"openclaw models auth login --provider {route.login_provider}"
    if route.key_provider:
        return f"openclaw models auth paste-api-key --provider {route.key_provider}"
    if route.login_provider:
        return f"openclaw models auth login --provider {route.login_provider}"
    return ""


def provider_account(route: Route, accounts: Mapping[str, Mapping[str, Any]] | None,
                     kit_env_names: Collection[str] = ()) -> AccountView:
    """`accounts` is `model_accounts.allowlist_status(<openclaw models status --json>)` (provider id -> {kind, profiles,
    source}): the allowlist already dropped every key field. `kit_env_names` are the NAMES present in the kit's own
    `.env`. The gateway does not read that file (docs/help/environment.md), so a key only there is `kit-env-only`."""
    if route.kind != "route":
        return AccountView(False, MISSING, "")
    if not route.selectable_without_provider_config:
        return AccountView(False, PROVIDER_CONFIG_NEEDED, route.requires)
    env_names = " or ".join(route.env)
    known = (accounts or {}).get(route.provider) or {}
    kind = known.get("kind") or ""
    count = known.get("profiles") if isinstance(known.get("profiles"), int) else 0
    if kind == "env":
        return AccountView(True, OPENCLAW_ENV, env_names)
    if kind == "profiles" or count > 0 or (kind and kind != "none"):
        prefix = route.profile_prefixes[0] + "*" if route.profile_prefixes else route.provider
        return AccountView(True, OPENCLAW_PROFILE, prefix)
    kit_names = [n for n in route.env if n in set(kit_env_names)]
    if kit_names:
        return AccountView(False, KIT_ENV_ONLY, " or ".join(kit_names), sign_in_command(route))
    return AccountView(False, MISSING, env_names or route.provider, sign_in_command(route))


# --- the question (plan I7): asks, never writes ---------------------------------------------------------------------------

OTHER_REF = "__other__"
SIGN_IN_AT_APPLY = "sign-in"
_METHODS = {"api-key": "API key (pasted into OpenClaw's own prompt on your terminal)",
            "oauth": "MiniMax OAuth (minimax-portal; billing NOT PROVEN)"}


def _get(doc: Any, *keys: str) -> Any:
    cur = doc
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def comfy_configured(doc: Mapping[str, Any] | None) -> bool:
    """True when the live config already has a ComfyUI provider block (the kit writes none)."""
    providers = _get(doc or {}, "models", "providers")
    return isinstance(providers, dict) and any("comfy" in str(k).lower() for k in providers)


def effective_route(route: Route, doc: Mapping[str, Any] | None) -> Route:
    """`route`, selectable when its provider config exists (ComfyUI with a workflow block)."""
    if route.id == "comfyui" and comfy_configured(doc):
        return replace(route, selectable_without_provider_config=True, free_text_model=True)
    return route


def default_mode(s: state.SetupState) -> str:
    """The pre-selected answer: the saved one, else `keep`. Nothing is ever pre-selected as advice."""
    saved = s.openclaw.image_mode
    return saved if saved in ROUTES else KEEP_ID


def live_image_value(doc: Mapping[str, Any] | None) -> Any:
    return _get(doc or {}, "agents", "defaults", "mediaModels", "image")


def ref_of(value: Any) -> str:
    """The primary model ref of a live `mediaModels.image` value (a string or {primary, ...}); "" when there is none."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("primary"), str):
        return value["primary"]
    return ""


def kit_shaped(value: Any) -> bool:
    """True for exactly the value `build_patch` writes: {primary, timeoutMs: 180000}. After the kit has applied a model, a
    live value of this shape is the kit's own (replaced without a second confirm); anything else was set by the operator."""
    return (isinstance(value, dict) and set(value) == {"primary", "timeoutMs"} and isinstance(value["primary"], str)
            and value["timeoutMs"] == TIMEOUT_MS)


def live_covering_entry(doc: Mapping[str, Any] | None) -> str | None:
    deny = _get(doc or {}, "tools", "deny")
    if isinstance(deny, list) and all(isinstance(x, str) for x in deny):
        return openclaw_tool_policy.covering_token(deny, openclaw_tool_policy.IMAGE_TOOL)
    return None


def route_choices(doc: Mapping[str, Any] | None, accounts: Mapping[str, Mapping[str, Any]] | None,
                  kit_env_names: Collection[str] = ()) -> list:
    """The one provider-style list: keep, disable, then every catalog route in catalog order with its labels and a
    visibility marker. A route that needs provider config the kit does not write is a disabled choice with its reason."""
    out = [ui.Choice(KEEP.label, value=KEEP_ID), ui.Choice(DISABLE.label, value=DISABLE_ID)]
    for route in CATALOG:
        route = effective_route(route, doc)
        view = provider_account(route, accounts, kit_env_names)
        label = choice_label(route)
        if route.selectable_without_provider_config:
            out.append(ui.Choice(f"{label} [{view.source_label}]", value=route.id))
        else:
            out.append(ui.Choice(label, value=route.id, disabled=route.requires))
    return out


def prompt(s: state.SetupState, accounts: Mapping[str, Mapping[str, Any]] | None = None,
           doc: Mapping[str, Any] | None = None, kit_env_names: Collection[str] = ()) -> None:
    """Phase 2.1. Sets answers on the in-memory state and writes nothing; its only I/O is the screen. A non-interactive
    run asks nothing and leaves the saved answer (or the never-answered "") untouched: that resolves to `keep`."""
    if ui.is_non_interactive():
        return
    ui.detail("OpenClaw generates images with the `image_generate` tool. Setup does not advise a route: every documented "
              "route is listed with its billing and proof label. Nothing here asks for a key.")
    mode = ui.select("Which image generation setup should OpenClaw have?", route_choices(doc, accounts, kit_env_names),
                     default=default_mode(s))
    s.openclaw.image_mode = mode
    covering = live_covering_entry(doc)
    live_ref = ref_of(live_image_value(doc))
    if mode == KEEP_ID:
        ui.info("Setup leaves image generation as it is. OpenClaw picks a provider by itself from any sign-in it "
                "already has" + (f" ({', '.join(sorted(_profile_names(accounts)))})" if _profile_names(accounts) else "") + ".")
        return
    if mode == DISABLE_ID:
        if covering:
            ui.info(f"image_generate is already denied by your tools.deny entry {covering!r}; setup changes nothing.")
        else:
            ui.warn("This adds image_generate to the global tools.deny: it affects ALL agents, including lane agents. "
                    "Live effect on running sessions: NOT PROVEN.")
        return
    route = effective_route(ROUTES[mode], doc)
    s.openclaw.image_model = _prompt_model(s, route)
    view = provider_account(route, accounts, kit_env_names)
    s.openclaw.image_account = _prompt_account(s, route, view)
    if mode == "openai-key" and (accounts or {}).get("openai", {}).get("profiles"):
        ui.warn("An OpenAI sign-in already exists in OpenClaw; with it present OpenClaw does not fall back to "
                "OPENAI_API_KEY.")
    if live_ref and live_ref != s.openclaw.image_model:
        ui.warn(f"OpenClaw already has an image model set ({live_ref}). Setup asks before it changes it.")
    if covering:
        ui.warn(f"Your tools.deny entry {covering!r} still blocks image_generate; setup does not remove it.")
    elif s.openclaw.image_deny_added:
        ui.info("Setup's earlier image_generate entry in tools.deny is removed after the model is set.")
    if view.source_label == KIT_ENV_ONLY:
        ui.warn(f"{view.name} is only in the kit's .env, which the gateway does not read. Sign in with: {view.fix}")


def _profile_names(accounts: Mapping[str, Mapping[str, Any]] | None) -> list[str]:
    names = []
    for provider, info in (accounts or {}).items():
        if isinstance(info, Mapping) and (info.get("profiles") or info.get("kind") in ("profiles", "env")):
            names.append(provider)
    return names


def _validate(route: Route):
    def check(value: str):
        try:
            validate_model_ref(value, route)
            return True
        except ValueError as e:
            return str(e)
    return check


def _prompt_model(s: state.SetupState, route: Route) -> str:
    """The model for `route`: a catalog ref, or a validated free-text one. Never a secret prompt."""
    saved = s.openclaw.image_model
    catalog = [m for m in route.models if "<" not in m]
    if route.free_text_model or not catalog:
        hint = f"{route.provider}/<deployment>" if route.free_text_model else f"{route.provider}/<model>"
        value = ui.text(f"{route.label} model ref ({hint}):", default=saved if saved.startswith(route.provider + "/") else "",
                        validate=_validate(route))
        try:
            return validate_model_ref(value, route)
        except ValueError:
            return ""
    choices = [ui.Choice(m, value=m) for m in catalog] + [ui.Choice("Other model ref...", value=OTHER_REF)]
    pick = ui.select(f"Model for {route.label}?", choices, default=saved if saved in catalog else catalog[0])
    if pick != OTHER_REF:
        return pick
    value = ui.text(f"{route.label} model ref ({route.provider}/<model>):", default="", validate=_validate(route))
    try:
        return validate_model_ref(value, route)
    except ValueError:
        return catalog[0]


def _prompt_account(s: state.SetupState, route: Route, view: AccountView) -> str:
    """The account answer is a name, never a value: a profile prefix, an env var NAME, or the auth method for a route that
    has two (MiniMax). Signing in itself is OpenClaw's, on the operator's terminal, at Apply (plan I9)."""
    if len(route.auth) > 1:
        saved = s.openclaw.image_account if s.openclaw.image_account in route.auth else route.auth[0]
        return ui.select(f"How do you sign in to {route.label}?",
                         [ui.Choice(_METHODS.get(a, a), value=a) for a in route.auth], default=saved)
    if view.gateway_sees_it:
        ui.info(f"OpenClaw already has a sign-in for {route.label}: {view.name}")
        return view.name
    ui.info(f"OpenClaw has no sign-in for {route.label} yet. At Apply you can sign in through OpenClaw "
            f"(its own prompt, on your terminal); setup never sees the key.")
    return SIGN_IN_AT_APPLY


# --- the sign-in hand-off to OpenClaw (plan I9) ---------------------------------------------------------------------------

SIGN_IN_TIMEOUT_S = 1200
PRESENT, PRINTED, DECLINED, FAILED, SIGNED_IN, NOT_NEEDED = (
    "present", "printed", "declined", "failed", "signed-in", "not-needed")


class SignInResult(NamedTuple):
    status: str
    command: str = ""
    profile_id: str = ""                      # an id only, never a value
    exit_code: int = 0


def sign_in_for(route: Route, method: str = "") -> tuple[list[str], str]:
    """`(argv, profile id the command creates)` for `route`, or `([], "")` when no provider id is established. The argv
    carries the provider id and nothing else: no key, no token, no stdin. For a route with two methods `method` picks one
    (`oauth` or `api-key`). `paste-api-key` names its profile `<provider>:manual` (docs/cli/models.md:390); the OAuth login
    does not say, so no profile id is claimed for it (teardown then offers no sign-out for it)."""
    use_oauth = (method == "oauth") if len(route.auth) > 1 else ("oauth" in route.auth and "api-key" not in route.auth)
    if use_oauth and route.login_provider:
        return ["openclaw", "models", "auth", "login", "--provider", route.login_provider], ""
    if not use_oauth and route.key_provider:
        return ["openclaw", "models", "auth", "paste-api-key", "--provider", route.key_provider], f"{route.key_provider}:manual"
    return [], ""


def ensure_account(route: Route, view: AccountView, *, method: str = "", run, interactive: bool, confirm,
                   dry_run: bool, say=lambda msg: None) -> SignInResult:
    """Hand the credential to OpenClaw, never take it. Runs `openclaw models auth ...` with the operator's own terminal
    (the caller's `run(argv)` must inherit stdin/stdout/stderr: no capture, no `input=`), only on a real interactive Apply,
    only when the gateway does not see a credential, only after a default-No confirm. Everything else prints the command.
    A non-zero exit is reported by code only and the caller writes no config."""
    if route.kind != "route" or view.gateway_sees_it:
        return SignInResult(PRESENT if view.gateway_sees_it else NOT_NEEDED)
    argv, profile_id = sign_in_for(route, method)
    command = " ".join(argv)
    if not argv:
        say(f"{route.label}: sign in through OpenClaw yourself ({route.requires or 'see docs/tools/image-generation.md'}); "
            "setup has no established provider id for it.")
        return SignInResult(PRINTED)
    if dry_run:
        say(f"would run: {command}")
        return SignInResult(PRINTED, command)
    if not interactive:
        say(f"{route.label} has no sign-in in OpenClaw yet. Run on your terminal: {command}")
        return SignInResult(PRINTED, command)
    if not confirm(f"Sign in to {route.label} through OpenClaw now? OpenClaw asks for the credential on this terminal; "
                   "setup never sees it.", default=False):
        say(f"Not signed in. Run on your terminal when ready: {command}")
        return SignInResult(DECLINED, command)
    rc = run(argv)
    if rc != 0:
        say(f"OpenClaw sign-in exited with code {rc}; the image model was not changed.")
        return SignInResult(FAILED, command, "", rc)
    return SignInResult(SIGNED_IN, command, profile_id)


# --- Review rows and the dry-run preview (plan I10): names and refs only, no I/O -------------------------------------------

CTX_ATTR = "_image_ctx"
"""Instance attribute on the setup state (never serialised): what the image question learned in this run."""
NOT_PROVEN = "live effect on running sessions: NOT PROVEN"


def make_ctx(doc: Mapping[str, Any] | None, accounts: Mapping[str, Mapping[str, Any]] | None,
             kit_env_names: Collection[str], *, deny_hot: bool) -> dict:
    """The facts Review needs, taken once while the question ran. Names only: a covering deny entry, the live model ref,
    the auth profile ids and whether `tools.deny` would apply live."""
    ids: list[str] = []
    for info in (accounts or {}).values():
        if isinstance(info, Mapping):
            ids += [i for i in info.get("ids") or [] if isinstance(i, str)]
    return {"covering": live_covering_entry(doc), "live_ref": ref_of(live_image_value(doc)),
            "profile_ids": sorted(set(ids)), "accounts": dict(accounts or {}), "kit_env": sorted(kit_env_names),
            "deny_hot": bool(deny_hot)}


def _safe(text: str) -> str:
    return ui.safe_text(text)


def review_rows(s: state.SetupState, ctx: Mapping[str, Any] | None = None,
                doc: Mapping[str, Any] | None = None) -> list[tuple[str, str]]:
    """The Review rows for the image answer. Empty is never returned for an OpenClaw run: even `keep` is a row, worded as
    what setup does NOT do, never as advice. The caller adds none of these when OpenClaw is not a selected tool."""
    ctx = ctx or {}
    o = s.openclaw
    mode = o.image_mode or KEEP_ID
    covering = ctx.get("covering")
    rows: list[tuple[str, str]] = []
    if mode == KEEP_ID:
        ids = ctx.get("profile_ids") or []
        seen = f" ({', '.join(ids)})" if ids else ""
        rows.append(("Image generation", _safe("not configured by setup (OpenClaw auto-detects from existing sign-ins"
                                                f"{seen})")))
        if o.image_deny_added:
            rows.append(("", "setup's earlier image_generate entry in tools.deny stays until teardown"))
        return rows
    if mode == DISABLE_ID:
        if covering:
            text = f"already denied by operator entry {covering} (not changed)"
        elif ctx.get("deny_hot", True) is False:
            text = openclaw_deny_not_hot_text()
        else:
            text = ("disabled (tools.deny adds image_generate; operator entries kept; affects all agents; "
                    + NOT_PROVEN + ")")
        rows.append(("Image generation", _safe(text)))
        return rows
    route = effective_route(ROUTES.get(mode, KEEP), doc)
    if route.kind != "route":
        return [("Image generation", _safe(f"unknown answer {mode}; left unchanged"))]
    ref = o.image_model or "(model not set)"
    rows.append(("Image generation", _safe(f"{route.label} -> {ref}")))
    view = provider_account(route, ctx.get("accounts"), ctx.get("kit_env") or ())
    account = {OPENCLAW_PROFILE: "OpenClaw sign-in", OPENCLAW_ENV: "OpenClaw environment",
               KIT_ENV_ONLY: "only in the kit .env (the gateway does not read it)", MISSING: "no sign-in yet",
               PROVIDER_CONFIG_NEEDED: "needs provider config"}[view.source_label]
    rows.append(("Image account", _safe(f"{account}: {view.name}" if view.name else account)))
    rows.append(("Image billing", _safe(route.billing_label)))
    rows.append(("Image proof", _safe(route.proof_label)))
    rows.append(("Image undo", "teardown restores the previous value"))
    if covering and covering != "image_generate":
        rows.append(("", _safe(f"stays denied by operator entry {covering}; setup does not remove it")))
    elif o.image_deny_added:
        rows.append(("", "removes the image_generate entry setup added (after the model is set)"))
    return rows


def openclaw_deny_not_hot_text() -> str:
    return ("Image generation will NOT be disabled: OpenClaw would need a restart to reload tools.deny. Run "
            "`openclaw config get tools.deny` and add `image_generate` by hand, or re-run setup after enabling hot reload.")


def dry_run_lines(s: state.SetupState, ctx: Mapping[str, Any] | None = None) -> list[str]:
    """What a real Apply would do for the image answer, in words (tool names and model refs only)."""
    mode = s.openclaw.image_mode or KEEP_ID
    if mode == KEEP_ID:
        return ["image generation: keep (no change)"]
    if mode == DISABLE_ID:
        return ["image generation: would add image_generate to tools.deny (operator entries kept, affects all agents)"]
    route = ROUTES.get(mode)
    lines = [f"image generation: would set {MEDIA_PATH} to {s.openclaw.image_model or '(model not set)'}"]
    if route is not None and route.kind == "route":
        cmd = sign_in_command(route)
        view = provider_account(route, (ctx or {}).get("accounts"), (ctx or {}).get("kit_env") or ())
        if cmd and not view.gateway_sees_it:
            lines.append(f"would run: {cmd}  (only after your confirm, on your terminal)")
    if s.openclaw.image_deny_added:
        lines.append("would remove the image_generate entry setup added to tools.deny (after the model is set)")
    return [_safe(line) for line in lines]
