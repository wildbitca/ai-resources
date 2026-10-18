"""`ai-resources setup --help` describes the guided flow (ADR-0008)."""
from __future__ import annotations

import pytest

from ai_resources import cli


def _help() -> str:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["setup", "--help"])
    return ""


def test_setup_help_mentions_the_guided_flow(capsys):
    _help()
    out = capsys.readouterr().out
    for needle in ("10 steps", "Ctrl-B", "Ctrl-C", "Ctrl-D", "Edit a section", "--non-interactive",
                   "--dry-run", "--lanes", "--no-lanes", "--lanes-dry-run", "NO_COLOR"):
        assert needle in out, f"setup --help does not mention {needle!r}"
    for step in ("Tools", "Mode", "Gateway", "Providers", "Credentials", "Models", "Cockpits",
                 "Lifecycle", "Review", "Verification"):
        assert step.lower() in out.lower()


def test_setup_help_documents_the_scope_of_yes_and_backend(capsys):
    _help()
    flat = " ".join(capsys.readouterr().out.split())
    assert "Claude Code, Antigravity (agy) and OpenClaw" in flat
    assert "no other tool is installed by --yes" in flat
    assert "the mode question is not asked" in flat
