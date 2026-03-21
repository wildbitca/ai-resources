#!/usr/bin/env python3
"""
Normalize CHANGELOG.md to Keep a Changelog format:
- Standard header, [Unreleased], version order (newest first)
- Replace commit-hash lines and ### Summary with one human-readable Changed line per release
- Keep commit hash as a link to GitHub (e.g. [abc1234](https://github.com/org/repo/commit/abc1234))
- Collapse excessive blank lines
"""
import re
import subprocess
import sys

STANDARD_HEADER = """# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
"""

SUMMARY_TO_LINE = {
    "cambios menores en .tf (patch)": "Minor Terraform and config changes.",
    "cambios desde último tag (patch por defecto)": "Minor Terraform and config changes.",
    "nueva(s) variable(s) (additive)": "New optional variable(s).",
    "nuevo(s) recurso(s) (additive)": "New resource(s).",
    "nuevo(s) output(s) (additive)": "New output(s).",
    "upgrade mayor de provider (breaking)": "Major provider upgrade (breaking).",
    "recurso(s) eliminado(s) (breaking)": "Removed resource(s) (breaking).",
    "variable(s) eliminada(s) (breaking)": "Removed variable(s) (breaking).",
    "override manual (--bump)": "Manual version bump.",
}


def parse_version(v: str) -> tuple:
    if v.lower() == "unreleased":
        return (0, 0, 0)
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", v.strip())
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (0, 0, 0)


def version_sort_key(v: str) -> tuple:
    t = parse_version(v)
    if t == (0, 0, 0):
        return (0, 0, 0)
    return (-t[0], -t[1], -t[2])


def summary_to_human(text: str) -> str:
    for key, human in SUMMARY_TO_LINE.items():
        if key in text:
            return human
    return text or "Terraform and provider updates."


def get_repo_base_url(changelog_path: str) -> str | None:
    """Get GitHub (or GitLab) repo base URL from git remote. Returns e.g. https://github.com/org/repo."""
    import os
    repo_dir = os.path.dirname(os.path.abspath(changelog_path))
    try:
        out = subprocess.run(
            ["git", "-C", repo_dir, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0 or not out.stdout:
            return None
        url = out.stdout.strip()
        # git@github.com:org/repo.git -> https://github.com/org/repo
        if url.startswith("git@"):
            url = url.replace(":", "/", 1).replace("git@", "https://", 1)
        if url.startswith("https://") or url.startswith("http://"):
            url = url.rstrip("/").removesuffix(".git")
            return url
        return None
    except Exception:
        return None


def hash_line_to_short_hash(s: str) -> str | None:
    """Extract short commit hash from a line like '- 2be7436 chore: ...'."""
    m = re.match(r"^- ([a-f0-9]{7,})\s", s)
    return m.group(1) if m else None


def clean_body(body_lines: list, is_unreleased: bool, base_url: str | None = None) -> tuple[list, str | None, list[str]]:
    """Replace commit-hash lines and ### Summary with one Changed line; collect hashes for linking.
    Returns (cleaned_lines, changed_line, list_of_hashes).
    """
    out = []
    i = 0
    changed_line = None
    hashes: list[str] = []
    hash_line_re = re.compile(r"^- [a-f0-9]{7,}\s+")

    while i < len(body_lines):
        line = body_lines[i]
        s = line.strip()

        if s == "### Summary":
            j = i + 1
            while j < len(body_lines) and not body_lines[j].strip().startswith("#"):
                if body_lines[j].strip().startswith("- "):
                    changed_line = summary_to_human(body_lines[j].strip()[2:].strip())
                    break
                j += 1
            i = j + 1
            continue

        if hash_line_re.match(s):
            h = hash_line_to_short_hash(s)
            if h:
                hashes.append(h)
            i += 1
            continue

        out.append(line)
        i += 1

    def bullet_line(line_text: str, commit_hashes: list[str]) -> str:
        if not base_url or not commit_hashes:
            return f"- {line_text}\n"
        links = ", ".join(f"[{h}]({base_url}/commit/{h})" for h in commit_hashes)
        return f"- {line_text} ({links})\n"

    # If we captured a summary and the only remaining content is empty or ### Changed with no real bullets,
    # output ### Changed + one bullet (with hash link)
    if changed_line and not is_unreleased:
        has_content = False
        for line in out:
            s = line.strip()
            if s.startswith("### ") and s != "### Changed":
                has_content = True
                break
            if s.startswith("- ") and not hash_line_re.match(s):
                has_content = True
                break
        if not has_content:
            new_out = []
            skip_until_heading = False
            for line in out:
                if line.strip() == "### Changed":
                    new_out.append("### Changed\n")
                    new_out.append(bullet_line(changed_line, hashes))
                    skip_until_heading = True
                    continue
                if skip_until_heading and (line.strip().startswith("### ") or line.strip().startswith("## ")):
                    skip_until_heading = False
                if not skip_until_heading:
                    new_out.append(line)
            return (new_out if new_out else ["### Changed\n", bullet_line(changed_line, hashes)], changed_line, hashes)

        # Has real content: add our line with hash link if ### Changed is empty
        new_out = []
        in_changed = False
        changed_has_bullet = False
        for line in out:
            if line.strip() == "### Changed":
                in_changed = True
                changed_has_bullet = False
                new_out.append(line)
                continue
            if in_changed:
                if line.strip().startswith("- ") and not hash_line_re.match(line.strip()):
                    changed_has_bullet = True
                elif line.strip().startswith("### ") or line.strip().startswith("## "):
                    if not changed_has_bullet and changed_line:
                        new_out.append(bullet_line(changed_line, hashes))
                    in_changed = False
                new_out.append(line)
                continue
            new_out.append(line)
        if in_changed and not changed_has_bullet and changed_line:
            new_out.append(bullet_line(changed_line, hashes))
        return (new_out, changed_line, hashes)

    return (out, changed_line, hashes)


def split_sections(content: str) -> list:
    """Return [(version, date, body_lines), ...]. Body does not include the ## line."""
    sections = []
    pattern = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?")
    lines = content.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        m = pattern.match(lines[i].strip())
        if m:
            version, date = m.group(1), (m.group(2) or "")
            i += 1
            body = []
            while i < len(lines) and not pattern.match(lines[i].strip()):
                body.append(lines[i])
                i += 1
            sections.append((version, date, body))
        else:
            i += 1
    return sections


def normalize(content: str, base_url: str | None = None) -> str:
    content = content.strip()
    # Find first ## [ to start parsing (skip any header before it)
    first = content.find("## [")
    if first >= 0:
        content = content[first:]
    sections = split_sections(content)
    if not sections:
        return STANDARD_HEADER.strip() + "\n\n## [Unreleased]\n\n"

    # Sort: Unreleased first, then by version desc
    unreleased = [(v, d, b) for v, d, b in sections if v.lower() == "unreleased"]
    rest = [(v, d, b) for v, d, b in sections if v.lower() != "unreleased"]
    rest.sort(key=lambda x: version_sort_key(x[0]))
    sections = unreleased + rest

    # Build output: always have [Unreleased] at top
    out = [STANDARD_HEADER.strip(), "", "## [Unreleased]", ""]
    if unreleased and unreleased[0][2] and any(l.strip() for l in unreleased[0][2]):
        for line in unreleased[0][2]:
            out.append(line)
        out.append("")

    first_release = True
    for version, date, body in sections:
        if version.lower() == "unreleased":
            continue
        cleaned, _, _ = clean_body(body, is_unreleased=False, base_url=base_url)
        if not first_release:
            out.append("")  # blank line between releases for readability
        first_release = False
        out.append(f"## [{version}] - {date}" if date else f"## [{version}]")
        out.append("")
        out.extend(cleaned)
        if cleaned and not cleaned[-1].strip().endswith("\n"):
            out.append("")
        out.append("")

    # Collapse 3+ blank lines to 2 (one blank line between releases for readability)
    result = []
    blank_count = 0
    for line in out:
        if isinstance(line, str) and not line.endswith("\n") and line:
            line = line + "\n"
        is_blank = not line.strip()
        if is_blank:
            blank_count += 1
            if blank_count > 2:
                continue
            result.append(line if line else "\n")
        else:
            blank_count = 0
            result.append(line)
    return "".join(result).rstrip() + "\n"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = [a for a in sys.argv[1:] if a.startswith("--")]
    path = args[0] if args else None
    base_url = None
    for o in opts:
        if o.startswith("--repo-url="):
            base_url = o.split("=", 1)[1].strip().rstrip("/").removesuffix(".git")
            break
    if not path:
        print("Usage: normalize-changelog.py [--repo-url=URL] <CHANGELOG.md>", file=sys.stderr)
        sys.exit(1)
    if base_url is None:
        base_url = get_repo_base_url(path)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    normalized = normalize(content, base_url=base_url)
    with open(path, "w", encoding="utf-8") as f:
        f.write(normalized)
    print(f"Normalized {path}")


if __name__ == "__main__":
    main()
