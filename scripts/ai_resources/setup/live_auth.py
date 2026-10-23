"""Live sign-in state of each cockpit, read from the cockpit itself (ADR-0016 D1).

The kit does not collect or remember how a cockpit signs in: it asks the cockpit. Everything here is read-only and
carries METHOD information only:

- `claude auth status` JSON is reduced to its method fields; the email, org id, org name and every account id are
  dropped at the parse and never kept, printed or stored.
- Key values are never read, only whether `ANTHROPIC_API_KEY` or an `apiKeyHelper` is present.
- No gateway call (ADR-0003): OpenClaw is asked through `openclaw models status --json` and
  `openclaw models auth list --json --agent <id>`, which read the local auth store.

`runner(argv) -> (returncode, output)` is the seam; `default_runner` is replaced by the test suite, so no test runs the
operator's real `claude`, `openclaw` or `agy`. `handoff(argv)` runs a native sign-in flow on the operator's own
terminal and is only reachable on an explicit choice.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import model_accounts

# kinds
SUBSCRIPTION, API_KEY, API_KEY_HELPER, VERTEX, BEDROCK = "subscription", "api_key", "api_key_helper", "vertex", "bedrock"
OAUTH_GOOGLE, ADC, NONE, UNKNOWN = "oauth_google", "adc", "none", "unknown"
KINDS = (SUBSCRIPTION, API_KEY, API_KEY_HELPER, VERTEX, BEDROCK, OAUTH_GOOGLE, ADC, NONE, UNKNOWN)

# who is billed
B_SUBSCRIPTION, B_API, B_GOOGLE, B_UNKNOWN = "subscription", "api_per_token", "google_plan", "unknown"
BILLED_LABEL = {B_SUBSCRIPTION: "subscription", B_API: "API per token", B_GOOGLE: "Google plan", B_UNKNOWN: "unknown"}

Runner = Callable[[list[str]], "tuple[int, str]"]

STATUS_TIMEOUT_S = 20


@dataclass(frozen=True)
class LiveAuth:
    kind: str = UNKNOWN
    billed_to: str = B_UNKNOWN
    label: str = "unknown"

    def line(self) -> str:
        return f"{self.label} (billed to: {BILLED_LABEL.get(self.billed_to, 'unknown')})"


UNKNOWN_AUTH = LiveAuth(UNKNOWN, B_UNKNOWN, "unknown")

_BILLED_OF_KIND = {SUBSCRIPTION: B_SUBSCRIPTION, API_KEY: B_API, API_KEY_HELPER: B_API, VERTEX: B_API, BEDROCK: B_API,
                   ADC: B_API, OAUTH_GOOGLE: B_GOOGLE}


def billed_for_kind(kind: str) -> str:
    """Who is billed for a recorded live kind. A kind that says nothing about billing (none, unknown, '') is "unknown":
    the Review table shows that word rather than guess."""
    return _BILLED_OF_KIND.get(kind, B_UNKNOWN)
NO_AUTH = LiveAuth(NONE, B_UNKNOWN, "not signed in")


def default_runner(argv: list[str]) -> tuple[int, str]:
    """Run a READ-ONLY status command and capture its output. Replaced by the suite; never used to sign in."""
    binary = shutil.which(argv[0])
    if not binary:
        return 127, f"{argv[0]} not found on PATH"
    try:
        r = subprocess.run([binary, *argv[1:]], capture_output=True, text=True, timeout=STATUS_TIMEOUT_S,
                           cwd=str(Path.home()))
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout or "") + ("" if r.returncode == 0 else (r.stderr or ""))


def run(argv: list[str], runner: Runner | None = None) -> tuple[int, str]:
    return (runner or default_runner)(argv)


def native_handoff(argv: list[str]) -> int:
    """Give the terminal to a cockpit's own sign-in flow: stdin, stdout and stderr are inherited, nothing is captured,
    nothing is piped in, so a credential goes from the keyboard to the cockpit and the kit never sees it."""
    binary = shutil.which(argv[0])
    if not binary:
        return 127
    try:
        return subprocess.run([binary, *argv[1:]], cwd=str(Path.home()), check=False).returncode
    except OSError:
        return 1
    except KeyboardInterrupt:
        return 130


# The seam the wizard calls; the suite replaces it with a stub that fails the test when a native flow is reached by
# accident, and a test of the explicit "change" choice installs a recording stub.
handoff: Callable[[list[str]], int] = native_handoff


def run_handoff(argv: list[str]) -> int:
    return handoff(argv)


# --- Claude Code -------------------------------------------------------------------------------------------------------

def _parse_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        if start < 0:
            return None
        try:
            return json.loads(text[start:])
        except ValueError:
            return None


def method_fields(status: Any) -> dict[str, Any]:
    """The only fields of `claude auth status` that survive the parse. Everything else (email, orgId, orgName, account
    uuids, ...) is dropped here and never reaches a `LiveAuth`."""
    if not isinstance(status, dict):
        return {}
    keep = {}
    for k in ("loggedIn", "authMethod", "apiProvider", "subscriptionType"):
        v = status.get(k)
        if isinstance(v, (str, bool)):
            keep[k] = v
    billing = status.get("billing")
    if isinstance(billing, dict) and isinstance(billing.get("auth_mode"), str):
        keep["auth_mode"] = billing["auth_mode"]
    return keep


def _helper_present(settings: Mapping | None) -> bool:
    return bool(isinstance(settings, dict) and isinstance(settings.get("apiKeyHelper"), str)
                and settings["apiKeyHelper"].strip())


def read_claude(runner: Runner | None = None, env: Mapping[str, str] | None = None,
                settings: Mapping | None = None, claude_json: Mapping | None = None) -> LiveAuth:
    """Claude Code's live sign-in. Order: ANTHROPIC_API_KEY, settings apiKeyHelper, Vertex/Bedrock env, then
    `claude auth status`; otherwise none/unknown. "API key" is reported only when a key or helper is present."""
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_API_KEY"):
        return LiveAuth(API_KEY, B_API, "Anthropic API key (ANTHROPIC_API_KEY)")
    if _helper_present(settings):
        return LiveAuth(API_KEY_HELPER, B_API, "Anthropic API key (apiKeyHelper)")
    if env.get("CLAUDE_CODE_USE_VERTEX") == "1":
        return LiveAuth(VERTEX, B_API, "Google Vertex AI (CLAUDE_CODE_USE_VERTEX)")
    if env.get("CLAUDE_CODE_USE_BEDROCK") == "1":
        return LiveAuth(BEDROCK, B_API, "Amazon Bedrock (CLAUDE_CODE_USE_BEDROCK)")
    rc, out = run(["claude", "auth", "status"], runner)
    if rc == 127:
        return LiveAuth(NONE, B_UNKNOWN, "claude is not installed")
    fields = method_fields(_parse_json(out)) if rc == 0 else {}
    if not fields:
        return UNKNOWN_AUTH
    if fields.get("loggedIn") is False:
        return NO_AUTH
    mode = str(fields.get("auth_mode") or "")
    # apiProvider "firstParty" alone proves nothing (an API key is first party too): only the method does.
    if mode == "subscription" or fields.get("authMethod") == "claude.ai":
        plan = fields.get("subscriptionType")
        label = f"Claude subscription ({plan})" if isinstance(plan, str) and plan else "Claude subscription"
        return LiveAuth(SUBSCRIPTION, B_SUBSCRIPTION, label)
    # API-key, Vertex and Bedrock shapes of the status command are unobserved: never guess, say unknown.
    return UNKNOWN_AUTH


def claude_signed_in(auth: LiveAuth) -> bool:
    return auth.kind not in (NONE, UNKNOWN)


# --- OpenClaw ----------------------------------------------------------------------------------------------------------

def read_openclaw(runner: Runner | None = None, agents: list[str] | None = None) -> dict[str, dict[str, LiveAuth]]:
    """OpenClaw's live auth: agent id -> provider id -> LiveAuth, from the local auth store (no gateway call).

    `models status --json` gives the provider kinds; `models auth list --json --agent <id>` the per-agent profiles. A
    provider with neither a profile nor an env credential is absent from the result (not usable). Profile ids, labels
    and key material are never read: only the provider and the credential type.
    """
    out: dict[str, dict[str, LiveAuth]] = {}
    rc, text = run(["openclaw", "models", "status", "--json"], runner)
    shared = _status_providers(_parse_json(text)) if rc == 0 else {}
    for agent in agents or ["main"]:
        per = dict(shared)
        rc, text = run(["openclaw", "models", "auth", "list", "--json", "--agent", agent], runner)
        if rc == 0:
            for prov, auth in _profile_providers(_parse_json(text)).items():
                per[prov] = auth
        out[agent] = per
    return out


def _status_providers(doc: Any) -> dict[str, LiveAuth]:
    found: dict[str, LiveAuth] = {}
    for prov, info in model_accounts.allowlist_status(doc if isinstance(doc, dict) else None).items():
        kind = info.get("kind") or ""
        if info.get("profiles", 0) <= 0 and kind not in ("env", "synthetic", "profiles"):
            continue
        # `models status` does not say the credential type of a profile: never guess API key (billed per token).
        found[prov] = _provider_auth(prov, "env" if kind == "env" else "")
    return found


def _profile_providers(doc: Any) -> dict[str, LiveAuth]:
    found: dict[str, LiveAuth] = {}
    profiles = doc.get("profiles") if isinstance(doc, dict) else None
    for p in profiles if isinstance(profiles, list) else []:
        if not isinstance(p, dict) or not isinstance(p.get("provider"), str):
            continue
        kind = p.get("type") if isinstance(p.get("type"), str) else ""
        found[model_accounts.OPENCLAW_PROVIDER.get(p["provider"], p["provider"])] = _provider_auth(p["provider"], kind)
    return found


def _provider_auth(provider: str, how: str) -> LiveAuth:
    if provider in ("google-vertex", "vertex"):
        return LiveAuth(ADC, B_API, "Google Vertex (ADC / project / location)")
    if provider == "claude-cli" or provider == "anthropic" and how in ("oauth", "token"):
        return LiveAuth(SUBSCRIPTION, B_SUBSCRIPTION, "Claude sign-in (claude-cli)")
    if provider in ("agy-cli", "google-antigravity"):
        return LiveAuth(OAUTH_GOOGLE, B_GOOGLE, "Google account (agy)")
    if how in ("api_key", "api-key", "env"):
        return LiveAuth(API_KEY, B_API, "API key")
    if how in ("oauth", "token"):
        return LiveAuth(UNKNOWN, B_UNKNOWN, "OAuth sign-in")
    return UNKNOWN_AUTH


def usable_providers(live: Mapping[str, Mapping[str, LiveAuth]], agent: str | None = None) -> set[str]:
    """Providers OpenClaw reports usable (any agent, or the one named)."""
    if agent is not None:
        return set(live.get(agent, {}))
    found: set[str] = set()
    for per in live.values():
        found |= set(per)
    return found


# --- agy ---------------------------------------------------------------------------------------------------------------

AGY_LOGIN_ARGV = ["agy"]

def agy_home() -> Path:
    """agy's home (`~/.gemini`), resolved at call time so the suite points it at a scratch directory."""
    return Path.home() / ".gemini"


def read_agy(exists: Callable[[Path], bool] | None = None, home: Path | None = None) -> LiveAuth:
    """Presence only: whether a Google account state file exists under agy's home. Nothing in it is read."""
    exists = exists or (lambda p: p.exists())
    base = home or agy_home()
    for rel in ("google_accounts.json", "oauth_creds.json", "gemini-credentials.json"):
        if exists(base / rel):
            return LiveAuth(OAUTH_GOOGLE, B_GOOGLE, "Google account signed in")
    return NO_AUTH


def read_claude_settings(path: Path) -> dict:
    """settings.json as a dict, or {} (a missing or unreadable file reads as empty)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}
