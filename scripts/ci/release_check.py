#!/usr/bin/env python3
"""Release consistency check for the CI release job (pure logic, no network).

Reads the version from scripts/ai_resources/__init__.py, requires it to match
.claude-plugin/plugin.json and Formula/ai-resources.rb (tag + version) and a
"## [X.Y.Z]" section in CHANGELOG.md, then writes that section as release notes.
Prints `version=X.Y.Z` and `tag=vX.Y.Z` on success (the form $GITHUB_OUTPUT takes).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")      # used with fullmatch: no trailing newline, no Unicode digits


def read_package_version(text: str) -> str | None:
    m = re.search(r'^__version__[ \t]*=[ \t]*"([^"\n]+)"', text, re.M)
    return m.group(1) if m else None


def read_plugin_version(text: str) -> str | None:
    try:
        value = json.loads(text).get("version")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def read_formula(text: str) -> tuple[str | None, str | None]:
    """Return (tag without the leading v, version) from the Formula."""
    tag = re.search(r'^\s*tag:\s*"v?([^"]+)"', text, re.M)
    ver = re.search(r'^\s*version\s+"([^"]+)"', text, re.M)
    return (tag.group(1) if tag else None, ver.group(1) if ver else None)


def extract_notes(changelog: str, version: str) -> str | None:
    """Body between "## [version]" and the next "## [" header; None when the section is missing."""
    lines = changelog.splitlines()
    header = f"## [{version}]"
    body: list[str] = []
    found = False
    for line in lines:
        if not found:
            if line.startswith(header):
                found = True
            continue
        if line.startswith("## ["):
            break
        body.append(line)
    if not found:
        return None
    return "\n".join(body).strip("\n")


def check(package: str, plugin: str, formula: str, changelog: str) -> tuple[str | None, str | None, list[str]]:
    """Return (version, notes, errors)."""
    errors: list[str] = []
    version = read_package_version(package)
    if not version or not SEMVER.fullmatch(version):
        return None, None, [f"scripts/ai_resources/__init__.py: no X.Y.Z __version__ (got {version!r})"]
    plugin_v = read_plugin_version(plugin)
    if plugin_v != version:
        errors.append(f".claude-plugin/plugin.json version {plugin_v!r} != {version!r}")
    f_tag, f_ver = read_formula(formula)
    if f_tag != version:
        errors.append(f"Formula/ai-resources.rb tag {f_tag!r} != 'v{version}'")
    if f_ver != version:
        errors.append(f"Formula/ai-resources.rb version {f_ver!r} != {version!r}")
    notes = extract_notes(changelog, version)
    if notes is None:
        errors.append(f"CHANGELOG.md has no '## [{version}]' section")
    elif not notes.strip():
        errors.append(f"CHANGELOG.md section [{version}] is empty")
    return version, notes, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="repository root")
    ap.add_argument("--notes-file", required=True, help="where to write the release notes")
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        version, notes, errors = check(
            (root / "scripts/ai_resources/__init__.py").read_text(encoding="utf-8"),
            (root / ".claude-plugin/plugin.json").read_text(encoding="utf-8"),
            (root / "Formula/ai-resources.rb").read_text(encoding="utf-8"),
            (root / "CHANGELOG.md").read_text(encoding="utf-8"),
        )
    except OSError as exc:
        print(f"release_check: {exc}", file=sys.stderr)
        return 2
    if errors:
        for e in errors:
            print(f"release_check: {e}", file=sys.stderr)
        return 1
    Path(args.notes_file).write_text((notes or "") + "\n", encoding="utf-8")
    print(f"version={version}")
    print(f"tag=v{version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
