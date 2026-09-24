#!/usr/bin/env python3
"""Narrate what a Claude Code team is doing into the Telegram topic of the OpenClaw agent.

WHY THIS FILE EXISTS AND NOT A CONFIG OPTION
OpenClaw receives the sub-agent events and DROPS them on purpose. In
dist/cli-live-session-registry-BoIpFmTy.mjs:280 it defines

    function isClaudeSubagentRecord(parsed) { return parsed.parent_tool_use_id != null; }

and uses it as an early return at 284, 389, 590, 726, 1091 and 1111: tool rows, reasoning,
text deltas, assistant messages and the sub-agent's system/init record. There is no knob
that lifts it. So team progress is published from the Claude Code side, where the events do
exist, and delivered with `openclaw message send`.

WHAT ACTUALLY ARRIVES (measured on 2026-09-18 with a hook that only logged)
  PreToolUse/Agent   -> tool_input.subagent_type, effort, cwd, session_id   (no agent_id yet)
  PreToolUse/<tool>  -> agent_id + agent_type      when the call is INSIDE a team member
  SubagentStop       -> agent_id, agent_type, last_assistant_message, agent_transcript_path
The MODEL is not in the payload: it is resolved from the role's frontmatter in ~/.claude/agents.

RULES
- Speaks only when OPENCLAW_CLI=1 (that is, only in sessions started by the gateway) AND the
  host env file opts in (OPENCLAW_NARRATION is `milestones` or `every-step`). In a plain
  terminal, or without that key, it is a silent no-op: OPENCLAW_CLI=1 is the normal state on a
  gateway host, so it cannot be the only gate for a hook that publishes to a chat.
- Never publishes prompts, raw tool_input or tool output: only the role, the file and the
  member's final message, trimmed.
- Volume ceilings, because Telegram is not a terminal: EDITS_PER_MEMBER and a sliding window
  of MESSAGES_PER_WINDOW per WINDOW_SECONDS per session. Over the ceiling it says so once per
  window and goes quiet until the window recovers. Milestones (the request, the team start,
  each member's hand-off, the workflow banner, the turn close) bypass the window and are
  never dropped: a session that lives for days must not fall silent for good.
- Each member's live message is kept by a detached watcher process. The watch-<agent_id> marker
  holds that process' PID: a marker whose process is gone is dead, it is swept, it does not
  count against MAX_WATCHERS, and the member's next tool call launches the watcher again.
- Detail level, read from `~/.openclaw/kit-host.env` (OPENCLAW_NARRATION):
    (absent)     off: nothing is published, nothing is logged. The default.
    milestones   the request, the team start, each member's hand-off, the close
    every-step   also each edit, the periodic pulse and the per-member live message
  Any other value also means off: an unreadable choice must not publish.
- Absolute best effort: any failure exits 0 and says nothing.
"""
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

OPENCLAW_JSON = os.path.expanduser("~/.openclaw/openclaw.json")
KIT_HOST_ENV = os.path.expanduser("~/.openclaw/kit-host.env")
AGENTS_DIR = os.path.expanduser("~/.claude/agents")
LOG_PAYLOADS = os.path.expanduser("~/.openclaw/logs/team-hook.jsonl")
STATE_DIR = f"/run/user/{os.getuid()}/openclaw-team-hook"
MAX_LOG = 2_000_000
EDITS_PER_MEMBER = 6
# Sliding window for non-milestone messages. A lifetime cap silenced sessions that live for
# days (an elinvo workflow went mute for hours on 2026-09-24); a window recovers by itself.
WINDOW_SECONDS = 600
MESSAGES_PER_WINDOW = 30
PULSE_EVERY = 3  # measured: members batch a lot in Bash; with 5 it almost never pulsed
MAX_DESC = 90
MAX_RESULT = 260
MAX_WATCHERS = 3
WATCHER_TAG = "openclaw-team-watch"
DEFAULT_NARRATION = "off"
NARRATION_LEVELS = ("milestones", "every-step")

# Fallbacks for a session whose PATH does not carry the Homebrew prefix.
OPENCLAW_FALLBACKS = ("/home/linuxbrew/.linuxbrew/bin/openclaw", "/opt/homebrew/bin/openclaw",
                      "/usr/local/bin/openclaw")

# What reaches Telegram. Kept in one place so it can be localised without touching the logic.
MESSAGES = {
    "request": "\U0001F4E5 request received · model *{model}*{tail}",
    "turn_done": "\U0001F3C1 turn complete",
    "member_done": "✅ *{role}* finished",
    "team_start": "\U0001F465 team · starting *{role}* ({model}{tail})",
    "inherited": "{model} (inherited)",
    "inherited_unknown": "inherited model",
    "workflow": "\U0001F9E9 workflow *{name}*",
    "workflow_phases": " · {n} phases",
    "unnamed": "unnamed",
    "a_member": "a member",
    "edit": "✏️ *{role}* → `{path}`",
    "edit_more": "✏️ *{role}* keeps editing (rest omitted)",
    "pulse": "⏳ *{role}* · {n} tools · {secs}s · last `{tool}`",
    "rate_omitted": "⏸️ messages omitted to keep the pace (max {n} every {mins} min); milestones keep coming",
}


def read_payload():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def clean(text, limit=MAX_DESC):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return (text[: limit - 1] + "…") if len(text) > limit else text


def log_payload(p):
    """Skeleton of the payload on disk: lets the hook be redesigned on data, not on faith."""
    try:
        if os.path.exists(LOG_PAYLOADS) and os.path.getsize(LOG_PAYLOADS) > MAX_LOG:
            # One generation is kept: rotating beats going silent at the cap.
            os.replace(LOG_PAYLOADS, LOG_PAYLOADS + ".1")
        ti = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        os.makedirs(os.path.dirname(LOG_PAYLOADS), exist_ok=True)
        with open(LOG_PAYLOADS, "a") as fh:
            fh.write(json.dumps({
                "t": time.strftime("%H:%M:%S"),
                "event": p.get("hook_event_name"),
                "tool": p.get("tool_name"),
                "payload_keys": sorted(p.keys()),
                "agent_id": p.get("agent_id"),
                "agent_type": p.get("agent_type"),
                "subagent_type": ti.get("subagent_type"),
                "cwd": p.get("cwd"),
            }) + "\n")
    except Exception:
        pass


def narration_level():
    """`milestones` or `every-step` when the host env file opts in; `off` otherwise.

    Absent file, absent key, an unknown value or an unreadable file all mean `off`: this hook
    publishes to a chat, so it speaks only when the operator asked for it."""
    try:
        with open(KIT_HOST_ENV) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("OPENCLAW_NARRATION="):
                    value = line.split("=", 1)[1].strip().strip("'\"")
                    return value if value in NARRATION_LEVELS else DEFAULT_NARRATION
    except Exception:
        pass
    return DEFAULT_NARRATION


def openclaw_bin():
    found = shutil.which("openclaw")
    if found:
        return found
    for candidate in OPENCLAW_FALLBACKS:
        if os.path.exists(candidate):
            return candidate
    return "openclaw"


def kit_root():
    env = os.environ.get("AGENT_KIT")
    if env:
        return env
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def watcher_path():
    """The kit's watcher, else the pre-kit location so an in-flight host keeps working."""
    for candidate in (os.path.join(kit_root(), "scripts", "openclaw", "openclaw-team-watch.py"),
                      os.path.expanduser("~/.local/bin/openclaw-team-watch.py")):
        if os.path.exists(candidate):
            return candidate
    return ""


def effort_of(p):
    """`effort` arrives as a string in some events and as an object in others (measured: the
    Telegram message came out as "{'level': '..." because the dict's repr was printed)."""
    e = p.get("effort")
    if isinstance(e, dict):
        e = e.get("level") or e.get("effort") or e.get("value")
    return str(e) if isinstance(e, (str, int)) else None


def _load_config():
    try:
        with open(OPENCLAW_JSON) as fh:
            return json.load(fh)
    except Exception:
        return None


def _agent_for(cfg, cwd):
    """(agent id, workspace length) of the agent whose workspace is the longest prefix of cwd."""
    cwd = os.path.realpath(cwd or os.getcwd())
    agent, best = None, -1
    for aid, entry in ((cfg.get("agents") or {}).get("entries") or {}).items():
        ws = entry.get("workspace")
        if not ws:
            continue
        ws = os.path.realpath(os.path.expanduser(ws))
        if (cwd == ws or cwd.startswith(ws + os.sep)) and len(ws) > best:
            agent, best = aid, len(ws)
    return agent


def agent_model(cwd):
    """The model a sub-agent without its own `model:` inherits is the one of the OpenClaw agent
    that launched it. Saying "inherits the agent's" told nobody anything."""
    cfg = _load_config()
    if cfg is None:
        return None
    cwd = os.path.realpath(cwd or os.getcwd())
    best, model = -1, None
    defaults = cfg.get("agents", {})
    default_model = ((defaults.get("defaults") or {}).get("model") or {}).get("primary")
    for entry in (defaults.get("entries") or {}).values():
        ws = entry.get("workspace")
        if not ws:
            continue
        ws = os.path.realpath(os.path.expanduser(ws))
        if (cwd == ws or cwd.startswith(ws + os.sep)) and len(ws) > best:
            best = len(ws)
            model = ((entry.get("model") or {}).get("primary")) or default_model
    m = model or default_model
    return m.split("/")[-1] if m else None


def role_model(role):
    """Each member's model lives in the frontmatter of its definition, not in the payload."""
    if not role:
        return None
    path = os.path.join(AGENTS_DIR, f"{os.path.basename(str(role))}.md")
    try:
        with open(path) as fh:
            for line in fh.read().split("---")[1].splitlines():
                if line.startswith("model:"):
                    return line.split(":", 1)[1].strip() or None
    except Exception:
        return None
    return None


def resolve_target(cwd):
    """cwd -> (chatId, threadId) through the agent's workspace. The team worker (no topic of
    its own) is reported in the main agent's topic."""
    cfg = _load_config()
    if cfg is None:
        return None
    agent = _agent_for(cfg, cwd)
    if not agent:
        return None
    fallback = None
    groups = ((cfg.get("channels") or {}).get("telegram") or {}).get("groups") or {}
    for chat_id, group in groups.items():
        for thread_id, topic in (group.get("topics") or {}).items():
            if not (isinstance(topic, dict) and thread_id.isdigit()):
                continue
            if topic.get("agentId") == agent:
                return chat_id, thread_id
            if topic.get("agentId") == "main":
                fallback = (chat_id, thread_id)
    return fallback


def _safe(key, limit=70):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))[:limit]


def counter(key, ceiling):
    """Counts under /run (gone on reboot, which is exactly what is wanted).
    Returns (allowed, is_the_first_one_over)."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, _safe(key, 80))
        n = 0
        if os.path.exists(path):
            with open(path) as fh:
                n = int((fh.read() or "0").strip() or 0)
        n += 1
        with open(path, "w") as fh:
            fh.write(str(n))
        return n <= ceiling, n == ceiling + 1
    except Exception:
        return True, False


def pulse(agent_id):
    """How often to speak and how long the member has been at it. Without this a member that
    works for five minutes says nothing between its start and its end."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, "pulse-" + _safe(agent_id))
        n, t0 = 0, time.time()
        if os.path.exists(path):
            with open(path) as fh:
                parts = (fh.read() or "").split("|")
            n = int(parts[0] or 0)
            t0 = float(parts[1]) if len(parts) > 1 and parts[1] else t0
        n += 1
        with open(path, "w") as fh:
            fh.write(f"{n}|{t0}")
        return (n, int(time.time() - t0)) if n % PULSE_EVERY == 0 else (None, None)
    except Exception:
        return (None, None)


def window(session):
    """Sliding-window rate limit per session. Returns (allowed, first_one_over).

    The state file holds the timestamps of the messages sent in the last WINDOW_SECONDS and the
    time the "omitted" notice was last shown, so the notice appears once per window. The
    read-modify-write is flock'ed because the hook runs concurrently for parallel tool calls."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, "window-" + _safe(session))
        now = time.time()
        with open(path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            try:
                data = json.loads(fh.read() or "{}")
            except Exception:
                data = {}
            stamps = [t for t in data.get("t", []) if isinstance(t, (int, float)) and now - t < WINDOW_SECONDS]
            noticed_at = data.get("notice", 0) or 0
            noticed = bool(noticed_at) and now - noticed_at < WINDOW_SECONDS
            if len(stamps) < MESSAGES_PER_WINDOW:
                stamps.append(now)
                allowed, first = True, False
            else:
                allowed, first = False, not noticed
                if first:
                    noticed_at = now
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"t": stamps, "notice": noticed_at}))
        return allowed, first
    except Exception:
        return True, False


def member_transcript(p):
    """The parent transcript is `<proj>/<session>.jsonl`; the member's lives in
    `<proj>/<session>/subagents/agent-<agent_id>.jsonl`. Verified on disk.

    Members started by a Workflow live one level down, in
    `subagents/workflows/<workflow id>/agent-<agent_id>.jsonl` (measured on 2026-09-24: 346 of
    655 member transcripts). Looking only at the first layout meant no workflow member ever got
    a live message: its watcher waited for a file that was never there. When the file does not
    exist yet the direct path is returned and the watcher keeps looking."""
    tp, aid = p.get("transcript_path"), p.get("agent_id")
    if not (tp and aid):
        return None
    base = str(tp)[:-6] if str(tp).endswith(".jsonl") else str(tp)
    root = os.path.join(base, "subagents")
    direct = os.path.join(root, f"agent-{aid}.jsonl")
    if os.path.exists(direct):
        return direct
    found = glob.glob(os.path.join(glob.escape(root), "workflows", "*", f"agent-{glob.escape(str(aid))}.jsonl"))
    return found[0] if found else direct


def _mark_path(aid):
    return os.path.join(STATE_DIR, f"watch-{_safe(aid)}")


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode(errors="ignore")
    except Exception:
        return ""


def _pid_is_watcher(pid, aid):
    """True only for a live, non-zombie process that is this member's watcher. The command line
    is checked too, to guard against the kernel reusing the PID for something else."""
    try:
        pid = int(pid)
        if pid <= 1:
            return False
        os.kill(pid, 0)
        with open(f"/proc/{pid}/stat") as fh:
            if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
    except Exception:
        return False
    cmd = _cmdline(pid)
    return WATCHER_TAG in cmd and str(aid) in cmd


def _scan_watchers():
    """agent_id -> pid of every live watcher process. Only needed for markers written by an
    older hook, which are empty: they carry no PID to check."""
    alive = {}
    try:
        entries = os.listdir("/proc")
    except Exception:
        return alive
    for d in entries:
        if not d.isdigit():
            continue
        cmd = _cmdline(d)
        if WATCHER_TAG in cmd and "--agent-id" in cmd:
            m = re.search(r"--agent-id\s+(\S+)", cmd)
            if m:
                alive[m.group(1)] = int(d)
    return alive


def watcher_alive(aid, scan=None):
    """True when the member's watcher process is alive. The marker alone proves nothing: it
    outlives the process whenever the watcher exits without a SubagentStop."""
    try:
        with open(_mark_path(aid)) as fh:
            content = fh.read().strip()
    except Exception:
        return False
    if content.isdigit():
        return _pid_is_watcher(content, aid)
    return str(aid) in (scan if scan is not None else _scan_watchers())


def _empty_mark(name):
    try:
        return os.path.getsize(os.path.join(STATE_DIR, name)) == 0
    except Exception:
        return False


def sweep_markers():
    """Remove the markers whose watcher is gone and return how many are alive. A dead marker
    must not count against MAX_WATCHERS nor suppress the relaunch."""
    try:
        marks = [f for f in os.listdir(STATE_DIR) if f.startswith("watch-")]
    except Exception:
        return 0
    scan = _scan_watchers() if any(_empty_mark(f) for f in marks) else None
    alive = 0
    for f in marks:
        if watcher_alive(f[len("watch-"):], scan):
            alive += 1
        else:
            try:
                os.remove(os.path.join(STATE_DIR, f))
            except Exception:
                pass
    return alive


def sweep_stops(days=1):
    """stop-<id> markers are only removed by a watcher that saw them; the ones with no watcher
    pile up. Anything older than `days` belongs to a member that is long gone."""
    try:
        limit = time.time() - days * 86400
        for f in os.listdir(STATE_DIR):
            if f.startswith("stop-"):
                path = os.path.join(STATE_DIR, f)
                if os.path.getmtime(path) < limit:
                    os.remove(path)
    except Exception:
        pass


def claude_pid():
    """PID of the claude process that runs this hook, found by walking up the parent chain. The
    watcher uses it to tell "the member is quiet" from "nobody is left to run it"."""
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            break
        try:
            with open(f"/proc/{pid}/comm") as fh:
                if fh.read().strip() == "claude":
                    return pid
            with open(f"/proc/{pid}/stat") as fh:
                pid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except Exception:
            break
    return 0


def launch_watcher(p, target, role):
    """One detached process per member. The cap exists because each one edits its own message
    and Telegram rate-limits per chat. Returns True when the member has a live watcher after
    the call.

    Idempotent and self-healing: a live watcher is left alone; a marker whose process is gone is
    swept and the watcher is launched again, so a member that is still running gets its live
    message back on its next tool call."""
    aid = p.get("agent_id")
    path = member_transcript(p)
    watcher = watcher_path()
    if not (aid and path and watcher):
        return False
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, ".watch.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if watcher_alive(aid):
                return True
            alive = sweep_markers()
            sweep_stops()
            if alive >= MAX_WATCHERS:
                return False
            proc = subprocess.Popen(
                ["python3", watcher, "--agent-id", str(aid), "--role", str(role),
                 "--model", role_model(role) or (agent_model(p.get("cwd")) or ""),
                 "--transcript", path, "--chat", target[0], "--thread", target[1],
                 "--claude-pid", str(claude_pid())],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            with open(_mark_path(aid), "w") as fh:
                fh.write(str(proc.pid))
            return True
    except Exception:
        return False


def _send(target, text):
    # Fire and forget, on purpose: `openclaw message send` takes 1-2 s, and waiting for it
    # inside the hook added that latency to EVERY narrated tool call. The message is not the
    # work; the work must not wait for the message.
    try:
        subprocess.Popen(
            [openclaw_bin(), "message", "send", "--channel", "telegram",
             "--target", target[0], "--thread-id", target[1], "--message", text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception:
        pass


def publish(target, text, session_id, milestone=False):
    """A milestone bypasses the rate window and is never dropped. Everything else is limited to
    MESSAGES_PER_WINDOW per WINDOW_SECONDS per session, with one notice per window."""
    if not milestone:
        ok, first = window(session_id)
        if not ok:
            if first:
                _send(target, MESSAGES["rate_omitted"].format(n=MESSAGES_PER_WINDOW,
                                                              mins=WINDOW_SECONDS // 60))
            return
    _send(target, text)


def main():
    if os.environ.get("OPENCLAW_CLI") != "1":
        return
    level = narration_level()
    if level == "off":
        return
    p = read_payload()
    log_payload(p)
    target = resolve_target(p.get("cwd"))
    if not target:
        return
    event = p.get("hook_event_name")
    session = p.get("session_id") or "no-session"
    ti = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
    inner_role = p.get("agent_type")
    effort = effort_of(p)
    every_step = level == "every-step"

    # The request comes in. Before this there was silence until the first tool.
    if event == "UserPromptSubmit":
        model = agent_model(p.get("cwd")) or "?"
        tail = f" · effort {effort}" if effort else ""
        publish(target, MESSAGES["request"].format(model=model, tail=tail), session, milestone=True)
        return

    # The turn ends: close the story.
    if event == "Stop":
        publish(target, MESSAGES["turn_done"], session, milestone=True)
        return

    if event == "SubagentStop":
        aid = p.get("agent_id")
        if aid:
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                open(os.path.join(STATE_DIR, f"stop-{aid}"), "w").close()
                mark = _mark_path(aid)
                # The watcher removes its own marker when it sees the stop file. Removing it
                # here too keeps a finished member from counting as a live watcher.
                if os.path.exists(mark):
                    os.remove(mark)
            except Exception:
                pass
        role = clean(inner_role or MESSAGES["a_member"], 40)
        result = clean(p.get("last_assistant_message") or "", MAX_RESULT)
        publish(target, MESSAGES["member_done"].format(role=role) + (f"\n{result}" if result else ""),
                session, milestone=True)
        return

    if event != "PreToolUse":
        return

    tool = p.get("tool_name")

    if tool == "Agent":
        role = clean(ti.get("subagent_type") or "general-purpose", 40)
        model = role_model(ti.get("subagent_type"))
        if model:
            model_label = model
        else:
            inherited = agent_model(p.get("cwd"))
            model_label = (MESSAGES["inherited"].format(model=inherited) if inherited
                           else MESSAGES["inherited_unknown"])
        tail = f" · effort {effort}" if effort else ""
        desc = clean(ti.get("description") or "")
        publish(target, MESSAGES["team_start"].format(role=role, model=model_label, tail=tail)
                + (f"\n{desc}" if desc else ""), session, milestone=True)
        return

    if tool == "Workflow":
        name = clean(ti.get("name") or "", 40)
        if not name and ti.get("script"):
            m = re.search(r"name:\s*['\"]([^'\"]+)", str(ti["script"]))
            name = clean(m.group(1) if m else MESSAGES["unnamed"], 40)
        phases = len(re.findall(r"phase\(", str(ti.get("script") or "")))
        publish(target, MESSAGES["workflow"].format(name=name or MESSAGES["unnamed"])
                + (MESSAGES["workflow_phases"].format(n=phases) if phases else ""), session,
                milestone=True)
        return

    # From here down, only what happens INSIDE a team member: the parent agent's own tools
    # are already drawn by OpenClaw in its progress draft. Milestones stop here.
    if not inner_role or not every_step:
        return
    role = clean(inner_role, 40)

    # The member's first tool is when its agent_id exists, so it is when its live message starts.
    # Every later call re-checks that the watcher is alive and relaunches it if it is not. It
    # runs before the edit branch so a member whose first tool is an edit still gets one.
    has_watcher = launch_watcher(p, target, inner_role)

    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        path = clean(str(path).replace(os.path.expanduser("~"), "~"), 70)
        ok, last = counter(f"edits-{p.get('agent_id')}", EDITS_PER_MEMBER)
        if ok:
            publish(target, MESSAGES["edit"].format(role=role, path=path), session)
        elif last:
            publish(target, MESSAGES["edit_more"].format(role=role), session)
        return

    # The pulse is the fallback for when there is no watcher (cap reached, or the transcript
    # never appeared): with a live message it would be duplicate noise.
    if has_watcher:
        return
    n, secs = pulse(p.get("agent_id"))
    if n:
        publish(target, MESSAGES["pulse"].format(role=role, n=n, secs=secs, tool=clean(tool, 24)),
                session)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
