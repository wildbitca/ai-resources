"""OpenClaw channel intent for `ai-resources setup` (UI only; ADR-0010 N5, plan S11).

The operator is shown the channel accounts OpenClaw lists (read-only: `openclaw channels list --all --json`) and
unticks the ones that should be off. Nothing runs here and nothing is written to OpenClaw: the answer is recorded
on the in-memory state (`openclaw.channels_seen` / `channels_wanted`), the Review prints what a disable would run
and how to undo it, and the executor (plan S12) is a separate, guarded step. A ticked, disabled account only
yields an `openclaw channels add ...` line for the operator to run; the kit never executes an enable.
"""
from __future__ import annotations

from typing import Callable

from .. import state, ui
from ... import openclaw_channels as channels

UNDO_NOTE = "(run it yourself)"
INVENTORY_ATTR = "_channel_inventory"
"""Instance attribute on the setup state (never serialised): the accounts listed in this run."""


def _runner() -> Callable[..., "tuple[int, str]"]:
    from ... import openclaw_host
    return openclaw_host.default_runner


def prompt(s: state.SetupState, *, runner: Callable[..., "tuple[int, str]"] | None = None) -> None:
    """Ask which listed channel accounts stay on. Unattended runs ask nothing and plan nothing."""
    if ui.is_non_interactive():
        return
    inventory = channels.list_channels(runner or _runner())
    if inventory is None:
        ui.detail("Could not read the OpenClaw channel list; channels stay as they are.")
        s.__dict__.pop(INVENTORY_ATTR, None)
        return
    if not inventory:
        return
    o = s.openclaw
    on = [a.key for a in inventory if a.enabled]
    saved_seen = set(o.channels_seen)
    # Start from the current truth: what is enabled stays ticked unless the operator unticked it in an earlier run
    # while it was still enabled and it is still listed as seen.
    default = [k for k in on if k not in saved_seen or k in o.channels_wanted]
    default += [k for k in o.channels_wanted if k not in on and k in {a.key for a in inventory}]
    choices = [ui.Choice(_title(a), value=a.key) for a in inventory]
    picked = ui.checkbox(
        "Which channel accounts should stay on? (unticking one asks to turn it off at Apply, after its own confirm)",
        choices, default=default)
    keys = {a.key for a in inventory}
    o.channels_seen = on
    o.channels_wanted = [k for k in (picked or []) if k in keys]
    setattr(s, INVENTORY_ATTR, inventory)


def _title(a: channels.ChannelAccount) -> str:
    state_text = "on" if a.enabled else "off"
    return f"{a.channel} / {a.account} ({state_text})"


def review_lines(s: state.SetupState) -> list[str]:
    """The lines the Review prints for the channel plan: the disable command with its undo, and the add command
    for a ticked, disabled account. Empty without an inventory read in this run."""
    inventory = getattr(s, INVENTORY_ATTR, None)
    if not inventory:
        return []
    out: list[str] = []
    for item in channels.plan_changes(s.openclaw.channels_seen, s.openclaw.channels_wanted, inventory):
        if isinstance(item, channels.PrintOnly):
            out.append(f"Turn on yourself: {item.text}")
        else:
            out.append(f"Turn off at Apply (after its own confirm): {item.command}")
            out.append(f"  undo: {item.undo_command}  {UNDO_NOTE}")
    return out
