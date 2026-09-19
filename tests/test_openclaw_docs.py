"""docs/openclaw/* describe what the kit implements: every kit path they name exists."""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
DOCS = sorted((REPO / "docs" / "openclaw").glob("*.md"))
KIT_PREFIXES = ("scripts/", "hooks/", "templates/", "profiles/", "skills/", "workflows/", "docs/runbooks/", "tests/")


def _kit_paths(text: str) -> set[str]:
    found = set()
    for raw in re.findall(r"`([^`\s]+)`", text):
        path = raw.split("`")[0].rstrip(".,:;)")
        if path.startswith(KIT_PREFIXES) and not re.search(r"[*{}<>|\[]", path):
            found.add(path)
    return found


def test_every_kit_path_named_in_the_docs_exists():
    missing = [f"{d.name}: {p}" for d in DOCS for p in sorted(_kit_paths(d.read_text(encoding="utf-8")))
               if not (REPO / p).exists()]
    assert not missing


def test_no_section_c_card_still_reads_as_a_proposal():
    text = (REPO / "docs" / "openclaw" / "pitfalls.md").read_text(encoding="utf-8")
    section = text.split("# SECTION C")[1].split("## Sources")[0]
    cards = re.findall(r"^### (C\d\d) — (.*)$", section, re.M)
    assert [c for c, _ in cards] == [f"C{i:02d}" for i in range(1, 11)]
    for code, title in cards:
        assert "IMPLEMENTED" in title, code
    assert "NICE-TO-HAVE" not in section and "ESSENTIAL" not in section
    assert "Proposed paths" not in section


def test_the_docs_do_not_send_anyone_to_the_kit_scripts_in_local_bin():
    """`~/.local/bin` may still appear as history, never as where the kit's scripts are."""
    for doc in DOCS:
        for line in doc.read_text(encoding="utf-8").splitlines():
            if re.search(r"~/\.local/bin/openclaw-", line):
                assert "→" in line or "historical" in line.lower() or "originally" in line.lower(), (doc.name, line[:80])


def test_the_docs_say_the_restore_is_unrehearsed_and_the_kit_has_no_yes_flag():
    text = "\n".join(d.read_text(encoding="utf-8") for d in DOCS)
    assert "never been rehearsed" in text
    assert not re.search(r"(?<!no )(?<!no `)--yes", text), "the kit has no --yes flag: only --non-interactive"
