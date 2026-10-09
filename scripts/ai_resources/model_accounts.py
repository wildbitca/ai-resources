"""Which provider accounts have credentials, merged from the two places they live.

Credentials are split on a real host: ai-resources keeps its own keys in
`~/.config/ai-resources/.env`, and OpenClaw keeps others in its own auth store (an env var in the
gateway, an auth profile, the Claude CLI's native login). `credentials.has_key` alone would call
Google "not credentialed" on a host whose gateway has a working `GEMINI_API_KEY`.

`openclaw models status --json` returns partially masked key material in `auth.providers[].effective.
detail` and `env.value`. Those fields are NEVER read: only the provider id, `effective.kind`, the
profile count and the `env.source` label pass the allowlist below. Names and kinds are reported,
never values.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import model_providers

# The only fields read from `openclaw models status --json`, per provider entry.
ALLOWED_PROVIDER_FIELDS = ("provider", "effective", "profiles", "env")
ALLOWED_EFFECTIVE_FIELDS = ("kind",)
ALLOWED_PROFILE_FIELDS = ("count",)
ALLOWED_ENV_FIELDS = ("source",)

# OpenClaw's provider ids -> the kit's. claude-cli is Anthropic through the Claude CLI's own login.
OPENCLAW_PROVIDER = {"claude-cli": "anthropic"}

SOURCES = ("env-file", "openclaw-env", "openclaw-profile", "synthetic", "local", "adc")
OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
OLLAMA_TIMEOUT = 1.5


@dataclass
class Account:
    credentialed: bool = False
    sources: list[str] = field(default_factory=list)
    reason: str = ""
    # An OpenClaw-only credential can enable catalog discovery but not a `direct` smoke probe, which
    # needs the key in the kit's own environment.
    direct_key: bool = False

    def as_dict(self) -> dict:
        return {"credentialed": self.credentialed, "sources": list(self.sources), "reason": self.reason,
                "direct_key": self.direct_key}


@dataclass
class Deps:
    env: Callable[[], dict[str, str]]
    status: Callable[[], dict | None]          # parsed `openclaw models status --json`, or None
    file_exists: Callable[[str], bool] = lambda p: Path(p).is_file()
    ollama_up: Callable[[], bool] = lambda: False


def allowlist_status(status: dict | None) -> dict[str, dict]:
    """provider id -> {kind, profiles, source}; every other field of the status JSON is dropped."""
    out: dict[str, dict] = {}
    if not isinstance(status, dict):
        return out
    auth = status.get("auth") if isinstance(status.get("auth"), dict) else {}
    for raw in auth.get("providers") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("provider"), str):
            continue
        eff = raw.get("effective") if isinstance(raw.get("effective"), dict) else {}
        prof = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
        env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
        count = prof.get("count")
        out[OPENCLAW_PROVIDER.get(raw["provider"], raw["provider"])] = {
            "kind": eff.get("kind") if isinstance(eff.get("kind"), str) else "",
            "profiles": count if isinstance(count, int) else 0,
            "source": env.get("source") if isinstance(env.get("source"), str) else "",
        }
    return out


def detect(deps: Deps) -> dict[str, Account]:
    """provider id -> Account, for every provider in the registry (a hosted gateway included)."""
    env = deps.env() or {}
    status_err = ""
    try:
        oc = allowlist_status(deps.status())
    except Exception as e:  # noqa: BLE001
        oc, status_err = {}, f"openclaw status unavailable ({type(e).__name__})"
    status_missing = not oc and not status_err
    out: dict[str, Account] = {}
    for pid, adapter in model_providers.REGISTRY.items():
        acc = Account()
        if adapter.credential_kind == "none":                                  # ollama
            if deps.ollama_up():
                acc.credentialed, acc.sources, acc.reason = True, ["local"], "local endpoint answers"
            else:
                acc.reason = "local endpoint does not answer"
            out[pid] = acc
            continue
        if adapter.credential_kind == "adc":                                   # vertex
            have = {v: bool(env.get(v) or os.environ.get(v)) for v in adapter.credential}
            adc_path = env.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or ""
            if all(have.values()) and deps.file_exists(adc_path):
                acc.credentialed, acc.sources, acc.reason = True, ["adc"], "project, location and ADC file present"
            else:
                missing = [v for v, ok in have.items() if not ok]
                acc.reason = ("missing " + ", ".join(missing)) if missing else "ADC credentials file not found"
            out[pid] = acc
            continue
        env_ok = all(env.get(v) for v in adapter.credential) if adapter.credential else False
        if env_ok:
            acc.sources.append("env-file")
            acc.direct_key = True
        known = oc.get(pid)
        if known:
            kind = known["kind"]
            if kind == "env":
                acc.sources.append("openclaw-env")
            elif kind == "profiles" or known["profiles"] > 0:
                acc.sources.append("openclaw-profile")
            elif kind == "synthetic":
                acc.sources.append("synthetic")
            elif kind:
                acc.sources.append("openclaw-profile")
        acc.credentialed = bool(acc.sources)
        if acc.credentialed:
            acc.reason = "credentials found via " + ", ".join(acc.sources)
        elif status_err:
            acc.reason = f"no key in the env file; {status_err}"
        elif status_missing:
            acc.reason = "no key in the env file (openclaw reported no auth)"
        else:
            acc.reason = "no credentials"
        out[pid] = acc
    return out


def disagreements(accounts: dict[str, Account]) -> list[str]:
    """Info lines for a key that lives in only one of the two stores."""
    lines = []
    for pid, acc in accounts.items():
        in_env = "env-file" in acc.sources
        in_oc = any(s.startswith("openclaw") or s == "synthetic" for s in acc.sources)
        if in_env and not in_oc and pid != "anthropic":
            lines.append(f"{pid}: key in the env file but not known to OpenClaw")
        elif in_oc and not in_env:
            lines.append(f"{pid}: known to OpenClaw but no key in the env file (direct smoke probes need it)")
    return lines


def render_text(accounts: dict[str, Account]) -> str:
    rows = []
    for pid, acc in accounts.items():
        state = "credentialed (" + ", ".join(acc.sources) + ")" if acc.credentialed else "no credentials"
        rows.append(f"{pid}: {state}")
    return "\n".join(rows)


def to_json(accounts: dict[str, Account]) -> str:
    return json.dumps({p: a.as_dict() for p, a in accounts.items()}, indent=2, sort_keys=True)


def default_deps(runner=None) -> Deps:
    """The real wiring: the kit's env file, `openclaw models status --json`, a 1.5 s Ollama probe."""
    from . import openclaw_host
    from .setup import credentials

    def status() -> dict | None:
        run = runner or openclaw_host.default_runner
        rc, out = run([openclaw_host.resolve_openclaw_bin(), "models", "status", "--json"], timeout=30)
        if rc != 0:
            raise RuntimeError(f"rc {rc}")
        return json.loads(out[out.index("{"):out.rindex("}") + 1])

    def ollama_up() -> bool:
        try:
            import urllib.request
            with urllib.request.urlopen(OLLAMA_URL, timeout=OLLAMA_TIMEOUT) as r:    # noqa: S310 - fixed local URL
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    return Deps(env=credentials.load_env, status=status, ollama_up=ollama_up)
