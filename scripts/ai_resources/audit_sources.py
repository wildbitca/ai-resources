"""Usage loaders for `ai-resources audit`: Claude Code transcripts and OpenClaw native-runtime events.

Both loaders are read-only and return a flat list of `Usage` records so the report and the
snapshot/compare commands share one data model.

Claude Code: ~/.claude/projects/<slug>/*.jsonl plus <slug>/<session>/subagents/*.jsonl. Streamed
assistant messages are written several times with cumulative usage, so records are deduplicated by
`message.id` (fallback `requestId`); the last record wins.

OpenClaw: per-agent SQLite DBs at <agents_dir>/<agent>/agent/openclaw-agent.sqlite, tables `transcript_events` and
`session_transcript_archives` (deleted/reset sessions). Live rows are event_json or event_zstd when compressed.
Opened with mode=ro; the shared state/openclaw.sqlite is never touched.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import sleep_pattern

POLL_TOOLS = {"sessions_yield", "agents_wait", "Monitor"}


@dataclass
class Usage:
    ts: datetime
    source: str            # "claude-code" | "openclaw"
    project: str
    agent: str
    model: str
    input: int
    output: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    msg_id: str
    session: str = ""
    origin: str = ""
    poll: bool = False
    provider: str = ""     # who bills the call: anthropic, openai, google, ... (ADR-0015 S14)
    role: str = ""         # the kit role that made it: planner, verifier, main, ... ("" when unknown)

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def ctx(self) -> int:
        """Prompt size of the call: fresh input plus everything read from or written to the cache."""
        return self.input + self.cache_read + self.cache_write


@dataclass
class LoadStats:
    undecoded: int = 0
    databases: int = 0
    missing: bool = False
    notes: list[str] = field(default_factory=list)


def provider_of_model(model: str, default: str = "") -> str:
    """The provider a model id belongs to, from its name; `default` when it cannot be told."""
    m = (model or "").lower()
    head = m.split("/", 1)[0] if "/" in m else ""
    if head and head not in ("claude-cli", "claude-kit", "agy-cli", "agy-cli-lane", "codex"):
        return head
    if head in ("claude-cli", "claude-kit"):
        return "anthropic"
    if head in ("agy-cli", "agy-cli-lane"):
        return "google"
    if head == "codex":
        return "openai"
    if "claude" in m or any(f in m for f in ("sonnet", "opus", "haiku", "fable")):
        return "anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    if "gemini" in m:
        return "google"
    return default


_ROLE_ALIASES = {"general-purpose": "generalPurpose", "generalpurpose": "generalPurpose"}


def normalise_role(raw: str) -> str:
    """A Claude Code `agentType` as a kit role name (`general-purpose` -> `generalPurpose`, `Explore` -> `explore`)."""
    text = (raw or "").strip()
    if not text:
        return ""
    low = text.lower()
    return _ROLE_ALIASES.get(low, low)


def claude_subagent_role(transcript: Path) -> str:
    """The role of a Claude Code subagent transcript: `agentType` in `<stem>.meta.json` beside it, else 'subagent'.
    Read-only; an unreadable or absent meta file is not an error."""
    meta = transcript.with_suffix("").with_suffix(".meta.json") if transcript.suffix == ".jsonl" else None
    if meta is not None:
        try:
            doc = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("agentType"), str) and doc["agentType"].strip():
            return normalise_role(doc["agentType"])
    return "subagent"


def openclaw_role(agent: str) -> str:
    """The kit role of an OpenClaw agent id: `kit-role-architect` -> `software-architect`; the id itself otherwise."""
    if agent.startswith("kit-role-"):
        from .setup.cockpits import openclaw_roles
        short = agent[len("kit-role-"):]
        for role, word in openclaw_roles.SHORT.items():
            if short == word or short.startswith(word + "-"):
                return role
        return short.split("-")[0]
    return agent


def is_poll(name: str, args) -> bool:
    """A tool call whose only purpose is waiting: yields, waits, process polls, sleeps."""
    short = (name or "").split("__")[-1]
    text = args if isinstance(args, str) else json.dumps(args or {})
    if name in POLL_TOOLS or short in ("sessions_yield", "agents_wait"):
        return True
    if short == "process" and "poll" in text:
        return True
    if short not in ("exec", "Bash"):
        return False
    # The command string(s) only, one per line, so a sleep at command position is found by the shared definition.
    command = "\n".join(v for v in args.values() if isinstance(v, str)) if isinstance(args, dict) else text
    return sleep_pattern.has_sleep(command)


def _parse_ts(value) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None
    return None


def _in_window(ts: datetime, since: datetime | None, until: datetime | None) -> bool:
    return not ((since and ts < since) or (until and ts >= until))


def _claude_cache_writes(usage: dict) -> tuple[int, int]:
    detail = usage.get("cache_creation")
    if isinstance(detail, dict) and (detail.get("ephemeral_5m_input_tokens") or detail.get("ephemeral_1h_input_tokens")):
        return detail.get("ephemeral_5m_input_tokens") or 0, detail.get("ephemeral_1h_input_tokens") or 0
    return usage.get("cache_creation_input_tokens") or 0, 0


def _claude_files(root: Path, include_subagents: bool) -> list[Path]:
    if not root.is_dir():
        return []
    files = list(root.glob("*.jsonl"))
    if include_subagents:
        files += list(root.glob("*/subagents/**/*.jsonl"))  # workflows nest deeper
    return files


def load_claude_jsonl(roots: Iterable[Path], since: datetime | None = None, include_subagents: bool = False,
                      until: datetime | None = None) -> list[Usage]:
    """Usage records from Claude Code project dirs, one per distinct API message."""
    records: dict[str, Usage] = {}
    order = 0
    for root in roots:
        project = Path(root).name
        for f in sorted(_claude_files(Path(root), include_subagents), key=lambda p: p.stat().st_mtime):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if since and mtime < since:
                continue
            sub = "/subagents/" in str(f)
            entry = None
            role: str | None = None            # a subagent file's role, read once from its meta file
            try:
                fh = open(f, encoding="utf-8", errors="ignore")
            except OSError:
                continue
            with fh:
                for lineno, line in enumerate(fh):
                    if entry is None and '"entrypoint"' in line:
                        entry = "sdk-cli" if '"sdk-cli"' in line else "cli"
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                        m = d.get("message") or {}
                        u = m.get("usage") or {}
                        if not isinstance(m, dict) or not isinstance(u, dict):
                            continue
                        i, o = u.get("input_tokens") or 0, u.get("output_tokens") or 0
                        model = m.get("model", "unknown")
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        continue
                    if i + o == 0 or model == "<synthetic>":
                        continue
                    ts = _parse_ts(d.get("timestamp")) or mtime
                    if not _in_window(ts, since if d.get("timestamp") else None, until):
                        continue
                    w5, w1 = _claude_cache_writes(u)
                    polls = any(is_poll(c.get("name", ""), c.get("input")) for c in (m.get("content") or [])
                                if isinstance(c, dict) and c.get("type") == "tool_use")
                    key = m.get("id") or d.get("requestId")
                    key = f"id:{key}" if key else f"file:{f}:{lineno}"
                    origin = ("openclaw-cli" if entry == "sdk-cli" else "claude-code") + ("/subagent" if sub else "")
                    order += 1
                    if sub and role is None:
                        role = claude_subagent_role(f)
                    records[key] = Usage(
                        ts=ts, source="claude-code", project=project, agent=role if sub else "",
                        model=model, input=i, output=o, cache_read=u.get("cache_read_input_tokens") or 0,
                        cache_write_5m=w5, cache_write_1h=w1, msg_id=key,
                        session=str(f) if sub else (d.get("sessionId") or f.stem), origin=origin, poll=polls,
                        provider=provider_of_model(model, "anthropic"), role=(role if sub else "main"))
    return list(records.values())


# ---------------------------------------------------------------------------
# OpenClaw
# ---------------------------------------------------------------------------

Decoder = Callable[[bytes], "str | None"]


MAX_DECODED_BYTES = 256 * 1024 * 1024    # an archived transcript larger than this is reported as undecoded, never inflated


def decode_zstd(blob: bytes) -> str | None:
    """Decode an event_zstd blob: compression.zstd (3.14+), the zstandard module, then the zstd CLI.

    The output is capped at MAX_DECODED_BYTES on every path; a blob that would inflate beyond it returns None."""
    cap = MAX_DECODED_BYTES
    try:
        from compression import zstd  # type: ignore[import-not-found]
        d = zstd.ZstdDecompressor()
        out = d.decompress(blob, cap + 1)
        return out.decode("utf-8", "replace") if len(out) <= cap and d.eof else None
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - corrupt blob: fall through to the next decoder
        pass
    try:
        import zstandard  # type: ignore[import-not-found]
        return zstandard.ZstdDecompressor().decompress(blob, max_output_size=cap).decode("utf-8", "replace")
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - corrupt blob or output over the cap
        pass
    exe = shutil.which("zstd")
    if exe:
        try:
            proc = subprocess.Popen([exe, "-dc"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                out = _read_capped(proc, blob, cap)
                if out is None:
                    proc.kill()
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return None
            if out is not None and proc.returncode == 0:
                return out.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _read_capped(proc: "subprocess.Popen[bytes]", blob: bytes, cap: int) -> bytes | None:
    """Feed `blob` to `proc` and read at most `cap` bytes of stdout; None when there is more (the caller kills it)."""
    import threading
    feeder = threading.Thread(target=lambda: _feed(proc, blob), daemon=True)
    feeder.start()
    out = proc.stdout.read(cap + 1) if proc.stdout else b""
    return out if len(out) <= cap else None


def _feed(proc: "subprocess.Popen[bytes]", blob: bytes) -> None:
    try:
        if proc.stdin:
            proc.stdin.write(blob)
            proc.stdin.close()
    except (OSError, ValueError):
        pass


UNMETERED_PROVIDERS = ("claude-cli", "claude-kit")   # their turns can carry a zero usage block: tokens were spent, not recorded


def _openclaw_usage(ej: str, agent: str, sid, seq, ts: datetime, providers: tuple[str, ...] | None,
                    stats: LoadStats) -> tuple[str, Usage] | None:
    """One transcript event (live row or archived line) as `(dedupe key, Usage)`; None when it is not metered usage."""
    if '"usage"' not in ej:
        return None
    try:
        m = json.loads(ej).get("message") or {}
        u = m.get("usage") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(m, dict) or m.get("role") != "assistant" or not isinstance(u, dict):
        return None
    if providers is not None and m.get("provider") not in providers:
        return None
    if not u:
        if m.get("provider") in UNMETERED_PROVIDERS:
            stats.undecoded += 1          # `usage: {}` on a CLI turn is the same gap as an all-zero block
        return None
    w1 = u.get("cacheWrite1h") or 0
    w5 = max((u.get("cacheWrite") or 0) - w1, 0)
    if not ((u.get("input") or 0) + (u.get("output") or 0) + (u.get("cacheRead") or 0) + w1 + w5):
        if m.get("provider") in UNMETERED_PROVIDERS:
            stats.undecoded += 1          # a real CLI turn whose usage was never recorded: report it, do not hide it
        return None                       # delivery mirrors and automation markers carry a usage block of zeros
    polls = any(is_poll(c.get("name", ""), c.get("arguments")) for c in (m.get("content") or [])
                if isinstance(c, dict) and c.get("type") == "toolCall")
    key = m.get("responseId") or m.get("idempotencyKey") or f"{agent}:{sid}:{seq}"
    return f"{agent}:{key}", Usage(
        ts=ts, source="openclaw", project="", agent=agent, model=m.get("model", ""), input=u.get("input") or 0,
        output=u.get("output") or 0, cache_read=u.get("cacheRead") or 0, cache_write_5m=w5, cache_write_1h=w1,
        msg_id=str(key), session=str(sid),
        origin="openclaw-api" if m.get("provider") == "anthropic" else "openclaw-" + str(m.get("provider")),
        poll=polls, provider=str(m.get("provider") or provider_of_model(m.get("model", ""))), role=openclaw_role(agent))


def _archive_text(encoding: str, blob: bytes, decode: Decoder) -> str | None:
    if encoding == "identity":
        return bytes(blob).decode("utf-8", "replace")
    return decode(bytes(blob))


def load_openclaw_events(agents_dir: Path, since: datetime | None = None, until: datetime | None = None,
                         decoder: Decoder | None = None, providers: tuple[str, ...] | None = None,
                         ) -> tuple[list[Usage], LoadStats]:
    """Assistant usage from every per-agent transcript DB; never writes, never opens state/openclaw.sqlite.

    Two sources per DB: `transcript_events` (live sessions) and `session_transcript_archives` (sessions OpenClaw deleted
    or reset: one zstd/identity JSONL blob each, with the same event shape). Archived usage is real spend, so a cron
    run that was archived still counts; a response seen in both places is counted once."""
    decode = decoder or decode_zstd
    stats = LoadStats()
    agents_dir = Path(agents_dir)
    dbs = sorted(agents_dir.glob("*/agent/openclaw-agent.sqlite")) if agents_dir.is_dir() else []
    if not dbs:
        stats.missing = True
        return [], stats
    records: dict[str, Usage] = {}
    lo = int(since.timestamp() * 1000) if since else 0
    hi = int(until.timestamp() * 1000) if until else 2 ** 62
    for db in dbs:
        agent = db.parent.parent.name
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error as e:
            stats.notes.append(f"{agent}: cannot open ({e})")
            continue
        stats.databases += 1
        try:
            rows = con.execute(
                "select session_id, seq, event_json, event_zstd, created_at from transcript_events "
                "where created_at >= ? and created_at < ?", (lo, hi)).fetchall()
        except sqlite3.Error as e:
            stats.notes.append(f"{agent}: unreadable ({e})")
            con.close()
            continue
        try:
            archives = con.execute(
                "select session_id, encoding, archive_blob, created_at from session_transcript_archives "
                "where created_at >= ?", (lo,)).fetchall()      # archived AFTER the window start; events are filtered below
        except sqlite3.Error:
            archives = []                                       # an older DB has no archive table: not an error
        con.close()
        for sid, seq, ej, ez, created in rows:
            if ej is None:
                text = decode(ez) if ez is not None else None
                if text is None:
                    stats.undecoded += 1
                    continue
                ej = text
            got = _openclaw_usage(ej, agent, sid, seq, datetime.fromtimestamp(created / 1000, timezone.utc), providers, stats)
            if got:
                records[got[0]] = got[1]
        for sid, encoding, blob, archived_at in archives:
            text = _archive_text(encoding, blob, decode) if blob is not None else None
            if text is None:
                stats.undecoded += 1
                continue
            for seq, line in enumerate(text.splitlines()):
                if '"usage"' not in line:
                    continue
                try:
                    ts = _parse_ts(json.loads(line).get("timestamp"))
                except (json.JSONDecodeError, AttributeError):
                    continue
                ts = ts or datetime.fromtimestamp(archived_at / 1000, timezone.utc)
                if not lo <= int(ts.timestamp() * 1000) < hi:
                    continue
                got = _openclaw_usage(line, agent, sid, seq, ts, providers, stats)
                if got:
                    records.setdefault(got[0], got[1])           # a live copy of the same response wins
    return list(records.values()), stats
