"""Glue between setup state and the composition engine (the impure edge of the scoring package).

The engine (`compose.py`) is pure. This module reads the setup state, the host inventory and the user
override file, runs the opt-in headroom readers, and builds the `ComposeInput`. It never writes setup state
or any host config: the wizard decides what to store.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..setup import compat, profiles
from . import db as scores_db
from . import detect, explain, headroom as hr, headroom_sources, quota_signal
from .compose import Composition, ComposeInput, MODES, compose
from .gate import make_gate

BASELINE_PROFILE = profiles.RECOMMENDED_PROFILE
REFERENCE_PROFILES = ("all-gemini", "measured-best", "quality-first", "cost-optimized")
CODEX_REFERENCE = "all-openai-via-codex"
CODEX_REFERENCE_MODEL = "openai/gpt-6.1-sol@openclaw-codex-engine"

MIXED_REFUSAL = (
    "mixed mode is refused: the compat matrix has no verified cross-provider cell "
    "(tests/test_composition_matrix_facts.py pins this), so a mixed team is unverified by construction. "
    "Pass --allow-unverified to propose an unverified team; every non-Anthropic cell is then marked."
)


@dataclass
class Request:
    mode: str = "mixed"
    allow_unverified: bool = False
    headroom: dict[str, str] = field(default_factory=dict)        # pool id -> ample|normal|scarce
    live: tuple[str, ...] = ()                                    # pools the operator opted into reading live
    allow_fable: bool = False
    accept_stale: bool = False


# --- flags -------------------------------------------------------------------------------------
def parse_headroom(text: str, known_pools: list[str] | None = None) -> dict[str, str]:
    """`pool=level,pool=level` -> {pool: level}. Raises ValueError on an unknown level or pool."""
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        pool, sep, level = part.partition("=")
        pool, level = pool.strip(), level.strip().lower()
        if not sep or level not in hr.LEVELS:
            raise ValueError(f"--headroom {part!r}: use pool=ample|normal|scarce")
        if known_pools is not None and pool not in known_pools:
            raise ValueError(f"--headroom: unknown pool {pool!r} (known: {', '.join(sorted(known_pools))})")
        out[pool] = level
    return out


def parse_live(text: str, known_pools: list[str] | None = None) -> tuple[str, ...]:
    pools = tuple(p.strip() for p in (text or "").split(",") if p.strip())
    if known_pools is not None:
        unknown = [p for p in pools if p not in known_pools]
        if unknown:
            raise ValueError(f"--headroom-live: unknown pool {unknown[0]!r} (known: {', '.join(sorted(known_pools))})")
    return pools


def validate_why_json_path(path: str, cwd: Path | None = None, home: Path | None = None) -> Path:
    """The path must resolve under the workspace or ~/.cache. /tmp (tmpfs) is refused."""
    cwd = (cwd or Path.cwd()).resolve()
    home = (home or Path.home()).resolve()
    target = Path(os.path.realpath(os.path.expanduser(path)))
    for forbidden in ("/tmp", "/var/tmp", "/dev/shm"):
        if target == Path(forbidden) or Path(forbidden) in target.parents:
            raise ValueError(f"--why-json: {forbidden} is refused (tmpfs, emptied on reboot); use the workspace or ~/.cache")
    allowed = [cwd, home / ".cache"]
    if not any(root == target or root in target.parents for root in allowed):
        raise ValueError(f"--why-json: {target} is outside the workspace ({cwd}) and {home / '.cache'}")
    return target


def write_why_json(path: Path, comp: Composition, lanes: Any = None) -> None:
    """Atomic write, owner-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(explain.dumps(comp, lanes))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- inputs ------------------------------------------------------------------------------------
def enabled_providers(s: Any, inv: detect.EnabledSet) -> list[str]:
    """Providers the operator enabled in setup (steps 1-5); detection only proposes."""
    names = [p for p, ps in (getattr(s, "providers", {}) or {}).items() if getattr(ps, "enabled", False)]
    sel = s.get_selection() if hasattr(s, "get_selection") else None
    if sel is not None:
        names += sel.enabled_providers()
    if getattr(s, "mode", "") == "multi-model" and getattr(s, "backend", "") == "openrouter":
        names.append("openrouter")
    if getattr(s, "mode", "") != "multi-model" and not names:
        # single-model: Claude Code's own login and no provider step ran. The set is what setup declared, so it
        # never widens to detected providers; the Claude cockpit's only verified provider is anthropic.
        names = ["anthropic"]
    return sorted(set(names))


def baseline_models(name: str = BASELINE_PROFILE) -> dict[str, str]:
    prof = profiles.load_profile(name)
    return {role: cfg.get("model", "") for role, cfg in (prof.get("by_role") or {}).items()}


def reference_profiles() -> dict[str, dict[str, str]]:
    refs: dict[str, dict[str, str]] = {}
    for name in REFERENCE_PROFILES:
        try:
            refs[name] = baseline_models(name)
        except (FileNotFoundError, RuntimeError):
            continue
    refs[CODEX_REFERENCE] = {role: CODEX_REFERENCE_MODEL for role in profiles.KNOWN_ROLES}
    return refs


def askable_pools(inv: detect.EnabledSet, enabled: list[str], db: scores_db.ScoresDB) -> list[dict]:
    """Pools the wizard asks about: present, dispatchable, serving a route of an enabled provider.

    Antigravity pools (direct use only) and Codex pools (not dispatchable) are never asked."""
    present = {p.id for p in inv.pools if p.present}
    out = []
    for p in db.pools:
        if p["id"] not in present or p.get("usage") != "dispatch-ok" or not p.get("routes"):
            continue
        provs = {db.routes.get(r, {}).get("provider") for r in p["routes"]}
        vendors = set(p.get("vendors") or ())
        if any(x in enabled for x in provs) or (provs == {"vendor"} and vendors & set(enabled)):
            out.append(p)
    return out


# The injected input each non-sidecar reader needs. No production fetcher exists for them in this kit (a
# network reader needs its own review), so a pool is offered for a live read only when the caller supplies one.
_FETCHER_DEP = {"openrouter-key": "openrouter_fetch", "vertex-quotas": "vertex_fetch"}


def has_live_reader(pool: Mapping[str, Any], reader_deps: Mapping[str, Any] | None = None) -> bool:
    """True only when a live read can actually work: the Claude status-line sidecar reader is built in;
    OpenRouter and Vertex readers need an injected fetcher, which production code does not supply today."""
    if pool.get("id") in headroom_sources.NO_READER_POOLS:
        return False
    source = pool.get("headroom_source")
    if source == "statusline-sidecar":
        return True
    dep = _FETCHER_DEP.get(str(source))
    return bool(dep and (reader_deps or {}).get(dep))


def default_sidecar_path() -> Path:
    """Where the operator's own status line may write its rate-limit JSON; the kit never creates it."""
    env = os.environ.get("AI_RESOURCES_CLAUDE_SIDECAR")
    return Path(env) if env else Path.home() / ".cache" / "ai-resources" / "claude-rate-limits.json"


def read_live(db: scores_db.ScoresDB, live: tuple[str, ...], reader_deps: Mapping[str, Any] | None,
              now_epoch: int | None) -> dict[str, hr.HeadroomReading]:
    deps = dict(reader_deps or {})
    deps.setdefault("claude_sidecar_path", default_sidecar_path())
    deps.setdefault("quota_blocks_path", quota_signal.default_path())
    out: dict[str, hr.HeadroomReading] = {}
    for pid in live:
        pool = db.pool(pid)
        if pool is not None:
            out[pid] = headroom_sources.read_for_pool(pool, True, deps, now_epoch)
    return out


def today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def build_input(s: Any, db: scores_db.ScoresDB, inv: detect.EnabledSet, req: Request, *, as_of: str | None = None,
                now_epoch: int | None = None, reader_deps: Mapping[str, Any] | None = None,
                base_name: str | None = None) -> ComposeInput:
    """`base_name` is the profile the composed team is stored over; the default-kept check judges that one."""
    baseline_id = BASELINE_PROFILE
    baseline = None
    if base_name and base_name != BASELINE_PROFILE:
        try:
            baseline, baseline_id = baseline_models(base_name), base_name
        except (FileNotFoundError, RuntimeError):
            baseline = None
    enabled = enabled_providers(s, inv)
    mode_key = compat.mode_key(getattr(s, "mode", "single-model"), getattr(s, "backend", None))
    pools = inv.pool_ids(present_only=True)
    gate = make_gate(db, cockpit="claude", mode_key=mode_key, allow_unverified=req.allow_unverified,
                     enabled_pools=pools)
    return ComposeInput(
        db=db, mode=req.mode, enabled=tuple(enabled), as_of=as_of or today(), gate=gate,
        allow_unverified=req.allow_unverified, accept_stale=req.accept_stale, fable_allowed=req.allow_fable,
        static_weights={p: hr.level_weight(l) for p, l in req.headroom.items()},
        readings=read_live(db, req.live, reader_deps, now_epoch), enabled_pools=tuple(pools), now_epoch=now_epoch,
        baseline=baseline if baseline is not None else baseline_models(), baseline_name=baseline_id, references=reference_profiles())


def run(s: Any, req: Request, *, deps: detect.Deps | None = None, db: scores_db.ScoresDB | None = None,
        as_of: str | None = None, now_epoch: int | None = None,
        reader_deps: Mapping[str, Any] | None = None,
        base_name: str | None = None) -> tuple[Composition, ComposeInput]:
    db = db or scores_db.load()
    inv = detect.inventory(deps or detect.default_deps(), db)
    inp = build_input(s, db, inv, req, as_of=as_of, now_epoch=now_epoch, reader_deps=reader_deps,
                      base_name=base_name)
    return compose(inp), inp


def check_mode(req: Request, enabled: list[str]) -> str | None:
    """A refusal message for a mode that must not be offered, else None."""
    if req.mode not in MODES:
        return f"unknown mode {req.mode!r}; use one of {', '.join(MODES)}"
    if req.mode == "mixed":
        if len(enabled) < 2:
            return "mixed mode needs two or more enabled providers"
        if not req.allow_unverified:
            return MIXED_REFUSAL
    return None


# --- single-model aliases ------------------------------------------------------------------------
def to_aliases(customizations: dict[str, dict]) -> dict[str, dict]:
    """claude-native names Claude aliases (opus, sonnet, haiku, fable), not canonical ids."""
    out: dict[str, dict] = {}
    for role, entry in customizations.items():
        model = str(entry.get("model", ""))
        family = model.rsplit("claude-", 1)[-1].split("-", 1)[0] if "claude-" in model else ""
        if family in ("opus", "sonnet", "haiku", "fable"):
            out[role] = {"model": family}
    return out
