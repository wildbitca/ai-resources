#!/usr/bin/env python3
"""Keep ONE live Telegram message per team member, by editing it.

WHY IT EXISTS
OpenClaw draws a live draft with the tools of the PARENT agent, but drops everything a
sub-agent does (`isClaudeSubagentRecord`, cli-live-session-registry-*.mjs:280). The
`openclaw_team_progress.py` hook reports the seams -- start, pulse, edit, finish -- and that
leaves a gap: what the member does and says while it works.

This process reads the member's transcript and mirrors it into a single message that is
edited, the way OpenClaw does with its own. One message per member, not one per event: it is
the only way to give fidelity without running into Telegram's limits.

WHAT CAN BE SHOWN AND WHAT CANNOT (measured on 2026-09-18)
  yes: `assistant/text` (the member's narration), `assistant/tool_use` (tool and target), and
       its final message.
  no:  the THINKING. There are 151 `assistant/thinking` blocks in recent transcripts and ZERO
       with text: only the signature is persisted. It is not in the hooks, not here and not in
       OTel. If it is ever persisted, this file is where it gets added.

LIFECYCLE
The hook launches it detached on the member's first tool (that is when its `agent_id` shows
up). It dies when the SubagentStop hook touches its marker, or when the transcript stops
growing, or at the time cap. It never hangs around.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

OPENCLAW = shutil.which("openclaw") or "/home/linuxbrew/.linuxbrew/bin/openclaw"
STATE_DIR = f"/run/user/{os.getuid()}/openclaw-team-hook"
EDIT_INTERVAL = 4.0        # Telegram does not appreciate a faster pace than this
TRANSCRIPT_WAIT = 25.0     # the member's file takes a moment to exist
IDLE_STOP = 90.0           # no new lines for this long: close
LIFETIME_CAP = 1800.0
MAX_ROWS = 12
MAX_CHARS = 3500


def sh(args, capture=False):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
        return r.stdout if capture else None
    except Exception:
        return None


def send(chat, thread, text):
    out = sh([OPENCLAW, "message", "send", "--channel", "telegram",
              "--target", chat, "--thread-id", thread, "--message", text], capture=True) or ""
    m = re.search(r"Message ID:\s*(\d+)", out)
    return m.group(1) if m else None


def edit(chat, thread, mid, text):
    sh([OPENCLAW, "message", "edit", "--channel", "telegram", "--target", chat,
        "--thread-id", thread, "--message-id", mid, "--message", text])


def short(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return (s[: n - 1] + "…") if len(s) > n else s


def target_of(name, tool_input):
    """What makes a call identifiable without dumping its arguments."""
    if not isinstance(tool_input, dict):
        return ""
    for k in ("file_path", "notebook_path", "path", "pattern", "skill", "subagent_type", "url"):
        if tool_input.get(k):
            return short(str(tool_input[k]).replace(os.path.expanduser("~"), "~"), 54)
    if tool_input.get("command"):
        return short(str(tool_input["command"]).split("\n")[0], 54)
    if tool_input.get("query"):
        return short(tool_input["query"], 54)
    return ""


def render(role, model, rows, tools, t0, closed):
    head = f"\U0001F464 *{role}*" + (f" · {model}" if model else "")
    status = "finished" if closed else "working"
    head += f" · {status} · {int(time.time() - t0)}s · {tools} tools"
    body = "\n".join(rows[-MAX_ROWS:])
    text = head + ("\n" + body if body else "")
    return text[:MAX_CHARS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--role", default="member")
    ap.add_argument("--model", default="")
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--chat", required=True)
    ap.add_argument("--thread", required=True)
    a = ap.parse_args()

    stop_marker = os.path.join(STATE_DIR, f"stop-{a.agent_id}")
    t0 = time.time()

    # The transcript may not exist yet when the first tool arrives.
    while not os.path.exists(a.transcript):
        if time.time() - t0 > TRANSCRIPT_WAIT:
            return
        time.sleep(0.5)

    mid = send(a.chat, a.thread, render(a.role, a.model, [], 0, t0, False))
    if not mid:
        return

    rows, tools = [], 0
    pos = 0
    last_line = time.time()
    last_edit = 0.0
    dirty = False
    closed = False

    while True:
        try:
            with open(a.transcript, errors="ignore") as fh:
                fh.seek(pos)
                new = fh.readlines()
                pos = fh.tell()
        except Exception:
            new = []

        for line in new:
            try:
                d = json.loads(line)
            except Exception:
                continue
            m = d.get("message") or {}
            c = m.get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if b.get("type") == "tool_use":
                    tools += 1
                    target = target_of(b.get("name"), b.get("input"))
                    rows.append(f"\U0001F6E0️ `{short(b.get('name'), 22)}`" + (f" {target}" if target else ""))
                    dirty = True
                elif b.get("type") == "text" and (b.get("text") or "").strip():
                    rows.append("\U0001F4AC " + short(b["text"], 150))
                    dirty = True
            if new:
                last_line = time.time()

        if os.path.exists(stop_marker):
            closed = True
        if time.time() - last_line > IDLE_STOP or time.time() - t0 > LIFETIME_CAP:
            closed = True

        if (dirty or closed) and time.time() - last_edit >= EDIT_INTERVAL:
            edit(a.chat, a.thread, mid, render(a.role, a.model, rows, tools, t0, closed))
            last_edit = time.time()
            dirty = False

        if closed:
            try:
                os.remove(stop_marker)
            except Exception:
                pass
            return
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
