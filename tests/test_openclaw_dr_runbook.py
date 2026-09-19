"""The DR runbook and the host-setup workflow tell the truth about what is and is not proven."""
from __future__ import annotations

import pathlib
import re
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
RUNBOOK = REPO / "docs" / "runbooks" / "openclaw-host-dr.md"
WORKFLOW = REPO / "workflows" / "openclaw-host-setup.workflow.yaml"

from ai_resources import openclaw_host as host  # noqa: E402


def _only_negated_yes(text: str) -> bool:
    """The kit has no --yes flag: the docs may say so, never tell anyone to pass it."""
    return all(re.search(r"(no|not|without|has no)\s+`?--yes", text[max(0, m.start() - 12): m.end()])
               for m in re.finditer(r"--yes", text))


def _rb() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def test_the_runbook_says_plainly_that_the_restore_is_unrehearsed():
    top = "\n".join(_rb().splitlines()[:8])
    assert "UNREHEARSED" in top
    assert "Rehearsal: pending operator step" in _rb()
    assert "verified restorable" in top, "the banner must warn against claiming it"


def test_kit_host_env_is_restored_before_anything_that_depends_on_it():
    text = _rb()
    steps = {int(m.group(1)): m.group(0) for m in re.finditer(r"^(\d+)\. \*\*.*$", text.split("## 3.")[1], re.M)}
    assert "kit-host.env" in steps[1] and "first" in steps[1].lower()
    for later in (2, 4, 7, 9):
        assert "kit-host.env" not in steps[later] or later == 7
    assert text.index("OPENCLAW_INSTALLED_VERSION") < text.index("npm install -g")
    assert "only record" in text and "no version pin" in text


def test_the_runbook_names_the_pre_1_9_0_gap_as_an_accident():
    text = _rb()
    assert "before ai-resources 1.9.0" in text and "by accident" in text


def test_every_command_and_path_the_runbook_cites_exists():
    text = _rb()
    for verb in re.findall(r"ai-resources openclaw (\S+)", text):
        assert verb.strip("`,.") in {"status", "doctor", "bootstrap", "install-units", "agent-new",
                                     "render-gitops-backups"}, verb
    for rel in re.findall(r"\$AGENT_KIT/([A-Za-z0-9_./-]+)", text):
        assert (REPO / rel.rstrip(".")).exists(), rel
    assert (REPO / "templates" / "gitops" / "openclaw-backups").is_dir()
    assert host.ALLOW_SCRIPTS in text, "the install flags must be the ones bootstrap uses"
    assert _only_negated_yes(text)


def test_the_runbook_carries_no_infrastructure_literal():
    text = _rb().lower()
    for literal in ("wildbit", "bithome", "199022639860", "100.76.", "192.168.", "bitgandtter"):
        assert literal not in text


def _wf() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_names_ai_resources_setup_as_the_primary_path():
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "PRIMARY PATH: `ai-resources setup`" in raw
    steps = {s["id"]: s for s in _wf()["steps"]}
    assert list(steps) == ["preflight", "setup", "agents", "verify"]
    assert "ai-resources setup" in steps["setup"]["prompt_template"]
    assert "HUMAN ACTION" in steps["setup"]["prompt_template"]
    assert _only_negated_yes(raw) and "--non-interactive" in raw


def test_the_workflow_keeps_the_hard_rules_and_reports_the_restore_as_unproven():
    raw = WORKFLOW.read_text(encoding="utf-8")
    for rule in ("never restart or stop the gateway", "never\n# write ~/.openclaw/openclaw.json by hand",
                 "never run `openclaw doctor --fix`", "restores ~/.openclaw/kit-host.env FIRST"):
        assert rule in raw, rule
    verify = {s["id"]: s for s in _wf()["steps"]}["verify"]
    assert verify["subagent_type"] == "verifier" and "UNREHEARSED" in verify["prompt_template"]


def test_the_workflow_skill_is_generated_and_indexed():
    assert (REPO / "skills" / "workflow-openclaw-host-setup" / "SKILL.md").is_file()
    assert "workflow-openclaw-host-setup" in (REPO / "skills-index.json").read_text(encoding="utf-8")
