"""The one narrow channels writer (operator decision Q1, ADR-0013): two Slack cost keys, nothing else.

ADR-0001 and ADR-0010 keep the kit out of `channels.*`: `assert_channels_safe` stays the guard for every host
patch and refuses anything but `channels.telegram.streaming`. This module is a separate, explicit exception limited
to exactly:

  channels.slack.implicitMentions.threadParticipation = false   (the bot stops answering every message of a thread
                                                                 it once replied in)
  channels.slack.channels.<id>.allowBots = "mentions"           (a notification channel only reacts to bots that
                                                                 mention it; <id> is a channel the operator names)

Rules: fill-only (a key the operator set is kept), one confirm per path, interactive runs only, the old state is
recorded for teardown, and `assert_slack_cost_safe` refuses any patch outside those paths (no allowlist, no
`allowFrom`, no replace-path, no secret). Both keys are `hot` in the reload table, so nothing restarts.
"""
from __future__ import annotations

import json
import re

THREAD_PATH = ("channels", "slack", "implicitMentions", "threadParticipation")
ALLOW_BOTS_VALUE = "mentions"
_CHANNEL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def allow_bots_path(channel_id: str) -> tuple[str, ...]:
    return ("channels", "slack", "channels", channel_id, "allowBots")


def assert_slack_cost_safe(patch: dict, replace_paths: list[str] | None = None) -> None:
    """Refuse anything but the two keys above. Raises ValueError."""
    if replace_paths:
        raise ValueError("the Slack cost writer never sends a replace-path")
    leaves: list[tuple[str, ...]] = []

    def walk(node, path):
        if isinstance(node, dict) and node:
            for k, v in node.items():
                walk(v, path + (str(k),))
        else:
            leaves.append(path)

    walk(patch, ())
    for path in leaves:
        thread_ok = path == THREAD_PATH
        bots_ok = (len(path) == 5 and path[:3] == ("channels", "slack", "channels") and path[4] == "allowBots"
                   and bool(_CHANNEL_ID.match(path[3])))
        if not (thread_ok or bots_ok):
            raise ValueError("the Slack cost writer may only set implicitMentions.threadParticipation and "
                             f"channels.<id>.allowBots; refused: {'.'.join(path)}")
    # the values are fixed too
    if leaves:
        def get(path):
            cur = patch
            for k in path:
                cur = cur[k]
            return cur
        for path in leaves:
            want = False if path == THREAD_PATH else ALLOW_BOTS_VALUE
            if get(path) != want or isinstance(get(path), bool) != isinstance(want, bool):
                raise ValueError(f"the Slack cost writer sets only the fixed values; refused: {'.'.join(path)}")


def _get(doc: dict, path):
    cur = doc
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None, False
        cur = cur[k]
    return cur, True


def slack_channel_ids(doc: dict) -> list[str]:
    chans = ((doc.get("channels") or {}).get("slack") or {}).get("channels")
    return sorted(k for k in chans if isinstance(k, str) and _CHANNEL_ID.match(k)) if isinstance(chans, dict) else []


def slack_enabled(doc: dict) -> bool:
    slack = (doc.get("channels") or {}).get("slack")
    return isinstance(slack, dict) and slack.get("enabled") is not False


def thread_proposal(doc: dict) -> tuple[str, ...] | None:
    """The thread-participation path when the operator has not set it (fill-only), else None."""
    _, had = _get(doc, THREAD_PATH)
    return None if had else THREAD_PATH


def allow_bots_candidates(doc: dict) -> list[str]:
    """Slack channel ids with no `allowBots` of their own (an operator value is kept)."""
    return [c for c in slack_channel_ids(doc) if not _get(doc, allow_bots_path(c))[1]]


def build_patch(paths: list[tuple[str, ...]]) -> dict:
    patch: dict = {}
    for path in paths:
        cur = patch
        for k in path[:-1]:
            cur = cur.setdefault(k, {})
        cur[path[-1]] = False if path == THREAD_PATH else ALLOW_BOTS_VALUE
    assert_slack_cost_safe(patch)
    return patch


def change_records(paths: list[tuple[str, ...]], doc: dict) -> list[dict]:
    """State records so teardown deletes what the kit created (the highest ancestor that was absent)."""
    out = []
    for path in paths:
        delete = next(path[: i + 1] for i in range(len(path)) if not _get(doc, path[: i + 1])[1])
        out.append({"path": list(path), "previous": None, "had": False, "action": "filled", "delete": list(delete)})
    return out


def _allowed_leaf(path: tuple[str, ...]) -> bool:
    return path == THREAD_PATH or (len(path) == 5 and path[:3] == ("channels", "slack", "channels")
                                   and path[4] == "allowBots" and bool(_CHANNEL_ID.match(path[3])))


def restore_patch(changes: list[dict], doc: dict) -> dict:
    """The patch that deletes the recorded keys that still hold the kit's value.

    A record outside the two keys (a hand-edited state) is ignored, and so is a key the operator changed since. The
    delete target is the leaf or the `implicitMentions` block the kit created, never `channels` or `channels.slack`."""
    patch: dict = {}
    for ch in changes:
        path = tuple(ch.get("path") or ())
        if not _allowed_leaf(path):
            continue
        live, had = _get(doc, path)
        want = False if path == THREAD_PATH else ALLOW_BOTS_VALUE
        if not had or live != want or isinstance(live, bool) != isinstance(want, bool):
            continue                                   # absent or changed by hand since: not ours to remove
        delete = tuple(ch.get("delete") or path)
        if delete not in (path, THREAD_PATH[:3]) or delete[: len(delete)] != path[: len(delete)]:
            delete = path
        cur = patch
        for k in delete[:-1]:
            cur = cur.setdefault(k, {})
        cur[delete[-1]] = None
    return patch


def snippets(paths: list[tuple[str, ...]]) -> list[str]:
    return [f"openclaw config set {'.'.join(p)} {json.dumps(False if p == THREAD_PATH else ALLOW_BOTS_VALUE)}"
            for p in paths]
