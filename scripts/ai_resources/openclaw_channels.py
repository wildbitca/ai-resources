"""OpenClaw channel inventory and the pure disable planner (ADR-0010 N5, plan S3).

Read-only. `list_channels` runs `openclaw channels list --all --json` through an injectable runner and returns
the accounts it can read, or None when it cannot (missing binary, timeout, bad JSON, unknown shape). The planner
`plan_changes` is pure: it turns a (seen, wanted) pair into disable changes and print-only enable lines. The kit
only ever plans a DISABLE (`channels remove`, never `--delete`, never a credential flag). An ENABLE is printed
for the operator to run and is never an executable change.

Account names reach an argv, so every channel and account id must match `_ID` and be present in the inventory.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

Runner = Callable[..., "tuple[int, str]"]

LIST_ARGV = ("openclaw", "channels", "list", "--all", "--json")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class ChannelAccount:
    channel: str
    account: str
    enabled: bool = True
    agent: str | None = None      # the explicit owner the inventory names, if any

    @property
    def key(self) -> str:
        return f"{self.channel}:{self.account}"


@dataclass(frozen=True)
class ChannelChange:
    """An executable change. `kind` is only ever "disable"."""
    kind: str
    channel: str
    account: str
    argv: tuple[str, ...]
    undo_argv: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.channel}:{self.account}"

    @property
    def command(self) -> str:
        return " ".join(self.argv)

    @property
    def undo_command(self) -> str:
        return " ".join(self.undo_argv)


@dataclass(frozen=True)
class PrintOnly:
    """A ticked, disabled channel: the operator runs the add command; the kit never does."""
    channel: str
    account: str
    text: str


def add_argv(channel: str, account: str) -> tuple[str, ...]:
    return ("openclaw", "channels", "add", "--channel", channel, "--account", account)


def remove_argv(channel: str, account: str, agent: str | None = None) -> tuple[str, ...]:
    argv = ("openclaw", "channels", "remove", "--channel", channel, "--account", account)
    return argv + (("--agent", agent) if agent else ())


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


def parse_inventory(text: str) -> list[ChannelAccount] | None:
    """The accounts in a `channels list --all --json` payload, or None for a shape this parser does not know."""
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    chat = doc.get("chat") if isinstance(doc, dict) else None
    if not isinstance(chat, dict):
        return None
    out: list[ChannelAccount] = []
    for channel, info in chat.items():
        if not isinstance(info, dict) or not isinstance(info.get("accounts"), list):
            return None
        for raw in info["accounts"]:
            if isinstance(raw, str):
                account, enabled, agent = raw, True, None
            elif isinstance(raw, dict) and isinstance(raw.get("id"), str):
                account = raw["id"]
                enabled = raw.get("enabled", True)
                agent = raw.get("agent")
                if not isinstance(enabled, bool) or not (agent is None or isinstance(agent, str)):
                    return None
            else:
                return None
            if not (valid_id(channel) and valid_id(account) and (agent is None or valid_id(agent))):
                return None
            out.append(ChannelAccount(channel, account, enabled, agent))
    return out


def list_channels(runner: Runner, timeout: float = 15, *, oc: str = "openclaw") -> list[ChannelAccount] | None:
    argv = [oc, *LIST_ARGV[1:]]
    try:
        rc, text = runner(argv, timeout=timeout)
    except Exception:
        return None
    if rc != 0:
        return None
    return parse_inventory(text)


def plan_changes(seen: Iterable[str], wanted: Iterable[str],
                 inventory: Sequence[ChannelAccount]) -> list[ChannelChange | PrintOnly]:
    """Disable every account that was seen and is no longer wanted; print the enable for a ticked disabled one.

    Names come from the inventory only: a key that is not in it plans nothing.
    """
    by_key = {a.key: a for a in inventory}
    seen_set, wanted_set = set(seen), set(wanted)
    plan: list[ChannelChange | PrintOnly] = []
    for key in sorted(seen_set - wanted_set):
        acct = by_key.get(key)
        if acct is None or not acct.enabled:
            continue
        plan.append(ChannelChange("disable", acct.channel, acct.account,
                                  remove_argv(acct.channel, acct.account, acct.agent),
                                  add_argv(acct.channel, acct.account)))
    for key in sorted(wanted_set):
        acct = by_key.get(key)
        if acct is None or acct.enabled:
            continue
        plan.append(PrintOnly(acct.channel, acct.account,
                              " ".join(add_argv(acct.channel, acct.account)) + "  (run it yourself)"))
    return plan
