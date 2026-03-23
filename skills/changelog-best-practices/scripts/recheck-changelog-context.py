#!/usr/bin/env python3
"""
Generate a context file for rechecking a project's CHANGELOG using an LLM agent (e.g. Cursor).

For each version (from CHANGELOG.md or git tags), collects:
- Commits in that version's range (hash, subject, body, author date)
- List of changed files per commit

Output: a markdown file (default .changelog-recheck-context.md in repo root) that the agent
can read to generate conventional-commit-style summaries and human-readable changelog entries,
then regenerate CHANGELOG.md.

Usage:
  python3 recheck-changelog-context.py [--output FILE] [--from-tags] [REPO_DIR]
  REPO_DIR defaults to current directory. CHANGELOG.md is read from REPO_DIR unless --from-tags.
  --from-tags: use git tags only (ignore existing CHANGELOG) for version list.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SEMVER_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-[a-zA-Z0-9.-]+)?$")
CHANGELOG_VERSION_RE = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?")


def run(cmd: list[str], cwd: Path, capture: bool = True) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True, timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, out.strip()


def get_versions_from_changelog(changelog_path: Path) -> list[tuple[str, str]]:
    """Return [(version, date), ...] from CHANGELOG.md, newest first. Date may be ''."""
    if not changelog_path.is_file():
        return []
    text = changelog_path.read_text(encoding="utf-8")
    versions = []
    for m in CHANGELOG_VERSION_RE.finditer(text):
        ver, date = m.group(1), (m.group(2) or "")
        if ver.lower() == "unreleased":
            continue
        versions.append((ver, date))

    # Newest first: sort by semver desc
    def key(t):
        v = t[0]
        mo = SEMVER_TAG_RE.match(v.strip())
        if mo:
            return (-int(mo.group(1)), -int(mo.group(2)), -int(mo.group(3)))
        return (0, 0, 0)

    versions.sort(key=key)
    return versions


def get_versions_from_tags(repo: Path) -> list[tuple[str, str]]:
    """Return [(version, date), ...] from git tags (v* semver), newest first."""
    code, out = run(["git", "tag", "-l", "v*", "--sort=-v:refname"], cwd=repo)
    if code != 0 or not out:
        return []
    versions = []
    for tag in out.splitlines():
        tag = tag.strip()
        if not SEMVER_TAG_RE.match(tag):
            continue
        code2, date_out = run(
            ["git", "log", "-1", "--format=%ad", "--date=short", tag],
            cwd=repo,
        )
        date = date_out if code2 == 0 and date_out else ""
        versions.append((tag, date))
    return versions


def resolve_ref(repo: Path, version: str) -> str:
    """Resolve version to a git ref (tag or commit). Tries version then v+version."""
    for ref in (version, f"v{version}" if not version.startswith("v") else version):
        code, _ = run(["git", "rev-parse", "--verify", ref], cwd=repo)
        if code == 0:
            return ref
    return version


def get_commit_range(repo: Path, version: str, prev_ref: str | None) -> str:
    """Return ref range for this version: prev_ref..version or ..version for first."""
    ref = resolve_ref(repo, version)
    if prev_ref is None:
        return ref
    prev = resolve_ref(repo, prev_ref)
    return f"{prev}..{ref}"


def _parse_show_output(show_out: str, full_hash: str) -> dict | None:
    """Parse output of 'git show hash --format=... --name-status'. Returns commit dict or None."""
    lines = show_out.splitlines()
    # git show may print diff stats first; find line that is 40-char hex (our %H)
    start = 0
    for i, line in enumerate(lines):
        if re.match(r"^[0-9a-f]{40}$", line.strip()):
            start = i
            break
    else:
        return None
    rest = lines[start:]
    if len(rest) < 5:
        return None
    short_hash = rest[1].strip()
    subject = rest[2].strip()
    # Body: lines between subject and the YYYY-MM-DD date line
    body_parts = []
    date = ""
    file_start = len(rest)
    for i in range(3, len(rest)):
        if re.match(r"^\d{4}-\d{2}-\d{2}$", rest[i].strip()):
            date = rest[i].strip()
            file_start = i + 1
            break
        body_parts.append(rest[i])
    body = "\n".join(body_parts).strip()
    files = []
    for line in rest[file_start:]:
        line = line.strip()
        if not line:
            continue
        if len(line) >= 2 and line[1] == "\t":
            files.append((line[0], line[2:].strip()))
        else:
            files.append(("", line))
    return {
        "hash": full_hash,
        "short_hash": short_hash,
        "subject": subject,
        "body": body,
        "date": date,
        "files": files,
    }


def get_commits_in_range(repo: Path, ref_range: str) -> list[dict]:
    """Return list of {hash, short_hash, subject, body, date, files}."""
    code, out = run(["git", "log", ref_range, "--format=%H", "--reverse"], cwd=repo)
    if code != 0 or not out:
        return []
    hashes = [h.strip() for h in out.splitlines() if h.strip()]
    commits = []
    for full_hash in hashes:
        code2, show_out = run(
            [
                "git", "show", full_hash,
                "--format=%H%n%h%n%s%n%b%n%ad",
                "--date=short",
                "--name-status",
            ],
            cwd=repo,
        )
        if code2 != 0 or not show_out:
            continue
        parsed = _parse_show_output(show_out, full_hash)
        if parsed:
            commits.append(parsed)
    return commits


def get_repo_remote_url(repo: Path) -> str | None:
    code, out = run(["git", "-C", str(repo), "remote", "get-url", "origin"], cwd=repo)
    if code != 0 or not out:
        return None
    url = out.strip()
    if url.startswith("git@"):
        url = url.replace(":", "/", 1).replace("git@", "https://", 1)
    return url.rstrip("/").removesuffix(".git") if url.startswith("http") else None


def emit_context_md(
        repo: Path,
        versions: list[tuple[str, str]],
        version_commits: dict[str, list[dict]],
        base_url: str | None,
        out_path: Path,
) -> None:
    """Write the context markdown file for the LLM agent."""
    lines = [
        "# Changelog recheck context",
        "",
        "Use this file to regenerate CHANGELOG.md: for each version, review commits and changed files,",
        "then produce conventional-commit-style summaries and human-readable entries (Added/Changed/Fixed/etc.).",
        "",
        f"Repo: {repo.resolve()}",
        f"Commit base URL: {base_url or '(unknown)'}/commit/",
        "",
        "---",
        "",
    ]
    for version, date in versions:
        commits = version_commits.get(version, [])
        lines.append(f"## Version: {version}" + (f" - {date}" if date else ""))
        lines.append("")
        if not commits:
            lines.append("(no commits in range)")
            lines.append("")
            continue
        for c in commits:
            lines.append(f"### Commit {c['short_hash']} — {c['subject']}")
            lines.append("")
            lines.append(f"- **Date:** {c['date']}")
            if c["body"]:
                lines.append(f"- **Body:** {c['body']}")
            lines.append("- **Changed files:**")
            for status, path in c["files"]:
                lines.append(f"  - {status} {path}" if status else f"  - {path}")
            lines.append("")
        lines.append("---")
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate changelog recheck context (commits + changed files per version) for LLM agent."
    )
    ap.add_argument(
        "repo_dir",
        nargs="?",
        default=".",
        help="Repository directory (default: current directory)",
    )
    ap.add_argument(
        "--output", "-o",
        default=".changelog-recheck-context.md",
        help="Output context file path (default: .changelog-recheck-context.md in repo)",
    )
    ap.add_argument(
        "--from-tags",
        action="store_true",
        help="Use git tags for versions instead of parsing CHANGELOG.md",
    )
    args = ap.parse_args()

    repo = Path(args.repo_dir).resolve()
    if not repo.is_dir():
        print(f"Not a directory: {repo}", file=sys.stderr)
        return 1
    if not (repo / ".git").exists():
        print(f"Not a git repository: {repo}", file=sys.stderr)
        return 1

    changelog_path = repo / "CHANGELOG.md"
    if args.from_tags:
        versions = get_versions_from_tags(repo)
    else:
        versions = get_versions_from_changelog(changelog_path)
        if not versions and changelog_path.is_file():
            versions = get_versions_from_tags(repo)

    if not versions:
        print("No versions found (CHANGELOG or git tags).", file=sys.stderr)
        return 1

    base_url = get_repo_remote_url(repo)
    version_commits: dict[str, list[dict]] = {}
    # Build ranges oldest-first: for each version, commits are prev_ref..version (or ..version for first)
    prev_ref = None
    for version, date in reversed(versions):
        ref_range = get_commit_range(repo, version, prev_ref)
        version_commits[version] = get_commits_in_range(repo, ref_range)
        prev_ref = version

    out_path = Path(args.output) if os.path.isabs(args.output) else repo / args.output
    emit_context_md(repo, versions, version_commits, base_url, out_path)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
