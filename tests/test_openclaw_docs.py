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


def test_the_docs_say_the_restore_is_unrehearsed_and_name_yes_only_where_the_flag_exists():
    # Kit 2.2 added `setup --yes` (the tool-install offer only) and `openclaw doctor --yes`; before it the docs
    # said the kit had no --yes flag at all. It may still be named only for the verbs that have it, and a setup
    # `--yes` is never described as confirming anything that touches the gateway.
    text = "\n".join(d.read_text(encoding="utf-8") for d in DOCS)
    assert "never been rehearsed" in text
    allowed = ("openclaw doctor", "setup --yes", "apply-pending", "offbox-identity", "answers only the offer",
               "timers pass", "openclaw bootstrap --yes")      # bootstrap gained --yes in 2.2b (section 3b)
    for doc in DOCS:
        for line in doc.read_text(encoding="utf-8").splitlines():
            if re.search(r"(?<!no )(?<!no `)--yes", line):
                assert any(a in line for a in allowed), (doc.name, line[:100])
    assert "answers only the offer to install missing tools" in text


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


def test_the_egress_proxy_refusal_is_documented_as_bypass_hosts_not_the_allowlist():
    """2.1.1 F7: T44 and the runbook paragraph, with no host-specific name."""
    pit = (REPO / "docs" / "openclaw" / "pitfalls.md").read_text(encoding="utf-8")
    m = re.search(r"^## T44 — .*?(?=^## |^# SECTION|^---$)", pit, re.M | re.S)
    assert m, "T44 is missing"
    run = (REPO / "docs" / "openclaw" / "runbook.md").read_text(encoding="utf-8")
    para = re.search(r"^## An egress-proxy .*?(?=^## )", run, re.M | re.S)
    assert para, "the runbook paragraph is missing"
    for text in (m.group(0), para.group(0)):
        for needle in ("TLS certificate unavailable", "secrets.egressProxy.bypassHosts", "allowedHosts",
                       "graceful-restart", "--dry-run"):
            assert needle in text, needle
    assert "no wildcards" in m.group(0) and "SIGUSR2" in m.group(0)
    # The claim in the docstring: neither new section names a host (a GKE internal host is the shape that
    # leaked into the first draft).
    for text in (m.group(0), para.group(0)):
        assert not re.search(r"\.gke\.goog|\bgke-[a-z0-9-]+|\.internal\b|\b\d{1,3}(?:\.\d{1,3}){3}\b", text), \
            "a host-specific name leaked into the new documentation"


def test_the_native_exec_gap_is_documented_with_the_operator_decision():
    """2.2 B1: the Claude Code hook does not cover an OpenClaw-native exec session; the finding, the options and
    the decision that is the operator's are written down, and the docs claim no control the kit does not ship."""
    pit = (REPO / "docs" / "openclaw" / "pitfalls.md").read_text(encoding="utf-8")
    m = re.search(r"^## T45 — .*?(?=^## |^# SECTION|^---$)", pit, re.M | re.S)
    assert m, "T45 is missing"
    run = (REPO / "docs" / "openclaw" / "runbook.md").read_text(encoding="utf-8")
    para = re.search(r"^## An OpenClaw-native session can still restart the gateway \(T45\).*?(?=^## )", run,
                     re.M | re.S)
    assert para, "the runbook paragraph is missing"
    for text in (m.group(0), para.group(0)):
        for needle in ("PreToolUse", "tools.exec", "before_tool_call", "exec-policy show", "operator decision",
                       "graceful-restart"):
            assert needle in text, needle
    # nothing here may read as a control the kit applies
    assert "the kit does not change" in para.group(0)
    for text in (m.group(0), para.group(0)):
        assert not re.search(r"\.gke\.goog|\bgke-[a-z0-9-]+|\.internal\b|\b\d{1,3}(?:\.\d{1,3}){3}\b", text)


def test_the_guard_is_documented_as_an_accident_guard_not_a_security_boundary():
    """2.2 G1: T46 says what the guard is, what it cannot see, and the findings F1/F2 are cross-linked."""
    pit = (REPO / "docs" / "openclaw" / "pitfalls.md").read_text(encoding="utf-8")
    m = re.search(r"^## T46 — .*?(?=^## |^# SECTION|^---$)", pit, re.M | re.S)
    assert m, "T46 is missing"
    for needle in ("not a security boundary", "accident", "xargs", "alias", "eval", "T45"):
        assert needle in m.group(0), needle
    t44 = re.search(r"^## T44 — .*?(?=^## |^# SECTION|^---$)", pit, re.M | re.S).group(0)
    assert "2.2 candidate" not in t44, "the T44 finding is implemented now"
    assert "ai-resources verify" in t44 and "ai-resources openclaw status" in t44
    run = (REPO / "docs" / "openclaw" / "runbook.md").read_text(encoding="utf-8")
    para = re.search(r"^## An egress-proxy .*?(?=^## )", run, re.M | re.S).group(0)
    assert "ai-resources verify" in para and "status" in para
    t39 = re.search(r"^## T39 — .*?(?=^## |^# SECTION|^---$)", pit, re.M | re.S).group(0)
    assert "restart_close_failed" in t39 and "verify" in t39


def test_the_runbook_names_the_new_flags_and_the_exit_code_convention():
    run = (REPO / "docs" / "openclaw" / "runbook.md").read_text(encoding="utf-8")
    assert "ai-resources openclaw doctor --yes" in run
    assert "ai-resources setup --yes" in run
    assert re.search(r"declin\w+ .{0,60}exit(s)? (code )?1", run)


def test_the_unreleased_changelog_section_covers_every_group_and_no_version_moved():
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    head = text.split("## [2.1.1]")[0]
    assert "## [Unreleased]" in head
    unreleased = head.split("## [Unreleased]")[1]
    for needle in ("gateway guard", "command substitution", "T45", "T46", "setup-state lock", "hand-units",
                   "--yes", "models check", "TLS certificate unavailable"):
        assert needle in unreleased, needle
    assert "2.1.1" in (REPO / "scripts" / "ai_resources" / "__init__.py").read_text(encoding="utf-8")


def test_the_changelog_says_a_refused_graceful_restart_keeps_its_lock_file():
    """2.2b 2c/8a: a real refused restart creates the lock file and never removes it, by design."""
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text.split("## [Unreleased]")[1].split("## [2.1.1]")[0]
    flat = " ".join(unreleased.split())
    assert "a refused `graceful-restart` create no lock file" not in flat
    assert "health-restart.lock" in flat and "keeps it, by design" in flat
    assert "graceful-restart --dry-run" in flat


def test_t46_and_adr_0004_list_the_residual_classes_and_name_systemd_and_polkit_as_the_authority():
    """2.2b 7a/8e/8f: the residual list is written down, the closed classes are not on it, and the false positive
    that stays is said to stay."""
    pit = (REPO / "docs" / "openclaw" / "pitfalls.md").read_text(encoding="utf-8")
    m = re.search(r"^## T46 — .*?(?=^## |^# SECTION|^---$)", pit, re.M | re.S)
    t46 = " ".join(m.group(0).split())
    adr = " ".join(next((REPO / "docs" / "decisions").glob("0004*.md")).read_text(encoding="utf-8").split())
    for text in (t46, adr):
        for needle in ("xargs", "find -exec", "parallel", "alias", "zsh-only", "${V:-", "v=restart; openclaw gateway $v",
                       "polkit", "accident"):
            assert needle in text, needle
    reads = t46.split("What it does not read")[0]
    residual = t46.split("What it does not read")[1].split("A known false positive")[0]
    for closed in ("bash <<EOF", "cat <<EOF | sh", "bash <<< '...'"):
        assert closed in reads and closed not in residual, closed
    assert "V='...'; eval" in reads and "V='...'; eval" not in residual
    assert "A known false positive that stays" in t46 and "kept on purpose" in t46
