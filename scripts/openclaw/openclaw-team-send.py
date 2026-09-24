#!/usr/bin/env python3
"""Deliver team-narration messages to Telegram without breaking its rate limits and without
losing them.

WHY IT EXISTS
The narration hook and the per-member watchers used to call `openclaw message send/edit` on their
own: the hook fire-and-forget, the watcher with a 30 s timeout and every error thrown away. Telegram
limits a chat (a forum group and ALL its topics share one budget, roughly 20 messages a minute), so
with a few live members plus the gateway's own streaming drafts the limit was hit, and:
  - the gateway answers a 429 by waiting (a call took up to 76 s), so the watcher's 30 s timeout
    killed the call, lost the edit, and, when it was the first message, made the watcher give up;
  - the hook's fire-and-forget send had nobody to notice that it failed;
  - nothing was written anywhere, so a lost message left no trace.

WHAT IT DOES
One queue per chat, kept on disk and shared by every process on the host:
  - FIFO: a caller takes a ticket and waits for its turn, so milestones keep their order.
  - Pacing: at least INTERVAL seconds between two calls to the same chat, more after a 429 (it
    waits the `retry after N` Telegram asked for) or after a call that was slow (the gateway was
    already backing off), so the host stops fighting for the same budget.
  - Retries: a 429 waits and retries; a transient failure (gateway restarting, timeout) retries with
    a growing back-off; "message is not modified" counts as delivered; a permanent error (chat not
    found, message gone, bot blocked) stops at once.
  - Nothing is lost silently: every rate limit, retry and failure goes to team-outbox.log, and a
    message that could not be delivered is kept in team-outbox-dead/ and replayed later (a recent one
    is replayed automatically after the next successful delivery, any of them with --replay).
  - Latest wins: an edit takes its text from a function that runs when its turn comes, so a member
    that waited behind others publishes what it is doing NOW, not what it was doing when it queued.

USE
  openclaw-team-send.py send --channel telegram --target <chat> [--thread-id <n>] --message <text>
  openclaw-team-send.py edit --channel telegram --target <chat> [--thread-id <n>] --message-id <id> --message <text>
  openclaw-team-send.py --replay
The watcher imports this file and calls deliver(). Best effort: the command line always exits 0.
"""
import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

# The overrides exist so the tests can run against a throwaway state directory, a stub `openclaw` and
# short clocks. In production none of them is set and the defaults apply.
OPENCLAW = (os.environ.get("OPENCLAW_TEAM_HOOK_BIN") or shutil.which("openclaw")
            or "/home/linuxbrew/.linuxbrew/bin/openclaw")
STATE_DIR = os.environ.get("OPENCLAW_TEAM_HOOK_STATE") or f"/run/user/{os.getuid()}/openclaw-team-hook"
LOG_DIR = os.environ.get("OPENCLAW_TEAM_SEND_LOG_DIR") or os.path.expanduser("~/.openclaw/logs")


def _seconds(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


INTERVAL = _seconds("OPENCLAW_TEAM_SEND_INTERVAL", 4.0)        # min gap between two calls to one chat
CALL_TIMEOUT = _seconds("OPENCLAW_TEAM_SEND_TIMEOUT", 120.0)   # the gateway may wait out a 429 (76 s seen)
SLOW_CALL = _seconds("OPENCLAW_TEAM_SEND_SLOW", 15.0)          # slower than this: the gateway was backing off
SLOW_EXTRA = 5.0                                               # extra spacing after a slow call
QUEUE_WAIT = _seconds("OPENCLAW_TEAM_SEND_QUEUE_WAIT", 600.0)  # longest wait for a turn
ATTEMPTS = 5                                                   # transient failures before giving up
RATE_HITS = 8                                                  # 429s before giving up
BACKOFF = (2.0, 5.0, 15.0, 30.0)                               # waits after a transient failure
REPLAY_MAX_AGE = 1800.0                                        # a dead letter older than this is not auto-replayed
REPLAY_PER_CALL = 2
MAX_LOG = 1_000_000

PERMANENT = ("message to edit not found", "message can't be edited", "message thread not found",
             "chat not found", "bot was blocked", "forbidden", "not enough rights", "topic_closed",
             "topic closed", "user is deactivated", "message to be replied not found")


def _safe(value, limit=60):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))[:limit]


# --- observability: nothing is dropped without a trace ---------------------------------------------------------

def log(event, **fields):
    """One JSON line per event in team-outbox.log (rotated at MAX_LOG). Never raises."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "team-outbox.log")
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG:
            os.replace(path, path + ".1")
        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields},
                                ensure_ascii=False) + "\n")
    except Exception:
        pass


def _dead_dir():
    return os.path.join(LOG_DIR, "team-outbox-dead")


def keep_dead_letter(kind, chat, thread, text, mid, error):
    """A message that could not be delivered is kept, so it can be replayed. Edits are not kept: the
    next edit of the same member supersedes them."""
    if kind != "send":
        return
    try:
        os.makedirs(_dead_dir(), exist_ok=True)
        path = os.path.join(_dead_dir(), f"{time.time_ns():020d}-{os.getpid()}.json")
        with open(path, "w") as fh:
            json.dump({"kind": kind, "chat": chat, "thread": thread, "text": text, "mid": mid,
                       "error": str(error)[:300], "ts": time.time()}, fh, ensure_ascii=False)
        log("dead", kind=kind, chat=chat, thread=thread, error=str(error)[:200], head=str(text)[:80])
    except Exception:
        pass


# --- the per-chat FIFO queue ---------------------------------------------------------------------------------------

def _pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 1:
            return False
        os.kill(pid, 0)
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except Exception:
        return False


@contextlib.contextmanager
def turn(chat, timeout=None):
    """Wait for this caller's turn on the chat, in arrival order, and hold it until the block ends.

    A ticket is an empty file named <ns>-<pid> in the chat's directory; the oldest ticket whose
    process is alive goes first. Tickets of dead processes are removed, so a crash never blocks the
    queue, and the wait is bounded so a wedged caller cannot either."""
    timeout = QUEUE_WAIT if timeout is None else timeout
    directory = os.path.join(STATE_DIR, "outbox", _safe(chat))
    os.makedirs(directory, exist_ok=True)
    mine = f"{time.time_ns():020d}-{os.getpid()}"
    path = os.path.join(directory, mine)
    open(path, "w").close()
    try:
        deadline = time.time() + timeout
        while True:
            live = []
            for name in sorted(n for n in os.listdir(directory) if not n.startswith(".")):
                if name == mine or _pid_alive(name.rsplit("-", 1)[-1]):
                    live.append(name)
                else:
                    with contextlib.suppress(Exception):
                        os.remove(os.path.join(directory, name))
            if live and live[0] == mine:
                break
            if time.time() > deadline:
                log("queue_timeout", chat=chat, waited=round(timeout))
                break
            time.sleep(0.1)
        yield directory
    finally:
        with contextlib.suppress(Exception):
            os.remove(path)


def _wait_pacing(directory):
    try:
        with open(os.path.join(directory, ".next")) as fh:
            wait = float(fh.read().strip()) - time.time()
    except Exception:
        return
    if wait > 0:
        time.sleep(min(wait, 180.0))


def _space(directory, seconds):
    with contextlib.suppress(Exception):
        with open(os.path.join(directory, ".next"), "w") as fh:
            fh.write(str(time.time() + seconds))


# --- one call to the openclaw CLI ------------------------------------------------------------------------------------

def _run(argv):
    started = time.time()
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=CALL_TIMEOUT, check=False)
        return r.returncode, (r.stdout or "") + "\n" + (r.stderr or ""), time.time() - started
    except subprocess.TimeoutExpired:
        return 124, "timeout: the call did not finish", time.time() - started
    except Exception as exc:
        return 127, f"could not run openclaw: {exc}", time.time() - started


def classify(rc, output):
    """"ok", ("rate", seconds), "permanent" or "transient"."""
    low = (output or "").lower()
    if rc == 0:
        return "ok"
    if "message is not modified" in low:
        return "ok"
    m = re.search(r"retry after (\d+)", low) or re.search(r'"retry_after"\s*:\s*(\d+)', low)
    if m:
        return ("rate", int(m.group(1)))
    if "429" in low or "too many requests" in low:
        return ("rate", 30)
    if any(marker in low for marker in PERMANENT):
        return "permanent"
    return "transient"


def _message_id(output):
    m = re.search(r"Message ID:\s*(\d+)", output or "")
    if m:
        return m.group(1)
    m = re.search(r'"message_?[iI]d"\s*:\s*"?(\d+)', output or "")
    return m.group(1) if m else ""


def _argv(kind, chat, thread, text, mid):
    argv = [OPENCLAW, "message", kind, "--channel", "telegram", "--target", str(chat)]
    if thread not in (None, ""):
        argv += ["--thread-id", str(thread)]
    if kind == "edit":
        argv += ["--message-id", str(mid)]
    return argv + ["--message", text]


# --- delivery ----------------------------------------------------------------------------------------------------------

def deliver(kind, chat, thread, text=None, mid=None, text_fn=None, auto_replay=True):
    """Deliver one message. Returns (delivered, message_id).

    `text_fn`, when given, is called each time a call is about to be made (after the turn and the
    pacing wait), so an edit carries the state of that moment. A `send` returns the id Telegram gave
    it ("" when the output carried none). After a successful send, recent dead letters of the chat are
    replayed unless `auto_replay` is False (the replay itself must not replay)."""
    with turn(chat) as directory:
        transient = rate = 0
        last_error = ""
        while True:
            _wait_pacing(directory)
            body = text_fn() if text_fn else text
            rc, output, took = _run(_argv(kind, chat, thread, body, mid))
            verdict = classify(rc, output)
            if verdict == "ok":
                _space(directory, INTERVAL + (SLOW_EXTRA if took > SLOW_CALL else 0.0))
                if took > SLOW_CALL:
                    log("slow", kind=kind, chat=chat, thread=thread, seconds=round(took, 1))
                if kind == "send" and auto_replay:
                    _replay_recent(directory, chat)
                return True, _message_id(output)
            last_error = (output or "").strip().replace("\n", " ")[:300]
            if isinstance(verdict, tuple):
                rate += 1
                _space(directory, verdict[1] + 1)
                log("rate", kind=kind, chat=chat, thread=thread, retry_after=verdict[1], hit=rate)
                if rate >= RATE_HITS:
                    break
                continue
            if verdict == "permanent":
                log("permanent", kind=kind, chat=chat, thread=thread, error=last_error[:200])
                keep_dead_letter(kind, chat, thread, body, mid, last_error)
                return False, ""
            transient += 1
            wait = BACKOFF[min(transient - 1, len(BACKOFF) - 1)]
            log("retry", kind=kind, chat=chat, thread=thread, attempt=transient, wait=wait, error=last_error[:160])
            if transient >= ATTEMPTS:
                break
            _space(directory, wait)
        log("failed", kind=kind, chat=chat, thread=thread, error=last_error[:200])
        keep_dead_letter(kind, chat, thread, body, mid, last_error)
        return False, ""


def _replay_recent(directory, chat):
    """After a successful send, try the recent dead letters of this chat once more (same turn)."""
    try:
        names = sorted(os.listdir(_dead_dir()))
    except Exception:
        return
    done = 0
    for name in names:
        if done >= REPLAY_PER_CALL:
            return
        path = os.path.join(_dead_dir(), name)
        try:
            with open(path) as fh:
                item = json.load(fh)
            if str(item.get("chat")) != str(chat) or time.time() - float(item.get("ts", 0)) > REPLAY_MAX_AGE:
                continue
            _wait_pacing(directory)
            rc, output, took = _run(_argv("send", item["chat"], item.get("thread"), item["text"], None))
            if classify(rc, output) == "ok":
                os.remove(path)
                log("replayed", chat=chat, thread=item.get("thread"))
                _space(directory, INTERVAL + (SLOW_EXTRA if took > SLOW_CALL else 0.0))
                done += 1
            else:
                return
        except Exception:
            continue


def replay_all():
    """Retry every dead letter, oldest first, in the normal queue. Returns how many were delivered."""
    delivered = 0
    try:
        names = sorted(os.listdir(_dead_dir()))
    except Exception:
        return 0
    for name in names:
        path = os.path.join(_dead_dir(), name)
        try:
            with open(path) as fh:
                item = json.load(fh)
            os.remove(path)  # deliver() writes a fresh dead letter if it fails again
            ok, _ = deliver("send", item["chat"], item.get("thread"), text=item["text"], auto_replay=False)
            delivered += 1 if ok else 0
        except Exception:
            continue
    return delivered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", nargs="?", choices=("send", "edit"))
    ap.add_argument("--channel", default="telegram")
    ap.add_argument("--target")
    ap.add_argument("--thread-id", default="")
    ap.add_argument("--message-id", default="")
    ap.add_argument("--message", default="")
    ap.add_argument("--replay", action="store_true")
    a = ap.parse_args()
    if a.replay:
        print(f"replayed {replay_all()}")
        return
    if not (a.kind and a.target):
        return
    ok, mid = deliver(a.kind, a.target, a.thread_id, text=a.message, mid=a.message_id)
    if ok and mid:
        print(f"Message ID: {mid}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
