"""The model scores database: loader, validator, user overrides, prices and staleness.

The kit ships `scores/model-scores.yaml` read-only. A user override file
(`$XDG_CONFIG_HOME/ai-resources/model-scores.yaml`) shadows rows by key; it never edits the shipped file.

I/O is confined to `load_shipped`, `load_overrides` and `default_override_path`. There is no hidden clock:
every date is an argument. Verification is never stored here: only `setup/compat.py` knows it.
"""
from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    import yaml
except ImportError:  # pragma: no cover - the kit requires PyYAML
    yaml = None  # type: ignore

from .evidence import MEASURED_LABELS, ROW_LABELS, Label, is_known_spelling, normalise

SCHEMA_VERSION = 1
DEFAULT_EXPIRES_AFTER_DAYS = 90
USAGE_VALUES = ("dispatch-ok", "direct-only", "instructions-only")
BILLING_CLASSES = ("subscription", "metered-api", "gateway-metered")
HEADROOM_SOURCES = ("statusline-sidecar", "openrouter-key", "vertex-quotas", "kit-counters", "none")
MODEL_STATUSES = ("stable", "preview", "retired")
ROUTES = ("anthropic-direct", "vertex-global", "google-ai-studio", "openrouter", "litellm",
          "openclaw-codex-engine")
MEASURED_HERE_FIELDS = ("run_id", "n", "harness_version", "corpus_hash", "low_power")
# Vendor prefix of a canonical id -> the kit's provider id.
VENDOR_PROVIDER = {"anthropic": "anthropic", "google": "google", "openai": "openai", "deepseek": "deepseek",
                   "moonshotai": "moonshot", "moonshot": "moonshot"}


# --- plain records -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Row:
    role: str
    model: str
    route: str
    effort: str
    score: float | None
    label: Label
    as_of: str
    source_ref: str
    basis: str = ""
    expires_after_days: int = DEFAULT_EXPIRES_AFTER_DAYS
    conditions: str = ""
    notes: str = ""
    quota_pool: str = ""
    override: bool = False
    override_reason: str = ""

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.role, self.model, self.route, self.effort)


@dataclass(frozen=True)
class Price:
    in_price: float | None
    out_price: float | None
    cache_read: float | None
    tier: str = "standard"            # standard | long-context
    verify: bool = False
    source: str = ""
    as_of: str = ""
    valid_from: str | None = None
    valid_until: str | None = None
    approximate: bool = False
    notes: str = ""


def parse_date(text: object) -> _dt.date | None:
    if isinstance(text, _dt.datetime):
        return text.date()
    if isinstance(text, _dt.date):
        return text
    if isinstance(text, str):
        try:
            return _dt.date.fromisoformat(text.strip())
        except ValueError:
            return None
    return None


def _iso(text: object) -> str:
    d = parse_date(text)
    return d.isoformat() if d else ""


# --- the database ------------------------------------------------------------------------------
class ScoresDB:
    """A parsed, indexed view over a scores document (shipped, or shipped plus overrides)."""

    def __init__(self, data: Mapping[str, Any], problems: Iterable[str] = ()):
        self.data = dict(data)
        self.problems = tuple(problems)
        self.dataset_version = str(self.data.get("dataset_version", ""))
        self.policy: dict = dict(self.data.get("policy") or {})
        self.routes: dict = dict(self.data.get("routes") or {})
        self.surfaces: dict = dict(self.data.get("surfaces") or {})
        self.pools: tuple[dict, ...] = tuple(self.data.get("pools") or ())
        self.models: dict = dict(self.data.get("models") or {})
        self.aliases: dict[str, str] = {}
        for a in self.data.get("aliases") or ():
            if isinstance(a, dict) and a.get("alias") and a.get("target"):
                self.aliases[str(a["alias"])] = str(a["target"])
        self.rows: tuple[Row, ...] = tuple(_expand_rows(self.data.get("rows") or ()))
        self._by_role: dict[str, list[Row]] = {}
        for r in self.rows:
            self._by_role.setdefault(r.role, []).append(r)

    # -- lookups --
    def roles(self) -> list[str]:
        return list((self.policy.get("importance") or {}).keys())

    def importance(self, role: str) -> float:
        return float((self.policy.get("importance") or {}).get(role, 0.0))

    def tolerance_class(self, role: str) -> str:
        return str((self.policy.get("role_class") or {}).get(role, "standard"))

    def tolerance(self, role: str) -> float:
        classes = self.policy.get("tolerance_classes") or {}
        return float(classes.get(self.tolerance_class(role), classes.get("standard", 0.5)))

    def load_weight(self, role: str) -> float:
        return float((self.policy.get("load_weight") or {}).get(role, 1.0))

    def rows_for(self, role: str) -> list[Row]:
        return list(self._by_role.get(role, ()))

    def resolve_model(self, model: str) -> str:
        return self.aliases.get(model, model)

    def vendor_of(self, model: str) -> str:
        return model.split("/", 1)[0] if "/" in model else str((self.models.get(model) or {}).get("vendor", ""))

    def route_id(self, model: str, route: str) -> str | None:
        ids = (self.models.get(model) or {}).get("route_ids") or {}
        return ids.get(route)

    def routable(self, model: str, route: str) -> bool:
        """True when the id the route uses is in the kit's own routing lists (providers.KNOWN_MODELS).

        Computed from code, never stored: a model the kit does not list cannot be written to a profile that
        the setup flow would later offer or smoke-test."""
        return routable_in_kit(self.route_id(model, route), self.route_list_key(model, route))

    def route_list_key(self, model: str, route: str) -> str:
        spec = self.routes.get(route) or {}
        provider = spec.get("provider", "")
        if provider == "vendor":
            return VENDOR_PROVIDER.get(self.vendor_of(model), self.vendor_of(model))
        return str(provider)

    def required_provider(self, model: str, route: str) -> str:
        """The provider id that must be enabled in setup for this (model, route)."""
        return self.route_list_key(model, route)

    def compat_provider(self, model: str, route: str) -> str:
        """The compat-matrix provider column for this (model, route): the vendor, except Vertex."""
        spec = self.routes.get(route) or {}
        if spec.get("provider") == "vertex":
            return "vertex"
        return VENDOR_PROVIDER.get(self.vendor_of(model), self.vendor_of(model))

    def surface_of(self, route: str) -> str:
        return str((self.routes.get(route) or {}).get("surface", ""))

    def pool_for(self, vendor: str, route: str, enabled: Iterable[str] = ()) -> str:
        """Pool id for a vendor on a route: the first listed pool that covers both, preferring an enabled one."""
        enabled = tuple(enabled)
        matches = [p for p in self.pools
                   if route in (p.get("routes") or ()) and (not p.get("vendors") or vendor in p["vendors"])]
        for p in matches:
            if p["id"] in enabled:
                return str(p["id"])
        return str(matches[0]["id"]) if matches else ""

    def pool(self, pool_id: str) -> dict | None:
        for p in self.pools:
            if p.get("id") == pool_id:
                return p
        return None

    # -- staleness and prices --
    def is_stale(self, row: Row, as_of: str) -> bool:
        return is_stale(row, as_of)

    def price_at(self, model: str, date: str, prompt_tokens: int = 0, route: str | None = None) -> Price | None:
        return price_at(self.models.get(self.resolve_model(model)) or {}, date, prompt_tokens, route)


def routable_in_kit(bare_id: str | None, list_key: str) -> bool:
    if not bare_id:
        return False
    from ..setup import providers
    return bare_id in providers.KNOWN_MODELS.get(list_key, [])


def _expand_rows(raw: Iterable[Any]) -> Iterable[Row]:
    for r in raw:
        if not isinstance(r, dict):
            continue
        routes = r.get("route")
        routes = list(routes) if isinstance(routes, (list, tuple)) else [routes]
        score = r.get("score")
        for route in routes:
            yield Row(role=str(r.get("role", "")), model=str(r.get("model", "")), route=str(route),
                      effort=str(r.get("effort", "default")),
                      score=float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
                      label=normalise(r.get("label")), as_of=_iso(r.get("as_of")) or str(r.get("as_of", "")),
                      source_ref=str(r.get("source_ref", "")), basis=str(r.get("basis", "") or ""),
                      expires_after_days=int(r.get("expires_after_days", DEFAULT_EXPIRES_AFTER_DAYS)),
                      conditions=str(r.get("conditions", "") or ""), notes=str(r.get("notes", "") or ""),
                      quota_pool=str(r.get("quota_pool", "") or ""), override=bool(r.get("override", False)),
                      override_reason=str(r.get("override_reason", "") or ""))


def is_stale(row: Row, as_of: str) -> bool:
    """A row is stale when its evidence is older than `expires_after_days` at `as_of` (or undated)."""
    then, now = parse_date(row.as_of), parse_date(as_of)
    if then is None or now is None:
        return True
    return (now - then).days > row.expires_after_days


def _window_covers(p: Mapping[str, Any], day: _dt.date) -> bool:
    lo, hi = parse_date(p.get("valid_from")), parse_date(p.get("valid_until"))
    return (lo is None or day >= lo) and (hi is None or day <= hi)


def price_at(model_entry: Mapping[str, Any], date: str, prompt_tokens: int = 0,
             route: str | None = None) -> Price | None:
    """The price in force on `date` for a prompt of `prompt_tokens`; None when no source covers it.

    A route-specific price wins over the model-wide one. Above `long_context_over.tokens` the long-context
    tier applies. A price is never invented: an unknown part stays None."""
    day = parse_date(date)
    prices = [p for p in (model_entry.get("prices") or ()) if isinstance(p, dict)]
    if day is None or not prices:
        return None
    for want_route in ([route] if route else []) + [None]:
        for p in prices:
            if p.get("route") != want_route:
                continue
            if not _window_covers(p, day):
                continue
            return _price_from(p, prompt_tokens)
    return None


def _price_from(p: Mapping[str, Any], prompt_tokens: int) -> Price:
    in_p, out_p, cache = p.get("in"), p.get("out"), p.get("cache_read")
    tier, approx = "standard", False
    lc = p.get("long_context_over")
    if isinstance(lc, dict) and isinstance(lc.get("tokens"), int) and prompt_tokens > lc["tokens"]:
        tier = "long-context"
        if lc.get("in") is not None or lc.get("out") is not None:
            in_p, out_p, cache = lc.get("in"), lc.get("out"), lc.get("cache_read")
        elif lc.get("in_multiplier") is not None and in_p is not None:
            in_p, out_p, cache, approx = in_p * float(lc["in_multiplier"]), None, None, bool(lc.get("approximate", True))
    return Price(in_price=in_p, out_price=out_p, cache_read=cache, tier=tier, verify=bool(p.get("verify", False)),
                 source=str(p.get("price_source", "")), as_of=_iso(p.get("price_as_of")),
                 valid_from=_iso(p.get("valid_from")) or None, valid_until=_iso(p.get("valid_until")) or None,
                 approximate=approx, notes=str(p.get("notes", "") or ""))


# --- I/O ---------------------------------------------------------------------------------------
def shipped_path(kit_root: Path | str | None = None) -> Path:
    if kit_root is None:
        from .. import repo_root
        kit_root = repo_root()
    return Path(kit_root) / "scores" / "model-scores.yaml"


def default_override_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "ai-resources" / "model-scores.yaml"


def _read_yaml(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML required to load the scores database")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a mapping")
    return data


def load_shipped(kit_root: Path | str | None = None) -> dict:
    """The shipped document as a plain dict (raises if the file is missing or malformed)."""
    return _read_yaml(shipped_path(kit_root))


def load_overrides(path: Path | str | None = None) -> dict | None:
    """The user override document, or None when there is none (a missing file means no overrides)."""
    p = Path(path) if path is not None else default_override_path()
    if not p.is_file():
        return None
    return _read_yaml(p)


def _row_key(r: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    routes = r.get("route")
    routes = list(routes) if isinstance(routes, (list, tuple)) else [routes]
    return [(str(r.get("role")), str(r.get("model")), str(route), str(r.get("effort", "default")))
            for route in routes]


def merge(shipped: Mapping[str, Any], overrides: Mapping[str, Any] | None) -> tuple[dict, list[str]]:
    """Shipped document plus overrides. An override row shadows the shipped row of the same key.

    Every override row must carry `reason` and `as_of`; one that does not is dropped and reported. A pool
    override may add numeric `model_weights` and must carry a `reason`. Returns (document, problems)."""
    out = dict(shipped)
    problems: list[str] = []
    if not overrides:
        return out, problems
    rows = [dict(r) for r in shipped.get("rows") or ()]
    for r in overrides.get("rows") or ():
        if not isinstance(r, dict):
            problems.append("override row is not a mapping")
            continue
        if not r.get("reason") or not parse_date(r.get("as_of")):
            problems.append(f"override row {r.get('role')}/{r.get('model')} needs a reason and an ISO as_of; ignored")
            continue
        if "verified" in r:
            problems.append(f"override row {r.get('role')}/{r.get('model')} carries a `verified` key; ignored")
            continue
        new = dict(r)
        new["override"] = True
        new["override_reason"] = str(r["reason"])
        new.setdefault("source_ref", "user override")
        new.setdefault("label", "UNKNOWN")
        # Explode shipped rows that share a route list so one route can be shadowed on its own.
        want = set(_row_key(new))
        keep: list[dict] = []
        for old in rows:
            keys = _row_key(old)
            if not any(k in want for k in keys):
                keep.append(old)
                continue
            if isinstance(old.get("route"), (list, tuple)):
                for route, k in zip(old["route"], keys):
                    if k not in want:
                        split = dict(old)
                        split["route"] = route
                        keep.append(split)
        rows = keep + [new]
    out["rows"] = rows
    pools = [dict(p) for p in shipped.get("pools") or ()]
    for po in overrides.get("pools") or ():
        if not isinstance(po, dict) or not po.get("reason"):
            problems.append("override pool needs a reason; ignored")
            continue
        for p in pools:
            if p.get("id") == po.get("id") and isinstance(po.get("model_weights"), dict):
                p["model_weights"] = dict(po["model_weights"])
                p["weights_override_reason"] = str(po["reason"])
    out["pools"] = pools
    return out, problems


def load(kit_root: Path | str | None = None, override_path: Path | str | None = None,
         use_overrides: bool = True) -> ScoresDB:
    """Shipped database with the user overrides applied (when present)."""
    shipped = load_shipped(kit_root)
    over = load_overrides(override_path) if use_overrides else None
    doc, problems = merge(shipped, over)
    return ScoresDB(doc, problems)


# --- validation --------------------------------------------------------------------------------
def _find_key(obj: Any, key: str, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            if k == key:
                hits.append(here)
            hits += _find_key(v, key, here)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits += _find_key(v, key, f"{path}[{i}]")
    return hits


def validate(data: Mapping[str, Any], *, known_roles: Iterable[str] | None = None,
             check_numeric_claude_weights: bool = True) -> list[str]:
    """Every rule of the schema as a list of errors (empty when the document is valid)."""
    errs: list[str] = []
    if not isinstance(data, Mapping):
        return ["document is not a mapping"]
    if known_roles is None:
        from ..setup import profiles
        known_roles = profiles.KNOWN_ROLES
    known_roles = list(known_roles)
    if data.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version must be {SCHEMA_VERSION}")
    if not parse_date(data.get("dataset_version")):
        errs.append("dataset_version must be an ISO date")
    for hit in _find_key(data, "verified"):
        errs.append(f"{hit}: a `verified` key is not allowed (verification comes from compat only)")
    for hit in _find_key(data, "routable"):
        errs.append(f"{hit}: `routable` is computed from the kit's lists, never stored")

    policy = data.get("policy") or {}
    imp = policy.get("importance") or {}
    for role in imp:
        if role not in known_roles:
            errs.append(f"policy.importance: unknown role {role}")
        elif not isinstance(imp[role], (int, float)) or not 0 <= imp[role] <= 1:
            errs.append(f"policy.importance.{role} must be in [0, 1]")
    for role in known_roles:
        if role not in imp:
            errs.append(f"policy.importance: missing role {role}")
    classes = policy.get("tolerance_classes") or {}
    for cls in ("critical", "standard", "low"):
        if not isinstance(classes.get(cls), (int, float)):
            errs.append(f"policy.tolerance_classes.{cls} is required")
    for role, cls in (policy.get("role_class") or {}).items():
        if role not in known_roles:
            errs.append(f"policy.role_class: unknown role {role}")
        if cls not in classes:
            errs.append(f"policy.role_class.{role}: unknown class {cls}")
    for role in (policy.get("load_weight") or {}):
        if role not in known_roles:
            errs.append(f"policy.load_weight: unknown role {role}")
    for k in ("diversity_tolerance", "loss_cap"):
        if not isinstance(policy.get(k), (int, float)):
            errs.append(f"policy.{k} is required")

    routes = data.get("routes") or {}
    for rid in routes:
        if rid not in ROUTES:
            errs.append(f"routes: unknown route {rid}")
    surfaces = data.get("surfaces") or {}
    for sid, s in surfaces.items():
        if (s or {}).get("usage") not in USAGE_VALUES:
            errs.append(f"surfaces.{sid}.usage must be one of {USAGE_VALUES}")
    for rid, spec in routes.items():
        if (spec or {}).get("surface") not in surfaces:
            errs.append(f"routes.{rid}.surface {(spec or {}).get('surface')!r} is not a declared surface")

    pool_ids: set[str] = set()
    for p in data.get("pools") or ():
        pid = p.get("id")
        if not pid or pid in pool_ids:
            errs.append(f"pools: missing or duplicate id {pid!r}")
        pool_ids.add(pid)
        if p.get("billing_class") not in BILLING_CLASSES:
            errs.append(f"pools.{pid}.billing_class must be one of {BILLING_CLASSES}")
        if p.get("usage") not in USAGE_VALUES:
            errs.append(f"pools.{pid}.usage must be one of {USAGE_VALUES}")
        if p.get("headroom_source") not in HEADROOM_SOURCES:
            errs.append(f"pools.{pid}.headroom_source must be one of {HEADROOM_SOURCES}")
        if not p.get("source_ref"):
            errs.append(f"pools.{pid}.source_ref is required")
        for route in p.get("routes") or ():
            if route not in routes:
                errs.append(f"pools.{pid}.routes: unknown route {route}")
        mw = p.get("model_weights") or {}
        if check_numeric_claude_weights and pid == "claude-subscription":
            if mw.get("kind") != "qualitative" or any(isinstance(v, (int, float)) for v in (mw.get("tiers") or {}).values()):
                errs.append("pools.claude-subscription.model_weights must be qualitative (no published numeric weight)"
                            " unless an override supplies one with a reason")

    models = data.get("models") or {}
    for mid, m in models.items():
        if "/" not in mid:
            errs.append(f"models.{mid}: id must be <vendor>/<id>")
        if (m or {}).get("status") not in MODEL_STATUSES:
            errs.append(f"models.{mid}.status must be one of {MODEL_STATUSES}")
        for route in (m or {}).get("route_ids") or {}:
            if route not in routes:
                errs.append(f"models.{mid}.route_ids: unknown route {route}")
        errs += _validate_prices(mid, (m or {}).get("prices") or ())
        for i, b in enumerate((m or {}).get("benchmarks") or ()):
            where = f"models.{mid}.benchmarks[{i}]"
            for k in ("board", "metric", "label", "as_of", "source_ref"):
                if b.get(k) in (None, ""):
                    errs.append(f"{where}.{k} is required")
            if b.get("role_agnostic") is not True:
                errs.append(f"{where}.role_agnostic must be true (a proxy is never role evidence)")
            if b.get("label") not in ROW_LABELS:
                errs.append(f"{where}.label must be one of {ROW_LABELS}")
            if b.get("label") in MEASURED_LABELS and b.get("value") is None:
                errs.append(f"{where}: a MEASURED proxy needs a value")
    for a in data.get("aliases") or ():
        if a.get("target") not in models:
            errs.append(f"aliases: {a.get('alias')} -> {a.get('target')} does not resolve")
        if a.get("alias") == a.get("target"):
            errs.append(f"aliases: {a.get('alias')} points at itself")

    seen: set[tuple[str, str, str, str]] = set()
    for i, r in enumerate(data.get("rows") or ()):
        where = f"rows[{i}] ({r.get('role')}/{r.get('model')})"
        if r.get("role") not in known_roles:
            errs.append(f"{where}: unknown role")
        if r.get("model") not in models:
            errs.append(f"{where}: unknown model")
        if r.get("label") not in ROW_LABELS:
            errs.append(f"{where}: label is required and must be one of {ROW_LABELS}")
        if not parse_date(r.get("as_of")):
            errs.append(f"{where}: as_of must be an ISO date")
        if not r.get("source_ref"):
            errs.append(f"{where}: source_ref is required")
        score = r.get("score")
        if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 10):
            errs.append(f"{where}: score must be in [0, 10] or null")
        if r.get("label") == "INFERRED" and not r.get("basis"):
            errs.append(f"{where}: an INFERRED row needs a basis")
        if r.get("label") == "MEASURED-HERE":
            for f in MEASURED_HERE_FIELDS:
                if f not in r:
                    errs.append(f"{where}: MEASURED-HERE rows need {f}")
        if r.get("label") == "UNKNOWN" and score is not None and not r.get("override"):
            errs.append(f"{where}: an UNKNOWN row must have a null score")
        for route in (r.get("route") if isinstance(r.get("route"), list) else [r.get("route")]):
            if route not in routes:
                errs.append(f"{where}: unknown route {route}")
            elif r.get("model") in models and route not in (models[r["model"]].get("route_ids") or {}):
                errs.append(f"{where}: model has no route_ids entry for {route}")
        for key in _row_key(r):
            if key in seen:
                errs.append(f"{where}: duplicate row key {key}")
            seen.add(key)
    return errs


def _validate_prices(mid: str, prices: Iterable[Mapping[str, Any]]) -> list[str]:
    errs: list[str] = []
    by_route: dict[Any, list[Mapping[str, Any]]] = {}
    for i, p in enumerate(prices):
        where = f"models.{mid}.prices[{i}]"
        for k in ("price_source", "price_as_of"):
            if not p.get(k):
                errs.append(f"{where}.{k} is required")
        for part in ("in", "out"):
            if p.get(part) is not None and (not isinstance(p[part], (int, float)) or p[part] < 0):
                errs.append(f"{where}.{part} must be a non-negative number or null")
        by_route.setdefault(p.get("route"), []).append(p)
    for route, group in by_route.items():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                alo, ahi = parse_date(a.get("valid_from")) or _dt.date.min, parse_date(a.get("valid_until")) or _dt.date.max
                blo, bhi = parse_date(b.get("valid_from")) or _dt.date.min, parse_date(b.get("valid_until")) or _dt.date.max
                if alo <= bhi and blo <= ahi:
                    errs.append(f"models.{mid}: price windows overlap on route {route}")
    return errs


# --- reporting helpers used by `models scores` -------------------------------------------------
@dataclass
class Finding:
    kind: str
    subject: str
    detail: str


def check(db: ScoresDB, as_of: str, shipped: Mapping[str, Any] | None = None,
          horizon_days: int = 30) -> list[Finding]:
    """Everything `models scores check` lists, as findings (empty means clean)."""
    out: list[Finding] = []
    for r in db.rows:
        if r.score is not None and is_stale(r, as_of):
            out.append(Finding("stale-row", f"{r.role} {r.model} {r.route} {r.effort}",
                               f"as_of {r.as_of}, expires after {r.expires_after_days} days"))
    today = parse_date(as_of)
    for mid, m in sorted(db.models.items()):
        for p in m.get("prices") or ():
            until = parse_date(p.get("valid_until"))
            if until is not None and today is not None and 0 <= (until - today).days < horizon_days:
                out.append(Finding("price-window-closing", mid,
                                   f"{p.get('in')}/{p.get('out')} ends {until.isoformat()}"
                                   f" ({(until - today).days} days)"))
            if until is not None and today is not None and until < today and not any(
                    parse_date(q.get("valid_from")) and parse_date(q.get("valid_from")) > until
                    for q in m.get("prices") or ()):
                out.append(Finding("price-window-expired", mid, f"price ended {until.isoformat()} with no successor"))
            if p.get("verify"):
                out.append(Finding("price-verify", mid + (f" ({p['route']})" if p.get("route") else ""),
                                   f"{p.get('in')}/{p.get('out')} rests on one unconfirmed source: {p.get('price_source')}"))
        for route in sorted((m.get("route_ids") or {})):
            if not db.routable(mid, route):
                out.append(Finding("not-routable", f"{mid} via {route}", "the id is not in the kit's routing lists"))
        if m.get("status") == "retired":
            out.append(Finding("retired-model", mid, "status is retired"))
    for alias, target in sorted(db.aliases.items()):
        out.append(Finding("alias", alias, f"-> {target} (unconfirmed rename)"))
    if shipped is not None:
        shipped_rows = {k: r for r in ScoresDB(shipped).rows for k in [r.key]}
        for r in db.rows:
            old = shipped_rows.get(r.key)
            if r.override and old is not None and parse_date(old.as_of) and parse_date(r.as_of) \
                    and parse_date(old.as_of) > parse_date(r.as_of):
                out.append(Finding("override-older-than-shipped", f"{r.role} {r.model} {r.route} {r.effort}",
                                   f"override as_of {r.as_of} is older than the shipped row {old.as_of}"))
    return out


def diff(shipped: Mapping[str, Any], merged: Mapping[str, Any]) -> list[Finding]:
    """Rows where the merged document differs from the shipped one (the user overrides)."""
    a = {r.key: r for r in ScoresDB(shipped).rows}
    out: list[Finding] = []
    for r in ScoresDB(merged).rows:
        old = a.get(r.key)
        subject = f"{r.role} {r.model} {r.route} {r.effort}"
        if old is None:
            out.append(Finding("override-added", subject, f"score {r.score} ({r.label.value}); {r.override_reason}"))
        elif r.override and (old.score != r.score or old.label != r.label):
            out.append(Finding("override-changed", subject,
                               f"{old.score} ({old.label.value}) -> {r.score} ({r.label.value}); {r.override_reason}"))
    return out
