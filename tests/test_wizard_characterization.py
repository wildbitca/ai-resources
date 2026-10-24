"""Characterization of the real wizard's prompt order, step headers and exit code (plan S2).

Each path drives `wizard.run` (real lock, real `_run`, real step functions) with the prompts answered by
`wizard_fakes.Script` or by the real non-interactive wrappers, and records one ordered transcript of
section headers, prompts and the exit code. Steps 9 and 10 are replaced by recorders: they write to the host.
The expected transcripts are stored inline; a prompt or header change shows up as a diff here."""
from __future__ import annotations

import argparse
import types

import pytest

from ai_resources.setup import credentials, detection, state, tools, ui, wizard
from wizard_fakes import Script

DOCKER = ("docker", types.SimpleNamespace(installed=True, version="1", binary_path="/d"))
COCKPITS = {"claude": types.SimpleNamespace(installed=True, version="2.0", binary_path="/opt/claude",
                                            name="Claude Code")}


class _Quiet:
    def print(self, *a, **k):
        pass

    def rule(self, *a, **k):
        pass


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


def run_path(monkeypatch, tmp_path, args, answers=None, seed=None, tty=True):
    home = tmp_path / "home"
    (home / "xdg").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    # a run that answers the preset question leaves the module global set: restore it after the test
    monkeypatch.setattr(wizard, "_PRESET", wizard._PRESET)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: tty)
    monkeypatch.setattr(ui, "detected_table", lambda rows: None)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: dict(COCKPITS))
    monkeypatch.setattr(detection, "detect_container_runtime", lambda: DOCKER)
    monkeypatch.setattr(detection, "detect_compose", lambda cli: True)
    monkeypatch.setattr(tools, "offer", lambda *a, **k: [])
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "stored")
    monkeypatch.setattr(credentials, "get_claude_code_keychain_key", lambda: None)
    if seed is not None:
        monkeypatch.setattr(state, "_corrupt_notice", None, raising=False)
        state.config_root().mkdir(parents=True, exist_ok=True)
        seed = seed()
        if isinstance(seed, str):
            state.state_path().write_text(seed, encoding="utf-8")
        else:
            state.save(seed)
    log: list[str] = []
    script = Script(**(answers or {}))
    if answers is not None:
        script.install(monkeypatch)
    monkeypatch.setattr(ui, "console", lambda: _Quiet())
    for name in ("info", "warn", "error"):
        orig = getattr(ui, name)

        def note(msg, _o=orig, _n=name):
            if _n == "error" or msg.startswith("Skipped") or "DRY RUN" in msg:
                log.append(f"{_n}: {msg}")
            _o(msg)
        monkeypatch.setattr(ui, name, note)
    monkeypatch.setattr(ui, "banner", lambda *a, **k: log.append("banner"))
    monkeypatch.setattr(ui, "section", lambda n, t, title: log.append(f"section {n}/{t} {title}"))
    for kind in ("select", "checkbox", "text", "password", "confirm"):
        orig = getattr(ui, kind)

        def rec(message, *a, _o=orig, _k=kind, **k):
            log.append(f"{_k}: {message}")
            return _o(message, *a, **k)
        monkeypatch.setattr(ui, kind, rec)
    monkeypatch.setattr(wizard, "_step9_apply",
                        lambda s, dry_run=False: log.append(f"apply dry_run={dry_run}") or 0)
    monkeypatch.setattr(wizard, "_step10_verify", lambda s, t: log.append("verify") or 0)
    monkeypatch.setattr(wizard, "_print_completion", lambda s: None)
    rc = wizard.run(args)
    log.append(f"rc={rc}")
    return [line.replace(str(tmp_path), "<TMP>") for line in log]


def _first(values):
    return values[0]


def _saved(mode="multi-model", backend="litellm", profile="", acks=False):
    def make():
        s = state.SetupState()
        s.mode, s.backend = mode, backend
        s.profile.name = profile
        if acks:
            from ai_resources.setup import lanes_render, profiles
            roles = lanes_render.lane_roles(lanes_render.propose(
                profiles.load_profile(profile), lanes_render.current_by_role(s), s.mode))
            s.profile.lane_acks = {"anthropic": lanes_render.make_ack("anthropic", roles, "2026-10-15")}
        return s
    return make


SINGLE = {"Claude Code sign-in": "keep", "Model preset": "custom", "How do you want to choose models": "profile",
          "How should kit subagents pick": _first, "Configure detected cockpits": lambda v: list(v),
          "Apply this?": _first}
SUB = "subscription-claude-55"
LANES_ANSWERS = {**SINGLE, "Use previous answers": True, "Acknowledge for these roles?": True, "How should kit subagents pick": lambda v: SUB, "Orchestrator agent id": "main"}
CLI_TEAM = {"Claude Code sign-in": "keep", "Model preset": "balanced", "Use this?": True,
            "Enable subscription lanes?": False, "Configure detected cockpits": lambda v: list(v),
            "Apply this?": _first}
CORRUPT = "profile: [unclosed\n  name: {oops\n"

PATHS = {
    # ADR-0015: a saved gateway state is migrated to the cockpit's own sign-in; no gateway question follows.
    "saved-litellm-migrated": dict(args=_args(), answers={**SINGLE, "Use previous answers": True},
                                   seed=_saved("multi-model", "litellm")),
    "saved-openrouter-migrated": dict(args=_args(), answers={**SINGLE, "Use previous answers": True},
                                      seed=_saved("multi-model", "openrouter")),
    "single-model": dict(args=_args(), answers=SINGLE),
    "non-interactive-saved-state": dict(args=_args(non_interactive=True),
                                        seed=_saved("multi-model", "openrouter", "balanced-claude-55")),
    "dry-run": dict(args=_args(dry_run=True), answers=SINGLE),
    "lanes-with-stored-ack": dict(args=_args(lanes=True), answers=LANES_ANSWERS,
                                  seed=_saved("single-model", profile=SUB, acks=True)),
    "lanes-non-interactive-without-ack": dict(args=_args(lanes=True, non_interactive=True),
                                              seed=_saved("single-model", profile=SUB)),
    "lanes-dry-run": dict(args=_args(lanes_dry_run=True), answers=LANES_ANSWERS),
    "no-lanes": dict(args=_args(no_lanes=True), answers=SINGLE),
    "non-tty-guard": dict(args=_args(), answers={}, tty=False),
    # S1 of the setup simplification (ADR-0009): the subscription path as it is today (six questions), `--backend cli`
    # (V1: the mode question is still asked, with cli preselected) and `--yes` (the tool offer is faked in this module).
    "cli-team-subscription": dict(args=_args(), answers=CLI_TEAM),
    "backend-cli-flag": dict(args=_args(backend="cli"), answers=CLI_TEAM),
    "yes-flag": dict(args=_args(yes=True), answers=CLI_TEAM),
    "corrupt-state-non-interactive": dict(args=_args(non_interactive=True), seed=lambda: CORRUPT),
}


_CLI_TEAM_TRANSCRIPT = [
    'banner',
    'select: How should setup run?',
    'section 1/5 Your tools',
    'section 2/5 Main tool',
    'section 2/5 Sign-in',
    'select: Claude Code sign-in:',
    'section 3/5 Your team',
    'select: Starting team:',
    'section 4/5 Audio',
    'info: Skipped (voice notes need OpenClaw, which is not one of your tools).',
    'section 5/5 Review',
    'select: Apply this?',
    'apply dry_run=False',
    'verify',
    'rc=0',
]


# Recorded on main before the step runner was introduced, re-recorded for ADR-0015 (round 3). Notes:
# - The "How should they work?" screen is gone: every ticked tool asks its OWN sign-in ("Sign-in", phase 2). Steps 3, 4, 5
#   and 8 of the old gateway path (gateway, providers, API keys, start at login) no longer exist, so a saved gateway state
#   (the two "-migrated" paths) goes through the same questions as any other and prints no gateway line.
# - `--backend` is deprecated: it answers Claude Code's sign-in with the subscription, so that question is not asked.
# - "lanes-with-stored-ack" no longer asks the acknowledgement again: `_step6_single_model_profile` used to rebuild
#   `s.profile`, which dropped a stored `lane_acks` before the lanes step read it; ADR-0009 S7 keeps the profile.
# - The preset question opens step 6 (answered Custom here, so the earlier questions follow unchanged).
EXPECTED = {
    "cli-team-subscription": _CLI_TEAM_TRANSCRIPT,
    "backend-cli-flag": [
        'banner',
        'select: How should setup run?',
        'section 1/5 Your tools',
        'section 2/5 Main tool',
        'section 2/5 Sign-in',
        'section 3/5 Your team',
        'select: Starting team:',
        'section 4/5 Audio',
        'info: Skipped (voice notes need OpenClaw, which is not one of your tools).',
        'section 5/5 Review',
        'select: Apply this?',
        'apply dry_run=False',
        'verify',
        'rc=0',
    ],
    "yes-flag": _CLI_TEAM_TRANSCRIPT,
    "corrupt-state-non-interactive": [
        'banner',
        'error: The setup state file <TMP>/home/xdg/ai-resources/setup-state.yaml is not valid YAML; its content was copied to <TMP>/home/xdg/ai-resources/setup-state.yaml.corrupt. Setup would start from empty answers and replace the file.',
        'error: Not replacing it in a non-interactive run: fix or remove the file, or run `ai-resources setup` from a terminal.',
        'rc=1',
    ],
    "dry-run": [
        'banner',
        'select: How should setup run?',
        'section 1/5 Your tools',
        'section 2/5 Main tool',
        'section 2/5 Sign-in',
        'select: Claude Code sign-in:',
        'section 3/5 Your team',
        'select: Starting team:',
        'section 3/5 Your team - models',
        'select: How do you want to choose models?',
        'section 3/5 Your team - roles',
        'select: How should kit subagents pick their Claude model?',
        'section 4/5 Audio',
        'info: Skipped (voice notes need OpenClaw, which is not one of your tools).',
        'section 5/5 Review',
        'select: Apply this?',
        'apply dry_run=True',
        'rc=0',
    ],
    "lanes-dry-run": [
        'banner',
        'select: How should setup run?',
        'section 1/5 Your tools',
        'section 2/5 Main tool',
        'section 2/5 Sign-in',
        'select: Claude Code sign-in:',
        'section 3/5 Your team',
        'select: Starting team:',
        'section 3/5 Your team - models',
        'select: How do you want to choose models?',
        'section 3/5 Your team - roles',
        'select: How should kit subagents pick their Claude model?',
        'section 3/5 Your team - parallel runs',
        'text: Orchestrator agent id(s) that may dispatch lane agents (comma-separated)',
        'section 4/5 Audio',
        'info: Skipped (voice notes need OpenClaw, which is not one of your tools).',
        'section 5/5 Review',
        'select: Apply this?',
        'apply dry_run=True',
        'rc=0',
    ],
    "lanes-non-interactive-without-ack": [
        'banner',
        'section 1/5 Your tools',
        'section 2/5 Main tool',
        'section 2/5 Sign-in',
        'section 3/5 Your team - roles',
        'select: How should kit subagents pick their Claude model?',
        'section 3/5 Your team - parallel runs',
        'error: --lanes: no stored Anthropic acknowledgement lists every lane role; run `ai-resources setup --lanes` from a terminal to sign it',
        'rc=2',
    ],
    "lanes-with-stored-ack": [
        'banner',
        'select: How should setup run?',
        'section 1/5 Your tools',
        'section 2/5 Main tool',
        'section 2/5 Sign-in',
        'select: Claude Code sign-in:',
        'section 3/5 Your team',
        'select: Starting team:',
        'section 3/5 Your team - models',
        'select: How do you want to choose models?',
        'section 3/5 Your team - roles',
        'select: How should kit subagents pick their Claude model?',
        'section 3/5 Your team - parallel runs',
        'text: Orchestrator agent id(s) that may dispatch lane agents (comma-separated)',
        'section 4/5 Audio',
        'info: Skipped (voice notes need OpenClaw, which is not one of your tools).',
        'section 5/5 Review',
        'select: Apply this?',
        'apply dry_run=False',
        'verify',
        'rc=0',
    ],
    "no-lanes": [
        'banner',
        'select: How should setup run?',
        'section 1/5 Your tools',
        'section 2/5 Main tool',
        'section 2/5 Sign-in',
        'select: Claude Code sign-in:',
        'section 3/5 Your team',
        'select: Starting team:',
        'section 3/5 Your team - models',
        'select: How do you want to choose models?',
        'section 3/5 Your team - roles',
        'select: How should kit subagents pick their Claude model?',
        'section 3/5 Your team - parallel runs',
        'section 4/5 Audio',
        'info: Skipped (voice notes need OpenClaw, which is not one of your tools).',
        'section 5/5 Review',
        'select: Apply this?',
        'apply dry_run=False',
        'verify',
        'rc=0',
    ],
    "non-interactive-saved-state": [
        'banner',
        'section 1/5 Your tools',
        'section 2/5 Main tool',
        'section 2/5 Sign-in',
        'section 3/5 Your team - roles',
        'select: How should kit subagents pick their Claude model?',
        'section 4/5 Audio',
        'info: Skipped (voice notes need OpenClaw, which is not one of your tools).',
        'apply dry_run=False',
        'verify',
        'rc=0',
    ],
    "non-tty-guard": [
        'error: No terminal to prompt on. Re-run with --non-interactive to use saved answers, or from an interactive shell.',
        'rc=1',
    ],
    "saved-litellm-migrated": [
        'banner',
        'select: How should setup run?',
        'section 1/5 Your tools',
        'section 2/5 Main tool',
        'section 2/5 Sign-in',
        'select: Claude Code sign-in:',
        'section 3/5 Your team',
        'select: Starting team:',
        'section 3/5 Your team - models',
        'select: How do you want to choose models?',
        'section 3/5 Your team - roles',
        'select: How should kit subagents pick their Claude model?',
        'section 4/5 Audio',
        'info: Skipped (voice notes need OpenClaw, which is not one of your tools).',
        'section 5/5 Review',
        'select: Apply this?',
        'apply dry_run=False',
        'verify',
        'rc=0',
    ],
    "saved-openrouter-migrated": [
        'banner',
        'select: How should setup run?',
        'section 1/5 Your tools',
        'section 2/5 Main tool',
        'section 2/5 Sign-in',
        'select: Claude Code sign-in:',
        'section 3/5 Your team',
        'select: Starting team:',
        'section 3/5 Your team - models',
        'select: How do you want to choose models?',
        'section 3/5 Your team - roles',
        'select: How should kit subagents pick their Claude model?',
        'section 4/5 Audio',
        'info: Skipped (voice notes need OpenClaw, which is not one of your tools).',
        'section 5/5 Review',
        'select: Apply this?',
        'apply dry_run=False',
        'verify',
        'rc=0',
    ],
    "single-model": [
        'banner',
        'select: How should setup run?',
        'section 1/5 Your tools',
        'section 2/5 Main tool',
        'section 2/5 Sign-in',
        'select: Claude Code sign-in:',
        'section 3/5 Your team',
        'select: Starting team:',
        'section 3/5 Your team - models',
        'select: How do you want to choose models?',
        'section 3/5 Your team - roles',
        'select: How should kit subagents pick their Claude model?',
        'section 4/5 Audio',
        'info: Skipped (voice notes need OpenClaw, which is not one of your tools).',
        'section 5/5 Review',
        'select: Apply this?',
        'apply dry_run=False',
        'verify',
        'rc=0',
    ],
}


import os
import json


@pytest.mark.parametrize("name", sorted(PATHS))
def test_transcript(monkeypatch, tmp_path, name):
    got = run_path(monkeypatch, tmp_path, **PATHS[name])
    if os.environ.get("CHAR_OUT"):
        with open(os.environ["CHAR_OUT"], "a") as fh:
            fh.write(json.dumps({name: got}) + "\n")
    assert got == EXPECTED[name]


def test_every_path_is_pinned():
    assert sorted(PATHS) == sorted(EXPECTED)


def test_the_non_tty_guard_prints_nothing_before_its_error():
    assert EXPECTED["non-tty-guard"][0].startswith("error: No terminal")
