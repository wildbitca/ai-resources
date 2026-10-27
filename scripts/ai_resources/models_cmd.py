"""`ai-resources models ...`: status, check, update, approve, revoke, pin, unpin, exclude, rollback.

`update` asks on a terminal (see models_interaction) and never prompts unattended.

Thin over `models.py` (policy, patching, orchestration) and `model_pins.py` (declaration and overlay).
Unattended runs (a timer, no TTY, `--unattended`) never prompt."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import model_fanout, model_pins, model_providers, models, models_interaction
from .setup import ui

VERBS = ("status", "check", "update", "approve", "revoke", "pin", "unpin", "exclude", "rollback", "scores")
SCORES_ACTIONS = ("show", "check", "diff", "refresh")


def get_deps() -> models.Deps:
    """The seam the tests replace; the real wiring lives in models.default_deps()."""
    deps = models.default_deps()
    return deps


def _can_prompt(args: argparse.Namespace) -> bool:
    return not getattr(args, "unattended", False) and not ui.is_non_interactive() and ui.stdin_is_a_terminal()


def _overlay() -> dict:
    return model_pins.load_overlay() or model_pins.empty_overlay()


def _save(ov: dict, action: str, **detail) -> None:
    ov.setdefault("history", []).append(
        {"at": datetime.now(timezone.utc).isoformat(), "action": action, **detail})
    model_pins.save_overlay(ov)


def _check_class(cls: str, model_id: str | None = None) -> str | None:
    """Validate a class or a `<provider>:<family>` slot and, when given, that the id belongs to it."""
    slot = model_pins.slot_key(cls)
    provider, _, family = slot.partition(":")
    if provider == model_pins.PROVIDER_ANTHROPIC:
        if family not in model_pins.CLASSES:
            return f"unknown class {cls!r}; choose one of {', '.join(model_pins.CLASSES)}"
        if model_id is not None:
            parsed = model_pins.parse_id(model_id)
            if not parsed or parsed[0] != family:
                return f"{model_id!r} is not a {family} model id (expected e.g. claude-{family}-5)"
        return None
    adapter = model_providers.REGISTRY.get(provider)
    if not adapter or not family or provider == "openrouter":
        return f"unknown slot {cls!r}; use <provider>:<family> (providers: {', '.join(model_providers.model_families())})"
    if model_id is not None:
        ref = adapter.from_any_spelling(model_id)
        if not ref or ref.family != family:
            return f"{model_id!r} is not a {slot} model id"
    return None


def _print_result(r: models.Result, args: argparse.Namespace) -> None:
    if getattr(args, "json", False):
        print(json.dumps(r.as_dict(), indent=2))
        return
    print(f"{r.outcome}: {r.message}" if r.message else r.outcome)
    for row in r.providers:
        if row.status in ("skipped", "failed", "report-only"):
            print(f"  {row.provider}: {row.text}")
    for p in r.proposals:
        if p.decision == "current":
            note = f" ({'; '.join(p.reasons)})" if p.reasons else ""
            print(f"  {p.cls}: {p.old} up to date{note}")
            continue
        old = p.old or "-"
        extra = f" ({'; '.join(p.reasons)})" if p.reasons else ""
        print(f"  {p.cls}: {old} -> {p.new} [{p.kind}, price {p.price}] {p.decision}{extra}")
        if p.decision == "needs_approval":
            print(f"    approve with: {models.approve_command(p.slot, p.new)}")


def answer_label(overlay: dict, slot: str) -> str:
    """The remembered answer of a slot, as one word for status: always | ask | never (model|family) | frozen."""
    pol = model_pins.slot_policy(overlay, slot)
    if pol.get("frozen"):
        return "frozen"
    if pol.get("family_never") or pol.get("answer") == "never":
        return "never (family)"
    suffix = f", {len(pol['never_ids'])} id(s) refused" if pol.get("never_ids") else ""
    if pol.get("answer") == "never":
        return "never (family)"
    return f"{pol.get('answer', 'ask')}{suffix}"


def cmd_status(args: argparse.Namespace) -> int:
    ov = model_pins.load_overlay()
    deps = get_deps()
    selection = deps.selection()
    slots = model_pins.effective_slots(selection, ov)
    claude_eff = model_pins.effective(ov)
    print("Model slots (slot: effective id, kit default, host overlay, answer)")
    for slot, model_id in slots.items():
        provider, family = slot.split(":", 1)
        adapter = model_providers.REGISTRY.get(provider)
        default = (adapter.default_id(family) if adapter else None) or "-"
        pin = (ov.get("pins") or {}).get(slot, "-")
        frozen = " [frozen]" if model_pins.is_frozen(ov, slot) else ""
        print(f"  {slot:26} {model_id:24} default {default:22} overlay {pin}  answer {answer_label(ov, slot)}{frozen}")
    if selection is not None:
        try:
            accounts = deps.accounts()
        except Exception as e:  # noqa: BLE001
            accounts = {}
            print(f"\nAccounts: unavailable ({type(e).__name__})")
        print("\nProviders")
        for pid in model_providers.model_families():
            psel = selection.providers.get(pid)
            if psel is None or not psel.enabled:
                print(f"  {pid:10} skipped: not enabled")
                continue
            acc = accounts.get(pid)
            note = ("credentialed (" + ", ".join(acc.sources) + ")") if acc is not None and acc.credentialed else \
                   ("skipped: no credentials" if acc is not None else "enabled")
            print(f"  {pid:10} {note}")
    try:
        doc = deps.read_config()
    except Exception as e:  # noqa: BLE001
        doc = {}
        print(f"\nopenclaw.json: unreadable ({e})")
    drift = []
    for path, ref in models.collect_refs(doc):
        if "models" in path[:3] and len(path) == 4:
            continue
        wanted = model_pins.ref_drift(ref, claude_eff, slots)
        if wanted:
            drift.append((".".join(str(p) for p in path), ref, wanted))
    print("\nopenclaw.json references: " + ("all match the effective pins" if not drift else f"{len(drift)} differ"))
    for path, ref, wanted in drift:
        print(f"  DRIFT {path} = {ref} (effective: {wanted})")
    pending = ov.get("pending") or {}
    print("\nPending approvals: " + ("none" if not pending else ""))
    for slot, p in pending.items():
        print(f"  {slot} -> {p.get('to')} ({p.get('reason')}); {models.approve_command(slot, p.get('to'))}")
    st = ov.get("state") or {}
    print(f"\nLast run: {st.get('last_run', 'never')}  result: {st.get('last_result', '-')}  "
          f"last switch: {st.get('last_switch_at', '-')}")
    if st.get("pending_restart"):
        print("  A gateway restart is pending; the next `models update` resumes it.")
    from .setup.cockpits import _agy_quota
    print("\nAntigravity (agy) models: report only, edit model_pins.AGY_STATIC by hand")
    for m in model_pins.AGY_STATIC:
        print(f"  {m}  ({_agy_quota.pool_for_model(m) or 'unknown'} pool)")
    return 0


def _opts(args: argparse.Namespace, *, check: bool = False) -> models.Options:
    return models.Options(check=check or getattr(args, "check", False), dry_run=getattr(args, "dry_run", False),
                          classes=getattr(args, "classes", None) or None,
                          no_restart=getattr(args, "no_restart", False),
                          refresh=getattr(args, "refresh", True), unattended=getattr(args, "unattended", False),
                          apply_proposals=getattr(args, "apply_proposals", False))


def cmd_check(args: argparse.Namespace) -> int:
    r = models.run_update(_opts(args, check=True), get_deps())
    _print_result(r, args)
    return r.rc


def rerender_note(r: models.Result) -> str:
    """Which kit files the run re-rendered besides openclaw.json and the overlay."""
    if r.rendered:
        return "Also re-rendered: " + ", ".join(r.rendered)
    return ("No other kit file (executors.yaml, litellm.yaml, Claude subagents and settings, aider.conf.yml) "
            "embedded the old id.")


class _UiIO:
    """The buttons: `ui.select` behind the interface `models_interaction` asks through."""

    def select(self, message, choices, default=None):
        return ui.select(message, choices, default=default)


def _artifacts_line(deps: models.Deps, p) -> str:
    """The artifacts this proposal would touch, exactly as the fan-out registry reports them."""
    change = model_fanout.Change({p.slot: (p.old, p.new)})
    # The same plan and selection the apply passes, so the preview lists what the apply will really touch.
    ctx = model_fanout.Ctx(selection=deps.selection(), plan=deps.route_plan())
    ids = model_fanout.affected_ids(models.registry(deps.apply_patch, deps.artifacts()), change, ctx)
    return ", ".join(["overlay", *ids]) if ids else "overlay"


def _channel(p) -> str:
    adapter = model_providers.REGISTRY.get(p.provider)
    ref = adapter.from_any_spelling(p.new) if adapter else None
    return ref.channel if ref else "unknown"


def _print_plan(plan: models.Plan, deps: models.Deps, wanted: set[str]) -> list:
    """The table of what discovery found, one row per proposal. Returns the proposals to ask about."""
    path = getattr(plan.selection, "smoke_path", "claude-cli") if plan.selection is not None else "claude-cli"
    rows = [p for p in plan.props if p.decision not in ("current",) and (not wanted or p.slot in wanted)]
    for row in plan.disc.providers:
        if row.status in ("skipped", "failed", "report-only"):
            print(f"  {row.provider}: {row.text}")
    if not rows:
        print("Everything is up to date.")
    for p in rows:
        if p.kind == "new_family":
            print(f"  new family {p.new} (report only, never applied)")
            continue
        print(f"  {p.slot}: {p.old or '-'} -> {p.new}  [{p.provider}, {_channel(p)}, {p.kind}, price {p.price}, "
              f"smoke {path}]  {p.decision}" + (f" ({'; '.join(p.reasons)})" if p.reasons else ""))
        print(f"    updates: {_artifacts_line(deps, p)}")
    res = models_interaction.resolve(plan.props, lambda slot: model_pins.slot_policy(_overlay(), slot), True)
    return [p for p in res.ask if not wanted or p.slot in wanted]


def _interactive_update(args: argparse.Namespace, deps: models.Deps) -> int:
    """TTY run: discover once, show the table, ask, then apply THAT result through the fan-out."""
    opts = _opts(args)
    plan = models.make_plan(opts, deps)
    if isinstance(plan, models.Result):
        _print_result(plan, args)
        return plan.rc
    wanted = models._wanted(opts)
    to_ask = _print_plan(plan, deps, wanted)
    answers = models_interaction.ask(to_ask, _UiIO())
    if answers is None:
        print("cancelled; nothing was changed")
        return 0
    r = models.run_update(opts, deps, plan=plan, answers=answers, expect_revision=plan.revision)
    if r.rc == models.EXIT_LOCKED:
        print("an update is already running; nothing was changed")
        return r.rc
    _print_result(r, args)
    if r.outcome == "switched":
        print(rerender_note(r))
    return r.rc


def cmd_update(args: argparse.Namespace) -> int:
    deps = get_deps()
    dry = args.check or args.dry_run
    if getattr(args, "review", False) and not _can_prompt(args):
        print("--review needs a terminal (and no --unattended)", file=sys.stderr)
        return 2
    if not dry and not getattr(args, "apply_proposals", False) and _can_prompt(args):
        rc = _interactive_update(args, deps)
        return rc
    r = models.run_update(_opts(args), deps)
    _print_result(r, args)
    if r.outcome == "switched" and not getattr(args, "json", False):
        print(rerender_note(r))
    return r.rc


def _target(args: argparse.Namespace, want_id: bool) -> tuple[str | None, str | None]:
    """(slot-or-class, id) from `approve [--slot S] [cls] id`: --slot replaces the positional class."""
    ids = list(getattr(args, "ids", None) or [])
    slot = getattr(args, "slot", None)
    if slot:
        return slot, (ids[0] if want_id and ids else None)
    if want_id:
        return (ids[0], ids[1]) if len(ids) >= 2 else (ids[0] if ids else None, None)
    return (ids[0] if ids else None), None


def cmd_approve(args: argparse.Namespace) -> int:
    target, model_id = _target(args, want_id=True)
    if not target or not model_id:
        print("usage: ai-resources models approve (--slot <provider:family> | <class>) <id>", file=sys.stderr)
        return 2
    err = _check_class(target, model_id)
    if err:
        print(err, file=sys.stderr)
        return 2
    slot = model_pins.slot_key(target)
    ov = _overlay()
    ov.setdefault("approvals", {})[slot] = model_id
    ((ov.get("state") or {}).get("failed") or {}).pop(slot, None)
    _save(ov, "approve", cls=slot, id=model_id)
    print(f"approved {slot} -> {model_id}; the next `models update` may apply it")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    target, _ = _target(args, want_id=False)
    err = _check_class(target or "")
    if err:
        print(err, file=sys.stderr)
        return 2
    slot = model_pins.slot_key(target)
    ov = _overlay()
    removed = (ov.get("approvals") or {}).pop(slot, None)
    _save(ov, "revoke", cls=slot)
    print(f"revoked the approval for {slot}" if removed else f"no approval for {slot}")
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    target, model_id = _target(args, want_id=True)
    if not target or not model_id:
        print("usage: ai-resources models pin (--slot <provider:family> | <class>) <id>", file=sys.stderr)
        return 2
    err = _check_class(target, model_id)
    if err:
        print(err, file=sys.stderr)
        return 2
    slot = model_pins.slot_key(target)
    ov = _overlay()
    ov.setdefault("pins", {})[slot] = model_id
    ov.setdefault("policy", {}).setdefault("slots", {}).setdefault(slot, {}).update({"frozen": True})
    _save(ov, "pin", cls=slot, id=model_id)
    print(f"pinned {slot} at {model_id} (frozen: no automatic changes)")
    return 0


def cmd_unpin(args: argparse.Namespace) -> int:
    target, _ = _target(args, want_id=False)
    err = _check_class(target or "")
    if err:
        print(err, file=sys.stderr)
        return 2
    slot = model_pins.slot_key(target)
    ov = _overlay()
    ov.get("pins", {}).pop(slot, None)
    ((ov.get("policy") or {}).get("slots") or {}).pop(slot, None)
    _save(ov, "unpin", cls=slot)
    print(f"{slot} follows the kit default and the catalog again")
    return 0


def cmd_exclude(args: argparse.Namespace) -> int:
    ov = _overlay()
    globs = ov.setdefault("policy", {}).setdefault("exclude", [])
    if args.glob not in globs:
        globs.append(args.glob)
    _save(ov, "exclude", glob=args.glob)
    print(f"excluded {args.glob}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    r = models.run_rollback(get_deps())
    _print_result(r, args)
    return r.rc


# --- scores (ADR-0006): the model scores database -----------------------------------------------------------
def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _print_findings(findings, args, empty: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps([{"kind": f.kind, "subject": f.subject, "detail": f.detail} for f in findings], indent=2))
        return
    if not findings:
        print(empty)
    for f in findings:
        print(f"  {f.kind:28} {f.subject}: {f.detail}")


def _scores_show(db, as_of: str, shipped: dict, args) -> int:
    print(f"Model scores: dataset_version {db.dataset_version}, schema {db.data.get('schema_version')} "
          f"(as of {as_of})")
    over = [r for r in db.rows if r.override]
    print(f"Rows: {len(db.rows)} ({len(over)} from your overrides)")
    for mid in sorted(db.models):
        m = db.models[mid]
        price = db.price_at(mid, as_of, 0)
        ptxt = "no price" if price is None else f"{price.in_price}/{price.out_price}" + (" [verify]" if price.verify else "")
        rows = [r for r in db.rows if r.model == mid and r.score is not None]
        routes = ", ".join(f"{r}{'' if db.routable(mid, r) else ' (not routable)'}" for r in sorted(m.get("route_ids") or {}))
        best = f"{min(r.score for r in rows):g}-{max(r.score for r in rows):g}" if rows else "no scores"
        print(f"  {mid:36} {m.get('status', '?'):8} price {ptxt:16} scores {best:10} routes: {routes or '-'}")
    for problem in db.problems:
        print(f"  override problem: {problem}")
    return 0


def _scores_check(db, as_of: str, shipped: dict, args) -> int:
    from .scoring import db as scores_db
    findings = [scores_db.Finding("invalid", "shipped", e) for e in scores_db.validate(shipped)]
    findings += [scores_db.Finding("override-problem", "overrides", p) for p in db.problems]
    # The merged document is what the engine reads: validate it too (overrides cannot hide behind the shipped file).
    shipped_errs = set(scores_db.validate(shipped))
    weights_override = any(p.get("weights_override_reason") for p in db.data.get("pools") or ())
    findings += [scores_db.Finding("invalid", "merged", e)
                 for e in scores_db.validate(db.data, check_numeric_claude_weights=not weights_override)
                 if e not in shipped_errs]
    findings += scores_db.check(db, as_of, shipped=shipped)
    _print_findings(findings, args, "Scores database is clean.")
    return 1 if findings else 0


def _scores_diff(db, as_of: str, shipped: dict, args) -> int:
    from .scoring import db as scores_db
    findings = scores_db.diff(shipped, db.data)
    _print_findings(findings, args, "No user overrides differ from the shipped database.")
    return 0


def _override_doc(path) -> dict:
    from .scoring import db as scores_db
    return scores_db.load_overrides(path) or {}


def _write_override(path, doc: dict) -> None:
    """Atomic (tmp then os.replace), owner-only, like model_pins.save_overlay."""
    import os
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(yaml.safe_dump(doc, default_flow_style=False, sort_keys=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _proposals(shipped: dict, over: dict, from_doc: dict | None) -> list[dict]:
    """What `refresh` would change: rows from an operator-supplied file, and overrides a newer shipped row beats."""
    from .scoring import db as scores_db
    out: list[dict] = []
    have = {r.key: r for r in scores_db.ScoresDB({"rows": over.get("rows") or []}).rows}
    ship = {r.key: r for r in scores_db.ScoresDB({"rows": shipped.get("rows") or []}).rows}
    declined = {tuple(d) for d in over.get("declined") or [] if isinstance(d, list)}
    for row in (from_doc or {}).get("rows") or []:
        for key in scores_db._row_key(row):
            if key in declined:
                continue
            cur = have.get(key)
            if cur is not None and cur.score == row.get("score") and cur.label.value == row.get("label"):
                continue
            out.append({"op": "set", "key": key, "row": row,
                        "text": f"{'/'.join(key)}: score {row.get('score')} ({row.get('label')}), "
                                f"as_of {row.get('as_of')}; reason: {row.get('reason', '(none)')}"})
    for key, cur in have.items():
        old = ship.get(key)
        if old and scores_db.parse_date(old.as_of) and scores_db.parse_date(cur.as_of) \
                and scores_db.parse_date(old.as_of) > scores_db.parse_date(cur.as_of):
            out.append({"op": "drop", "key": key,
                        "text": f"{'/'.join(key)}: shipped row ({old.as_of}) is newer than your override "
                                f"({cur.as_of}); drop the override?"})
    return out


def _apply_proposal(doc: dict, prop: dict) -> None:
    from .scoring import db as scores_db
    rows = [r for r in doc.get("rows") or [] if prop["key"] not in scores_db._row_key(r)]
    if prop["op"] == "set":
        new = dict(prop["row"])
        new["route"] = prop["key"][2]
        rows.append(new)
    doc["rows"] = rows


def _invalid_proposal_rows(shipped: dict, from_doc: dict) -> list[str]:
    """Rows of a `--from` file that break the evidence rules or lack a reason; they are never proposed."""
    from .scoring import db as scores_db
    out: list[str] = []
    rows = from_doc.get("rows") if isinstance(from_doc, dict) else None
    for i, row in enumerate(rows or []):
        if not isinstance(row, dict):
            out.append(f"rows[{i}] is not a mapping")
            continue
        where = f"rows[{i}] ({row.get('role')}/{row.get('model')})"
        errs = scores_db.validate_row(dict(row, override=True), where, known_roles=scores_db._known_roles(shipped),
                                      models=shipped.get("models") or {}, routes=shipped.get("routes") or {})
        if not row.get("reason"):
            errs.append(f"{where}: a proposal needs a reason")
        if "verified" in row:
            errs.append(f"{where}: a `verified` key is not allowed")
        out += errs
    return out


def _scores_refresh(db, as_of: str, shipped: dict, args) -> int:
    """Proposals through an explicit answer; nothing is auto-approved and nothing is written unasked."""
    import yaml
    from .scoring import db as scores_db
    path = scores_db.default_override_path()
    from_doc = None
    src = getattr(args, "from_file", None)
    if src:
        try:
            from_doc = yaml.safe_load(open(src, encoding="utf-8").read()) or {}
        except (OSError, yaml.YAMLError) as e:
            print(f"cannot read {src}: {e}", file=sys.stderr)
            return 2
    if from_doc is not None:
        bad = _invalid_proposal_rows(shipped, from_doc)
        if bad:
            print(f"invalid proposal row(s) in {src}; nothing was changed:", file=sys.stderr)
            for line in bad:
                print(f"  invalid: {line}", file=sys.stderr)
            return 2
    over = _override_doc(path)
    props = _proposals(shipped, over, from_doc)
    print("Only score rows are proposed (from --from, and overrides a newer shipped row beats); "
          "prices are not proposed: a price changes only with a reviewed edit of the shipped dataset.")
    if not props:
        print("No score proposals; nothing to do.")
        return 0
    print(f"{len(props)} proposal(s); nothing is applied without an answer:")
    for p in props:
        print(f"  [{p['op']}] {p['text']}")
    if not _can_prompt(args):
        print("approval pending: run on a terminal to answer each proposal (nothing was changed)")
        return 10
    with models.update_lock() as got:
        if not got:
            print("a models run is already in progress; nothing was changed")
            return models.EXIT_LOCKED
        doc = _override_doc(path)
        changed = 0
        for p in props:
            answer = ui.select(f"{p['text']}", [ui.Choice("approve", value="approve"),
                                                ui.Choice("skip", value="skip"),
                                                ui.Choice("never (do not propose this row again)", value="never")],
                               default="skip")
            if answer == "approve":
                _apply_proposal(doc, p)
                changed += 1
            elif answer == "never":
                doc.setdefault("declined", []).append(list(p["key"]))
                changed += 1
        if changed:
            _write_override(path, doc)
            print(f"wrote {path} ({changed} change(s)); your other overrides were kept")
        else:
            print("nothing was changed")
    return 0


def cmd_scores(args: argparse.Namespace) -> int:
    from .scoring import db as scores_db
    action = getattr(args, "scores_action", "show") or "show"
    as_of = getattr(args, "as_of", "") or _today()
    try:
        shipped = scores_db.load_shipped()
        db = scores_db.ScoresDB(*scores_db.merge(shipped, scores_db.load_overrides(scores_db.default_override_path())))
    except Exception as e:  # noqa: BLE001
        print(f"cannot load the scores database: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    return {"show": _scores_show, "check": _scores_check, "diff": _scores_diff,
            "refresh": _scores_refresh}[action](db, as_of, shipped, args)


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("models", help="Find, approve and apply newer models (any enabled provider) for the OpenClaw host and gateways")
    sp = p.add_subparsers(dest="action", required=True, metavar="VERB")

    sp.add_parser("status", help="Effective pins, config drift, pending approvals, last run").set_defaults(func=cmd_status)

    c = sp.add_parser("check", help="Read-only: exit 0 nothing to do, 10 approval pending, 11 a change is ready")
    c.add_argument("--refresh", action="store_true", help="Refresh the catalog from the network first")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)

    u = sp.add_parser("update", help="Discover newer models and apply what the policy allows")
    u.add_argument("--check", action="store_true", help="Print the plan; no smoke call, no write")
    u.add_argument("--dry-run", action="store_true", help="Smoke-test and validate the patch; apply nothing")
    u.add_argument("--class", dest="classes", action="append", choices=list(model_pins.CLASSES),
                   help="Only this Claude class (alias of --slot anthropic:<class>; repeatable)")
    u.add_argument("--slot", dest="classes", action="append", metavar="PROVIDER:FAMILY",
                   help="Only this slot, for example google:gemini-flash (repeatable)")
    u.add_argument("--no-restart", action="store_true", help="Patch the config but leave the gateway running")
    u.add_argument("--unattended", action="store_true", help="Never prompt (the timer uses this)")
    u.add_argument("--review", action="store_true", help="Force the interactive listing (needs a terminal)")
    u.add_argument("--apply-proposals", action="store_true",
                   help="Scripted, no prompt: approve and apply every proposal that is waiting for a human")
    u.add_argument("--json", action="store_true")
    u.set_defaults(func=cmd_update, refresh=True)

    a = sp.add_parser("approve", help="Approve one specific upgrade for later unattended runs")
    a.add_argument("--slot", metavar="PROVIDER:FAMILY", help="The slot, for example google:gemini-flash")
    a.add_argument("ids", nargs="+", metavar="[CLASS] ID", help="[class] and the model id (class is replaced by --slot)")
    a.set_defaults(func=cmd_approve)

    r = sp.add_parser("revoke", help="Remove a slot's approval")
    r.add_argument("--slot", metavar="PROVIDER:FAMILY")
    r.add_argument("ids", nargs="*", metavar="CLASS")
    r.set_defaults(func=cmd_revoke)

    pi = sp.add_parser("pin", help="Freeze a slot at an explicit model id")
    pi.add_argument("--slot", metavar="PROVIDER:FAMILY")
    pi.add_argument("ids", nargs="+", metavar="[CLASS] ID")
    pi.set_defaults(func=cmd_pin)

    un = sp.add_parser("unpin", help="Let a slot follow the catalog again")
    un.add_argument("--slot", metavar="PROVIDER:FAMILY")
    un.add_argument("ids", nargs="*", metavar="CLASS")
    un.set_defaults(func=cmd_unpin)

    ex = sp.add_parser("exclude", help="Never apply a model id matching this glob")
    ex.add_argument("glob")
    ex.set_defaults(func=cmd_exclude)

    sp.add_parser("rollback", help="Undo the last switch (inverse patch, restart, health check)").set_defaults(func=cmd_rollback)

    sc = sp.add_parser("scores", help="Model scores database: show, check (exit 1 when anything is listed), diff, refresh")
    sc.add_argument("scores_action", nargs="?", default="show", choices=list(SCORES_ACTIONS))
    sc.add_argument("--as-of", dest="as_of", default="", help="ISO date to evaluate staleness and prices at (default: today)")
    sc.add_argument("--from", dest="from_file", default="", help="refresh: a file of operator-supplied override rows")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=cmd_scores)
