"""The openclaw-operations skill: bounded, routable, and consistent with the code it cites."""
from __future__ import annotations

import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
SKILL = REPO / "skills" / "openclaw-operations" / "SKILL.md"

from ai_resources import openclaw_host as host  # noqa: E402


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_the_skill_is_under_the_size_budget():
    assert len(_text()) < 10_000


def test_the_description_leads_with_when_to_use_it_and_names_the_triggers():
    desc = re.search(r'^description: "(.*)"$', _text(), re.M).group(1)
    assert desc.startswith("Use when")
    for word in ("openclaw", "gateway", "watchdog", "control ui", "telegram bot", "~/.openclaw", "openclaw-*.service"):
        assert word in desc.lower()
    assert len(desc) <= 1024


def test_it_is_indexed_with_its_globs():
    index = json.loads((REPO / "skills-index.json").read_text(encoding="utf-8"))
    (entry,) = [s for s in index["skills"] if s["id"] == "openclaw-operations"]
    assert entry["path"] == "skills/openclaw-operations/SKILL.md"
    assert "**/.openclaw/**" in entry["globs"]


def test_the_safe_doctor_is_the_normal_path_and_bare_fix_is_never_advised():
    text = _text()
    assert "ai-resources openclaw doctor" in text
    for line in text.splitlines():
        if "openclaw doctor --fix" in line and "ai-resources openclaw doctor" not in line:
            assert re.search(r"never|bare|only if drained|aborts|denies|stops the gateway|`doctor --fix`", line, re.I), line


def test_the_rules_the_skill_promises_are_all_stated():
    text = _text()
    for phrase in ("Drain before", "watchdog.off", "Never restart or stop the gateway from a tool",
                   "Never write `~/.openclaw/openclaw.json` by hand", "--non-interactive", "OPENCLAW_NARRATION",
                   "Off is the default"):
        assert phrase in text, phrase
    assert "--yes" not in text, "the kit has no --yes flag"


def test_every_stable_name_it_cites_exists_in_the_code():
    text = _text()
    assert "DOCTOR_NOISE" in text and hasattr(host, "DOCTOR_NOISE")
    assert host.GATEWAY_UNIT in text
    for verb in re.findall(r"`ai-resources openclaw (\S+?)[ `]", text):
        assert verb in {"status", "doctor", "bootstrap", "install-units", "agent-new"}, verb
    for rel in re.findall(r"`((?:hooks|scripts|templates)/[A-Za-z0-9_./-]+)`", text):
        assert (REPO / rel).exists(), rel
    for rel in re.findall(r"\$AGENT_KIT/(docs/openclaw/[A-Za-z0-9_./-]+)", text):
        assert (REPO / rel).exists(), rel
