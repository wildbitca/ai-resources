"""Doctor check: is a vendored skill pack's pin behind upstream? (ADR-0015 S9). Warn-only.

`resources.json` pins each imported pack to a commit (`sha`) of a `ref`. This asks `git ls-remote <url> <ref>` (one
read-only network call, 10 s cap) and compares. It is skipped without a word of failure when the host is offline, when
`AI_RESOURCES_OFFLINE` is set, or when `git` cannot answer: a doctor run must never fail because of the network, and a
pin that is behind is information, not a defect (re-importing is `ai-resources generate`)."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import repo_root

Runner = Callable[..., "tuple[int, str]"]
OFFLINE_ENV = "AI_RESOURCES_OFFLINE"


@dataclass(frozen=True)
class PinStatus:
    id: str
    pinned: str
    upstream: str            # "" when unknown
    status: str              # current | behind | unknown | skipped
    detail: str = ""


def _git(argv: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *argv], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, type(e).__name__
    return r.returncode, r.stdout


def sources(manifest: Path | None = None) -> list[dict]:
    path = manifest or (repo_root() / "resources.json")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = ((doc.get("skills") or {}).get("sources") or []) if isinstance(doc, dict) else []
    return [s for s in out if isinstance(s, dict) and s.get("type") == "git" and s.get("url") and s.get("sha")]


def upstream_sha(url: str, ref: str, runner: Runner = _git) -> str:
    """The commit `ref` points at upstream, or '' when it cannot be read."""
    rc, out = runner(["ls-remote", url, ref or "HEAD"], timeout=10)
    if rc != 0:
        return ""
    for line in out.splitlines():
        sha = line.split("\t", 1)[0].strip()
        if len(sha) >= 40 and all(c in "0123456789abcdef" for c in sha[:40]):
            return sha[:40]
    return ""


def check(manifest: Path | None = None, runner: Runner | None = None, environ: dict | None = None) -> list[PinStatus]:
    env = os.environ if environ is None else environ
    packs = sources(manifest)
    if env.get(OFFLINE_ENV):
        return [PinStatus(str(p.get("id")), str(p.get("sha")), "", "skipped", f"{OFFLINE_ENV} is set") for p in packs]
    run = runner or _git
    out: list[PinStatus] = []
    for pack in packs:
        pinned = str(pack["sha"])
        up = upstream_sha(str(pack["url"]), str(pack.get("ref") or "HEAD"), run)
        if not up:
            out.append(PinStatus(str(pack.get("id")), pinned, "", "skipped", "upstream not reachable"))
        elif up == pinned:
            out.append(PinStatus(str(pack.get("id")), pinned, up, "current"))
        else:
            out.append(PinStatus(str(pack.get("id")), pinned, up, "behind",
                                 f"upstream {pack.get('ref') or 'HEAD'} is at {up[:12]}"))
    return out
