"""OpenClaw channel inventory, the pure disable planner and the guarded disable executor (ADR-0010 N5, plan S3/S12).

`list_channels` runs `openclaw channels list --all --json` through the same bounded, scrubbed runner the executor
uses (60 s, capped output, an allow-listed environment) and returns the accounts it can read, or None when it cannot
(missing binary, timeout, bad JSON, unknown shape, oversize). The planner `plan_changes` is pure: it turns a
(seen, wanted) pair into disable changes and print-only enable lines. The kit only ever DISABLES an account
(`channels remove --channel C --account A`, never `--delete`, never a credential flag, never `--agent`). An ENABLE is
printed for the operator to run and is never an executable change; since a removed account lost its credentials,
the undo line says to re-add it with credentials.

`execute` runs ONE planned disable and checks every guard itself, whoever calls it: a terminal, not a dry run, not
`--yes`, the OpenClaw cockpit, reload rules that are readable and classify `channels.<C>` as hot, a fresh listing taken
immediately before (only an account still enabled in it is touched), a default-No confirm, an extra default-No confirm for
Telegram, a fresh listing afterwards and a report of any difference. Output is bounded and redacted; nothing it prints
or returns carries a credential.

Account names reach an argv, so every channel and account id must `fullmatch` `_ID` (never `$`, which accepts a trailing
newline) and be present in the inventory.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

Runner = Callable[..., "tuple[int, str]"]

LIST_ARGV = ("openclaw", "channels", "list", "--all", "--json")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TIMEOUT_S = 60
MAX_BYTES = 256 * 1024
OUTPUT_LINES = 6
OUTPUT_WIDTH = 200
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
            "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
_SECRET_NAME = re.compile(r"(token|secret|password|passwd|api[_-]?key|credential|auth)", re.I)
_SECRET_TEXT = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}"),                       # a Telegram bot token
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),                     # a Slack token
    re.compile(r"(?i)\b(?:bearer|token|secret|password|api[_-]?key)\b\s*[=:]?\s*\S+"),
    re.compile(r"[A-Za-z0-9+/_=-]{32,}"),                             # any long opaque string
)


@dataclass(frozen=True)
class ChannelAccount:
    channel: str
    account: str
    enabled: bool = True

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


def remove_argv(channel: str, account: str) -> tuple[str, ...]:
    """The one executable form. No `--agent`, no `--delete`, no credential flag: the account is only turned off."""
    return ("openclaw", "channels", "remove", "--channel", channel, "--account", account)


def valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def scrubbed_env(environ: dict | None = None) -> dict[str, str]:
    """The environment of every channel command: an allow-list, so no key, token or secret of the operator's shell
    reaches the `openclaw` child. `OPENCLAW_*` settings pass unless the name looks like a credential."""
    src = os.environ if environ is None else environ
    out = {k: src[k] for k in ENV_KEEP if k in src}
    out.update({k: v for k, v in src.items() if k.startswith("OPENCLAW_") and not _SECRET_NAME.search(k)})
    return out


def redact(text: str, *, lines: int = OUTPUT_LINES, width: int = OUTPUT_WIDTH) -> list[str]:
    """Bounded, redacted lines of a command's output: control characters out, secrets masked, at most `lines` lines
    of `width` characters."""
    out: list[str] = []
    for raw in str(text or "").splitlines():
        line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", raw).strip()
        if not line:
            continue
        for pattern in _SECRET_TEXT:
            line = pattern.sub("[redacted]", line)
        out.append(line[:width])
        if len(out) >= lines:
            break
    return out


def parse_inventory(text: str) -> list[ChannelAccount] | None:
    """The accounts in a `channels list --all --json` payload, or None for a shape this parser does not know."""
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(text, str) and len(text.encode("utf-8", "replace")) > MAX_BYTES:
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
                account, enabled = raw, True
            elif isinstance(raw, dict) and isinstance(raw.get("id"), str):
                account = raw["id"]
                enabled = raw.get("enabled", True)
                if not isinstance(enabled, bool):
                    return None
            else:
                return None
            if not (valid_id(channel) and valid_id(account)):
                return None
            out.append(ChannelAccount(channel, account, enabled))
    return out


def default_runner() -> Runner:
    from . import openclaw_host
    return openclaw_host.default_runner


def list_channels(runner: Runner | None = None, timeout: float = TIMEOUT_S, *, oc: str = "openclaw",
                  environ: dict | None = None) -> list[ChannelAccount] | None:
    """The accounts OpenClaw lists, read-only. The read has the executor's limits: its timeout, a byte cap and the
    scrubbed environment (the runner receives `env` and `max_bytes`; an unbounded answer is refused here as well)."""
    argv = [oc, *LIST_ARGV[1:]]
    try:
        rc, text = (runner or default_runner())(argv, timeout=timeout, env=scrubbed_env(environ), max_bytes=MAX_BYTES)
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
                                  remove_argv(acct.channel, acct.account),
                                  add_argv(acct.channel, acct.account)))
    for key in sorted(wanted_set):
        acct = by_key.get(key)
        if acct is None or acct.enabled:
            continue
        plan.append(PrintOnly(acct.channel, acct.account,
                              " ".join(add_argv(acct.channel, acct.account)) + "  (run it yourself)"))
    return plan


# --- the guarded executor (plan S12) ------------------------------------------------------------------------------

DONE, DECLINED, SKIPPED, REFUSED, FAILED = "done", "declined", "skipped", "refused", "failed"
CREDENTIAL_FLAG = re.compile(r"(?i)^--?(?:token|bot-?token|secret|password|passwd|api-?key|key|credential|auth\w*)")


@dataclass(frozen=True)
class Outcome:
    """What `execute` did. `lines` are safe to print; `ran` is True when the remove command was started."""
    status: str
    lines: tuple[str, ...]
    ran: bool = False


def undo_line(change: ChannelChange) -> str:
    """The way back. A removed account lost its credentials, so re-adding needs them: that is said, not implied."""
    return f"undo: re-add with credentials, yourself: {change.undo_command}"


def _is_planned(change: object, inventory: Sequence[ChannelAccount]) -> str:
    """"" when `change` is exactly a disable the planner could have made from `inventory`, else the reason."""
    if not isinstance(change, ChannelChange) or change.kind != "disable":
        return "only a planned disable can run"
    if not (valid_id(change.channel) and valid_id(change.account)):
        return "the channel or account name is not a plain id"
    if tuple(change.argv) != remove_argv(change.channel, change.account):
        return "the command is not the one remove form"
    if any(CREDENTIAL_FLAG.match(a) or a in ("add", "--delete") for a in change.argv):
        return "the command carries a forbidden word"
    if (change.channel, change.account) not in {(a.channel, a.account) for a in inventory}:
        return "the account is not in the inventory read in this run"
    return ""


def _enabled_keys(accounts: Sequence[ChannelAccount] | None) -> set[str] | None:
    return None if accounts is None else {a.key for a in accounts if a.enabled}


def execute(change: ChannelChange, *, runner: Runner, confirm: Callable[..., bool], tty: bool, dry_run: bool,
            yes: bool, cockpit: str, inventory: Sequence[ChannelAccount], reload_rules: object) -> Outcome:
    """Turn one channel account off, or say exactly why not. Every guard is checked here, in this order, and the first
    that fails ends the call before any command runs:

    planned from the inventory; a terminal; not a dry run; not `--yes`; the OpenClaw cockpit; reload rules read and
    `channels.<C>` hot; a fresh listing that still shows the account enabled; the operator's default-No confirm; and for
    Telegram a second default-No confirm. The remove command then runs once (60 s, capped output, scrubbed environment),
    and a listing taken afterwards is compared with what was expected."""
    from . import openclaw_reload_rules as rules
    bad = _is_planned(change, inventory)
    if bad:
        return Outcome(REFUSED, (f"{getattr(change, 'channel', '?')} / {getattr(change, 'account', '?')}: "
                                 f"refused: {bad}",))
    shown = f"{change.channel} / {change.account}"
    command = f"would run: {change.command}"
    undo = undo_line(change)
    if dry_run:
        return Outcome(SKIPPED, (f"{shown}: dry run: {command}", f"  {undo}"))
    if yes:
        return Outcome(REFUSED, (f"{shown}: refused: --yes does not confirm a channel change; {command}", f"  {undo}"))
    if not tty:
        return Outcome(REFUSED, (f"{shown}: refused: a channel change needs a terminal; {command}", f"  {undo}"))
    if cockpit != "openclaw":
        return Outcome(REFUSED, (f"{shown}: refused: the OpenClaw cockpit is not part of this run",))
    kind = rules.classify([f"channels.{change.channel}"], ruleset=reload_rules).get(f"channels.{change.channel}")
    if kind != rules.HOT:
        return Outcome(REFUSED, (f"{shown}: refused: OpenClaw's reload rules are unreadable or do not apply "
                                 f"channels.{change.channel} live; {command}", f"  {undo}"))
    before = list_channels(runner)
    if before is None:
        return Outcome(REFUSED, (f"{shown}: refused: the channel list could not be read just before the change; "
                                 "nothing was changed",))
    enabled_before = _enabled_keys(before) or set()
    if change.key not in enabled_before:
        return Outcome(SKIPPED, (f"{shown}: already off or no longer listed; nothing to do",))
    if not confirm(f"Turn off {shown}? OpenClaw may stop that account and restart its listener "
                   "(never the gateway).", default=False):
        return Outcome(DECLINED, (f"{shown}: left on", f"  {command}"))
    if change.channel == "telegram" and not confirm(
            "Telegram is how you reach OpenClaw. Turn this Telegram account off anyway?", default=False):
        return Outcome(DECLINED, (f"{shown}: left on (Telegram needs its own yes)", f"  {command}"))
    try:
        rc, text = runner(list(change.argv), timeout=TIMEOUT_S, env=scrubbed_env(), max_bytes=MAX_BYTES)
    except Exception as exc:  # noqa: BLE001 - reported, not raised: Apply goes on
        rc, text = 1, type(exc).__name__
    detail = [f"  {line}" for line in redact(text)]
    if rc != 0:
        why = "timed out after 60 s" if rc == 124 else f"failed (exit {rc})"
        return Outcome(FAILED, (f"{shown}: turning it off {why}; check `openclaw channels list`", *detail,
                                f"  {undo}"), True)
    after = _enabled_keys(list_channels(runner))
    lines = [f"{shown}: turned off ({change.command})"]
    if after is None:
        lines.append("  the channel list could not be read afterwards; check `openclaw channels list`")
    else:
        expected = enabled_before - {change.key}
        for key in sorted(after - expected):
            lines.append(f"  unexpected: {key} is on after the change")
        for key in sorted(expected - after):
            lines.append(f"  unexpected: {key} is off after the change")
        if change.key in after:
            lines.append(f"  {shown} is still listed as on")
    lines += detail
    lines.append(f"  {undo}")
    return Outcome(DONE, tuple(lines), True)
