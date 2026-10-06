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


def test_the_block_drift_identity_and_model_form_pitfalls_are_recorded():
    text = (REPO / "docs" / "openclaw" / "pitfalls.md").read_text(encoding="utf-8")
    for code in ("T36", "T37", "T38"):
        assert re.search(rf"^## {code} — ", text, re.M), code
    assert "the kit's own test suite stripped it" in text and "openclaw.CONFIG_ROOT" in text
    assert "identity.name" in text and "IDENTITY.md" in text and "`--dry-run`" in text
    assert "anyOf: [string, {primary, fallbacks}]" in text


def test_t39_records_the_blocked_stop_without_inventing_a_cause():
    text = (REPO / "docs" / "openclaw" / "pitfalls.md").read_text(encoding="utf-8")
    m = re.search(r"^## T39 — .*?(?=^## |^# SECTION)", text, re.M | re.S)
    assert m, "T39 is missing"
    t39 = m.group(0)
    for needle in ("stop_shutdown_timeout", "blocked_tool_call", "lower bounds", "gateway_restart_sentinel",
                   "records **no** stalled sessions"):
        assert needle in t39, needle
    sigterm = [ln for ln in t39.splitlines() if "SIGTERM" in ln]
    assert sigterm and all("not established" in ln for ln in sigterm), sigterm


def test_the_runbook_and_readme_name_the_verify_command_and_its_exit_rule():
    runbook = (REPO / "docs" / "openclaw" / "runbook.md").read_text(encoding="utf-8")
    assert "ai-resources verify" in runbook and "OPENCLAW_OFFBOX_LIST_CMD" in runbook
    assert "never `openclaw doctor --fix` bare" in runbook and "OPENCLAW_CLI" in runbook
    assert "ai-resources verify" in (REPO / "README.md").read_text(encoding="utf-8")
