"""Read-only detection aggregate for team composition.

`inventory(deps)` composes the existing cockpit detection, the account detection (names only) and a few file
presence checks into one `EnabledSet`: which cockpits, engines, providers and billing pools exist on this
host, and what each may be used for (`usage`).

Guarantees (tests pin them): no provider API call, no model listing, no `agy /usage` probe, no headroom
reader call and no write; a credential file (for example `~/.codex/auth.json`) is checked for presence and
never opened. Detection only proposes; it does not enable anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..setup import compat
from . import db as scores_db

# cockpit id -> the `surfaces` key of the scores database
SURFACE_OF = {"claude": "claude-code", "agy": "agy", "codex": "codex"}
ENGINE_SURFACE = {"antigravity": "engine:antigravity", "codex": "codex", "claude-code": "claude-code"}


@dataclass(frozen=True)
class CockpitInfo:
    id: str
    installed: bool
    usage: str                 # dispatch-ok | direct-only | instructions-only
    capability: str = ""       # no-dispatch | report-only | ""
    reason: str = ""


@dataclass(frozen=True)
class EngineInfo:
    id: str
    available: bool
    usage: str
    reason: str = ""


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    credentialed: bool
    sources: tuple[str, ...] = ()
    hint: str = ""


@dataclass(frozen=True)
class PoolInfo:
    id: str
    billing_class: str
    present: bool
    usage: str
    headroom_source: str = "none"
    reason: str = ""


@dataclass(frozen=True)
class EnabledSet:
    cockpits: tuple[CockpitInfo, ...] = ()
    engines: tuple[EngineInfo, ...] = ()
    providers: tuple[ProviderInfo, ...] = ()
    pools: tuple[PoolInfo, ...] = ()

    def credentialed_providers(self) -> list[str]:
        return [p.id for p in self.providers if p.credentialed]

    def pool_ids(self, present_only: bool = True) -> list[str]:
        return [p.id for p in self.pools if p.present or not present_only]


@dataclass
class Deps:
    """Injected readers. Every field is read-only by contract."""
    cockpits: Callable[[], Mapping[str, Any]]          # id -> object with .installed
    accounts: Callable[[], Mapping[str, Any]]          # provider -> Account (names only)
    env: Callable[[], Mapping[str, str]]               # the kit's env (names matter, values never shown)
    file_exists: Callable[[str], bool]
    home: Path = field(default_factory=Path.home)


def default_deps() -> Deps:
    """The real wiring: the cockpit version probes, the account detector and file presence checks."""
    from .. import model_accounts
    from ..setup import credentials, detection

    def cockpits():
        return detection.detect_all_cockpits()

    def accounts():
        return model_accounts.detect(model_accounts.default_deps())

    return Deps(cockpits=cockpits, accounts=accounts, env=credentials.load_env,
                file_exists=lambda p: Path(p).is_file())


def _cockpit_usage(cid: str, surfaces: Mapping[str, Any]) -> tuple[str, str, str]:
    key = SURFACE_OF.get(cid)
    if key and key in surfaces:
        spec = surfaces[key]
        usage = str(spec.get("usage", "dispatch-ok"))
        cap = {"instructions-only": "no-dispatch", "direct-only": "report-only"}.get(usage, "")
        return usage, cap, str(spec.get("reason", ""))
    cells = [compat.CELLS[(cid, p, m)] for p in compat.PROVIDER_COLUMNS for m in compat.MODES
             if (cid, p, m) in compat.CELLS]
    if cells and all(c.action in (compat.INSTRUCTIONS_ONLY, compat.SKIP) for c in cells):
        return "instructions-only", "no-dispatch", "the compat matrix has no model-setting cell for this cockpit"
    return "dispatch-ok", "", ""


def inventory(deps: Deps, db: scores_db.ScoresDB | None = None) -> EnabledSet:
    """What exists on this host, from read-only sources only."""
    db = db or scores_db.ScoresDB(scores_db.load_shipped())
    detected = deps.cockpits() or {}
    installed = {cid: bool(getattr(d, "installed", False)) for cid, d in detected.items()}

    cockpits: list[CockpitInfo] = []
    for cid in list(compat.COCKPITS) + list(compat.REPORT_ONLY):
        if cid not in installed:
            continue
        usage, cap, reason = _cockpit_usage(cid, db.surfaces)
        cockpits.append(CockpitInfo(cid, installed[cid], usage, cap, reason))

    engines: list[EngineInfo] = []
    if installed.get("openclaw"):
        runs = {"claude-code": installed.get("claude", False), "codex": installed.get("codex", False),
                "antigravity": installed.get("agy", False), "native": True}
        for eid in compat.ENGINES:
            spec = db.surfaces.get(ENGINE_SURFACE.get(eid, ""), {})
            usage = str(spec.get("usage", "dispatch-ok"))
            if eid == "codex":
                usage = "instructions-only"       # no codex exec dispatch path (usage-openai...:164)
            engines.append(EngineInfo(eid, bool(runs.get(eid)), usage, str(spec.get("reason", ""))))

    env = deps.env() or {}
    accounts = deps.accounts() or {}
    providers: list[ProviderInfo] = []
    for pid in sorted(accounts):
        acc = accounts[pid]
        hint = ""
        if pid == "vertex":
            adc = str(deps.home / ".config" / "gcloud" / "application_default_credentials.json")
            hint = "GOOGLE_CLOUD_PROJECT set" if env.get("GOOGLE_CLOUD_PROJECT") else "GOOGLE_CLOUD_PROJECT not set"
            hint += "; ADC file present" if deps.file_exists(adc) else "; ADC file absent"
        providers.append(ProviderInfo(pid, bool(getattr(acc, "credentialed", False)),
                                      tuple(getattr(acc, "sources", ()) or ()), hint))

    def cred(pid: str) -> bool:
        return any(p.id == pid and p.credentialed for p in providers)

    has = lambda name: bool(env.get(name))
    present = {
        "claude-subscription": installed.get("claude", False),
        "anthropic-api": has("ANTHROPIC_API_KEY") or cred("anthropic"),
        "vertex-claude": cred("vertex"),
        "openrouter": cred("openrouter") or has("OPENROUTER_API_KEY"),
        "gemini-api": cred("google") or has("GEMINI_API_KEY"),
        "openai-api": has("CODEX_API_KEY") or has("OPENAI_API_KEY") or cred("openai"),
        "deepseek-api": cred("deepseek") or has("DEEPSEEK_API_KEY"),
        "codex-subscription": deps.file_exists(str(deps.home / ".codex" / "auth.json")),   # presence only
        "agy-gemini": installed.get("agy", False),
        "agy-claude-gpt": installed.get("agy", False),
    }
    pools = [PoolInfo(str(p["id"]), str(p.get("billing_class", "")), bool(present.get(str(p["id"]), False)),
                      str(p.get("usage", "")), str(p.get("headroom_source", "none")),
                      "reporting only (direct use only)" if p.get("usage") == "direct-only" else "")
             for p in db.pools]
    return EnabledSet(tuple(cockpits), tuple(engines), tuple(providers), tuple(pools))
