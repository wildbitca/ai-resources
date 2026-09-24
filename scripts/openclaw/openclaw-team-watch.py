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
up) and launches it again if it finds the process gone while the member still runs. The old
rule closed it after 90 s without new transcript lines, but a member blocks for minutes inside
one tool call (sleep + watch loops), so the watcher died while the member was still working and
the topic went silent. Now it closes only when:
  - the hook touched stop-<agent_id> (SubagentStop): the member finished; or
  - the claude process that runs the member is gone (--claude-pid): nobody is left; or
  - the member has been silent for SILENCE_NO_PARENT and the parent cannot be checked; or
  - the member has been silent for SILENCE_HARD, or LIFETIME_CAP is reached (hard ceilings).
While the member is quiet it keeps editing its message every HEARTBEAT seconds ("still running,
last tool X, N s since last activity"), so the topic never looks dead.

DELIVERY
Every send and edit goes through openclaw-team-send.py (next to this file): one queue per chat,
paced, with retries on a 429, and a log and a dead-letter directory for whatever could not be
delivered. An edit is handed over as a function, so it is rendered when its turn comes and carries the
member's state at that moment. If that file cannot be loaded the watcher falls back to calling the
CLI directly, as it did before.
The hook writes this process' PID into watch-<agent_id>. On a relaunch the message is reused
(mid-<agent_id>) instead of opening a second one, and the transcript is replayed from the start
so the rows and counters are rebuilt.

A member started by a Workflow writes its transcript to
<session>/subagents/workflows/<workflow id>/agent-<id>.jsonl, not to
<session>/subagents/agent-<id>.jsonl, and the workflow id is not in the hook payload. The
transcript is therefore looked up while waiting for it to appear.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

# The two overrides below and the OPENCLAW_TEAM_WATCH_* timings exist so the tests can run this
# process against a throwaway state directory, a stub `openclaw` and short clocks. In production
# none of them is set and the defaults apply.
OPENCLAW = (os.environ.get("OPENCLAW_TEAM_HOOK_BIN") or shutil.which("openclaw")
            or "/home/linuxbrew/.linuxbrew/bin/openclaw")
STATE_DIR = os.environ.get("OPENCLAW_TEAM_HOOK_STATE") or f"/run/user/{os.getuid()}/openclaw-team-hook"


def _seconds(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


EDIT_INTERVAL = _seconds("OPENCLAW_TEAM_WATCH_INTERVAL", 6.0)        # one chat shares its budget among all topics
HEARTBEAT = _seconds("OPENCLAW_TEAM_WATCH_HEARTBEAT", 60.0)          # edit even while the member is quiet
TRANSCRIPT_WAIT = _seconds("OPENCLAW_TEAM_WATCH_WAIT", 25.0)         # the member's file takes a moment to exist
SILENCE_NO_PARENT = _seconds("OPENCLAW_TEAM_WATCH_SILENCE", 1500.0)  # 25 min, only when the parent is unknown
SILENCE_HARD = _seconds("OPENCLAW_TEAM_WATCH_SILENCE_HARD", 7200.0)  # 2 h with no transcript line at all
LIFETIME_CAP = _seconds("OPENCLAW_TEAM_WATCH_LIFETIME", 6 * 3600.0)
QUIET_AFTER = _seconds("OPENCLAW_TEAM_WATCH_QUIET", 20.0)  # the "still running" line shows after this much quiet
TICK = _seconds("OPENCLAW_TEAM_WATCH_TICK", 1.0)           # how often the transcript is polled
MAX_ROWS = 12
MAX_CHARS = 3500


def _load_bus():
    """The shared delivery queue, or None when it cannot be loaded (then the CLI is called directly)."""
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "openclaw-team-send.py")
        spec = importlib.util.spec_from_file_location("openclaw_team_send", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


BUS = _load_bus()


def sh(args, capture=False):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
        return r.stdout if capture else None
    except Exception:
        return None


def send(chat, thread, text):
    if BUS is not None:
        _, mid = BUS.deliver("send", chat, thread, text=text)
        return mid or None
    out = sh([OPENCLAW, "message", "send", "--channel", "telegram",
              "--target", chat, "--thread-id", thread, "--message", text], capture=True) or ""
    m = re.search(r"Message ID:\s*(\d+)", out)
    return m.group(1) if m else None


def edit(chat, thread, mid, text):
    """`text` is a string or a function returning one; a function is called when the edit's turn comes."""
    if BUS is not None:
        BUS.deliver("edit", chat, thread, mid=mid, text=None if callable(text) else text,
                    text_fn=text if callable(text) else None)
        return
    sh([OPENCLAW, "message", "edit", "--channel", "telegram", "--target", chat,
        "--thread-id", thread, "--message-id", mid, "--message", text() if callable(text) else text])


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


def render(role, model, rows, tools, t0, reason, last_tool=None, idle=0.0):
    """`reason` is None while running, "stop" when the member finished, anything else when the
    watcher gave up without a stop signal."""
    head = f"\U0001F464 *{role}*" + (f" · {model}" if model else "")
    if reason is None:
        status = "working"
    elif reason == "stop":
        status = "finished"
    else:
        status = "no signal from the member"
    head += f" · {status} · {int(time.time() - t0)}s · {tools} tools"
    body = "\n".join(rows[-MAX_ROWS:])
    if reason is None and last_tool and idle >= QUIET_AFTER:
        # Only shown once the member has been quiet for a moment, so a busy member's message is
        # not cluttered. It also guarantees the text changes between heartbeats.
        body += ("\n" if body else "") + f"⏱️ still running · last `{last_tool}` · {int(idle)}s since last activity"
    text = head + ("\n" + body if body else "")
    return text[:MAX_CHARS]


def resolve_transcript(path, agent_id):
    """`path` is <session>/subagents/agent-<id>.jsonl. A workflow member writes to
    <session>/subagents/workflows/<workflow id>/agent-<id>.jsonl instead."""
    if os.path.exists(path):
        return path
    root = os.path.dirname(path)
    found = glob.glob(os.path.join(glob.escape(root), "workflows", "*", f"agent-{glob.escape(agent_id)}.jsonl"))
    return found[0] if found else path


def parent_alive(pid):
    """True / False when the claude process is known, None when it was not passed."""
    if not pid or pid <= 1:
        return None
    try:
        os.kill(pid, 0)
        with open(f"/proc/{pid}/stat") as fh:
            if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
        with open(f"/proc/{pid}/comm") as fh:
            return fh.read().strip() == "claude"
    except Exception:
        return False


def close_reason(now, t0, last_line, has_stop, parent):
    """Why the watcher should close now, or None to keep going. Pure so it can be tested."""
    if has_stop:
        return "stop"
    if now - t0 > LIFETIME_CAP:
        return "lifetime"
    if parent is False:
        return "parent"
    silence = now - last_line
    if silence > SILENCE_HARD:
        return "silence"
    if parent is None and silence > SILENCE_NO_PARENT:
        return "silence"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--role", default="member")
    ap.add_argument("--model", default="")
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--chat", required=True)
    ap.add_argument("--thread", required=True)
    ap.add_argument("--claude-pid", type=int, default=0)
    a = ap.parse_args()

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", a.agent_id)[:70]
    stop_marker = os.path.join(STATE_DIR, f"stop-{a.agent_id}")
    watch_marker = os.path.join(STATE_DIR, f"watch-{safe}")
    msg_marker = os.path.join(STATE_DIR, f"mid-{safe}")
    me = str(os.getpid())

    try:
        live(a, stop_marker, msg_marker, time.time())
    finally:
        # Leave no marker behind that claims a watcher exists. The hook wrote our PID there; only
        # remove it when it is still ours (a relaunch may have replaced it).
        try:
            with open(watch_marker) as fh:
                if fh.read().strip() == me:
                    os.remove(watch_marker)
        except Exception:
            pass


def live(a, stop_marker, msg_marker, t0):
    # The transcript may not exist yet when the first tool arrives.
    while True:
        a.transcript = resolve_transcript(a.transcript, a.agent_id)
        if os.path.exists(a.transcript):
            break
        if time.time() - t0 > TRANSCRIPT_WAIT:
            return
        time.sleep(0.5)

    # A relaunched watcher keeps editing the message the previous one opened; the transcript is
    # replayed from the start below, so the rows and counters are rebuilt.
    mid = None
    try:
        with open(msg_marker) as fh:
            parts = fh.read().strip().split("|")
        mid = parts[0] or None
        if len(parts) > 1 and parts[1]:
            t0 = float(parts[1])
    except Exception:
        mid = None
    if not mid:
        mid = send(a.chat, a.thread, render(a.role, a.model, [], 0, t0, None))
        if not mid:
            return
        try:
            with open(msg_marker, "w") as fh:
                fh.write(f"{mid}|{t0}")
        except Exception:
            pass

    rows, tools = [], 0
    last_tool = None
    pos = 0
    last_line = time.time()
    last_edit = 0.0
    dirty = True  # a relaunch has to repaint the reused message

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
                    last_tool = short(b.get("name"), 22)
                    target = target_of(b.get("name"), b.get("input"))
                    rows.append(f"\U0001F6E0️ `{last_tool}`" + (f" {target}" if target else ""))
                    dirty = True
                elif b.get("type") == "text" and (b.get("text") or "").strip():
                    rows.append("\U0001F4AC " + short(b["text"], 150))
                    dirty = True
        if new:
            last_line = time.time()

        now = time.time()
        reason = close_reason(now, t0, last_line, os.path.exists(stop_marker), parent_alive(a.claude_pid))
        idle = now - last_line

        if reason:
            # The closing edit is never skipped by the rate limit: wait out the interval so the
            # last thing the topic shows is the final state, not a stale "working".
            wait = EDIT_INTERVAL - (time.time() - last_edit)
            if wait > 0:
                time.sleep(wait)
            edit(a.chat, a.thread, mid,
                 lambda: render(a.role, a.model, rows, tools, t0, reason, last_tool, idle))
            if reason == "stop":
                for path in (stop_marker, msg_marker):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
            return

        if (dirty or now - last_edit >= HEARTBEAT) and now - last_edit >= EDIT_INTERVAL:
            edit(a.chat, a.thread, mid,
                 lambda: render(a.role, a.model, rows, tools, t0, None, last_tool, time.time() - last_line))
            last_edit = time.time()
            dirty = False
        time.sleep(TICK)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
