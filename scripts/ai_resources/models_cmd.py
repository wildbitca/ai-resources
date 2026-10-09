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
        return _interactive_update(args, deps)
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
