"""Which `openclaw.json` keys make a live OpenClaw gateway restart when they change.

`openclaw config patch --dry-run --json` has no restart field, so the kit reads the reload-rule table
out of the installed package (`dist/config-reload-plan-*.mjs`, a file read: no gateway, no `node`, no
subprocess) and classifies keys from THAT table. It never compares the installed version with a
constant: the rules follow whatever OpenClaw is installed right now (ADR-0003, amended).

The read is strict and fails closed. The parser returns None unless every policy object and every
prefix has the exact shape it understands; the classifier then calls every key `restart`. The same
goes for `gateway.reload.mode: off` and for a path no rule matches. A wrong "restart" costs a deferred
key; a wrong "hot" costs a forced restart that cuts runs in flight (T40).

`BASELINE_RULES` is the table the kit was written against (read from OpenClaw `BASELINE_READ_FROM`; it
is documentation, compared with nothing). Its `restart` entries always win over the installed table
("restart dominance"): a key the installed OpenClaw now calls hot stays restart-required until a
developer moves the baseline, and `status` lists those prefixes. A key that became restart-required
in the installed OpenClaw is restart immediately, because the installed table is what is classified.

Not modelled, and always on the conservative side: `canHotReloadGatewayAuthCredentials`, the
`gateway.roles` model-policy noop and the channel plugins' runtime `configPrefixes` all make OpenClaw
LESS restart-happy than the static table, so the kit may defer a key that would have been applied live.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Callable

# The OpenClaw whose dist table `BASELINE_RULES` was copied from. Documentation only.
BASELINE_READ_FROM = "2026.10.3"


class RestartRequired(Exception):
    """A write was refused because it touches restart-required keys (or keys that cannot be
    classified). Raised BEFORE any CLI call, so an ignored return value cannot skip it silently."""

    def __init__(self, paths: list[str]):
        super().__init__("restart-required config keys: " + ", ".join(paths))
        self.paths = list(paths)

# (prefix, kind) in the source order of the dist: CORE_RELOAD_POLICIES then DEFAULT_RELOAD_POLICIES.
# kind: "restart" | "hot" | "none". Longest prefix wins (OpenClaw's compareReloadRules).
BASELINE_RULES: tuple[tuple[str, str], ...] = (
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


def _kind_of(path: str, rules: "tuple[tuple[str, str], ...]") -> str | None:
    best: tuple[tuple[int, int], str] | None = None
    for prefix, kind in rules:  # source order breaks ties, as OpenClaw's stable sort does
        if _prefix_matches(prefix, path):
            spec = _specificity(prefix)
            if best is None or spec > best[0]:
                best = (spec, kind)
    return best[1] if best else None


_KINDS = (RESTART, HOT, NONE)
_PREFIX_RE = re.compile(r"[A-Za-z0-9_-]+(\.([A-Za-z0-9_-]+|\*))*")
MIN_RULES = 40
# The real dist is a few KB. A bigger input is not a reload table, and the cap bounds the work.
MAX_SOURCE_BYTES = 256 * 1024

# Tokens of the linear scanner. Each is a flat pattern anchored with `.match(src, pos)`: no nested
# quantifier and no alternation whose branches can match the same text, so the scan never backtracks.
_WS = re.compile(r"\s*")
_STR_TOKEN = re.compile(r'"([^"\\\n]*)"')
_SPREAD_TOKEN = re.compile(r"\.\.\.AUTH_CREDENTIAL_PATHS(?![A-Za-z0-9_$])")
_IDENT_TOKEN = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_CONST_TOKEN = re.compile(r"[A-Z][A-Z0-9_]*(?![A-Za-z0-9_$])")
_ANY_SPREAD_TOKEN = re.compile(r"\.\.\.[A-Z][A-Z0-9_]*(?![A-Za-z0-9_$])")
_TRUE_TOKEN = re.compile(r"true(?![A-Za-z0-9_$])")
_HEADER = r"^const %s = \["


class _Scan:
    """A cursor over `src`. Every method returns None / False on a mismatch and never backtracks."""

    def __init__(self, src: str, pos: int = 0):
        self.src, self.pos = src, pos

    def ws(self) -> None:
        self.pos = _WS.match(self.src, self.pos).end()  # type: ignore[union-attr]

    def lit(self, text: str) -> bool:
        self.ws()
        if self.src.startswith(text, self.pos):
            self.pos += len(text)
            return True
        return False

    def ident(self) -> "str | None":
        self.ws()
        m = _IDENT_TOKEN.match(self.src, self.pos)
        if m is None:
            return None
        self.pos = m.end()
        return m.group(0)

    def string(self) -> "str | None":
        self.ws()
        m = _STR_TOKEN.match(self.src, self.pos)
        if m is None:
            return None
        self.pos = m.end()
        return m.group(1)

    def key(self, name: str) -> bool:
        return self.ident() == name and self.lit(":")

    def at_end(self) -> bool:
        self.ws()
        return self.pos >= len(self.src)

    def list_items(self, auth: "list[str] | None", *, any_const: bool = False) -> "list[str] | None":
        """Items of `[ ... ]` (the `[` already consumed): string literals, and the AUTH spread when
        `auth` is given (or the spread of any UPPER_CASE constant when `any_const`, for the inert
        `actions` list). Items are comma separated, one trailing comma is allowed. None on any other
        token."""
        items: list[str] = []
        self.ws()
        if self.src.startswith("]", self.pos):
            self.pos += 1
            return items
        while True:
            self.ws()
            m = _SPREAD_TOKEN.match(self.src, self.pos) if auth is not None else None
            if m is None and any_const:
                m = _ANY_SPREAD_TOKEN.match(self.src, self.pos)
            if m is not None:
                self.pos = m.end()
                items.extend(auth or [])
            else:
                tok = self.string()
                if tok is None:
                    return None
                items.append(tok)
            if self.lit("]"):
                return items
            if not self.lit(","):
                return None
            if self.lit("]"):
                return items


def _single_block(src: str, name: str, end: str) -> "str | None":
    heads = list(re.finditer(_HEADER % name, src, re.M))
    if len(heads) != 1:
        return None
    start = heads[0].end()
    stop = src.find(end, start)
    return None if stop < 0 else src[start:stop]


def _actions_value(sc: "_Scan") -> bool:
    """The inert value of `actions:` : `[ "a", ...SOME_CONST ]` or a bare UPPER_CASE constant."""
    sc.ws()
    if sc.src.startswith("[", sc.pos):
        sc.pos += 1
        return sc.list_items(None, any_const=True) is not None
    m = _CONST_TOKEN.match(sc.src, sc.pos)
    if m is None:
        return False
    sc.pos = m.end()
    return True


def _parse_policies(block: str, auth: "list[str]") -> "list[tuple[str, str]] | None":
    """The (prefix, kind) pairs of one policy array body, or None.

    Each object must be exactly `{ prefixes: [...], kind: "..." }`, optionally followed by the two
    data-only fields OpenClaw's dist carries, in this order: `actions` (a list of string literals and
    UPPER_CASE constant spreads, or one UPPER_CASE constant) and `replaceChannelPlugins: true`. Any other field (a `when:` predicate, a function, a spread) makes
    the whole table unreadable: an object the kit cannot fully read must never classify a key hot.
    """
    sc = _Scan(block)
    out: list[tuple[str, str]] = []
    n_objects = 0
    while not sc.at_end():
        if n_objects and not sc.lit(","):
            return None
        if sc.at_end():                       # a trailing comma after the last object
            break
        if not sc.lit("{") or not sc.key("prefixes") or not sc.lit("["):
            return None
        prefixes = sc.list_items(auth)
        if prefixes is None or not sc.lit(",") or not sc.key("kind"):
            return None
        kind = sc.string()
        if kind is None or kind not in _KINDS:
            return None
        if sc.lit(","):
            if sc.lit("}"):                   # trailing comma before the closing brace
                pass
            else:
                if not sc.key("actions") or not _actions_value(sc):
                    return None
                if sc.lit(","):
                    if not sc.lit("}"):
                        if not sc.key("replaceChannelPlugins"):
                            return None
                        sc.ws()
                        m = _TRUE_TOKEN.match(sc.src, sc.pos)
                        if m is None:
                            return None
                        sc.pos = m.end()
                        sc.lit(",")
                        if not sc.lit("}"):
                            return None
                elif not sc.lit("}"):
                    return None
        elif not sc.lit("}"):
            return None
        n_objects += 1
        for prefix in prefixes:
            if not _PREFIX_RE.fullmatch(prefix):
                return None
            out.append((prefix, kind))
    return out if n_objects else None


def parse_reload_rules(src: str) -> "tuple[tuple[str, str], ...] | None":
    """The (prefix, kind) table in `src` (a `config-reload-plan-*.mjs`), or None. Never raises.

    None unless the three const blocks are each found exactly once, every policy object is exactly
    `{ prefixes: [...], kind: "..." }` (plus the data-only `actions` / `replaceChannelPlugins` fields
    the dist carries; any other field is refused), every prefix is a string literal or the spread of
    AUTH_CREDENTIAL_PATHS, every kind and prefix is well formed, the input is under MAX_SOURCE_BYTES
    and the table is not implausibly small. A parse the kit does not fully understand must never
    classify a key hot. The scan is linear in the size of `src`.
    """
    try:
        return _parse_reload_rules(src)
    except Exception:  # noqa: BLE001 - fail closed on anything unforeseen
        return None


def _parse_reload_rules(src: str) -> "tuple[tuple[str, str], ...] | None":
    if not isinstance(src, str) or len(src) > MAX_SOURCE_BYTES:
        return None
    auth_body = _single_block(src, "AUTH_CREDENTIAL_PATHS", "];")
    if auth_body is None:
        return None
    sc = _Scan(auth_body + "]")
    auth = sc.list_items(None)
    if auth is None or not sc.at_end():
        return None
    rules: list[tuple[str, str]] = []
    for name in ("CORE_RELOAD_POLICIES", "DEFAULT_RELOAD_POLICIES"):
        block = _single_block(src, name, "\n];")
        if block is None:
            return None
        parsed = _parse_policies(block, auth)
        if parsed is None:
            return None
        rules.extend(parsed)
    if len(rules) < MIN_RULES:
        return None
    have = set(rules)
    if ("gateway", RESTART) not in have or ("discovery", RESTART) not in have:
        return None
    return tuple(rules)


@dataclass(frozen=True)
class RuleSet:
    """The reload rules of the installed OpenClaw. `rules` is what `classify` uses (installed table
    plus the baseline's restart entries); None when the installed table could not be read."""
    version: str | None
    source: str                                   # "installed" | "unavailable"
    rules: "tuple[tuple[str, str], ...] | None" = None
    reason: str = ""
    dist: str | None = None
    stricter: tuple[str, ...] = ()
    installed_count: int = 0


def _merge_baseline(installed: "tuple[tuple[str, str], ...]") -> "tuple[tuple[tuple[str, str], ...], tuple[str, ...]]":
    """(effective rules, prefixes where the kit is stricter): the baseline's `restart` entries win."""
    restart_prefixes = [p for p, k in BASELINE_RULES if k == RESTART]
    rset = set(restart_prefixes)
    out: list[tuple[str, str]] = []
    stricter: list[str] = []
    for p, k in installed:
        # A hot/none installed entry the baseline resolves to restart (the exact prefix, or a new more
        # specific one nested under a baseline restart catch-all) stays restart: the kit wins.
        if k in (HOT, NONE) and (p in rset or _kind_of(p, BASELINE_RULES) == RESTART):
            out.append((p, RESTART))
            stricter.append(p)
        else:
            out.append((p, RESTART if p in rset else k))
    present = {p for p, _k in installed}
    for p in dict.fromkeys(restart_prefixes):
        ik = _kind_of(p, installed)
        if ik in (HOT, NONE) and p not in stricter:
            stricter.append(p)
        if p not in present and ik != RESTART:
            out.append((p, RESTART))
    return tuple(out), tuple(dict.fromkeys(stricter))


def ruleset_from_source(src: str, *, version: str | None, dist: str | None = None) -> RuleSet:
    """A RuleSet from the text of a reload-plan dist file (unavailable when it does not parse)."""
    installed = parse_reload_rules(src)
    if installed is None:
        return RuleSet(version, "unavailable", None,
                       "the installed OpenClaw's reload table is not in a shape the kit understands", dist)
    rules, stricter = _merge_baseline(installed)
    return RuleSet(version, "installed", rules, "", dist, stricter, len(installed))


def unavailable(version: str | None, reason: str) -> RuleSet:
    return RuleSet(version, "unavailable", None, reason)


def _locate_dist(package_json) -> "tuple[object | None, str]":
    if package_json is None:
        return None, "the openclaw package was not found next to the openclaw on PATH"
    try:
        found = sorted((package_json.parent / "dist").glob("config-reload-plan-*.mjs"))
    except OSError as e:
        return None, f"the package dist directory cannot be read ({e.__class__.__name__})"
    if not found:
        return None, "no dist/config-reload-plan-*.mjs in the installed package"
    if len(found) > 1:
        return None, "several dist/config-reload-plan-*.mjs files in the installed package"
    return found[0], ""


def locate_reload_plan_dist(package_json) -> "object | None":
    """`<package dir>/dist/config-reload-plan-*.mjs` when there is exactly one, else None."""
    return _locate_dist(package_json)[0]


# dist path -> ((mtime_ns, size), parsed rules or None); an in-place upgrade changes mtime or size.
_PARSE_CACHE: dict[str, tuple[tuple[int, int], "tuple | None"]] = {}


def _read_installed_rules(dist) -> "tuple[tuple | None, str]":
    try:
        st = dist.stat()
    except OSError as e:
        return None, f"the reload table cannot be read ({e.__class__.__name__})"
    key = (st.st_mtime_ns, st.st_size)
    hit = _PARSE_CACHE.get(str(dist))
    if hit is not None and hit[0] == key:
        parsed = hit[1]
    else:
        try:
            src = dist.read_text(encoding="utf-8")
        except (OSError, ValueError) as e:
            return None, f"the reload table cannot be read ({e.__class__.__name__})"
        parsed = parse_reload_rules(src)
        _PARSE_CACHE[str(dist)] = (key, parsed)
    if parsed is None:
        return None, "the installed OpenClaw's reload table is not in a shape the kit understands"
    return parsed, ""


def current_ruleset(runner: Callable[..., "tuple[int, str]"], *, refresh: bool = False) -> RuleSet:
    """The reload rules of the OpenClaw installed right now. Never raises; unreadable means
    `source == "unavailable"` and every key is then restart-required."""
    try:
        version = installed_openclaw_version(runner, refresh=refresh)
    except Exception:  # noqa: BLE001 - a version that cannot be read is information, not a failure
        version = None
    try:
        dist, why = _locate_dist(_find_package_json())
        if dist is None:
            return unavailable(version, why)
        installed, why = _read_installed_rules(dist)
        if installed is None:
            return RuleSet(version, "unavailable", None, why, str(dist))
        rules, stricter = _merge_baseline(installed)
        return RuleSet(version, "installed", rules, "", str(dist), stricter, len(installed))
    except Exception as e:  # noqa: BLE001 - fail closed
        return unavailable(version, f"the reload table cannot be read ({e.__class__.__name__})")


def classify(paths: list[str], *, ruleset: "RuleSet | None",
             reload_mode: str | None = None) -> dict[str, str]:
    """{path: "restart" | "hot" | "none"}. Fails closed: see the module docstring."""
    rules = ruleset.rules if ruleset is not None and reload_mode != "off" else None
    out: dict[str, str] = {}
    for p in paths:
        kind = _kind_of(p, rules) if rules is not None else None
        out[p] = kind or RESTART
    return out


def restart_paths(patch: dict, *, ruleset: "RuleSet | None",
                  reload_mode: str | None = None) -> list[str]:
    kinds = classify(leaf_paths(patch), ruleset=ruleset, reload_mode=reload_mode)
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


_VERSION_RE = r"\b(\d{4}\.\d+\.\d+)\b"


def locate_package_json() -> "Path | None":
    """The `package.json` next to the `openclaw` entry point the PATH resolves, or None.

    Reading the file needs no gateway and no subprocess: the CLI answers slowly (or not at all) while the
    gateway is under memory pressure, and a None version makes every key restart-required (E3).
    """
    import os
    from pathlib import Path
    from .setup import detection
    found = detection._which_extra("openclaw")      # PATH first, then Homebrew / npm-global / fnm bins
    if not found:
        return None
    pj = Path(os.path.realpath(found)).parent / "package.json"
    return pj if pj.is_file() else None


# The name read_installed_version calls; the test suite replaces THIS name (conftest).
_find_package_json = locate_package_json


def _version_from_package_json(path) -> str | None:
    import json
    import re
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("name") != "openclaw":
        return None
    v = data.get("version")
    return v if isinstance(v, str) and re.fullmatch(r"\d{4}\.\d+\.\d+", v) else None


def read_installed_version(runner: Callable[..., "tuple[int, str]"], *, refresh: bool = False) -> str | None:
    """The installed OpenClaw version, e.g. "2026.9.9"; None when it cannot be read. Cached per runner.

    The installed package's `package.json` comes first (no gateway needed); `openclaw --version` is
    the fallback. Garbage in either gives None, so the fail-closed rule is unchanged.
    """
    import re
    pj = _find_package_json()
    if pj is not None:
        from_file = _version_from_package_json(pj)
        if from_file:
            return from_file
    hit = _VERSION_CACHE.get(id(runner))
    if hit is not None and hit[0] is runner and not refresh:
        return hit[1]
    rc, out = runner(["openclaw", "--version"], timeout=30)
    m = re.search(_VERSION_RE, out or "") if rc == 0 else None
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
