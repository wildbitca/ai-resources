"""Usage loaders for `ai-resources audit`: Claude Code transcripts and OpenClaw native-runtime events.

Both loaders are read-only and return a flat list of `Usage` records so the report and the
snapshot/compare commands share one data model.

Claude Code: ~/.claude/projects/<slug>/*.jsonl plus <slug>/<session>/subagents/*.jsonl. Streamed
assistant messages are written several times with cumulative usage, so records are deduplicated by
`message.id` (fallback `requestId`); the last record wins.

OpenClaw: per-agent SQLite DBs at <agents_dir>/<agent>/agent/openclaw-agent.sqlite, table
`transcript_events` (event_json, or event_zstd when compressed). Opened with mode=ro; the shared
state/openclaw.sqlite is never touched.
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


def is_poll(name: str, args) -> bool:
    """A tool call whose only purpose is waiting: yields, waits, process polls, sleeps."""
    short = (name or "").split("__")[-1]
    text = args if isinstance(args, str) else json.dumps(args or {})
    if name in POLL_TOOLS or short in ("sessions_yield", "agents_wait"):
        return True
    if short == "process" and "poll" in text:
        return True
    return short in ("exec", "Bash") and "sleep " in text


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
                    records[key] = Usage(
                        ts=ts, source="claude-code", project=project, agent="",
                        model=model, input=i, output=o, cache_read=u.get("cache_read_input_tokens") or 0,
                        cache_write_5m=w5, cache_write_1h=w1, msg_id=key,
                        session=str(f) if sub else (d.get("sessionId") or f.stem), origin=origin, poll=polls)
    return list(records.values())


# ---------------------------------------------------------------------------
# OpenClaw
# ---------------------------------------------------------------------------

Decoder = Callable[[bytes], "str | None"]


def decode_zstd(blob: bytes) -> str | None:
    """Decode an event_zstd blob: compression.zstd (3.14+), the zstandard module, then the zstd CLI."""
    try:
        from compression import zstd  # type: ignore[import-not-found]
        return zstd.decompress(blob).decode("utf-8", "replace")
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - corrupt blob: fall through to the next decoder
        pass
    try:
        import zstandard  # type: ignore[import-not-found]
        return zstandard.ZstdDecompressor().decompress(blob).decode("utf-8", "replace")
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass
    exe = shutil.which("zstd")
    if exe:
        try:
            r = subprocess.run([exe, "-dc"], input=blob, capture_output=True, timeout=15, check=False)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def load_openclaw_events(agents_dir: Path, since: datetime | None = None, until: datetime | None = None,
                         decoder: Decoder | None = None, providers: tuple[str, ...] | None = None,
                         ) -> tuple[list[Usage], LoadStats]:
    """Assistant usage from every per-agent transcript DB; never writes, never opens state/openclaw.sqlite."""
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
        con.close()
        for sid, seq, ej, ez, created in rows:
            if ej is None:
                text = decode(ez) if ez is not None else None
                if text is None:
                    stats.undecoded += 1
                    continue
                ej = text
            if '"usage"' not in ej:
                continue
            try:
                m = json.loads(ej).get("message") or {}
                u = m.get("usage") or {}
            except (json.JSONDecodeError, AttributeError):
                continue
            if m.get("role") != "assistant" or not isinstance(u, dict) or not u:
                continue
            if providers is not None and m.get("provider") not in providers:
                continue
            w1 = u.get("cacheWrite1h") or 0
            w5 = max((u.get("cacheWrite") or 0) - w1, 0)
            if not ((u.get("input") or 0) + (u.get("output") or 0) + (u.get("cacheRead") or 0) + w1 + w5):
                continue                  # delivery mirrors and automation markers carry a usage block of zeros
            polls = any(is_poll(c.get("name", ""), c.get("arguments")) for c in (m.get("content") or [])
                        if isinstance(c, dict) and c.get("type") == "toolCall")
            key = m.get("responseId") or m.get("idempotencyKey") or f"{agent}:{sid}:{seq}"
            records[f"{agent}:{key}"] = Usage(
                ts=datetime.fromtimestamp(created / 1000, timezone.utc), source="openclaw", project="",
                agent=agent, model=m.get("model", ""), input=u.get("input") or 0, output=u.get("output") or 0,
                cache_read=u.get("cacheRead") or 0, cache_write_5m=w5, cache_write_1h=w1, msg_id=str(key),
                session=str(sid), origin="openclaw-api" if m.get("provider") == "anthropic" else "openclaw-" + str(m.get("provider")),
                poll=polls)
    return list(records.values()), stats
