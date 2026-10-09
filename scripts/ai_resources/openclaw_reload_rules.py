"""Which `openclaw.json` keys make a live OpenClaw gateway restart when they change.

`openclaw config patch --dry-run --json` has no restart field, and the rule table inside the
installed package lives in a hashed `dist/` file, so the kit pins a copy of it here (ADR-0003).
The copy is only trusted for the OpenClaw version it was read from: any other version, an
unknown version, `gateway.reload.mode: off`, or a path no rule matches is classified
`restart` (fail closed). A wrong "restart" costs a deferred key; a wrong "hot" costs a forced
restart that cuts runs in flight (T40).

Refresh after an OpenClaw upgrade: update `RULES` and `PINNED_OPENCLAW_VERSION` from
`dist/config-reload-plan-*.mjs` (CORE_RELOAD_POLICIES then DEFAULT_RELOAD_POLICIES, in source
order) and `tests/fixtures/openclaw_reload_rules_<version>.json` with them. The drift test in
`tests/test_openclaw_reload_rules.py` says when this is needed.
"""
from __future__ import annotations

import copy
from typing import Callable

PINNED_OPENCLAW_VERSION = "2026.9.9"


class RestartRequired(Exception):
    """A write was refused because it touches restart-required keys (or keys that cannot be
    classified). Raised BEFORE any CLI call, so an ignored return value cannot skip it silently."""

    def __init__(self, paths: list[str]):
        super().__init__("restart-required config keys: " + ", ".join(paths))
        self.paths = list(paths)

# (prefix, kind) in the source order of the dist: CORE_RELOAD_POLICIES then DEFAULT_RELOAD_POLICIES.
# kind: "restart" | "hot" | "none". Longest prefix wins (OpenClaw's compareReloadRules).
RULES: tuple[tuple[str, str], ...] = (
    ("gateway.remote", "none"),
    ("gateway.reload", "none"),
    ("gateway.auth.token", "restart"),
    ("gateway.auth.password", "restart"),
    ("mcp.apps", "restart"),
    ("secrets.egressProxy", "restart"),
    ("gateway.portals", "restart"),
    ("gateway.http.endpoints", "hot"),
    ("gateway.http.securityHeaders.strictTransportSecurity", "hot"),
    ("gateway.tools", "hot"),
    ("gateway.uploads", "hot"),
    ("gateway.cliAgents", "hot"),
    ("gateway.controlUi.enabled", "hot"),
    ("gateway.controlUi.environment", "hot"),
    ("gateway.controlUi.communityInvite", "hot"),
    ("gateway.controlUi.newSessionModelDefaults", "hot"),
    ("gateway.controlUi.github", "hot"),
    ("gateway.controlUi.sessionObserver", "hot"),
    ("gateway.controlUi.embedSandbox", "hot"),
    ("gateway.controlUi.allowExternalEmbedUrls", "hot"),
    ("gateway.controlUi.automaticallyFetchFavicons", "hot"),
    ("gateway.controlUi.experimental.customPlugins", "hot"),
    ("gateway.controlUi.allowedOrigins", "hot"),
    ("gateway.controlUi.dangerouslyAllowHostHeaderOriginFallback", "hot"),
    ("gateway.nodes.browser", "hot"),
    ("gateway.nodes.pairing", "hot"),
    ("gateway.nodes.commands", "hot"),
    ("gateway.nodes.pluginTools.enabled", "hot"),
    ("gateway.nodes.allowSkills", "hot"),
    ("gateway.push.apns.relay", "hot"),
    ("gateway.terminal", "hot"),
    ("gateway.auth.rateLimit", "hot"),
    ("gateway.roles", "hot"),
    ("gateway.trustedProxies", "hot"),
    ("gateway.allowRealIpFallback", "hot"),
    ("gateway.auth.allowTailscale", "hot"),
    ("gateway.auth.identityScopes", "hot"),
    ("gateway.auth.trustedProxy", "hot"),
    ("diagnostics.enabled", "hot"),
    ("discovery.mdns.mode", "hot"),
    ("mcp.apps.sandboxOrigin", "hot"),
    ("agents.defaults", "hot"),
    ("desktop.host", "hot"),
    ("cloudWorkers", "hot"),
    ("hooks.gmail", "hot"),
    ("hooks.internal", "hot"),
    ("agents.defaults.workspace", "hot"),
    ("hooks", "hot"),
    ("agents.defaults.heartbeat", "hot"),
    ("agents.defaults.models", "hot"),
    ("agents.defaults.modelPolicy", "hot"),
    ("agents.defaults.model", "hot"),
    ("models", "hot"),
    ("agent.heartbeat", "hot"),
    ("agents.entries", "hot"),
    ("agents.entries.*.decisionModel", "hot"),
    ("agents.defaults.sessionStore", "hot"),
    ("agents.ownership", "hot"),
    ("skills.workshop.autonomous.mode", "hot"),
    ("agents.defaults.decisionModel", "hot"),
    ("plugins.load", "hot"),
    ("plugins.installs", "hot"),
    ("cron", "hot"),
    ("transcripts", "hot"),
    ("cloudWorkers.profiles", "hot"),
    ("mcp", "hot"),
    ("gateway.publicOrigin", "hot"),
    ("talk.provider", "hot"),
    ("talk.realtime.provider", "hot"),
    ("meta", "none"),
    ("identity", "none"),
    ("wizard", "none"),
    ("logging", "none"),
    ("agents", "none"),
    ("bindings", "none"),
    ("audio", "none"),
    ("agent", "none"),
    ("routing", "none"),
    ("messages", "none"),
    ("session", "none"),
    ("talk", "none"),
    ("skills", "none"),
    ("secrets", "none"),
    ("tui", "none"),
    ("ui", "none"),
    ("tools", "hot"),
    ("approvals.exec", "hot"),
    ("approvals.plugin", "hot"),
    ("auth.order", "hot"),
    ("auth.profiles", "hot"),
    ("broadcast", "hot"),
    ("memory.citations", "hot"),
    ("worktreeRoot", "hot"),
    ("worktreeAcceleration", "hot"),
    ("security.audit.suppressions", "hot"),
    ("security.installPolicy", "hot"),
    ("diagnostics.cacheTrace.enabled", "hot"),
    ("acp", "hot"),
    ("attachments.ttlHours", "hot"),
    ("update.checkOnStart", "hot"),
    ("update.channel", "hot"),
    ("update.auto.enabled", "hot"),
    ("telemetry.enabled", "hot"),
    ("telemetry.consentedAt", "hot"),
    ("plugins", "hot"),
    ("channels", "hot"),
    ("gateway", "restart"),
    ("discovery", "restart"),
)

RESTART = "restart"
HOT = "hot"
NONE = "none"


def _specificity(prefix: str) -> tuple[int, int]:
    segs = prefix.split(".")
    return len(segs), -sum(1 for s in segs if s == "*")


def _prefix_matches(prefix: str, path: str) -> bool:
    ps, xs = prefix.split("."), path.split(".")
    if len(ps) > len(xs):
        return False
    return all(p == "*" or p == x for p, x in zip(ps, xs))


def leaf_paths(patch: dict) -> list[str]:
    """Dotted path of every leaf a patch sets. A None (delete) leaf counts; a list is one leaf."""
    out: list[str] = []

    def walk(node, path: list[str]) -> None:
        if isinstance(node, dict) and node:
            for k, v in node.items():
                walk(v, path + [str(k)])
        elif path:
            out.append(".".join(path))

    walk(patch, [])
    return out


def _kind_of(path: str) -> str | None:
    best: tuple[tuple[int, int], str] | None = None
    for prefix, kind in RULES:  # source order breaks ties, as OpenClaw's stable sort does
        if _prefix_matches(prefix, path):
            spec = _specificity(prefix)
            if best is None or spec > best[0]:
                best = (spec, kind)
    return best[1] if best else None


def classify(paths: list[str], *, openclaw_version: str | None,
             reload_mode: str | None = None) -> dict[str, str]:
    """{path: "restart" | "hot" | "none"}. Fails closed: see the module docstring."""
    trusted = openclaw_version == PINNED_OPENCLAW_VERSION and reload_mode != "off"
    out: dict[str, str] = {}
    for p in paths:
        kind = _kind_of(p) if trusted else None
        out[p] = kind or RESTART
    return out


def restart_paths(patch: dict, *, openclaw_version: str | None,
                  reload_mode: str | None = None) -> list[str]:
    kinds = classify(leaf_paths(patch), openclaw_version=openclaw_version, reload_mode=reload_mode)
    return [p for p, k in kinds.items() if k == RESTART]


def _covers(restart: list[str], dotted: str) -> bool:
    return any(dotted == r or dotted.startswith(r + ".") or r.startswith(dotted + ".")
               for r in restart)


def split_patch(patch: dict, restart: list[str]) -> tuple[dict, dict]:
    """(hot_patch, restart_patch): disjoint, and their deep-merge equals `patch`."""
    rset = set(restart)

    def walk(node, path: list[str]):
        if isinstance(node, dict) and node:
            hot: dict = {}
            res: dict = {}
            for k, v in node.items():
                h, r = walk(v, path + [str(k)])
                if h is not _ABSENT:
                    hot[k] = h
                if r is not _ABSENT:
                    res[k] = r
            return (hot if hot else _ABSENT), (res if res else _ABSENT)
        dotted = ".".join(path)
        if dotted in rset:
            return _ABSENT, node
        return node, _ABSENT

    hot, res = walk(patch, [])
    return (hot if hot is not _ABSENT else {}), (res if res is not _ABSENT else {})


class _Absent:
    pass


_ABSENT = _Absent()


def split_replace_paths(replace_paths: list[str] | None, restart: list[str]) -> tuple[list[str], list[str]]:
    """Split `--replace-path` values the same way: one that touches a restart path goes with it."""
    hot: list[str] = []
    res: list[str] = []
    for rp in (replace_paths or []):
        (res if _covers(restart, rp) else hot).append(rp)
    return hot, res


def split_changes(changes: list[dict], restart: list[str]) -> tuple[list[dict], list[dict]]:
    """Split change records (`path` is a list of segments) into (hot, restart)."""
    hot: list[dict] = []
    res: list[dict] = []
    for ch in changes:
        dotted = ".".join(ch["path"])
        (res if _covers(restart, dotted) else hot).append(ch)
    return hot, res


def deep_merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# Keyed by the runner object (kept alive in the value) so a fake runner in a test never sees a
# version cached for another one; the real runner is one object per process.
_VERSION_CACHE: dict[int, tuple[object, str | None]] = {}


def read_installed_version(runner: Callable[..., "tuple[int, str]"], *, refresh: bool = False) -> str | None:
    """`openclaw --version` parsed to e.g. "2026.9.9"; None when it cannot be read. Cached per runner."""
    import re
    hit = _VERSION_CACHE.get(id(runner))
    if hit is not None and hit[0] is runner and not refresh:
        return hit[1]
    rc, out = runner(["openclaw", "--version"], timeout=30)
    m = re.search(r"\b(\d{4}\.\d+\.\d+)\b", out or "") if rc == 0 else None
    version = m.group(1) if m else None
    _VERSION_CACHE[id(runner)] = (runner, version)
    return version


# The name callers use. The test suite replaces THIS name (conftest) so a fake `openclaw` need not
# answer `--version`; `read_installed_version` stays the real implementation.
installed_openclaw_version = read_installed_version


def reload_mode_of(doc: dict | None) -> str | None:
    """`gateway.reload.mode` of a config document, or None."""
    cur = doc or {}
    for k in ("gateway", "reload", "mode"):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur if isinstance(cur, str) else None
