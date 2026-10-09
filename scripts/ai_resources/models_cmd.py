"""`ai-resources models ...`: status, check, update, approve, revoke, pin, unpin, exclude, rollback.

Thin over `models.py` (policy, patching, orchestration) and `model_pins.py` (declaration and overlay).
Unattended runs (a timer, no TTY, `--unattended`) never prompt."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import model_pins, models
from .setup import ui

VERBS = ("status", "check", "update", "approve", "revoke", "pin", "unpin", "exclude", "rollback")


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
    if cls not in model_pins.CLASSES:
        return f"unknown class {cls!r}; choose one of {', '.join(model_pins.CLASSES)}"
    if model_id is not None:
        parsed = model_pins.parse_id(model_id)
        if not parsed or parsed[0] != cls:
            return f"{model_id!r} is not a {cls} model id (expected e.g. claude-{cls}-5)"
    return None


def _print_result(r: models.Result, args: argparse.Namespace) -> None:
    if getattr(args, "json", False):
        print(json.dumps(r.as_dict(), indent=2))
        return
    print(f"{r.outcome}: {r.message}" if r.message else r.outcome)
    for p in r.proposals:
        if p.decision == "current":
            note = f" ({'; '.join(p.reasons)})" if p.reasons else ""
            print(f"  {p.cls}: {p.old} up to date{note}")
            continue
        old = p.old or "-"
        extra = f" ({'; '.join(p.reasons)})" if p.reasons else ""
        print(f"  {p.cls}: {old} -> {p.new} [{p.kind}, price {p.price}] {p.decision}{extra}")
        if p.decision == "needs_approval":
            print(f"    approve with: ai-resources models approve {p.cls} {p.new}")


def cmd_status(args: argparse.Namespace) -> int:
    ov = model_pins.load_overlay()
    eff = model_pins.effective(ov)
    print("Claude pins (class: effective, kit default, host overlay)")
    for cls in model_pins.CLASSES:
        pin = (ov.get("pins") or {}).get(cls, "-")
        frozen = " [frozen]" if model_pins.is_frozen(ov, cls) else ""
        print(f"  {cls:7} {eff[cls]:22} default {model_pins.DEFAULTS[cls]:20} overlay {pin}{frozen}")
    deps = get_deps()
    try:
        doc = deps.read_config()
    except Exception as e:  # noqa: BLE001
        doc = {}
        print(f"\nopenclaw.json: unreadable ({e})")
    drift = []
    for path, ref in models.collect_refs(doc):
        if "models" in path[:3] and len(path) == 4:
            continue
        if not ref.startswith("anthropic/"):
            continue
        parsed = model_pins.parse_id(ref.split("/", 1)[1])
        if parsed and ref != model_pins.openclaw_ref(eff[parsed[0]]):
            drift.append((".".join(str(p) for p in path), ref, parsed[0]))
    print("\nopenclaw.json references: " + ("all match the effective pins" if not drift else f"{len(drift)} differ"))
    for path, ref, cls in drift:
        print(f"  DRIFT {path} = {ref} (effective {cls}: {model_pins.openclaw_ref(eff[cls])})")
    pending = ov.get("pending") or {}
    print("\nPending approvals: " + ("none" if not pending else ""))
    for cls, p in pending.items():
        print(f"  {cls} -> {p.get('to')} ({p.get('reason')}); ai-resources models approve {cls} {p.get('to')}")
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
                          refresh=getattr(args, "refresh", True), unattended=getattr(args, "unattended", False))


def cmd_check(args: argparse.Namespace) -> int:
    r = models.run_update(_opts(args, check=True), get_deps())
    _print_result(r, args)
    return r.rc


def _offer_approvals(args: argparse.Namespace, deps: models.Deps) -> None:
    """Interactive only: offer each needs_approval proposal, persisting the answer as an approval."""
    try:
        disc = models.discover(deps.runner, refresh=getattr(args, "refresh", True))
    except models.DiscoveryError:
        return
    ov = _overlay()
    for p in models.propose(model_pins.effective(ov), disc, ov):
        if p.decision != "needs_approval":
            continue
        answer = ui.select(f"{p.cls}: {p.old} -> {p.new} ({'; '.join(p.reasons)}). Apply it?",
                           ["Approve and apply", "Skip"], default="Skip")
        if answer == "Approve and apply":
            ov.setdefault("approvals", {})[p.cls] = p.new
            ((ov.get("state") or {}).get("failed") or {}).pop(p.cls, None)
            _save(ov, "approve", cls=p.cls, id=p.new)


def cmd_update(args: argparse.Namespace) -> int:
    deps = get_deps()
    if not (args.check or args.dry_run) and _can_prompt(args):
        _offer_approvals(args, deps)
    r = models.run_update(_opts(args), deps)
    _print_result(r, args)
    return r.rc


def cmd_approve(args: argparse.Namespace) -> int:
    err = _check_class(args.cls, args.id)
    if err:
        print(err, file=sys.stderr)
        return 2
    ov = _overlay()
    ov.setdefault("approvals", {})[args.cls] = args.id
    ((ov.get("state") or {}).get("failed") or {}).pop(args.cls, None)
    _save(ov, "approve", cls=args.cls, id=args.id)
    print(f"approved {args.cls} -> {args.id}; the next `models update` may apply it")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    err = _check_class(args.cls)
    if err:
        print(err, file=sys.stderr)
        return 2
    ov = _overlay()
    removed = (ov.get("approvals") or {}).pop(args.cls, None)
    _save(ov, "revoke", cls=args.cls)
    print(f"revoked the approval for {args.cls}" if removed else f"no approval for {args.cls}")
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    err = _check_class(args.cls, args.id)
    if err:
        print(err, file=sys.stderr)
        return 2
    ov = _overlay()
    ov.setdefault("pins", {})[args.cls] = args.id
    ov.setdefault("policy", {}).setdefault("classes", {}).setdefault(args.cls, {})["mode"] = "frozen"
    _save(ov, "pin", cls=args.cls, id=args.id)
    print(f"pinned {args.cls} at {args.id} (frozen: no automatic changes)")
    return 0


def cmd_unpin(args: argparse.Namespace) -> int:
    err = _check_class(args.cls)
    if err:
        print(err, file=sys.stderr)
        return 2
    ov = _overlay()
    ov.get("pins", {}).pop(args.cls, None)
    ((ov.get("policy") or {}).get("classes") or {}).pop(args.cls, None)
    _save(ov, "unpin", cls=args.cls)
    print(f"{args.cls} follows the kit default and the catalog again")
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


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("models", help="Find, approve and apply newer Claude models for the OpenClaw host")
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
                   help="Only this class (repeatable)")
    u.add_argument("--no-restart", action="store_true", help="Patch the config but leave the gateway running")
    u.add_argument("--unattended", action="store_true", help="Never prompt (the timer uses this)")
    u.add_argument("--json", action="store_true")
    u.set_defaults(func=cmd_update, refresh=True)

    a = sp.add_parser("approve", help="Approve one specific upgrade for later unattended runs")
    a.add_argument("cls")
    a.add_argument("id")
    a.set_defaults(func=cmd_approve)

    r = sp.add_parser("revoke", help="Remove a class's approval")
    r.add_argument("cls")
    r.set_defaults(func=cmd_revoke)

    pi = sp.add_parser("pin", help="Freeze a class at an explicit model id")
    pi.add_argument("cls")
    pi.add_argument("id")
    pi.set_defaults(func=cmd_pin)

    un = sp.add_parser("unpin", help="Let a class follow the catalog again")
    un.add_argument("cls")
    un.set_defaults(func=cmd_unpin)

    ex = sp.add_parser("exclude", help="Never apply a model id matching this glob")
    ex.add_argument("glob")
    ex.set_defaults(func=cmd_exclude)

    sp.add_parser("rollback", help="Undo the last switch (inverse patch, restart, health check)").set_defaults(func=cmd_rollback)
