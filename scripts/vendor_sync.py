#!/usr/bin/env python3
"""Sync third-party skill vendors from vendor-manifest.json (git clone + copy)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "vendor-manifest.json"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        raise SystemExit(r.returncode)


def _default_skills_root() -> Path:
    env = os.environ.get("AGENT_SKILLS_ROOT")
    if env:
        return Path(env)
    ak = os.environ.get("AGENT_KIT")
    if ak:
        return Path(ak) / "cursor" / "skills"
    return Path(__file__).resolve().parents[1] / "cursor" / "skills"


def main() -> int:
    root = _default_skills_root()
    root.mkdir(parents=True, exist_ok=True)

    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for v in data.get("vendors", []):
        vid = v["id"]
        if v.get("type") != "git":
            print(f"Skip unsupported vendor type: {v.get('type')}", file=sys.stderr)
            continue
        url = v["url"]
        ref = v.get("ref", "main")
        depth = int(v.get("depth", 1))
        with tempfile.TemporaryDirectory(prefix=f"vendor-{vid}-") as tmp:
            tpath = Path(tmp)
            clone_dir = tpath / "src"
            run(
                [
                    "git",
                    "clone",
                    "--depth",
                    str(depth),
                    "--branch",
                    ref,
                    url,
                    str(clone_dir),
                ]
            )
            r = subprocess.run(
                ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            )
            rev = (r.stdout or "").strip() or "unknown"

            for item in v.get("copy", []):
                src = clone_dir / item["src"]
                dest = root / item["dest"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(src, dest)
                else:
                    shutil.copy2(src, dest)

            vendor_root = root / "vendor" / vid
            vendor_root.mkdir(parents=True, exist_ok=True)
            (vendor_root / "UPSTREAM_REVISION.txt").write_text(rev + "\n", encoding="utf-8")
            (vendor_root / "UPSTREAM_URL.txt").write_text(url + "\n", encoding="utf-8")
            print(f"Synced vendor {vid} @ {rev[:8]} → {vendor_root}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
