"""Interactive setup wizard — five phases (tools, cockpit, team, audio, more) over ten internal steps (ADR-0010)."""
from __future__ import annotations

import argparse
import copy
import platform
import sys
import time
import types
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    state,
    detection,
    credentials,
    flow,
    litellm,
    profiles,
    providers,
    smoke,
    tools,
    ui,
)
from . import compat, model_selection, profile_names, team_view
from .cockpits import ALL as ALL_COCKPITS
from .cockpits import _shared
from .. import __version__, model_pins, repo_root, roles_policy, selection as selection_mod


TOTAL_STEPS = 10
SINGLE_MODEL_DEFAULT_PROFILE = "balanced"       # ADR-0010 N4: one preset id on every row; claude-native keeps loading
# The default for "multi-model via your CLI subscriptions" (ADR-0008): the subscription team, no gateway.
CLI_TEAM_DEFAULT_PROFILE = "balanced"


def _carry_targets(old: "state.ProfileState", new: "state.ProfileState") -> "state.ProfileState":
    """The tools ticked on the first screen travel in `profile.customizations['__targets__']` until apply: a profile
    that is rebuilt after that screen must take them along."""
    targets = (old.customizations or {}).get("__targets__")
    if targets:
        new.customizations = {**(new.customizations or {}), "__targets__": targets}
    return new


def _reset_profile_for_cli(s: state.SetupState, was_cli: bool) -> None:
    """Moving from a non-CLI mode to the CLI team: an uncustomized claude-native/empty profile is the old
    starting point, not a choice, so it gives way to the subscription team profile."""
    if was_cli or not getattr(s, "cli_team", False):
        return
    if not s.profile.customized and s.profile.name in ("", SINGLE_MODEL_DEFAULT_PROFILE, "claude-native"):
        s.profile = _carry_targets(s.profile, state.ProfileState(name=CLI_TEAM_DEFAULT_PROFILE))


def _single_default_profile(s: state.SetupState) -> str:
    """The profile a single-model-shaped state starts from: the subscription team in CLI mode."""
    return CLI_TEAM_DEFAULT_PROFILE if getattr(s, "cli_team", False) else SINGLE_MODEL_DEFAULT_PROFILE
# The multi-model recommendation: Claude only, three tiers, and valid in every compatibility cell it can
# reach without --allow-unverified (tests/test_profile_validity.py derives that from the matrix).
# Gateway-agnostic, so it is the recommendation for either backend. `measured-best` stays available as the
# explicit multi-provider profile (it names Google and DeepSeek models the Claude cockpit has not verified).
# It is only the default of a host that has not chosen a profile: a saved choice is never replaced.
RECOMMENDED_PROFILE = profiles.RECOMMENDED_PROFILE        # "balanced-claude-55"

# The step 6 preset question (ADR-0008 addendum): three curated presets plus Custom. A preset resolves to a kit
# profile by how the host reaches its models, so the question never names a vendor or a model. The three
# profiles of a row differ, so a saved profile maps back to exactly one preset. Gateway rows use Claude-only
# profiles that are valid in every compatibility cell without --allow-unverified; "single" and "cli" share the
# two subscription-* alias profiles because both need no gateway.
PRESET_CHOICES = (
    ("balanced", "Balanced (recommended)", "strong models where defects are caught, cheaper ones for the rest"),
    ("performance", "Performance", "the strongest models on planning, review and verification; costs more"),
    ("low-cost", "Low cost", "the cheapest models that still keep review, verification and security strong"),
    ("custom", "Custom", "choose a profile, pick models from your accounts, or set each role yourself"),
)
class _PresetRow(dict):
    """One row of PRESET_PROFILES. The key `low-pricing` was renamed `low-cost` (ADR-0010 N4) and is still accepted
    wherever a key is read."""

    def __missing__(self, key):
        renamed = profile_names.preset_key(key)
        if renamed != key:
            return self[renamed]
        raise KeyError(key)

    def get(self, key, default=None):
        return super().get(profile_names.preset_key(key), default)


PRESET_PROFILES: dict[str, dict[str, str]] = {
    "cli": _PresetRow({"balanced": "balanced", "performance": "performance", "low-cost": "low-cost"}),
    "single": _PresetRow({"balanced": "balanced", "performance": "performance", "low-cost": "low-cost"}),
    "litellm": _PresetRow({"balanced": "balanced-claude-55", "performance": "gateway/performance",
                           "low-cost": "gateway/low-cost"}),
    "openrouter": _PresetRow({"balanced": "gateway/balanced", "performance": "gateway/performance",
                              "low-cost": "gateway/low-cost"}),
}
# The preset chosen in THIS run ("" = none: Custom, the flags path or a non-interactive run). Not saved: the
# state file keeps only the resulting profile name.
_PRESET = ""

# Set when the profile was named on the command line, which is not the same as
# a profile carried over in the saved state. A saved name that no longer fits
# the resolved mode or backend must degrade quietly — that is what keeps step 6
# from crashing on a state written under the other backend. A name the user
# typed must not: substituting it silently ran setup under a profile they did
# not ask for and never mentioned it.
_REQUESTED_PROFILE = ""
_ASSUME_YES = False          # `setup --yes`: the explicit yes to the tool-install offer (kit 2.2, E1)
_DRY_RUN = False             # `setup --dry-run`: no installer is offered or run (kit 2.2c, 3b)
# `setup --compose ...` (ADR-0006): the composition flags of this run; empty values mean "not given".
_COMPOSE: dict[str, Any] = {}

# The cockpits step 9 configured, in registry order. Step 10 verifies exactly these: re-deriving
# the selection from the state would judge toolings the operator never picked, and that noise is
# what teaches people to skip the step.
_APPLIED_TARGETS: list[str] = []
# Set by Review > More options (ADR-0009): the operator asked for the Claude parallel runs / the OpenClaw questions in
# THIS run. Neither is asked on the default path.
_PARALLEL_RUNS = False
_GEMINI_ASKED: set[str] = set()     # teams for which Review already asked the OpenRouter Gemini opt-in in this run
_OPEN_OPENCLAW = False
_OPEN_LIFECYCLE = False


def _reject_unavailable_requested_profile(available: list[str], context: str) -> int | None:
    """1 if an explicitly requested profile is not offered here, else None."""
    if _REQUESTED_PROFILE and _REQUESTED_PROFILE not in available:
        ui.error(f"--profile {_REQUESTED_PROFILE} is not available for {context}.")
        ui.detail(f"Available: {', '.join(available)}")
        return 1
    return None


def run(args: argparse.Namespace) -> int:
    """Main wizard entry point: the whole run holds the setup-state lock (load -> modify -> save), so a second
    `setup`, an `apply-pending` or a teardown cannot overwrite what this one records. A dry run writes nothing
    and takes no lock."""
    global _ASSUME_YES, _DRY_RUN, _COMPOSE
    _ASSUME_YES = bool(getattr(args, "yes", False))
    ui.set_assume_yes(_ASSUME_YES)
    _DRY_RUN = bool(getattr(args, "dry_run", False) or getattr(args, "lanes_dry_run", False))
    _COMPOSE = {"mode": getattr(args, "compose", "") or "", "headroom": getattr(args, "headroom", "") or "",
                "live": getattr(args, "headroom_live", "") or "", "why_json": getattr(args, "why_json", "") or "",
                "allow_fable": bool(getattr(args, "allow_fable", False)),
                "allow_unverified": bool(getattr(args, "allow_unverified", False))}
    if _DRY_RUN and _ASSUME_YES:
        ui.info("DRY RUN: --yes installs nothing in a dry run; no installer is offered or run.")
    with ui.prompt_scope(wizard=True):
        try:
            return state.locked_run(lambda: _run(args), out=lambda m: ui.error(m),
                                    enabled=not _DRY_RUN)
        except (ui.Cancelled, KeyboardInterrupt) as e:
            # The lock is already released (locked_run is a context manager). The step name and the list of
            # real side effects are read here, inside the scope, before prompt_scope restores them.
            return _report_cancel(e)


def _report_cancel(e: BaseException) -> int:
    step = getattr(e, "step", "") or ui.current_step()
    where = f" at {step}" if step else ""
    done = ui.actions_done()
    print(f"\n! Setup cancelled{where}.", file=sys.stderr)
    if done:
        print("  Already done in this run: " + "; ".join(done) + ".", file=sys.stderr)
    else:
        print("  Nothing was applied.", file=sys.stderr)
    print("  Re-run `ai-resources setup` to resume with your previous answers.", file=sys.stderr)
    return 130


def _run(args: argparse.Namespace) -> int:
    ui.require_deps()
    global _APPLIED_TARGETS, _PRESET, _PARALLEL_RUNS, _OPEN_OPENCLAW, _OPEN_LIFECYCLE
    _APPLIED_TARGETS = []
    _PRESET = ""
    _PARALLEL_RUNS = _OPEN_OPENCLAW = _OPEN_LIFECYCLE = False
    _GEMINI_ASKED.clear()
    dry_run = bool(getattr(args, "dry_run", False) or getattr(args, "lanes_dry_run", False))

    # Both flags were declared in the CLI and never read, so they accepted input
    # and changed nothing. Non-interactive makes every prompt answer with its
    # saved value; --profile seeds the profile step's default, which that mode
    # then returns. Without a terminal and without the flag the wizard would
    # block forever on the first prompt, so say so instead of hanging.
    if getattr(args, "non_interactive", False):
        ui.set_non_interactive(True)
    elif not ui.stdin_is_a_terminal():
        ui.error("No terminal to prompt on. Re-run with --non-interactive to use "
                 "saved answers, or from an interactive shell.")
        return 1
    ui.banner(
        "AI Resources Kit — Setup Wizard",
        subtitle=BANNER_SUBTITLE,
        version=f"v{__version__}",
    )
    if dry_run:
        ui.console().print(f"[bold yellow]DRY RUN {'—' if ui.unicode_ok() else '-'} no files will be written.[/bold yellow]\n")

    prev_s = state.load()  # frozen snapshot of the previous run (used for teardown)
    s = state.load()
    corrupt = state.take_corrupt_notice()
    if corrupt:
        # 2.2c (3d): the file could not be read; it was copied aside and is read as empty. Unattended runs stop
        # here; an interactive one says so and goes on, the copy being the operator's way back.
        ui.error(f"The setup state file {state.state_path()} is not valid YAML; its content was copied to "
                 f"{corrupt}. Setup would start from empty answers and replace the file.")
        if ui.is_non_interactive() or dry_run:
            if not dry_run:
                ui.error("Not replacing it in a non-interactive run: fix or remove the file, or run "
                         "`ai-resources setup` from a terminal.")
                return 1
        elif not ui.confirm("Continue with empty answers and replace the file?", default=False):
            return 1
        state.allow_replacing_corrupt()
    is_first = state.is_first_run()
    if not is_first:
        last = s.last_run or "(unknown)"
        ui.info(f"Previous config detected at {state.state_path()}")
        ui.detail(f"Last run: {last}")
        if not ui.confirm("Use previous answers as defaults?", default=True):
            s = state.SetupState()
    else:
        ui.info("First-time setup detected.")

    if s.profile.name in profile_names.ALIASES:
        ui.info(f"Profile {s.profile.name} is now called {profile_names.canonical(s.profile.name)}")

    # After state.load() and after the branch that discards previous answers,
    # or the assignment is either an UnboundLocalError or silently overwritten.
    if getattr(args, "profile", ""):
        global _REQUESTED_PROFILE
        _REQUESTED_PROFILE = profile_names.canonical(args.profile)
        s.profile.name = _REQUESTED_PROFILE

    # Model-selection flags are checked before anything is written: a contradiction exits non-zero
    # with zero writes (the same plan table is printed non-interactively).
    rc = _preflight_selection(args, s)
    if rc != 0:
        return rc
    rc = _preflight_compose()
    if rc != 0:
        return rc

    # The questions and the apply run as one ordered list of steps (flow.Wizard): the order, the skips and
    # their wording are the ones this function had when it called the step functions one after the other.
    ctx = types.SimpleNamespace(s=s, prev_s=prev_s, args=args, dry_run=dry_run, is_first=is_first)
    runner = flow.Wizard(
        _build_steps(dry_run),
        on_skip=_report_skip,
        on_enter=_enter_step,
        snapshot=lambda: _snapshot_run(s),
        restore=lambda snap: _restore_run(s, snap),
        notify=lambda m: ui.warn(m.lstrip("! ")),
        scope=lambda spec, back: ui.back_scope(back),
        changes=lambda: len(ui.actions_done()),
    )
    global _RUNNER, _REVIEW_SHOWN
    _RUNNER, _REVIEW_SHOWN = runner, False
    try:
        rc = runner.run(ctx)
    finally:
        _RUNNER = None
        ui.set_progress(0, [])
    if rc != 0:
        return rc

    verify_rc = 0
    if not dry_run:
        # An old profile name is saved under its new one (ADR-0010 N4); every other name is saved as it is.
        s.profile.name = profile_names.canonical(s.profile.name)
        s.openclaw.channels_seen, s.openclaw.channels_wanted = [], []        # never saved (C6)
        state.save(s)
        ui.record_action("saved setup state")
        verify_rc = _step10_verify(s, list(_APPLIED_TARGETS))
        _print_completion(s)
        refused = list(getattr(s.openclaw, "_refused_applies", []))
        if refused:
            ui.error("setup is NOT complete: the restart-required key(s) " + "; ".join(refused) + " were requested "
                     "but not applied. They stay pending; run `ai-resources openclaw apply-pending` from "
                     "outside the gateway.")
            verify_rc = verify_rc or 1
    return verify_rc   # non-zero when a tooling has an error-level finding or a requested apply was refused


def _enter_step(spec: flow.StepSpec, w: flow.Wizard) -> None:
    """Point the progress rail at `spec`; the skipped marks come from the runner history, so a step that a
    Back or an Edit made run again is no longer shown as skipped."""
    by_number: dict[int, list[bool]] = {}
    for d in w.history:
        by_number.setdefault(w.steps[d.index].number, []).append(d.skipped)
    # A numbered step with sub-steps (6a, 6b, 6c) is skipped only when every one of its sub-steps was.
    skipped = {n for n, marks in by_number.items() if n < spec.number and all(marks)}
    ui.set_progress(spec.number, [n for n in range(1, spec.number)], skipped)


def _snapshot_run(s: state.SetupState) -> tuple:
    """What a Back rewind restores: the setup state and the run-scoped globals a step may set."""
    return (copy.deepcopy(s.__dict__), _REQUESTED_PROFILE, list(_APPLIED_TARGETS), _PRESET, _PARALLEL_RUNS,
            _OPEN_OPENCLAW, _OPEN_LIFECYCLE)


def _restore_run(s: state.SetupState, snap: tuple) -> None:
    global _REQUESTED_PROFILE, _APPLIED_TARGETS, _PRESET, _PARALLEL_RUNS, _OPEN_OPENCLAW, _OPEN_LIFECYCLE
    s.__dict__.clear()
    s.__dict__.update(copy.deepcopy(snap[0]))
    _REQUESTED_PROFILE = snap[1]
    _APPLIED_TARGETS = list(snap[2])
    _PRESET = snap[3] if len(snap) > 3 else ""
    _PARALLEL_RUNS = snap[4] if len(snap) > 4 else False
    _OPEN_OPENCLAW = snap[5] if len(snap) > 5 else False
    _OPEN_LIFECYCLE = snap[6] if len(snap) > 6 else False


def _run_mode_step(c) -> int:
    """Step 2 and, right after it, the cleanup offer when the operator just left multi-model."""
    rc = _step1_mode(c.s, getattr(c, "args", None), is_first=getattr(c, "is_first", True))
    if rc != 0:
        return rc
    # If the user just switched multi-model -> single-model, offer to clean up everything we previously
    # installed (the tracking record drives the actions).
    if c.prev_s.mode == "multi-model" and c.s.mode == "single-model" and not c.is_first:
        return _teardown_multi_model(c.prev_s, c.s)
    return 0


def _report_skip(spec: flow.StepSpec, title: str, reason: str) -> None:
    """A skipped step prints its header and reason; a step a preset made redundant (empty reason) prints nothing."""
    if not reason:
        return
    if spec.id in STEP_PHASE:                # a phase that shares another step's number must not mark it skipped
        _section(spec.number, title, phase=STEP_PHASE[spec.id])
    else:
        ui.mark_skipped(spec.number)
        _section(spec.number, title)
    ui.info(reason)


def _build_steps(dry_run: bool) -> list[flow.StepSpec]:
    """The wizard's steps in order. A step function keeps printing its own header; a skipped step gets its
    header and reason from the runner (`skip` returns the header title and the reason)."""
    S = flow.StepSpec

    def multi(c) -> bool:
        return c.s.mode == "multi-model"

    def local_gateway(c) -> bool:
        return multi(c) and c.s.backend == "litellm"

    def gateway_skip(c):
        """Steps 3, 4, 5 and 8 belong to the gateway path: on any other path they are skipped without a line of
        output (ADR-0009). A gateway path that skips one of them still says why."""
        if not multi(c):
            return ("Gateway", "")
        if c.s.backend != "litellm":
            return ("Gateway", "Skipped \u2014 OpenRouter is hosted; there is nothing to install or supervise.")
        return None

    def lifecycle_skip(c):
        if local_gateway(c):
            return None
        return ("Start at login", "Skipped (no local gateway to supervise)." if multi(c) else "")

    def preset_done(c):
        """6a and 6b are answered by the preset: skipped without a line of output."""
        return ("Models and roles", "") if _PRESET else None

    def tool_config_done(c):
        """Step 7 only asks the questions a ticked tool has (OpenClaw): without one it is skipped without output.
        The engine was already asked before the team when this run's cockpit step did it."""
        selected = (c.s.profile.customizations.get("__targets__") or {}).get("selected") or []
        if "openclaw" in selected and (_OPEN_OPENCLAW or not c.s.openclaw.engine
                                       or c.s.__dict__.get(_PHASE2_ATTR)):
            return None
        return ("Tool configuration", "")

    def audio_skip(c):
        """Audio is asked only on the path where the cockpit step asked the engine first; without OpenClaw one line says
        why nothing is asked (ADR-0010 S13)."""
        if "openclaw" not in _selected_tools(c.s):
            return ("Audio", "Skipped (voice notes need OpenClaw, which is not one of your tools).")
        if c.s.__dict__.get(_PHASE2_ATTR) and not c.s.__dict__.get(_WHOLE_ATTR):
            return None
        return ("Audio", "")

    def more_skip(c):
        """The host section and the stale-plugin check are asked once, on the first configure (the path where the
        cockpit step asked the engine); a saved answer is kept and More options > OpenClaw asks it all again."""
        if "openclaw" in _selected_tools(c.s) and c.s.__dict__.get(_PHASE2_ATTR) \
                and not c.s.__dict__.get(_WHOLE_ATTR):
            return None
        return ("More", "")

    def cockpit_skip(c):
        """The main-tool step names the tool among the ticked, installed ones and asks only the OpenClaw questions
        that come before the team; with nothing to ask it is a skip that prints its one line (Back steps over it)."""
        capable = _capable_cockpits(c.s)
        if not capable:
            return ("Main tool", "")
        if "openclaw" in capable and not c.s.openclaw.engine:
            return None
        return ("Main tool", _cockpit_line(capable))

    return [
        S("tools", 1, "Your tools", lambda c: _step2_cockpits(c.s), rewindable=False),
        S("cockpit", 1, "Main tool", lambda c: _step_cockpit_choice(c.s, dry_run=c.dry_run), back=True,
          skip=cockpit_skip),
        S("mode", 2, "How should they work?", _run_mode_step, edit_label="How"),
        S("gateway", 3, "Gateway", lambda c: _step3_litellm(c.s, c.dry_run), rewindable=dry_run,
          skip=gateway_skip, edit_label="Gateway"),
        S("providers", 4, "Providers", lambda c: _step4_providers(c.s),
          skip=lambda c: None if multi(c) else ("Providers", ""),
          edit_label="Providers"),
        S("credentials", 5, "API keys", lambda c: _step5_credentials(c.s, c.dry_run),
          skip=lambda c: None if multi(c) else ("API keys", ""), edit_label="API keys"),
        S("preset", 6, "Your team", lambda c: _step_preset(c.s, c.args), back=True, edit_label="Models"),
        S("models", 6, "Your team - models", lambda c: _step_models(c.s, c.args, c.dry_run), back=True,
          skip=preset_done, edit_label="Models"),
        S("profile", 6, "Your team - roles",
          lambda c: _step6_profile(c.s) if multi(c) else _step6_single_model_profile(c.s), back=True,
          skip=preset_done, edit_label="Role assignment"),
        S("lanes", 6, "Parallel runs", lambda c: _step_lanes(c.s, c.args, dry_run=c.dry_run),
          back=True, edit_label="Parallel runs"),
        S("cockpits", 7, "Tool configuration", lambda c: _step7_cockpit_config(c.s, dry_run=c.dry_run),
          back=True, skip=tool_config_done, edit_label="OpenClaw"),
        S("audio", 7, "Audio", lambda c: _step_audio(c.s), back=True, skip=audio_skip, edit_label="Audio"),
        S("more", 8, "More", lambda c: _step_more(c.s), back=True, skip=more_skip, edit_label="More"),
        S("lifecycle", 8, "Start at login", lambda c: _step8_lifecycle(c.s), skip=lifecycle_skip,
          edit_label="Start at login"),
        S("review", 9, "Review", lambda c: _step_summary(c.s, c.args), rewindable=False),
        S("apply", 9, "Apply", lambda c: _step9_apply(c.s, dry_run=c.dry_run), rewindable=False),
    ]


# --- Model selection (Addendum A1: tools, then accounts, then models, then a summary) ---------
def _openclaw_runner():
    from .. import openclaw_host
    return openclaw_host.default_runner


def _discover_for_wizard(probe: selection_mod.Selection):
    """Read-only discovery for the model lists (cached catalog; no refresh, no write)."""
    from .. import models as models_mod
    runner = _openclaw_runner()
    accounts = model_selection.detect_accounts()
    return models_mod.discover(runner, refresh=False, selection=probe, accounts=accounts,
                               http=models_mod.make_http(), key_for=_key_for)


def _key_for(provider: str) -> str:
    adapter = model_selection.model_providers.REGISTRY.get(provider)
    return credentials.get_key(adapter.credential[0]) if adapter and adapter.credential else ""


def _detected_cockpit_ids(s: state.SetupState) -> list[str]:
    return [cid for cid, cs in s.cockpits.items() if cs.installed]


def _preflight_selection(args: argparse.Namespace, s: state.SetupState) -> int:
    """Validate the model flags before any write. Sets `s.selection` (in memory) when they describe one."""
    wanted = any(getattr(args, a, "") for a in ("models", "shape", "providers", "smoke_path", "backend", "cockpits")) \
        or getattr(args, "allow_unverified", False)
    if not wanted:
        return 0
    backend = getattr(args, "backend", "") or None
    if backend and backend not in ("litellm", "openrouter", "cli"):
        ui.error(f"--backend must be litellm, openrouter or cli, not {backend!r}")
        return 2
    # `cli` is no gateway: single-model shaped (nothing gateway-side can engage), flagged as the CLI team.
    if backend == "cli":
        mode, eff_backend = "single-model", s.backend
    else:
        mode, eff_backend = ("multi-model", backend) if backend else (s.mode, s.backend)

    def apply_backend() -> None:
        if backend == "cli":
            was_cli = bool(getattr(s, "cli_team", False))
            s.mode, s.cli_team = "single-model", True
            _reset_profile_for_cli(s, was_cli)
        elif backend:
            s.mode, s.backend, s.cli_team = "multi-model", backend, False
    try:
        accounts = model_selection.detect_accounts()
        cands = model_selection.candidates(accounts, _discover_for_wizard)
        selection = model_selection.selection_from_args(args, accounts, cands, mode, eff_backend)
    except model_selection.SelectionError as e:
        ui.error(str(e))
        return 2
    except Exception as e:  # noqa: BLE001 - a broken host probe must not look like a contradiction
        ui.error(f"could not read the accounts: {type(e).__name__}")
        return 1
    detected = [cid for cid, det in detection.detect_all_cockpits().items() if det.installed]
    requested = [c.strip() for c in (getattr(args, "cockpits", "") or "").split(",") if c.strip()]
    unknown = [c for c in requested if c not in ALL_COCKPITS]
    if unknown:
        ui.error(f"--cockpits: unknown cockpit(s) {', '.join(unknown)}")
        return 2
    if selection is None and not requested:
        apply_backend()
        return 0
    actions = model_selection.plan_table(selection, mode, eff_backend, requested or detected,
                                         allow_unverified=bool(getattr(args, "allow_unverified", False)))
    for line in model_selection.render_summary(actions, selection, mode, eff_backend):
        ui.detail(line)
    bad = [a for a in actions if a.action == compat.SKIP and a.cockpit in requested]
    if bad and selection is not None:
        for a in bad:
            ui.error(f"--cockpits {a.cockpit}: {a.reason} (set --backend or --allow-unverified where the matrix allows it)")
        return 2
    apply_backend()
    if selection is not None:
        s.set_selection(selection)
    if requested:
        s.profile.customizations.setdefault("__targets__", {})["selected"] = requested
    return 0


def _ask_missing_keys(s: state.SetupState, accounts: dict, dry_run: bool) -> dict:
    """Ask only about the providers that have no credentials yet (masked prompts, same env file)."""
    missing = [pid for pid, a in accounts.items()
               if not a.credentialed and pid not in ("openrouter", "vertex", "ollama")]
    if not missing:
        return accounts
    picked = ui.checkbox("Add a key for these providers? (none needed for the ones you do not use)",
                         [ui.Choice(f"{providers.get(pid).name}", value=pid) for pid in missing], default=[])
    updates: dict[str, str] = {}
    for pid in picked or []:
        env_var = providers.get(pid).primary_env_var
        val = ui.password(env_var)
        if val:
            updates[env_var] = val
    if updates and not dry_run:
        _, added = credentials.update_env_tracked(updates)
        for k in added:
            if k not in s.tracking.env_keys_added:
                s.tracking.env_keys_added.append(k)
        ui.ok(f"Credentials written to {state.env_path()}")
        ui.record_action("saved credentials")
        return model_selection.detect_accounts()
    return accounts


def _saved_defaults(cands: list, saved: Any) -> list:
    """The candidates of `cands` that the saved selection already holds.

    Per slot the exact saved model wins; only when that model is no longer listed does the first stable
    candidate of the same slot stand in. A preview is never pre-checked."""
    slots = getattr(saved, "slots", None) or {}
    by_slot: dict[str, list] = {}
    for c in cands:
        by_slot.setdefault(c.slot, []).append(c)
    out = []
    for slot, group in by_slot.items():
        held = slots.get(slot)
        if held is None:
            continue
        ref = getattr(held, "ref", "")
        exact = [c for c in group if c.ref == ref and c.channel == "stable"]
        if exact:
            out.append(exact[0])
            continue
        stable = [c for c in group if c.channel == "stable"]
        if stable:
            out.append(stable[0])
    order = {id(c): i for i, c in enumerate(cands)}
    return sorted(out, key=lambda c: order[id(c)])


def _pick_candidates(shape: str, cands: dict[str, list], saved: Any = None) -> list | None:
    """The questions of the chosen shape; None when the user backs out. `saved` is the previous selection:
    what it holds is the default of every question."""
    def choices_for(pid):
        return [ui.Choice(c.label, value=c) for c in cands[pid]]

    def models_default(pid):
        held = _saved_defaults(cands[pid], saved)
        return held or [c for c in cands[pid][:1] if c.channel == "stable"]

    if shape == model_selection.SHAPE_SINGLE:
        flat = [ui.Choice(f"{pid}: {c.label}", value=c) for pid in cands for c in cands[pid]]
        held = _saved_defaults([c for pid in cands for c in cands[pid]], saved)
        one = ui.select("Which model?", flat, default=held[0] if held else None)
        return None if one is None else [one]
    if shape == model_selection.SHAPE_ONE_PROVIDER:
        held = _saved_defaults([c for p in cands for c in cands[p]], saved)
        pid = ui.select("Which provider?", [ui.Choice(p, value=p) for p in cands],
                        default=held[0].provider if held else None)
        if pid is None:
            return None
        picked = ui.checkbox(f"Models from {pid}:", choices_for(pid), default=models_default(pid))
        return picked or None
    held_providers = sorted({c.provider for p in cands for c in _saved_defaults(cands[p], saved)})
    chosen = ui.checkbox("Providers:", [ui.Choice(p, value=p) for p in cands], default=held_providers)
    if not chosen or len(chosen) < 2:
        ui.error("Pick at least two providers for this shape.")
        return None
    out: list = []
    for pid in chosen:
        picked = ui.checkbox(f"Models from {pid}:", choices_for(pid), default=models_default(pid))
        out += picked or []
    return out or None


def _agy_lanes_on(s: state.SetupState) -> bool:
    """True when the saved lanes table holds an Antigravity lane agent (a fresh install never does)."""
    return team_view.agy_lanes_on(getattr(s.profile, "lanes", None))


def _agy_team_roles(doc: dict) -> list[str]:
    """What Antigravity would take in this starting team (display only); see `team_view.agy_team_roles`."""
    return team_view.agy_team_roles(doc)


def _show_agy_in_custom(s: state.SetupState) -> None:
    """The Custom model list: the Antigravity group as informational rows (installed) or the one-line reason
    Gemini is absent. Not a candidate: picking happens in step 6c."""
    installed = detection.agy_installed()
    if installed:
        for line in model_selection.agy_group_lines():
            ui.detail(line)
    for line in model_selection.agy_why_lines(installed, _agy_lanes_on(s)):
        ui.info(line)


def _preset_key(s: state.SetupState) -> str:
    """The row of PRESET_PROFILES that applies: how this host reaches its models."""
    if s.mode != "multi-model":
        return "cli" if getattr(s, "cli_team", False) else "single"
    return "openrouter" if s.backend == "openrouter" else "litellm"


def preset_profile(s: state.SetupState, preset: str) -> str:
    """The kit profile a preset resolves to for this state's mode and backend."""
    return PRESET_PROFILES[_preset_key(s)][profile_names.preset_key(preset)]


def _preset_default(s: state.SetupState) -> str:
    """What a saved state means for the question: its preset when the saved profile is one of the presets'
    profiles, Custom for anything else (a customized, composed or account-picked team, another profile), and
    Balanced for a host that has chosen nothing. A saved choice is never replaced by the default."""
    if s.get_selection() is not None or s.profile.customized or s.profile.composed:
        return "custom"
    if not s.profile.name:
        return "balanced"
    saved = profile_names.canonical(s.profile.name)
    for preset, name in PRESET_PROFILES[_preset_key(s)].items():
        if name == saved:
            return preset
    # A profile that predates the preset ids (claude-native, openrouter-balanced, ...) keeps its own file, but the
    # question still opens on the preset it stands for, never on Custom.
    return profile_names.LEGACY_PRESET.get(s.profile.name, "custom")


def _preset_bypassed(args: argparse.Namespace) -> bool:
    """The flags path (and an unattended run) never gets the question: --profile, --models, --shape,
    --providers and --compose already decide, and non-interactive answers from the saved state."""
    return bool(ui.is_non_interactive() or _REQUESTED_PROFILE or _COMPOSE.get("mode")
                or any(getattr(args, a, "") for a in ("profile", "models", "shape", "providers", "compose")))


def _team_rows(s: state.SetupState, base: dict, rows: list) -> list[tuple[str, str, str]]:
    """The team table of screen 3 (see `team_view.tool_rows`)."""
    from . import lanes_render
    members = (s.profile.customizations.get("__targets__") or {}).get("selected") or []
    return team_view.tool_rows(
        members, base, rows, cli_row=_preset_key(s) == "cli", agy_installed=detection.agy_installed(),
        codex_selected="codex" in members or bool(s.cockpits.get("codex") and s.cockpits["codex"].installed),
        codex_held=not lanes_render.CODEX_LANE_ADR_ACCEPTED)


def _show_team_table(s: state.SetupState, base: dict, rows: list) -> None:
    ui.team_table(_team_rows(s, base, rows), title="Who does what")
    if _preset_key(s) == "cli" and not detection.agy_installed():
        for line in model_selection.agy_why_lines(False, False):
            ui.info(line)
    ui.detail("Writing code always stays with Claude Code (safety rule).")


def _show_legacy_difference(s: state.SetupState, new_name: str, mode: str, backend: str | None, rows: list) -> None:
    """A host saved under a profile that predates the preset ids (claude-native, balanced-claude-55,
    openrouter-balanced) keeps its own file until it picks the preset; picking it shows what changes (ADR-0010 N4)."""
    old = s.profile.name
    if not old or old == new_name or old not in profile_names.LEGACY_PRESET:
        return
    try:
        before = profiles.to_executors(profiles.load_profile(old, mode), backend or "litellm")["by_role"]
    except (FileNotFoundError, RuntimeError):
        return
    after = {role: {"model": model} for role, _vendor, model in rows}
    profiles.print_role_diff(before, after, title=f"{old} -> {new_name}")


def _team_view_args(s: state.SetupState) -> dict:
    """What the team view needs to know about the host: its row, the ticked tools, the valid acknowledgements and the
    OpenRouter opt-in."""
    from . import lanes_render
    members = (s.profile.customizations.get("__targets__") or {}).get("selected") or []
    return {"row": _preset_key(s), "ticked": "agy" in members,
            "acks": lanes_render.valid_acks(s.profile.lane_acks),
            "allow_unverified": bool(s.profile.allow_unverified or _COMPOSE.get("allow_unverified")),
            "cell_verified": compat.claude_google_openrouter_verified()}


def _show_team(s: state.SetupState, base: dict, rows: list, *, title: str) -> None:
    """The role table with its real provider and lane, the one held/degrade/next-action line set, and on the CLI row
    the table of tools (ADR-0010 S10)."""
    kwargs = _team_view_args(s)
    ui.role_lane_table(team_view.role_lane_rows(rows, base, **kwargs), title=title)
    for line in team_view.provider_mix(base, rows, **kwargs).lines:
        ui.info(line)
    if kwargs["row"] == "cli":
        _show_team_table(s, base, rows)


def _step_preset(s: state.SetupState, args: argparse.Namespace) -> int:
    """Screen 3, "Your team": ONE question (Balanced, Performance, Low cost or Custom). Choosing a starting team is the
    confirmation: the role table and the team table are shown with it and nothing is asked again (ADR-0009). A starting
    team sets the kit profile and no provider, vendor or model is asked; Custom leaves the earlier steps (6a models,
    6b roles) to run unchanged."""
    global _PRESET
    _PRESET = ""
    if _preset_bypassed(args):
        return 0
    _section(6, "Your team")
    mode = "multi-model" if s.mode == "multi-model" else "single-model"
    backend = s.backend if mode == "multi-model" else None
    available = profiles.list_profiles(mode=mode, backend=backend)
    choices = [ui.Choice(f"{label} - {line}", value=key) for key, label, line in PRESET_CHOICES]
    while True:
        preset = ui.select("Starting team:", choices, default=_preset_default(s))
        if preset is None:
            return 130
        if preset == "custom":
            return 0
        name = preset_profile(s, preset)
        if name not in available:
            ui.error(f"The {preset} starting team needs the profile {name}, which is not available for this setup.")
            continue
        base = profiles.load_profile(name, mode)
        if mode == "multi-model":
            rows = profiles.role_table(profiles.to_executors(base, backend))
        else:
            rows = profiles.role_table(profiles.to_executors(base))
        label = next(lbl for key, lbl, _ in PRESET_CHOICES if key == preset)
        _show_legacy_difference(s, name, mode, backend, rows)
        targets = (s.profile.customizations or {}).get("__targets__")
        s.set_selection(None)
        s.profile.name = name
        s.profile.composed = None
        s.profile.customized = False
        s.profile.customizations = {"__targets__": targets} if targets else {}
        ui.console().print()
        _show_team(s, base, rows, title=f"{label.replace(' (recommended)', '')}: {name}")
        _PRESET = preset
        return 0


def _step_models(s: state.SetupState, args: argparse.Namespace, dry_run: bool = False) -> int:
    """Pick models from the accounts the earlier steps found (interactive), or keep what the flags set."""
    if s.get_selection() is not None and (getattr(args, "models", "") or ui.is_non_interactive()):
        return 0                                    # set by the flags (or saved) and not being changed
    if ui.is_non_interactive():
        return 0
    _section(6, "Your team - models")
    how = ui.select("How do you want to choose models?",
                    [ui.Choice("Use a kit profile (presets for every role)", value="profile"),
                     ui.Choice("Pick models from my accounts (live lists)", value="pick")],
                    default="pick" if s.get_selection() is not None else "profile")
    if how is None:
        return 130
    if how != "pick":
        return 0
    _show_agy_in_custom(s)
    ui.info("Looking at your accounts (names only; no key is shown)...")
    accounts = model_selection.detect_accounts()
    accounts = _ask_missing_keys(s, accounts, dry_run)
    while True:
        creds = {p: a for p, a in accounts.items() if a.credentialed}
        vendor: dict[str, bool] = {}
        for pid in model_selection.VENDOR_LISTING_PROVIDERS:
            if pid in creds and accounts[pid].direct_key:
                vendor[pid] = ui.confirm(f"List new {providers.get(pid).name} models with your key?", default=False)
        cands = model_selection.candidates(accounts, _discover_for_wizard, vendor)
        cands = {p: v for p, v in cands.items() if v}
        if not cands:
            ui.warn("No provider with credentials lists any model; keeping the kit profile.")
            return 0
        saved = s.get_selection()
        saved_shape = getattr(saved, "shape", None)
        shape = ui.select("What do you want to use?",
                          [ui.Choice(label, value=key) for key, label in model_selection.SHAPE_LABELS.items()],
                          default=saved_shape if saved_shape in model_selection.SHAPE_LABELS else None)
        if shape is None:
            return 130
        picks = _pick_candidates(shape, cands, saved)
        if not picks:
            continue
        primary = None
        if len(picks) > 1:
            saved_primary = getattr(saved, "primary", None)
            primary = ui.select("Primary model:", [ui.Choice(f"{c.slot} = {c.id}", value=c.slot) for c in picks],
                                default=saved_primary if saved_primary in {c.slot for c in picks} else None)
        providers_used = sorted({c.provider for c in picks})
        smoke = model_selection.choose_smoke_path(providers_used, s.backend, s.mode, accounts)
        try:
            selection = model_selection.build_selection(shape, picks, primary, accounts=accounts,
                                                        vendor_listing=vendor, smoke_path=smoke)
        except model_selection.SelectionError as e:
            ui.error(str(e))
            continue
        s.set_selection(selection)
        return 0


_RUNNER: "flow.Wizard | None" = None        # the running step list (kept for tests that stub it)
_REVIEW_SHOWN = False                        # the review printed the step 9 header; apply does not repeat it


def _credential_state(s: state.SetupState) -> str:
    """`set` or `missing` per credential this setup needs. Never a value."""
    if s.mode != "multi-model":
        return "native cockpit auth"
    if s.backend == "openrouter":
        names = ["OPENROUTER_API_KEY"]
    else:
        names = []
        for pid, ps in s.providers.items():
            if not ps.enabled or pid == "ollama":
                continue
            try:
                var = "GOOGLE_CLOUD_PROJECT" if pid == "vertex" else providers.get(pid).primary_env_var
            except KeyError:
                continue
            if var:
                names.append(var)
    if not names:
        return "none needed"
    return " \u00b7 ".join(f"{n} {'set' if credentials.get_key(n) else 'missing'}" for n in names)


def _team_label(s: state.SetupState) -> str:
    """The starting team's name for the review: a preset's label when the saved profile is one of the presets', else
    the profile name, marked when it is customized, composed or picked from accounts."""
    name = s.profile.name or "(default)"
    if s.get_selection() is not None:
        return "Custom: models picked from your accounts"
    if s.profile.customized or s.profile.composed:
        return f"Custom: {name} (customized)"
    saved = profile_names.canonical(s.profile.name)
    for key, label, _line in PRESET_CHOICES:
        if key != "custom" and (PRESET_PROFILES[_preset_key(s)].get(key) == saved
                                or profile_names.LEGACY_PRESET.get(s.profile.name) == key):
            return f"{label.replace(' (recommended)', '')}: {name}"
    return f"Custom: {name}"


def _tools_text(s: state.SetupState) -> str:
    targets = (s.profile.customizations or {}).get("__targets__", {}).get("selected") or _detected_cockpit_ids(s)
    names = [tools.TOOLS[c].label if c in tools.TOOLS else c for c in targets]
    return ", ".join(names) or "none"


def _review_rows(s: state.SetupState) -> list[tuple[str, str]]:
    """The review screen's rows from the setup state: How, Team, Tools and what will be written. The gateway rows
    (Gateway, Providers, API keys, Start at login) appear only on the gateway path."""
    multi = s.mode == "multi-model"
    cli_team = not multi and getattr(s, "cli_team", False)
    if cli_team:
        how = "As a team, on your subscriptions (no gateway)"
    elif not multi:
        how = "Each on its own (no gateway)"
    else:
        how = f"Advanced: {s.backend} gateway"
    sel = s.get_selection()
    members = (s.profile.customizations or {}).get("__targets__", {}).get("selected") or []
    team = _team_label(s)
    if sel is not None:
        team += " (" + ", ".join(f"{slot}={sl.ref}" for slot, sl in sel.slots.items()) + ")"
    if "agy" in members and ("google" in (s.profile.lane_acks or {}) or _agy_lanes_on(s)):
        team += (" + Antigravity (reviews and checks only; writing stays on Claude Code)" if _agy_lanes_on(s)
                 else " + Antigravity (terms accepted; held until its gates are proven)")
    rows = [("How", how), ("Team", team), ("Tools", _tools_text(s))]
    if multi:
        if s.backend == "openrouter":
            gateway = "openrouter (hosted)"
        elif s.litellm.deployment == "remote":
            gateway = f"litellm (remote {s.litellm.remote.url or 'url not set'})"
        elif s.litellm.deployment == "skipped":
            gateway = "litellm (skipped)"
        elif litellm.is_container_mode(s.litellm.local.runtime):
            gateway = f"litellm ({s.litellm.local.runtime})"
        else:
            gateway = f"litellm ({s.litellm.local.runtime or 'local'})"
        rows.append(("Gateway", gateway))
        rows.append(("Providers", ", ".join(p for p, ps in s.providers.items() if ps.enabled) or "none"))
        rows.append(("API keys", _credential_state(s)))
        if s.backend == "litellm" and s.litellm.deployment == "local":
            if litellm.is_container_mode(s.litellm.local.runtime):
                rows.append(("Start at login", "container restart policy"))
            else:
                rows.append(("Start at login", "login service" if s.litellm.local.auto_start else "manual start"))
    lane_agents = ((s.profile.lanes or {}).get("agents") or {}) if isinstance(s.profile.lanes, dict) else {}
    if any((a or {}).get("backend") == "claude-cli" for a in lane_agents.values()):
        rows.append(("Parallel runs", "on"))
    from .cockpits import _openclaw_channels
    for i, line in enumerate(_openclaw_channels.review_lines(s)):
        rows.append(("OpenClaw channels" if i == 0 else "", line))
    if "openclaw" in members:                       # image generation (ADR-0011): no row for any other tool, no I/O here
        from .cockpits import _openclaw_image as _image
        rows += _image.review_rows(s, getattr(s, _image.CTX_ATTR, None))
    written = "executors.yaml, tool config files" + (", lanes.yaml" if state.lanes_enabled(s) else "")
    rows.append(("Will write", written))
    return rows


CHANGE_TOOLS, CHANGE_HOW, CHANGE_TEAM, MORE_OPTIONS = "change-tools", "change-how", "change-team", "more-options"
"""The review's choices besides Apply and Cancel (their titles are what the operator reads)."""


def _more_options(s: state.SetupState) -> list:
    """The entries of Review > More options that apply to this run; each opens the existing step and comes back to
    the review. Nothing here is asked on the default path."""
    members = (s.profile.customizations or {}).get("__targets__", {}).get("selected") or []
    out = []
    if getattr(s, "cli_team", False):
        out.append(ui.Choice("Parallel runs - Claude Code on your subscription (off by default)", value="parallel"))
    if s.mode == "multi-model":
        out.append(ui.Choice("Gateway and API keys", value="gateway"))
        if s.backend == "litellm" and s.litellm.deployment == "local":
            out.append(ui.Choice("Start at login", value="lifecycle"))
    if "openclaw" in members:
        out.append(ui.Choice("OpenClaw - engine, host and voice", value="openclaw"))
    return out


def _forget_tool_answers(s: state.SetupState) -> None:
    """Change tools asks the list again: the answers cached for this run are dropped."""
    for attr in ("_install_answer_this_run", "_preview_answer_this_run", "_tool_members_this_run",
                 "_sign_in_answers_this_run"):
        s.__dict__.pop(attr, None)


_REENTRANT = ("gateway", "providers", "credentials", "lifecycle")
"""Steps More options > Gateway / Start at login re-run by design: they are idempotent, so they never refuse."""


def _google_roles(s: state.SetupState) -> list[str]:
    """The roles of this state's profile that run a Google model, in role order."""
    try:
        base = profiles.load_profile(profiles.saved_or_recommended(s.profile.name, s.backend), "multi-model")
    except (FileNotFoundError, RuntimeError):
        return []
    if s.profile.customized and s.profile.customizations:
        base = profiles.merge_customizations(
            base, {k: v for k, v in s.profile.customizations.items() if k in profiles.KNOWN_ROLES})
    by_role = base.get("by_role") or {}
    return [r for r in profiles.KNOWN_ROLES if str((by_role.get(r) or {}).get("model", "")).startswith("google/")]


def _openrouter_gemini_optin(s: state.SetupState) -> None:
    """OpenRouter Balanced declares Gemini, which Claude Code does not verify through OpenRouter yet. The roles that
    use it would silently run on the session model, so Review asks once (default No, never answered for the operator)
    and records `profile.allow_unverified` on a Yes; a No names the roles instead. Skipped when the cell is verified,
    when the flag already allows it, or when the profile has no Google role (ADR-0010 S6b, PD-5)."""
    if s.mode != "multi-model" or s.backend != "openrouter" or compat.claude_google_openrouter_verified():
        return
    roles = _google_roles(s)
    if not roles or _COMPOSE.get("allow_unverified"):
        return
    if "claude" not in _detected_cockpit_ids(s):
        return
    key = profiles.saved_or_recommended(s.profile.name, s.backend) + ("*" if s.profile.customized else "")
    if key in _GEMINI_ASKED:                     # asked once per team in this run, not again on a return to Review
        return
    _GEMINI_ASKED.add(key)
    rows = [(r, "google", "") for r in roles]
    held = team_view.provider_mix({}, rows, row="openrouter").lines[0]
    if ui.is_non_interactive() or _ASSUME_YES:
        # `--yes` answers the install offer only; this opt-in is never answered for the operator.
        if not s.profile.allow_unverified:
            ui.info(held)
        return
    pretty = ", ".join(roles[:-1]) + " and " + roles[-1] if len(roles) > 1 else roles[0]
    if ui.confirm(f"Use Gemini on {pretty} through OpenRouter (not yet verified with Claude Code)?", default=False):
        s.profile.allow_unverified = True
    else:
        s.profile.allow_unverified = False
        ui.info(held)


def _step_summary(s: state.SetupState, args: argparse.Namespace) -> int:
    """Screen 4, "Review", BEFORE the write: the answers by name, then Apply, Change tools, Change how, Change team,
    More options or Cancel. A Change jumps back to that screen with the current answers as defaults and returns here.

    With a recorded model selection the review also lists what each tool will get. A non-interactive run keeps its
    silent skip on the kit-profile path, because nothing can answer the question."""
    global _REVIEW_SHOWN, _PARALLEL_RUNS, _OPEN_OPENCLAW, _OPEN_LIFECYCLE
    selection = s.get_selection()
    if selection is None and ui.is_non_interactive():
        return 0
    _section(9, "Review")
    _REVIEW_SHOWN = True
    _openrouter_gemini_optin(s)
    ui.review_table(_review_rows(s), title="Your answers")
    if selection is not None:
        detected = _detected_cockpit_ids(s)
        actions = model_selection.plan_table(selection, s.mode, s.backend, detected,
                                             engine=selection.openclaw_engine or None)
        ui.console().print()
        ui.info("Summary of what will be configured")
        for line in model_selection.render_summary(actions, selection, s.mode, s.backend):
            ui.detail(line)
    if ui.is_non_interactive():
        return 0
    choices = [model_selection.APPLY, ui.Choice("Change tools", value=CHANGE_TOOLS),
               ui.Choice("Change how", value=CHANGE_HOW), ui.Choice("Change team", value=CHANGE_TEAM)]
    more = _more_options(s)
    if more:
        choices.append(ui.Choice("More options...", value=MORE_OPTIONS))
    choices.append(model_selection.CANCEL)
    answer = ui.select("Apply this?", choices, default=model_selection.APPLY)
    which = ""
    if answer == MORE_OPTIONS:
        which = ui.select("More options", more + [ui.Choice("Back to the review", value="")])
        if not which:
            return _step_summary(s, args)
    target = {CHANGE_TOOLS: "tools", CHANGE_HOW: "mode"}.get(answer) or {
        "parallel": "lanes", "gateway": "gateway", "lifecycle": "lifecycle", "openclaw": "cockpits"}.get(which)
    if answer == CHANGE_TEAM and selection is None:
        target = "preset"
    if target and _RUNNER is not None:
        refusal = _RUNNER.jump_refusal(target, reentrant=_REENTRANT if which in ("gateway", "lifecycle") else ())
        if refusal:
            ui.warn(refusal.lstrip("! "))
            return _step_summary(s, args)
    if answer == CHANGE_TOOLS:
        _forget_tool_answers(s)
        raise flow.JumpTo("tools")
    if answer == CHANGE_HOW:
        raise flow.JumpTo("mode")
    if answer == CHANGE_TEAM:
        if selection is None:
            raise flow.JumpTo("preset")
        s.set_selection(None)                  # a team picked from accounts: pick again, then show the review again
        rc = _step_models(s, args, bool(getattr(args, "dry_run", False)))
        return rc if rc != 0 else _step_summary(s, args)
    if answer == MORE_OPTIONS:
        if which == "parallel":
            _PARALLEL_RUNS = True
            raise flow.JumpTo("lanes")
        if which == "gateway":
            raise flow.JumpTo("gateway")
        if which == "lifecycle":
            _OPEN_LIFECYCLE = True
            raise flow.JumpTo("lifecycle")
        if which == "openclaw":
            _OPEN_OPENCLAW = True
            raise flow.JumpTo("cockpits")
        return _step_summary(s, args)
    if answer != model_selection.APPLY:
        # The same block as every other cancel point: it lists all earlier real actions of this run.
        return _report_cancel(ui.Cancelled(ui.current_step()))
    return 0


# --- Step 1 — Mode ---------------------------------------------------------------
def _try_install_pipx() -> bool:
    """Offer to install pipx automatically. Returns True if attempt succeeded."""
    import platform
    import subprocess

    options: list[tuple[str, list[str], str]] = []
    if platform.system() == "Darwin" and detection._which("brew"):
        options.append(("brew install pipx", ["brew", "install", "pipx"],
                        "Homebrew (macOS) — recommended"))
    if detection._which("apt"):
        options.append(("apt install pipx (sudo)", ["sudo", "apt", "install", "-y", "pipx"],
                        "Debian/Ubuntu apt"))
    if detection._which("dnf"):
        options.append(("dnf install pipx (sudo)", ["sudo", "dnf", "install", "-y", "pipx"],
                        "Fedora/RHEL dnf"))
    # Always available: pip --user
    options.append(("python3 -m pip install --user pipx",
                    ["python3", "-m", "pip", "install", "--user", "pipx"],
                    "Universal — pip user install"))
    options.append(("Skip — I'll install manually", [], ""))

    choices = [ui.Choice(f"{label}  {hint}", value=i) for i, (label, _, hint) in enumerate(options)]
    # Enter skips: the first entry is an installer (a package manager, sometimes with sudo).
    pick = ui.select("How should I install pipx?", choices, default=len(options) - 1)
    if pick is None:
        return False
    label, cmd, _ = options[pick]
    if not cmd:
        ui.detail("Install pipx manually then re-run `ai-resources setup`.")
        return False

    r = ui.run_with_spinner(f"Running: {' '.join(cmd)}", cmd, timeout=300)
    if r.returncode != 0:
        ui.error("pipx was not installed")
        return False
    ui.ok("pipx installed")
    ui.record_action("installed pipx")

    # Run pipx ensurepath so the binary lands in PATH for future shells
    pipx_path = detection._which("pipx") or str(Path.home() / ".local" / "bin" / "pipx")
    if Path(pipx_path).exists():
        subprocess.run([pipx_path, "ensurepath"], capture_output=True, text=True)
    return True


def _gateway_credential_warning(s: state.SetupState) -> None:
    """Stated up front because it is irreversible in practice and surprises people: a gateway credential
    replaces the claude.ai login, so the subscription stops applying and every token is billed to the gateway."""
    ui.warn("A gateway credential replaces your claude.ai subscription for Claude Code: "
            "usage is billed per token to the gateway, not to your plan.")
    if s.backend == "openrouter":
        ui.detail("Claude Code's own cost figures (/usage, statusline, --max-budget-usd) are "
                  "computed at Anthropic list price and will be wrong. Set spend limits on the "
                  "OpenRouter key instead.")


HOW_CHOICES = (
    ("team", "As a team", "each role goes to the tool you choose, on your subscriptions, no API keys"),
    ("alone", "Each on its own", "every tool keeps its own sign-in and settings"),
    ("advanced", "Advanced", "gateway (OpenRouter) with an API key and budgets"),
)
# ADR-0010 (N1): LiteLLM is no longer offered to new setups. A saved litellm gateway still loads and runs.
GATEWAY_CHOICES = (
    ("openrouter", "OpenRouter", "hosted: one key, nothing to install"),
)
LEGACY_GATEWAY_CHOICE = ("litellm", "LiteLLM (saved; no longer offered for new setups)",
                         "the gateway this kit installed and supervises for you")
LITELLM_LEGACY_NOTICE = ("LiteLLM is no longer offered for new setups; your saved gateway keeps working. "
                         "Choose OpenRouter to switch.")
HOW_PROMPT = "Pick how they work together:"


def _how_default(s: state.SetupState, is_first: bool = True) -> str:
    """What a saved state means for the question, else team when two or more tools are ticked, else each alone."""
    if s.mode == "multi-model":
        return "advanced"
    members = (s.profile.customizations.get("__targets__") or {}).get("selected") or []
    if getattr(s, "cli_team", False) or "agy" in members:
        return "team"
    if not is_first:
        return "alone"
    return "team" if len(members) >= 2 else "alone"


def _step1_mode(s: state.SetupState, args: "argparse.Namespace | None" = None, *, is_first: bool = True) -> int:
    """Screen 2: how the tools work together. The answer is the same state as the four-way mode question wrote
    (ADR-0009): As a team is `single-model` + `cli_team`, Each on its own is `single-model`, Advanced asks which
    gateway and is `multi-model`."""
    backend = getattr(args, "backend", "") if args is not None else ""
    if backend:
        # `--backend` already decided the mode (the preflight applied it to the state): no question (ADR-0009).
        ui.info(f"Mode set by --backend {backend}; the mode question is skipped.")
        if backend == "litellm":
            ui.info(LITELLM_LEGACY_NOTICE)
        if s.mode == "multi-model":
            _gateway_credential_warning(s)
        return 0
    _section(2, "How should they work?")
    chosen = s.get_selection()
    if chosen is not None and s.mode == "single-model":
        # A single-model selection is already recorded: the choice is made and the summary says which
        # tools it leaves out. Pass --backend to move to a gateway; no question is asked.
        ui.info("Single-model selection recorded; no gateway or backend is asked for "
                "(pass --backend to use one).")
        return 0
    choices = [ui.Choice(f"{label} - {line}", value=key, description=" ") for key, label, line in HOW_CHOICES]
    picked = ui.select(HOW_PROMPT, choices, default=_how_default(s, is_first))
    if picked is None:
        ui.warn("Setup cancelled.")
        return 130

    was_cli = bool(getattr(s, "cli_team", False))
    if picked == "alone":
        s.mode, s.cli_team = "single-model", False
        return 0
    if picked == "team":
        # No gateway: the mode stays single-model-shaped, the flag names the team (ADR-0008 addendum).
        s.mode, s.cli_team = "single-model", True
        _reset_profile_for_cli(s, was_cli)
        return 0
    saved_litellm = s.mode == "multi-model" and s.backend == "litellm"
    offered = ((LEGACY_GATEWAY_CHOICE,) if saved_litellm else ()) + GATEWAY_CHOICES
    if saved_litellm:
        ui.info(LITELLM_LEGACY_NOTICE)
    if len(offered) == 1:
        gateway = offered[0][0]
        ui.info(f"Gateway: {offered[0][1]}.")
    else:
        gateway = ui.select("Which gateway?", [ui.Choice(f"{label} - {line}", value=key, description=" ")
                                               for key, label, line in offered],
                            default="litellm" if saved_litellm else "openrouter")
        if gateway is None:
            ui.warn("Setup cancelled.")
            return 130
    s.mode, s.cli_team, s.backend = "multi-model", False, gateway
    _gateway_credential_warning(s)
    return 0


# --- Teardown — multi-model → single-model ---------------------------------------
def _teardown_multi_model(prev_s: state.SetupState, s: state.SetupState) -> int:
    """When switching multi-model → single-model, undo what we previously installed.

    Drives off `prev_s.tracking` (the audit trail from the prior run). Asks once
    for confirmation, listing every action it will take. On approval, executes
    each action and resets the relevant `s` fields so the wizard continues with
    a clean slate.
    """
    ui.console().print()
    target = "'As a team'" if s.cli_team else "'Each on its own'"
    if hasattr(ui.console(), "rule"):
        ui.console().rule(f"[bold yellow]Switching to {target} — cleanup[/]",
                          style="yellow", align="left")
    else:
        ui.info(f"Switching to {target} — cleanup")

    actions = litellm.plan_multi_model_teardown(prev_s)
    if not actions:
        ui.info("Nothing to clean up (no tracked artifacts from previous run).")
        return 0

    ui.info("The previous setup installed the following artifacts. "
            f"Switching to {target} will undo them:")
    for _aid, desc in actions:
        ui.detail(f"  • {desc}")
    ui.console().print()

    if not ui.confirm("Proceed with cleanup?", default=True):
        ui.warn("Cleanup skipped — multi-model artifacts will remain on disk.")
        ui.detail("You can re-run setup later or remove them manually.")
        return 0

    ui.record_action("ran the multi-model cleanup")
    results = litellm.execute_multi_model_teardown(prev_s)
    for aid, ok, msg in results:
        if ok:
            ui.ok(f"{aid}: {msg}")
        else:
            ui.warn(f"{aid}: {msg}")

    # Reset the parts of `s` that no longer apply in single-model. `tracking` carries
    # the claude/agy/openclaw tool-install record (S4) alongside the multi-model
    # artifacts this function just tore down; those tools are mode-agnostic (step 2
    # offers them regardless of single- vs multi-model), so a plain reset here would
    # silently orphan that record and leave a later `ai-resources setup` teardown
    # unable to find what it installed. OpenClaw's own orchestration state
    # (`s.openclaw`) is untouched entirely — this function only undoes the LiteLLM/
    # OpenRouter gateway setup, never the chat-facing engine the kit pointed OpenClaw at.
    s.backend = "litellm"
    s.litellm = state.LiteLLMState()
    s.providers = {}
    s.profile = _carry_targets(s.profile, state.ProfileState(name=SINGLE_MODEL_DEFAULT_PROFILE, customized=False))
    if s.cli_team:
        s.profile.name = CLI_TEAM_DEFAULT_PROFILE
    s.tracking = state.InstallTracking(
        tools_installed_by_us=list(s.tracking.tools_installed_by_us),
        tool_install_methods=dict(s.tracking.tool_install_methods),
        rc_lines_added={k: list(v) for k, v in s.tracking.rc_lines_added.items()},
        install_tools_answer=s.tracking.install_tools_answer,
    )
    return 0


# --- Screen 1 "Your tools" (step 1) --------------------------------------------------
PHASES = 5
"""The operator sees five phases (Tools, Cockpit, Team, Audio, More) over the ten internal steps; the rail and
`TOTAL_STEPS` stay (ADR-0010 PD-3, superseding the four screens of ADR-0009)."""
PHASE_LABELS = flow.PHASE_LABELS
# Phase of each internal step: tools 1; cockpits 2; mode, gateway, providers, credentials and the whole team 3;
# voice 4; lifecycle, review and apply 5. Step 7 ("Tool configuration") joins phase 3 until the cockpit
# questions move to phase 2 (plan S11) and the voice questions to phase 4 (S13).
PHASE_OF = {1: 1, 2: 3, 3: 3, 4: 3, 5: 3, 6: 3, 7: 3, 8: 5, 9: 5, 10: 5}
"""Which phase each internal step belongs to; an unknown step belongs to the last phase."""
STEP_PHASE = {"cockpit": 2, "audio": 4}
"""Steps that sit in another phase than their internal number says (they share a number with a neighbour)."""
# The four-screen names stay importable for callers that still use them.
SCREENS = PHASES
SCREEN_OF = PHASE_OF
BANNER_SUBTITLE = "Set up your AI coding tools"


def _phase_of(step: int) -> int:
    return PHASE_OF.get(step, PHASES)


_screen_of = _phase_of


def _section(step: int, title: str, phase: int | None = None) -> None:
    """A step header numbered by phase (`[3/5]`); the rail under it still counts the ten steps. `phase` overrides
    the step's own phase for a step that shares a number with the team (the cockpit step is phase 2)."""
    ui.section(phase or _phase_of(step), PHASES, title)
HIDDEN_TOOLS = ("gemini",)
"""Retired tools: never listed by the wizard (an old state that holds one still loads)."""


def _agy_tick_restorable(s: state.SetupState) -> bool:
    """A saved Antigravity tick is restored only when a valid stored Google acknowledgement exists (the text hash of
    today's text, the account named). A new user, or a saved tick with no valid acknowledgement, starts unticked, so
    the terms check is never skipped by a leftover choice (ADR-0010 S10)."""
    from . import lanes_render
    return "google" in lanes_render.valid_acks(s.profile.lane_acks)


def _default_members(s: state.SetupState, detected: dict) -> list[str]:
    """The tools ticked when the screen opens (and the team of a run that cannot ask): the saved or requested
    selection that is still installed, else every installed tool. Antigravity is the exception: it starts ticked only
    when it was saved ticked (or the saved lanes hold it) AND a valid Google acknowledgement is stored; a fresh install
    never opts in on its own."""
    installed = [cid for cid, det in detected.items() if det.installed]
    saved = (s.profile.customizations.get("__targets__") or {}).get("selected")
    restorable = _agy_tick_restorable(s)
    held = [c for c in (saved or []) if c in installed and (c != "agy" or restorable)]
    if held:
        return held
    return [cid for cid in installed if cid not in HIDDEN_TOOLS and (cid != "agy" or (_agy_lanes_on(s) and restorable))]


def _step2_cockpits(s: state.SetupState) -> int:
    _section(1, "Your tools")
    ui.info("Scanning installed AI CLIs...")
    detected = detection.detect_all_cockpits()

    def print_scan() -> None:
        """The plain scan list. tools.offer calls it only where its selector is NOT shown: the selector is the same
        list (installed ones with their version, installable ones tickable), so it is never printed twice."""
        rows = []
        for cid, det in detected.items():
            if cid in HIDDEN_TOOLS and not det.installed:
                continue
            status = "✔" if det.installed else "✘"
            info = f"v{det.version}  {det.binary_path}" if det.installed else ""
            rows.append((status, det.name, info))
        ui.detected_table(rows)

    # The one list: tick the tools to use, tick a missing one to install it. It runs right after the scan and before
    # the "no tools" check, so a machine with none can still install one here. Re-detecting afterwards lets the later
    # steps see what was installed.
    installed_tools = tools.offer(s, assume_yes=_ASSUME_YES, dry_run=_DRY_RUN, cockpits=detected,
                                  print_scan=print_scan, members=True,
                                  members_default=tuple(_default_members(s, detected)))
    if installed_tools:
        detected = detection.detect_all_cockpits()

    installed_count = 0
    for cid, det in detected.items():
        cs = s.cockpits.get(cid) or state.CockpitState()
        cs.installed = det.installed
        cs.version = det.version
        cs.binary_path = det.binary_path
        s.cockpits[cid] = cs
        if det.installed:
            installed_count += 1

    if installed_count == 0:
        ui.console().print()
        ui.warn("No tools detected. Install at least one (e.g. Claude Code) and re-run setup.")
        return 1

    picked = getattr(s, "_tool_members_this_run", None)
    members = [c for c in (picked if picked is not None else _default_members(s, detected))
               if c in detected and detected[c].installed]
    s.profile.customizations.setdefault("__targets__", {})["selected"] = members  # stash for apply step
    ui.console().print()
    ui.info(f"{len(members)} tool(s) in use. To add another, install it by your usual method, then re-run "
            "`ai-resources setup`.")
    return 0


# --- Step 3 — LiteLLM gateway ----------------------------------------------------
def _step3_litellm(s: state.SetupState, dry_run: bool = False) -> int:
    _section(3, "Gateway (LiteLLM)")

    # Detect what's available so we can flag disabled options
    docker_runtime, docker_det = detection.detect_container_runtime()

    docker_label = (f"Local — Docker container  ({docker_runtime} {docker_det.version}"
                    f"  recommended, sidesteps Python compat)") if docker_det.installed \
                   else "Local — Docker container  (recommended — install Docker Desktop / Colima first)"

    choices = [
        ui.Choice(docker_label, value="local-docker",
                  disabled=None if docker_det.installed else "docker/podman/colima not detected"),
        ui.Choice("Local — without Docker  (pip + venv, or pipx)", value="local-nodocker"),
        ui.Choice("Use existing remote endpoint  (URL + master key)", value="remote"),
        ui.Choice("Skip  (advanced — direct API calls without gateway)", value="skipped"),
    ]

    # Map saved deployment → choice value for default
    saved = s.litellm.deployment
    if saved == "local":
        default = "local-docker" if litellm.is_container_mode(s.litellm.local.runtime) else "local-nodocker"
    elif saved == "remote":
        default = "remote"
    elif saved == "skipped":
        default = "skipped"
    else:
        default = "local-docker" if docker_det.installed else "local-nodocker"

    choice = ui.select("Where is your gateway?", choices, default=default)
    if choice is None:
        return 130

    if choice == "local-docker":
        s.litellm.deployment = "local"
        s.litellm.local.runtime = docker_runtime or "docker"
        return _step3_docker(s)
    if choice == "local-nodocker":
        s.litellm.deployment = "local"
        return _step3_local_nodocker(s)
    if choice == "remote":
        s.litellm.deployment = "remote"
        return _step3_remote(s, dry_run)
    s.litellm.deployment = "skipped"
    return 0  # skipped


def _validate_port(value: str) -> bool | str:
    """Inline validation for a TCP port; an empty answer keeps the default."""
    raw = (value or "").strip()
    if not raw:
        return True
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return True
    return "Enter a port between 1 and 65535"


def _validate_url(value: str) -> bool | str:
    """Inline validation for a gateway URL: http or https with a host."""
    from urllib.parse import urlparse
    parsed = urlparse((value or "").strip())
    if parsed.scheme in ("http", "https") and "@" in parsed.netloc:
        # Never echoed back: the value may be a credential. The master key is asked for separately.
        return "Remove the credentials (user:password@) from the URL; the master key is asked for next"
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return True
    return "Enter an http:// or https:// URL, e.g. https://litellm.example.com"


def _write_smoke_log(path: Path, results: list) -> None:
    """Append the smoke results, masked, to a file only the operator can read (0600, even when an older run
    left it looser): a gateway error body can echo a credential."""
    import os
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "a", encoding="utf-8") as fp:
            fp.write(f"\n=== {datetime.now(timezone.utc).isoformat()} ===\n")
            for label, ok, msg in results:
                fp.write(f"[{'PASS' if ok else 'FAIL'}] {ui.safe_text(label)}\n")
                if msg:
                    fp.write(f"  {ui.safe_text(msg)}\n")
    except OSError:
        pass


def _validate_model_id(value: str) -> bool | str:
    """Inline validation for a typed model id: no spaces. An empty answer is valid and skips the role."""
    raw = (value or "").strip()
    if not raw:
        return True
    if any(ch.isspace() for ch in raw):
        return "A model id has no spaces"
    return True


def _required(what: str):
    """A validator for a prompt that cannot be left empty (a key the setup cannot do without)."""
    def check(value: str) -> bool | str:
        return True if (value or "").strip() else f"{what} is required"
    return check


def _ask_bind_and_port(s: state.SetupState) -> None:
    """Shared prompt for bind address + port — used by all local modes."""
    bind_choices = [
        ui.Choice("127.0.0.1  (localhost only — recommended)", value="127.0.0.1"),
        ui.Choice("0.0.0.0  (DANGEROUS — exposes to LAN)", value="0.0.0.0"),
    ]
    s.litellm.local.bind_address = ui.select(
        "Bind interface:", bind_choices,
        default=s.litellm.local.bind_address or "127.0.0.1",
    )
    s.litellm.local.port = int(ui.text(
        "Port", default=str(s.litellm.local.port or 4000),
        validate=_validate_port,
    ) or 4000)


def _step3_docker(s: state.SetupState) -> int:
    """Docker mode — pull image, container managed via docker compose."""
    runtime, det = detection.detect_container_runtime()
    if not det.installed:
        ui.error("No container runtime found. Install Docker Desktop / Colima / Podman first.")
        ui.detail("macOS:  brew install --cask docker  (or)  brew install colima && colima start")
        ui.detail("Linux:  https://docs.docker.com/engine/install/")
        return 1
    ui.ok(f"{runtime} {det.version}  {det.binary_path}")
    s.litellm.local.runtime = runtime  # docker | podman
    compose_cli = litellm.container_cli(runtime)
    if not detection.detect_compose(compose_cli):
        ui.error(f"{compose_cli} compose not found. Install the compose plugin for {runtime}.")
        ui.detail("Docker:  Docker Desktop, or the `docker-compose-plugin` package")
        ui.detail("Podman:  the `podman-compose` package")
        return 1

    _ask_bind_and_port(s)

    image = ui.text(
        "Container image",
        default=s.litellm.local.image or litellm.DEFAULT_DOCKER_IMAGE,
    ) or litellm.DEFAULT_DOCKER_IMAGE
    s.litellm.local.image = image

    if _DRY_RUN:
        ui.info(f"DRY RUN: would offer to pull {image}; nothing is pulled and nothing is saved.")
        return 0

    # Save state NOW so pull_image() can read s.litellm.local.runtime
    state.save(s)
    ui.record_action("saved gateway state")

    if ui.confirm(f"Pull {image} now?  (~400MB, takes 30-90s)", default=False):
        with ui.spinner(f"Pulling {image}"):
            ok, msg = litellm.pull_image(image)
        if not ok:
            ui.error(f"Pull failed: {ui.safe_excerpt(msg, 300)}")
            ui.log_tail(str(msg))
            return 1
        # Track only if image wasn't already cached locally before this run.
        if "Already exists" not in msg and "Image is up to date" not in msg:
            s.tracking.docker_image_pulled_by_us = True
            s.tracking.docker_image = image
        ui.ok("Image pulled")
        ui.record_action("pulled the LiteLLM image")
    return 0


def _step3_local_nodocker(s: state.SetupState) -> int:
    """Local install without Docker — choose pip-venv or pipx."""
    ui.console().print()

    runtime_choices = [
        ui.Choice("pip + dedicated venv at ~/.config/ai-resources/venv  (universal — needs python 3.10-3.13)",
                  value="pip-venv"),
        ui.Choice("pipx-managed  (cleanest if you already use pipx)",
                  value="pipx"),
    ]
    default = s.litellm.local.runtime if s.litellm.local.runtime in ("pip-venv", "pipx") else "pip-venv"
    runtime = ui.select("Local mode (no Docker):", runtime_choices, default=default)
    if runtime is None:
        return 130
    s.litellm.local.runtime = runtime

    _ask_bind_and_port(s)

    if runtime == "pip-venv":
        return _step3_pipvenv(s)
    return _step3_pipx(s)


def _step3_pipx(s: state.SetupState) -> int:
    """Install LiteLLM via pipx + verify."""
    pipx_det = detection.detect_pipx()
    if not pipx_det.installed:
        ui.warn("pipx not detected on this system.")
        if _DRY_RUN:
            ui.info("DRY RUN: would offer to install pipx; nothing is installed.")
            return 0
        if not _try_install_pipx():
            return 1
        pipx_det = detection.detect_pipx()
        if not pipx_det.installed:
            ui.error("pipx still not on PATH after install attempt.")
            ui.detail("Run `pipx ensurepath` and start a new shell, then re-run setup.")
            return 1
    ui.ok(f"pipx {pipx_det.version}  {pipx_det.binary_path}")

    # LiteLLM's transitive deps (orjson via pyo3, etc.) max out at Python 3.13.
    # If the system default python is 3.14+, force a compatible interpreter.
    py = detection.find_compatible_python()
    if not py.installed:
        ui.error("No compatible Python found (need 3.10–3.13 for LiteLLM deps).")
        ui.detail("Install one:  brew install python@3.12  (macOS)")
        ui.detail("              sudo apt install python3.12  (Debian/Ubuntu)")
        return 1
    ui.ok(f"Using Python {py.version} for the install  ({py.binary_path})")
    s.litellm.local.python_path = py.binary_path

    # Check if litellm is already installed
    existing = detection.detect_litellm_binary()
    if existing.installed:
        ui.ok(f"litellm already installed: {existing.binary_path} v{existing.version}")
        s.litellm.local.binary_path = existing.binary_path
        # Pre-existing — do NOT track as our install (don't uninstall on teardown)
        if _DRY_RUN:
            ui.info("DRY RUN: would offer to reinstall LiteLLM via pipx; nothing is installed.")
        elif ui.confirm("Reinstall to ensure latest?", default=False):
            with ui.spinner("Reinstalling LiteLLM via pipx"):
                ok, msg = litellm.install_litellm_pipx()
            if not ok:
                ui.error(f"Install failed: {ui.safe_excerpt(msg, 200)}")
                ui.log_tail(str(msg))
                return 1
            ui.ok("LiteLLM updated")
            ui.record_action("reinstalled LiteLLM")
    else:
        # Installs code from PyPI: Enter skips, and an unattended run never does it (kit 2.2b, 3b).
        if _DRY_RUN:
            ui.info("DRY RUN: would offer `pipx install` of LiteLLM; nothing is installed.")
            return 0
        if not ui.confirm(f"Install LiteLLM via `pipx install '{litellm.DEFAULT_LITELLM_PIP_SPEC}' --python {py.binary_path}`?",
                          default=False):
            ui.detail("LiteLLM was not installed. Run `ai-resources setup` from a terminal and answer yes, or "
                      f"install it yourself: pipx install '{litellm.DEFAULT_LITELLM_PIP_SPEC}'")
            return 1
        with ui.spinner("Installing LiteLLM (this may take 1-3 minutes)"):
            ok, msg = litellm.install_litellm_pipx(python_path=py.binary_path)
        if not ok:
            ui.error("LiteLLM install failed:")
            ui.log_tail(str(msg))
            ui.console().print()
            ui.detail("To debug manually, run: pipx install 'litellm[proxy]' --verbose")
            return 1
        new_det = detection.detect_litellm_binary()
        if not new_det.installed:
            ui.error("Installation completed but litellm not in PATH. Run `pipx ensurepath` and re-run.")
            return 1
        s.litellm.local.binary_path = new_det.binary_path
        # Track that WE installed this — teardown is allowed to uninstall it.
        s.tracking.litellm_installed_by_us = True
        s.tracking.litellm_install_method = "pipx"
        ui.ok(f"LiteLLM installed at {new_det.binary_path}")
        ui.record_action("installed LiteLLM")
    return 0


def _step3_pipvenv(s: state.SetupState) -> int:
    """Install LiteLLM into a dedicated venv at ~/.config/ai-resources/venv/."""
    # Need Python 3.10-3.13 for litellm (pyo3 maxes at 3.13)
    py = detection.find_compatible_python()
    if not py.installed:
        ui.error("No compatible Python found (need 3.10–3.13 for LiteLLM deps).")
        ui.detail("Install one:  brew install python@3.12  (macOS)")
        ui.detail("              sudo apt install python3.12  (Debian/Ubuntu)")
        return 1
    ui.ok(f"Using Python {py.version}  ({py.binary_path})")
    s.litellm.local.python_path = py.binary_path

    # If venv already exists with litellm, skip
    existing_bin = litellm.venv_dir() / "bin" / "litellm"
    if existing_bin.is_file():
        ui.ok(f"LiteLLM already installed at {existing_bin}")
        s.litellm.local.binary_path = str(existing_bin)
        s.litellm.local.venv_path = str(litellm.venv_dir())
        # Pre-existing venv — already tracked by an earlier setup run; if tracking
        # is missing (e.g. user hand-created it), leave the flag alone so teardown
        # won't blow away a venv we didn't make.
        if _DRY_RUN:
            ui.info("DRY RUN: would offer to reinstall LiteLLM via pip; nothing is installed.")
        elif ui.confirm("Reinstall to ensure latest?", default=False):
            with ui.spinner("Reinstalling LiteLLM via pip"):
                ok, msg = litellm.install_litellm_venv(python_path=py.binary_path)
            if not ok:
                ui.error("LiteLLM reinstall failed:")
                ui.log_tail(str(msg))
                return 1
            ui.ok("LiteLLM updated")
            ui.record_action("reinstalled LiteLLM")
        return 0

    if _DRY_RUN:
        ui.info("DRY RUN: would offer to create the LiteLLM venv and pip install into it; nothing is installed.")
        return 0
    if not ui.confirm("Create venv at ~/.config/ai-resources/venv and pip install LiteLLM?",
                      default=False):
        ui.detail("LiteLLM was not installed. Run `ai-resources setup` from a terminal and answer yes.")
        return 1
    with ui.spinner("Creating venv + installing LiteLLM (this may take 1-3 minutes)"):
        ok, msg = litellm.install_litellm_venv(python_path=py.binary_path)
    if not ok:
        ui.error("LiteLLM install failed:")
        ui.log_tail(str(msg))
        ui.console().print()
        ui.detail(f"To debug manually:")
        ui.detail(f"  source {litellm.venv_dir()}/bin/activate")
        ui.detail(f"  pip install 'litellm[proxy]' --verbose")
        return 1
    s.litellm.local.binary_path = msg
    s.litellm.local.venv_path = str(litellm.venv_dir())
    # Track that WE created this venv — teardown may delete it.
    s.tracking.litellm_installed_by_us = True
    s.tracking.litellm_install_method = "pip-venv"
    ui.ok(f"LiteLLM installed at {msg}")
    ui.record_action("installed LiteLLM")
    return 0


def _step3_remote(s: state.SetupState, dry_run: bool = False) -> int:
    ui.console().print()
    url = (ui.text(
        "Remote LiteLLM URL (e.g. https://litellm.example.com)",
        default=s.litellm.remote.url,
        validate=_validate_url,
    ) or "").strip()
    if not url.startswith("http"):
        ui.error("URL must start with http:// or https://")
        return 1
    problem = _validate_url(url)
    if problem is not True:
        ui.error(problem)
        return 1
    s.litellm.remote.url = url.rstrip("/")

    master_key = ui.password("Master key (will be stored in .env)",
                             validate=_required("A master key"))
    if not master_key:
        ui.error("Master key required for remote endpoint.")
        return 1
    if dry_run:
        ui.detail(f"(dry run) would store {s.litellm.remote.master_key_env} "
                  f"in {state.env_path()}")
    else:
        _, newly_added = credentials.update_env_tracked(
            {s.litellm.remote.master_key_env: master_key})
        ui.record_action("saved the remote master key")
        for k in newly_added:
            if k not in s.tracking.env_keys_added:
                s.tracking.env_keys_added.append(k)

    with ui.spinner(f"Validating {url}"):
        ok, msg = litellm.validate_remote(url, master_key)
    if not ok:
        ui.error(f"Remote unreachable: {ui.safe_text(msg)}")
        if not ui.confirm("Continue anyway?", default=False):
            return 1
    else:
        ui.ok("Remote endpoint reachable")
    return 0


# --- Step 4 — Providers ----------------------------------------------------------
def _step4_providers(s: state.SetupState) -> int:
    _section(4, "Providers")

    if s.backend == "openrouter":
        # One upstream key fans out to the whole catalogue, so there is nothing
        # to choose here: picking providers is a LiteLLM-only concern.
        ps = s.providers.get("openrouter") or state.ProviderState()
        ps.enabled = True
        ps.auth_method = "api_key"
        ps.env_var = providers.get("openrouter").primary_env_var
        s.providers = {"openrouter": ps}
        ui.info("OpenRouter reaches every provider with one key — no per-provider selection needed.")
        return 0

    enabled_now = {p for p, ps in s.providers.items() if ps.enabled}
    choices = [
        ui.Choice(f"{providers.get(pid).name}  — {providers.get(pid).description}",
                  value=pid)
        for pid in providers.all_ids()
    ]
    selected = ui.checkbox(
        "Select providers to enable:",
        choices,
        default=list(enabled_now) or ["anthropic", "google"],
    )
    if not selected:
        ui.error("Select at least one provider.")
        return 1

    # Update state
    new_providers: dict[str, state.ProviderState] = {}
    for pid in selected:
        prov = providers.get(pid)
        existing = s.providers.get(pid) or state.ProviderState()
        existing.enabled = True
        existing.env_var = prov.primary_env_var
        # Determine auth method
        if pid == "vertex":
            existing.auth_method = "vertex_adc"
        else:
            existing.auth_method = prov.auth_methods[0]
        new_providers[pid] = existing
    # Disable any not selected
    for pid in s.providers:
        if pid not in selected:
            s.providers[pid].enabled = False
            new_providers[pid] = s.providers[pid]
    s.providers = new_providers

    return 0


# --- Step 5 — Credentials --------------------------------------------------------
def _step5_credentials(s: state.SetupState, dry_run: bool = False) -> int:
    _section(5, "API keys")
    ui.info(f"Stored in {state.env_path()} (chmod 600)")
    ui.info("Press Enter to keep an existing value")

    updates: dict[str, str] = {}

    if s.backend == "openrouter":
        env_var = "OPENROUTER_API_KEY"
        existing = credentials.get_key(env_var)
        if existing:
            ui.detail(f"{env_var}: {credentials.mask(existing)}  (saved)")
            if ui.confirm(f"Update {env_var}?", default=False):
                new_val = ui.password(env_var)
                if new_val:
                    updates[env_var] = new_val
        else:
            new_val = ui.password(env_var, validate=_required(f"{env_var} (OpenRouter backend)"))
            if not new_val:
                ui.error(f"{env_var} is required for the OpenRouter backend.")
                return 1
            updates[env_var] = new_val
        ui.detail("Set a spend limit on this key at https://openrouter.ai/settings/keys — "
                  "it is the only budget ceiling that applies in this mode.")
        if updates and dry_run:
            ui.detail(f"(dry run) would write {', '.join(sorted(updates))} "
                      f"to {state.env_path()}")
        elif updates:
            _, newly_added = credentials.update_env_tracked(updates)
            for k in newly_added:
                if k not in s.tracking.env_keys_added:
                    s.tracking.env_keys_added.append(k)
            ui.ok(f"Credentials written to {state.env_path()}")
            ui.record_action("saved credentials")
        return 0

    # Master key for LiteLLM.
    # Claude Code v2+ reads its API key from the macOS Keychain ("Claude Code"
    # service) and sends it verbatim — it ignores ANTHROPIC_API_KEY in settings.json.
    # The gateway master key must equal that Keychain key for auth to succeed.
    keychain_key = credentials.get_claude_code_keychain_key()
    current_master = credentials.get_key("LITELLM_MASTER_KEY")
    if keychain_key:
        if current_master != keychain_key:
            updates["LITELLM_MASTER_KEY"] = keychain_key
            ui.detail(f"LITELLM_MASTER_KEY synced from Claude Code Keychain ({credentials.mask(keychain_key)})")
        else:
            ui.detail(f"LITELLM_MASTER_KEY already matches Keychain ({credentials.mask(current_master)})")
    elif not current_master:
        master = credentials.generate_master_key()
        updates["LITELLM_MASTER_KEY"] = master
        ui.detail(f"Generated LITELLM_MASTER_KEY ({credentials.mask(master)})")

    # Per-provider keys
    for pid, ps in s.providers.items():
        if not ps.enabled:
            continue
        prov = providers.get(pid)

        if pid == "vertex":
            # Vertex needs project + location, not an API key
            existing_proj = credentials.get_key("GOOGLE_CLOUD_PROJECT")
            project = ui.text("GOOGLE_CLOUD_PROJECT (GCP project ID)", default=existing_proj)
            if project:
                updates["GOOGLE_CLOUD_PROJECT"] = project
            # "global" rather than a specific region: regional endpoints serve
            # Claude Sonnet 4.6 and earlier only, so a regional default silently
            # rules out every current model. Multi-region ("us"/"eu") and
            # regional endpoints also carry a 10% premium over global.
            existing_loc = credentials.get_key("GOOGLE_CLOUD_LOCATION") or "global"
            location = ui.text("GOOGLE_CLOUD_LOCATION (global, us, eu, or a region)",
                               default=existing_loc)
            if location:
                updates["GOOGLE_CLOUD_LOCATION"] = location
            ui.info("Run `gcloud auth application-default login` if you haven't already.")
            continue

        if pid == "ollama":
            ui.detail("(Ollama: no credentials needed; ensure localhost:11434 is reachable)")
            continue

        env_var = prov.primary_env_var
        existing = credentials.get_key(env_var)
        if existing:
            ui.detail(f"{env_var}: {credentials.mask(existing)}  (saved)")
            if ui.confirm(f"Update {env_var}?", default=False):
                new_val = ui.password(f"{env_var}")
                if new_val:
                    updates[env_var] = new_val
        else:
            new_val = ui.password(f"{env_var}")
            if not new_val:
                ui.warn(f"{env_var} skipped — provider may fail validation")
            else:
                updates[env_var] = new_val

    if updates and dry_run:
        ui.detail(f"(dry run) would write {', '.join(sorted(updates))} "
                  f"to {state.env_path()}")
    elif updates:
        _, newly_added = credentials.update_env_tracked(updates)
        for k in newly_added:
            if k not in s.tracking.env_keys_added:
                s.tracking.env_keys_added.append(k)
        ui.ok(f"Credentials written to {state.env_path()}")
        ui.record_action("saved credentials")

    return 0


# --- Composed teams (ADR-0006) -----------------------------------------------------------------
def _preflight_compose() -> int:
    """Validate the composition flags before anything is written (exit 2 on a contradiction)."""
    c = _COMPOSE
    if not (c.get("mode") or c.get("headroom") or c.get("live") or c.get("why_json") or c.get("allow_fable")):
        return 0
    from ..scoring import db as scores_db, flow
    from ..scoring.compose import MODES
    if c.get("mode") and c["mode"] not in MODES:
        ui.error(f"--compose must be one of {', '.join(MODES)}, not {c['mode']!r}")
        return 2
    try:
        known = [p["id"] for p in scores_db.load_shipped().get("pools") or ()]
        flow.parse_headroom(c.get("headroom", ""), known)
        flow.parse_live(c.get("live", ""), known)
        if c.get("why_json"):
            flow.validate_why_json_path(c["why_json"])
    except (ValueError, OSError) as e:
        ui.error(str(e))
        return 2
    if c.get("mode") == "mixed" and not c.get("allow_unverified"):
        ui.error(flow.MIXED_REFUSAL)
        return 2
    return 0


def _compose_profile(s: state.SetupState, base_name: str, mode: str | None, single_model: bool = False) -> int | None:
    """Propose a team from the scores database and, on acceptance, store it as customizations.

    Returns 0 when a team was stored, a non-zero code on a refusal of a flagged run, and None when the operator
    declined (the plain profile path then continues). Detection and the engine are read-only; the only host
    reads are the opt-in headroom readers."""
    import time
    from ..scoring import db as scores_db, detect as scoring_detect, explain, flow, profile_out
    flagged = bool(_COMPOSE.get("mode"))
    try:
        db = scores_db.load()
    except Exception as e:  # noqa: BLE001 - a broken scores file must not take setup down
        ui.error(f"The scores database could not be read ({type(e).__name__}): {e}")
        return 1 if flagged else None
    for problem in db.problems:
        ui.warn(f"scores overrides: {problem}")
    deps = scoring_detect.default_deps()
    inv = scoring_detect.inventory(deps, db)
    enabled = flow.enabled_providers(s, inv)
    allow_unverified = bool(_COMPOSE.get("allow_unverified"))

    if mode is None:
        offered = [("uniform", "uniform — one model for every role"),
                   ("single-provider", "single-provider — a model per role, all from one provider")]
        if len(enabled) >= 2 and allow_unverified:
            offered.append(("mixed", "mixed — balance pools across providers (unverified cells, marked)"))
        elif len(enabled) >= 2:
            ui.detail("Mixed mode is offered only with --allow-unverified: no cross-provider cell is verified today.")
        mode = ui.select("Composition mode:", [ui.Choice(label, value=m) for m, label in offered])
        if mode is None:
            return None
    req = flow.Request(mode=mode, allow_unverified=allow_unverified, allow_fable=bool(_COMPOSE.get("allow_fable")))
    refusal = flow.check_mode(req, enabled)
    if refusal:
        ui.error(refusal)
        return 2 if flagged else None

    levels = flow.parse_headroom(_COMPOSE.get("headroom", ""))
    live = set(flow.parse_live(_COMPOSE.get("live", "")))
    pools = flow.askable_pools(inv, enabled, db)
    if mode == "mixed" and not flagged:
        for pool in pools:
            level = ui.select(f"How much headroom does {pool['id']} ({pool['billing_class']}) have?",
                              [ui.Choice("ample (1.0)", value="ample"), ui.Choice("normal (0.7)", value="normal"),
                               ui.Choice("scarce (0.3)", value="scarce")], default=levels.get(pool["id"], "ample"))
            if level is None:
                return None
            levels[pool["id"]] = level
            if flow.has_live_reader(pool) and ui.confirm(
                    f"Read live headroom for {pool['id']} now? (read-only, opt-in; unknown falls back to the level)",
                    default=False):
                live.add(pool["id"])
    if not flagged and "anthropic" in enabled and ui.confirm(
            "Allow Claude Fable 5.1 as a candidate? (needs a Max or Premium seat, or API billing; it bills "
            "credits on Pro and on claude -p)", default=False):
        req.allow_fable = True
    req.headroom, req.live = levels, tuple(sorted(live))

    comp, inp = flow.run(s, req, deps=deps, db=db, now_epoch=int(time.time()), base_name=base_name)
    for pid in req.live:
        reading = inp.readings.get(pid)
        if reading is None or reading.state != "known":
            ui.info(f"Live headroom for {pid} is unknown ({reading.source if reading else 'no reading'}); "
                    "the static level is used.")
    from . import lanes_render
    lane_table = lanes_render.explain_lanes(s.profile.lanes)
    ui.console().print(explain.render_text(comp, lane_table), markup=False, highlight=False)
    why = _COMPOSE.get("why_json")
    if why:
        if _DRY_RUN:
            ui.info("DRY RUN: --why-json is not written.")
        else:
            flow.write_why_json(flow.validate_why_json_path(why), comp, lane_table)
            ui.ok(f"Why table (JSON): {why}")
    if comp.refused:
        ui.error(f"No team proposed: {comp.refusal}")
        return 1 if flagged else None

    overlay = profile_out.to_customizations(comp)
    if single_model:
        overlay = flow.to_aliases(overlay)
    try:
        base = profiles.load_profile(base_name, "single-model" if single_model else "multi-model")
        before = profiles.to_executors(base, s.backend)["by_role"]
        after = profiles.to_executors(profiles.merge_customizations(base, overlay), s.backend)["by_role"]
        profiles.print_role_diff(before, after, title=f"{base_name} -> composed ({mode})")
    except (FileNotFoundError, RuntimeError):
        pass
    if not flagged and not ui.confirm("Use this team?", default=False):
        return None

    keep = {k: v for k, v in (s.profile.customizations or {}).items() if k not in profiles.KNOWN_ROLES}
    s.profile.name = base_name
    s.profile.customizations = {**keep, **overlay}
    s.profile.customized = bool(overlay)
    s.profile.composed = profile_out.provenance(comp)
    sel = s.get_selection()
    if sel is not None and (levels or live):
        sel.headroom = {pid: {"level": levels.get(pid, "ample"), "live": pid in live}
                        for pid in sorted(set(levels) | live)}
        s.set_selection(sel)
    if comp.kept_default:
        ui.ok(f"{base_name} is within band on every role: no override is written.")
    else:
        ui.ok(f"Composed team stored over {base_name} ({len(overlay)} role(s)).")
    return 0


# --- Step 6 — Profile + per-role customization ------------------------------------
def _note_newer_recommendation(saved: str, recommended: str, backend: str) -> None:
    """Informational only: the saved profile is kept and nothing is written.

    Interactively one question, default No, shows the role-by-role difference; a non-interactive run prints it
    with the exact command that would switch. Neither path changes the profile."""
    command = f"ai-resources setup --profile {recommended}"
    ui.info(f"The recommended profile is now {recommended}; this host keeps {saved} until you choose otherwise.")
    if not ui.is_non_interactive() and not ui.confirm(
            f"Show how {recommended} differs from {saved}?", default=False):
        return
    try:
        before = profiles.to_executors(profiles.load_profile(saved, "multi-model"), backend)["by_role"]
        after = profiles.to_executors(profiles.load_profile(recommended, "multi-model"), backend)["by_role"]
    except (FileNotFoundError, RuntimeError):
        return
    profiles.print_role_diff(before, after, title=f"{saved} -> {recommended}")
    ui.info(f"To switch: {command}")


# Profiles that still let a checker share a model family with a writer (the shrink-only KNOWN_VIOLATIONS of
# tests/test_profile_checker_independence.py; ADR-0010 Q-1, R4). They stay in the Custom list, with a warning.
OPEN_CHECKER_PROFILES: dict[str, str] = {
    "all-gemini": "every role runs on Gemini, so the security-auditor and the implementer use the identical model",
    "claude-inherit": "every role inherits the session model, so a checker shares its family with the writers",
}


def _warn_open_checker_profile(name: str) -> None:
    why = OPEN_CHECKER_PROFILES.get(profile_names.canonical(name))
    if why:
        ui.warn(f"{name}: {why}. The checkers are not independent of the writers with this profile.")


def _step6_profile(s: state.SetupState) -> int:
    _section(6, "Your team - roles")

    available = profiles.list_profiles(mode="multi-model", backend=s.backend)
    if not available:
        ui.error(f"No profiles found at {profiles.profiles_dir()}")
        return 1
    rc = _reject_unavailable_requested_profile(available, f"the {s.backend} backend")
    if rc is not None:
        return rc

    # Build choices with descriptions
    choices = []
    for name in available:
        try:
            p = profiles.load_profile(name, "multi-model")
            desc = p.get("description", "").split("\n")[0][:80]
        except (FileNotFoundError, RuntimeError):
            desc = ""
        label = f"{name}"
        if name == profiles.recommended_profile(s.backend):
            label += "  (recommended)"
        if desc:
            label += f"  — {desc}"
        choices.append(ui.Choice(label, value=name))
    choices.append(ui.Choice("custom — Configure each role individually", value="__custom__"))
    choices.append(ui.Choice("compose — Propose a team from the scores database (dated evidence, explained)",
                             value="__compose__"))

    # The default has to exist in `available`, and `available` is now filtered by
    # backend: a LiteLLM profile is not offered under openrouter, nor the reverse.
    # A hardcoded name here crashed the wizard for anyone whose saved profile
    # belonged to the other backend.
    # Gateway-agnostic, so the same recommendation holds for either backend.
    preferred = profiles.recommended_profile(s.backend)
    saved = profile_names.canonical(s.profile.name)
    if saved in available:
        default = saved
    elif preferred in available:
        default = preferred
    else:
        default = available[0]

    ui.detail("No gateway? A gateway profile needs LiteLLM or OpenRouter; without one, go back and choose "
              "'As a team' or 'Each on its own'.")
    if (saved in available and saved != preferred and preferred in available
            and not _REQUESTED_PROFILE):
        _note_newer_recommendation(saved, preferred, s.backend)

    if _COMPOSE.get("mode"):
        # `--compose`: the explicit flag path; the base profile is the one a plain run would have used.
        return _compose_profile(s, _REQUESTED_PROFILE if _REQUESTED_PROFILE in available else default,
                                _COMPOSE["mode"]) or 0

    chosen = ui.select("Choose profile:", choices, default=default)
    if chosen is None:
        return 130
    _warn_open_checker_profile(chosen)

    if chosen == "__compose__":
        rc = _compose_profile(s, default, None)
        if rc is not None:
            return rc
        chosen = default                       # declined or refused: the plain profile path continues

    if chosen == "__custom__":
        # Start from the backend's own recommended profile. Hardcoding
        # quality-first here loaded bare model IDs under the openrouter backend,
        # whose namespaced catalogue rejects them — the same defect as the
        # default above, except this one corrupts the config instead of crashing.
        chosen = default if default in available else available[0]
    base = profiles.load_profile(chosen, "multi-model")

    s.profile.name = chosen
    s.profile.composed = None          # a plain profile replaces any earlier composed team (ADR-0006)

    # Show proposed mapping
    executors = profiles.to_executors(base, s.backend)
    rows = profiles.role_table(executors)
    ui.console().print()
    ui.role_table(rows, title=f"Profile: {chosen}")

    if not ui.confirm("Customize any role?", default=False):
        s.profile.customized = False
        s.profile.customizations = _carry_targets(s.profile, state.ProfileState()).customizations
        # Stash full executors in state.profile.customizations as overrides
        return 0

    s.profile.customized = True
    customizations: dict[str, dict] = dict(s.profile.customizations or {})

    # Per-role customization loop
    enabled_providers = [p for p, ps in s.providers.items() if ps.enabled]
    while True:
        role = ui.select(
            "Pick a role to customize (or done):",
            [ui.Choice(r, value=r) for r in profiles.KNOWN_ROLES] + [ui.Choice("(done)", value="__done__")],
        )
        if role is None or role == "__done__":
            break
        # The vendor is still asked, because it narrows the model list — but it
        # is no longer stored. What gets written is the canonical
        # `<vendor>/<model>` ID, which is the only thing that decides routing.
        current = executors["by_role"].get(role, {}).get("model", "")
        vendor = ui.select(
            f"Vendor for {role}:",
            [ui.Choice(p, value=p) for p in enabled_providers],
            default=current.split("/", 1)[0] if "/" in current else None,
        )
        if vendor is None:
            continue
        models = providers.models_for(vendor)
        model = ui.select(
            f"Model for {role}:",
            [ui.Choice(providers.model_label(vendor, m), value=m) for m in models]
            + [ui.Choice("(custom — type below)", value="__custom__")],
        )
        if model == "__custom__":
            model = (ui.text(f"Model id for {role} (leave empty to skip this role)",
                             validate=_validate_model_id) or "").strip()
        if not model:
            continue
        canonical = model if "/" in model else f"{vendor}/{model}"
        customizations[role] = {"model": canonical}

    s.profile.customizations = customizations
    # Show updated table
    merged = profiles.merge_customizations(base, {k: v for k, v in customizations.items()
                                                  if k in profiles.KNOWN_ROLES})
    executors = profiles.to_executors(merged, s.backend)
    ui.role_table(profiles.role_table(executors), title="Final mapping")
    return 0


def _step6_single_model_profile(s: state.SetupState) -> int:
    """Single-model: choose how kit subagents pick a Claude model (no gateway involved)."""
    _section(6, "Your team - roles")
    available = profiles.list_profiles(mode="single-model")
    if not available:
        ui.info("No single-model profiles found — kit subagents inherit the session model.")
        return 0
    rc = _reject_unavailable_requested_profile(available, "single-model mode")
    if rc is not None:
        return rc

    choices = []
    for name in available:
        desc = profiles.load_profile(name, "single-model").get("description", "").split("\n")[0][:80]
        choices.append(ui.Choice(f"{name}  — {desc}" if desc else name, value=name))
    saved = profile_names.canonical(s.profile.name)
    if saved in available:
        default = saved
    else:
        default = _single_default_profile(s) if _single_default_profile(s) in available else available[0]
    chosen = ui.select("How should kit subagents pick their Claude model?", choices, default=default)
    if chosen is None:
        return 130
    _warn_open_checker_profile(chosen)
    if chosen != saved:
        # A different profile starts clean; the acknowledgements belong to the accounts, not to a profile, and the
        # tools ticked on the first screen travel along. Keeping the same profile keeps the saved team with it
        # (customizations, composed team, lanes), which a re-run used to drop (ADR-0009).
        s.profile = _carry_targets(s.profile, state.ProfileState(name=chosen, customized=False,
                                                                 lane_acks=s.profile.lane_acks))
    if _COMPOSE.get("mode"):
        if _COMPOSE["mode"] != "uniform":
            ui.error("single-model mode offers --compose uniform only (one Claude model for every role); "
                     "the other modes need a gateway and several providers.")
            return 2
        rc = _compose_profile(s, chosen, "uniform", single_model=True)
        if rc:
            return rc
    ui.role_table(profiles.role_table(_single_model_executors(s)), title=f"Profile: {chosen}")
    return 0


def _single_model_executors(s: state.SetupState) -> dict:
    """Executors for single-model mode: the chosen single-model profile, else the default one."""
    available = profiles.list_profiles(mode="single-model")
    base_name = profile_names.canonical(s.profile.name) if profile_names.canonical(s.profile.name) in available else (
        _single_default_profile(s) if _single_default_profile(s) in available else "")
    if not base_name:
        return {"by_role": {}}
    base = profiles.load_profile(base_name, "single-model")
    if s.profile.customized and s.profile.customizations:        # a composed uniform team (ADR-0006)
        base = profiles.merge_customizations(
            base, {k: v for k, v in s.profile.customizations.items() if k in profiles.KNOWN_ROLES})
    return profiles.to_executors(base)


# --- Step 7 — per-tool configuration questions -----------------------------------------
_PHASE2_ATTR = "_openclaw_phase2"
"""Instance attribute on the setup state (never serialised): this run's cockpit step already asked the engine."""
COCKPIT_TOOLS = ("claude", "openclaw")
COCKPIT_LABELS = {"claude": "Claude Code", "openclaw": "OpenClaw"}


def _selected_tools(s: state.SetupState) -> list[str]:
    return (s.profile.customizations.get("__targets__") or {}).get("selected") or []


def _capable_cockpits(s: state.SetupState) -> list[str]:
    """The ticked, installed tools that can host the team (ADR-0010 PD-4)."""
    selected = _selected_tools(s)
    return [c for c in COCKPIT_TOOLS if c in selected and (s.cockpits.get(c) or state.CockpitState()).installed]


def _cockpit_line(capable: list[str]) -> str:
    names = [COCKPIT_LABELS[c] for c in capable]
    if len(names) == 1:
        return f"Main tool: {names[0]} (the only one among your tools)."
    return f"Main tools: {' and '.join(names)} (each is set up)."


def _step_cockpit_choice(s: state.SetupState, dry_run: bool = False) -> int:
    """Phase 2: name the main tool among the ticked, installed tools (nothing to choose between when there is one;
    with several each is set up, so no question would change anything), then ask the OpenClaw engine and the channel
    accounts before the team is picked. Writes nothing. A run with nothing to ask is a skip that prints the line."""
    capable = _capable_cockpits(s)
    _section(1, "Main tool", phase=2)
    ui.info(_cockpit_line(capable))
    if "openclaw" in capable and not s.openclaw.engine:
        from .cockpits import openclaw as _openclaw_cockpit
        from .cockpits import _openclaw_channels
        setattr(s, _PHASE2_ATTR, True)
        _openclaw_cockpit.prompt_cockpit(s, dry_run=dry_run)
        _openclaw_channels.prompt(s)
    if "openclaw" in capable and not s.openclaw.image_mode:
        from .cockpits import openclaw as _openclaw_cockpit
        _openclaw_cockpit.prompt_image(s, dry_run=dry_run)          # phase 2.1: asks once, writes nothing (ADR-0011)
    return 0


_WHOLE_ATTR = "_openclaw_whole"
"""Instance attribute on the setup state (never serialised): the whole OpenClaw prompt ran in one step this run."""


def _step7_cockpit_config(s: state.SetupState, dry_run: bool = False) -> int:
    """The tools were ticked on screen 1; this step only asks the questions a tool has of its own (OpenClaw).
    When the cockpit step already asked the engine, only the questions that need the team come here (the model);
    voice notes follow on the Audio step and the host section after it."""
    if "openclaw" in _selected_tools(s):
        _section(7, "OpenClaw")
        from .cockpits import openclaw as _openclaw_cockpit
        if _OPEN_OPENCLAW or not s.__dict__.get(_PHASE2_ATTR):
            setattr(s, _WHOLE_ATTR, True)               # engine, model, voice and host in one go (as before)
            _openclaw_cockpit.prompt(s, dry_run=dry_run)
            if _OPEN_OPENCLAW:
                from .cockpits import _openclaw_channels
                _openclaw_channels.prompt(s)
                _openclaw_cockpit.prompt_image(s, dry_run=dry_run)      # More options asks the saved answer again
        else:
            _openclaw_cockpit.prompt_model(s, dry_run=dry_run)
    return 0


def _step_more(s: state.SetupState) -> int:
    """Phase 5: the OpenClaw host section and the stale-plugin check (asked once; kept afterwards)."""
    _section(8, "More")
    from .cockpits import openclaw as _openclaw_cockpit
    _openclaw_cockpit.prompt_more(s)
    return 0


def _step_audio(s: state.SetupState) -> int:
    """Phase 4: how OpenClaw hears voice notes. Asked here and nowhere else on the phased path."""
    _section(7, "Audio", phase=4)
    from .cockpits import openclaw as _openclaw_cockpit
    _openclaw_cockpit.prompt_audio(s)
    return 0


# --- Step 8 — Lifecycle ---------------------------------------------------------
def _step8_lifecycle(s: state.SetupState) -> int:
    _section(8, "Start at login")

    if s.litellm.deployment != "local":
        ui.info("Skipped (gateway is not local).")
        return 0

    # Docker / Podman containers self-restart via compose's `restart: always`
    # whenever the runtime comes back up — no LaunchAgent / systemd unit needed.
    if litellm.is_container_mode(s.litellm.local.runtime):
        s.litellm.local.auto_start = True  # implicit via container restart policy
        s.litellm.local.log_dir = str(litellm.log_dir())
        ui.info(f"Skipped — {s.litellm.local.runtime} container uses "
                f"`restart: always` (no LaunchAgent needed).")
        ui.detail("Container starts automatically when the Docker daemon is running.")
        return 0

    if s.litellm.local.lifecycle_path and not _OPEN_LIFECYCLE:
        # A complete saved answer (the unit path was recorded when it was given) is kept without a question;
        # More options > Start at login asks it again (ADR-0010 PD-5).
        ui.detail("Start at login: keeping your saved answer ("
                  + ("auto-start on" if s.litellm.local.auto_start else "manual start")
                  + "); change it from More options.")
        return 0

    s.litellm.local.auto_start = ui.confirm(
        "Auto-start LiteLLM at every login?",
        default=s.litellm.local.auto_start,
    )

    if s.litellm.local.auto_start:
        path = litellm.lifecycle_path()
        s.litellm.local.lifecycle_path = str(path)
        s.litellm.local.log_dir = str(litellm.log_dir())
        if litellm.is_macos():
            ui.detail(f"Will install macOS LaunchAgent: {path}")
        else:
            ui.detail(f"Will install systemd-user service: {path}")
    return 0


# --- Subscription lanes: the lane step and lanes.yaml (ADR-0007) --------------------
def _current_by_role(s: state.SetupState) -> dict:
    from . import lanes_render
    return lanes_render.current_by_role(s)


def _agy_roles_of(doc: dict) -> list[str]:
    """Roles the profile gives an agy candidate (roles and second members), sorted."""
    block = doc.get("lanes") or {}
    out = set()
    for key in ("roles", "second"):
        for role, cands in (block.get(key) or {}).items():
            if any(isinstance(c, str) and c.startswith("agy:") for c in cands or ()):
                out.add(role)
    return sorted(out)


def _drop_agy(lanes: dict) -> dict:
    """The lanes table without the Antigravity gate and its agents (the claude-cli lanes keep running)."""
    agy = {a for a, ag in (lanes.get("agents") or {}).items() if (ag or {}).get("backend") != "claude-cli"}
    refs = {f"openclaw-agent:{a}" for a in agy}
    out = {k: v for k, v in lanes.items() if k not in ("gates", "held")}
    out["agents"] = {a: ag for a, ag in (lanes.get("agents") or {}).items() if a not in agy}
    out["roles"] = {r: {**(c or {}), "candidates": [x for x in (c or {}).get("candidates") or [] if x not in refs]}
                    for r, c in (lanes.get("roles") or {}).items()}
    second = {r: [x for x in (c or []) if x not in refs] for r, c in (lanes.get("second") or {}).items()}
    out["second"] = {r: c for r, c in second.items() if c}
    return out


def agy_terms_check(s: state.SetupState, roles: list[str], interactive: bool = True, *,
                   flag: str = "--lanes-antigravity") -> tuple[bool, dict, int]:
    """The Antigravity (Google) terms check, ADR-0007 section 11: the one place the Google acknowledgement is shown
    and stored. Returns `(accepted, acks, rc)`; `s` is never modified, the caller stores `acks` when it accepts.

    An acknowledgement stored for the current text that lists a role (and covers `roles`, or whose extension the
    operator confirms) is kept without asking again. Otherwise the text is shown once, verbatim, the Google account
    is named and the roles it may run are ticked (the implementer is unticked by default: ticking it is explicit).
    A decline, an empty account or no role picked is `(False, acks, 0)` and stores nothing; Ctrl-B and Ctrl-C raise
    `GoBack` / `Cancelled` before anything is stored. Without a prompt (`interactive` False) a missing, stale or
    empty acknowledgement is `(False, acks, 2)` with the same message the flag has always printed."""
    from . import lanes_render
    acks = dict(s.profile.lane_acks or {})
    have = lanes_render.valid_acks(acks).get("google")
    listed = set((have or {}).get("roles") or ())
    if not interactive:
        if not have or not listed:
            ui.error(f"{flag}: no stored valid Google acknowledgement lists a role (hash of the "
                     "current text, account named); run `ai-resources setup --lanes --lanes-antigravity` "
                     "from a terminal to sign it")
            return False, acks, 2
        return True, acks, 0
    if not (have is None or (set(roles) - listed and ui.confirm(
            "Extend the Google acknowledgement to: " + ", ".join(sorted(set(roles) - listed)) + "?",
            default=False))):
        return True, acks, 0
    ui.warn(lanes_render.ACK_TEXT["google"])
    ui.detail("Roles an agy lane may run: " + ", ".join(roles))
    account = (ui.text("Google account that signs this acknowledgement (one per account)",
                       default=str((have or {}).get("account") or "")) or "").strip()
    if not account:
        ui.error(f"{flag}: the Google account must be named; the Antigravity lane stays closed")
        return False, acks, 0
    choices = [ui.Choice(r, value=r) if hasattr(ui, "Choice") else r for r in roles]
    default = sorted(listed | {r for r in roles if r != roles_policy.DEFAULT_INTEGRATING_WRITER})
    picked = ui.checkbox("Roles acknowledged for this Google account (the implementer is "
                         "unticked by default: tick it explicitly)", choices, default=default)
    if not picked or not ui.confirm("Acknowledge for this Google account and these roles?", default=False):
        ui.info("Not acknowledged; the Antigravity lane stays closed.")
        return False, acks, 0
    acks["google"] = lanes_render.make_ack("google", list(picked), datetime.now(timezone.utc).isoformat(),
                                           account=account)
    return True, acks, 0


def _members_of(s: state.SetupState) -> list[str]:
    """The tools ticked on the first screen (the stash apply reads)."""
    return list((s.profile.customizations or {}).get("__targets__", {}).get("selected") or [])


def _untick(s: state.SetupState, tool: str) -> None:
    sel = (s.profile.customizations or {}).get("__targets__")
    if isinstance(sel, dict) and tool in (sel.get("selected") or []):
        sel["selected"] = [t for t in sel["selected"] if t != tool]


def _without_claude_cli(doc: dict) -> dict:
    """The profile with its `claude-cli:` lane candidates removed (claude-code and agy candidates stay): what
    ticking Antigravity alone proposes, so it does not switch the Claude parallel runs on (ADR-0009)."""
    out = copy.deepcopy(doc)
    block = out.get("lanes") or {}
    for key in ("roles", "second", "pins"):
        for role, cands in list((block.get(key) or {}).items()):
            block[key][role] = [c for c in (cands or ()) if not (isinstance(c, str) and c.startswith("claude-cli:"))]
    return out


def _step_lanes(s: state.SetupState, args: argparse.Namespace, *, dry_run: bool = False) -> int:
    """Enable, keep or clear subscription lanes. Disabled on a fresh install and without a flag.

    `--lanes` proposes the profile's table, asks for the orchestrator ids (OQ-7) and shows the Anthropic
    acknowledgement once, every lane-eligible role preselected (OQ-1). A non-interactive `--lanes` without a
    stored acknowledgement for those roles refuses. `--lanes-antigravity` adds the Antigravity gate: one Google
    acknowledgement per account (text shown once, the account named, the implementer unticked by default), and a
    non-interactive run needs a stored valid one listing at least one role. `--no-lanes-antigravity` clears the
    gate and the agy agents and keeps the claude-cli lanes. `--no-lanes` clears the table (the OpenClaw writer
    and `lanes.yaml` follow on apply). `--allow-unverified` implies nothing here.

    Ticking Antigravity on the first screen is the interactive opt-in (ADR-0009): the same terms check opens here
    (`agy_terms_check`), accept stores the Google acknowledgement and the Antigravity gate exactly as
    `--lanes-antigravity` does, and a decline, an empty account or Ctrl-B unticks the tool and writes nothing.
    Unticking an Antigravity that was on closes the gate like `--no-lanes-antigravity`. Without the interactive list
    (unattended, `--yes`, no terminal) only the flags decide, as before."""
    from . import lanes_render
    from ..scoring import lanes as scoring_lanes
    agy_on = bool(getattr(args, "lanes_antigravity", False))
    agy_off = bool(getattr(args, "no_lanes_antigravity", False))
    want = bool(getattr(args, "lanes", False)) or agy_on
    unwant = bool(getattr(args, "no_lanes", False))
    preview = bool(getattr(args, "lanes_dry_run", False))
    listed = getattr(s, "_tool_members_this_run", None) is not None and not ui.is_non_interactive()
    agy_team = listed and s.cli_team and "agy" in _members_of(s) and not (agy_off or unwant)
    if listed and "agy" not in _members_of(s) and _agy_lanes_on(s) and not (agy_on or unwant):
        agy_off = True                      # unticked on "Your tools": Antigravity is not used any more
    team_only = False
    if agy_team and not (preview or dry_run):
        prev_lanes = s.profile.lanes if isinstance(s.profile.lanes, dict) else {}
        claude_lanes = any((a or {}).get("backend") == "claude-cli" for a in (prev_lanes.get("agents") or {}).values())
        agy_on = True
        team_only = not (want or claude_lanes)
        want = True
    elif agy_team:
        ui.info("DRY RUN: the Antigravity terms check is not shown in a dry run; nothing is enabled.")
    if (s.cli_team and _PARALLEL_RUNS and not (want or unwant or preview or agy_off)
            and not ui.is_non_interactive()):
        # Review > More options > Parallel runs: the operator asked for the Claude parallel runs in this run.
        want = True
    if (want and unwant) or (agy_on and agy_off):
        ui.error("--lanes, --lanes-antigravity, --no-lanes and --no-lanes-antigravity contradict each other")
        return 2
    if team_only:
        _section(6, "Antigravity terms check")
    elif want or unwant or preview or agy_off:
        _section(6, "Your team - parallel runs")
    if unwant and preview:
        ui.info("Dry run: lanes stay as they are; nothing was cleared or written.")
        return 0
    if unwant:
        if s.profile.lanes:
            ui.info("Subscription lanes: cleared (lanes.yaml and the OpenClaw lane agents are removed on apply).")
        s.profile.lanes = None
        return 0
    if agy_off and not want:
        if isinstance(s.profile.lanes, dict) and not preview:
            s.profile.lanes = _drop_agy(s.profile.lanes)
            ui.info("Antigravity lane: gate cleared (agy agents leave lanes.yaml and the OpenClaw config on "
                    "apply; the claude-cli lanes and the Google acknowledgement record stay).")
        return 0
    if not (want or preview):
        return 0
    name = profiles.saved_or_recommended(s.profile.name, s.backend) if s.mode == "multi-model" else (
        s.profile.name or _single_default_profile(s))
    try:
        doc = profiles.load_profile(name, "multi-model" if s.mode == "multi-model" else "single-model")
    except FileNotFoundError:
        ui.error(f"--lanes: profile {name!r} not found")
        return 2
    prev = s.profile.lanes if isinstance(s.profile.lanes, dict) else {}
    orchestrators = list(getattr(args, "lanes_orchestrator", None) or prev.get("orchestrators") or [])

    def ask_orchestrators() -> list[str]:
        if not orchestrators and not ui.is_non_interactive():
            raw = ui.text("Orchestrator agent id(s) that may dispatch lane agents (comma-separated)", default="")
            return [o.strip() for o in (raw or "").split(",") if o.strip()]
        return orchestrators

    if not team_only:
        orchestrators = ask_orchestrators()
    acks = dict(s.profile.lane_acks or {})
    agy_roles = _agy_roles_of(doc)
    # the team screen never offers the writer role to Antigravity (writing stays on Claude Code)
    check_roles = [r for r in agy_roles if r != roles_policy.DEFAULT_INTEGRATING_WRITER] if team_only else agy_roles
    if team_only and not check_roles:
        # ticked, but this team gives Antigravity no role: nothing to ask and nothing to store
        _untick(s, "agy")
        ui.info("Antigravity takes no role in this team, so it stays off; pick another starting team to use it.")
        return 0
    if agy_on and not (preview or dry_run):
        # the Google acknowledgement comes before the proposal: an agy candidate enters state only with it
        agy_on, acks, rc = agy_terms_check(s, check_roles, interactive=not ui.is_non_interactive(),
                                           flag="--lanes-antigravity")
        if rc:
            return rc
        if team_only:
            if not agy_on:
                _untick(s, "agy")
                ui.info("Antigravity stays off; nothing was stored.")
                return 0
            orchestrators = ask_orchestrators()
            doc = _without_claude_cli(doc)
    try:
        from ..scoring import headroom_sources
        proposal = lanes_render.propose(doc, _current_by_role(s), s.mode, orchestrators=orchestrators,
                                        acks=acks, gates={"antigravity": agy_on},
                                        headroom=headroom_sources.lane_weights(now_epoch=int(time.time())))
    except lanes_render.LanesBlockError as e:
        ui.error(f"--lanes: {e}")
        return 2
    except scoring_lanes.LaneConflict as e:
        ui.error(f"--lanes: LaneConflict: {e}")
        return 2
    except scoring_lanes.LaneError as e:
        ui.error(f"--lanes: {e}")
        return 2
    roles = lanes_render.lane_roles(proposal)
    if team_only:
        ui.info("Antigravity: terms accepted for " + (", ".join(sorted((acks.get("google") or {}).get("roles") or ()))
                                                      or "no role") + ".")
    else:
        ui.info("Subscription lanes proposed (candidate order is the tie-break, not a rank):")
    for role in ([] if team_only else roles):
        cands = (proposal["roles"].get(role) or {}).get("candidates") or proposal["second"].get(role) or []
        ui.detail(f"  {role}: {', '.join(cands)}")
    for h in proposal.get("held") or []:
        # held candidates are the default of a fresh install (the agy lane is off): shown, not warned
        ui.detail(f"  {h['role']}: {h['candidate']} {h['reason']}")
    if preview or dry_run:
        ui.info("Dry run: nothing was enabled or written.")
        return 0
    # per-account role sets: the Anthropic acknowledgement covers the roles that run on a claude-cli lane (or
    # on none), never a role whose every candidate is an agy lane
    agy_only = {r for r in agy_roles
                if not any(c.startswith("claude-cli:") or c == "claude-code"
                           for key in ("roles", "second") for c in ((doc.get("lanes") or {}).get(key) or {}).get(r, ()))}
    anth_roles = [r for r in roles if r not in agy_only]
    if not team_only and not lanes_render.acked(acks, "anthropic", anth_roles):
        if ui.is_non_interactive():
            ui.error("--lanes: no stored Anthropic acknowledgement lists every lane role; run `ai-resources "
                     "setup --lanes` from a terminal to sign it")
            return 2
        ui.warn(lanes_render.ACK_TEXT["anthropic"])
        ui.detail("Roles: " + ", ".join(anth_roles))
        if not ui.confirm("Acknowledge for these roles?", default=False):
            ui.info("Not acknowledged; lanes stay disabled.")
            return 0
        acks["anthropic"] = lanes_render.make_ack("anthropic", anth_roles, datetime.now(timezone.utc).isoformat())
    s.profile.lane_acks = acks
    s.profile.lanes = proposal
    return 0


def _lanes_backup_dir() -> Path:
    return state.config_root() / "backups" / "lanes"


def _apply_lanes_file(s: state.SetupState, executors_doc: dict) -> dict | None:
    """Render (or remove) ~/.config/ai-resources/lanes.yaml for the state, in both modes, and track it.

    Written iff lanes are non-empty; `--no-lanes` empties them and the file and its tracking entry go.
    Returns the restore payload (None when nothing changed). Never opens openclaw.json."""
    from . import lanes_render
    try:
        res = lanes_render.sync_file(s, by_role=executors_doc.get("by_role") or {},
                                     backup_dir=_lanes_backup_dir())
    except OSError as e:
        ui.error(f"Failed to write {state.lanes_path()}: {e}")
        return None
    if res["action"] == "written":
        ui.ok(str(state.lanes_path()))
        ui.record_action("wrote lanes.yaml")
    elif res["action"] == "removed":
        ui.ok(f"removed {state.lanes_path()}")
        ui.record_action("removed lanes.yaml")
    return res["payload"]


def _restore_lanes_file(s: state.SetupState, payload: dict | None) -> None:
    """Put lanes.yaml back from its own backup when a later artifact failed; drop a tracking entry for a
    file that did not exist before."""
    if not payload:
        return
    from . import lanes_render
    try:
        lanes_render.restore(payload)
    except OSError as e:
        ui.warn(f"could not restore {state.lanes_path()}: {e}")
        return
    path = str(state.lanes_path())
    tracked = s.tracking.config_files_written
    if not payload.get("existed") and path in tracked:
        tracked.remove(path)
    elif payload.get("existed") and path not in tracked:
        tracked.append(path)


# --- Step 9 — Apply -------------------------------------------------------------
def _print_yaml(console, content: str) -> None:
    """Print rendered YAML verbatim: `[/]` or `[link=...]` in it is text, never markup."""
    for line in content.splitlines():
        console.print(f"  {line}", markup=False, highlight=False)


def _report_smoke_results(results) -> bool:
    """Show smoke results; a failure's detail is masked as a whole text, then split. True when any failed."""
    had_failures = False
    for label, ok, msg in results:
        if ok:
            ui.ok(label)
        else:
            had_failures = True
            ui.warn(f"{label}")
            for line in ui.safe_lines(msg or "(no detail)"):
                ui.detail(f"  {line[:300]}")
    return had_failures


def _routed_executors(s: state.SetupState, executors_doc: dict) -> dict:
    """The legacy LiteLLM gateway reaches Google directly, so a `google/*` role without an enabled Google provider
    runs on its declared Claude fallback, with one visible line (ADR-0010 S6c). Other rows are returned unchanged."""
    if s.mode != "multi-model" or s.backend != "litellm":
        return executors_doc
    routed, lines = team_view.degrade_unrouted(executors_doc, s.providers)
    for line in lines:
        ui.info(line)
    return routed


def _apply_channel_changes(s: state.SetupState, *, dry_run: bool = False) -> None:
    """Run the planned channel DISABLES, one at a time, each through `openclaw_channels.execute`, which re-checks every
    guard itself (a terminal, not a dry run, not `--yes`, the OpenClaw cockpit, hot reload rules, a fresh listing, the
    operator's default-No confirm and Telegram's second one). An enable is never run; Review already printed it."""
    from .. import openclaw_channels as channels
    from .. import openclaw_host, openclaw_reload_rules
    from .cockpits import _openclaw_channels
    inventory = getattr(s, _openclaw_channels.INVENTORY_ATTR, None)
    if not inventory:
        return
    plan = [c for c in channels.plan_changes(s.openclaw.channels_seen, s.openclaw.channels_wanted, inventory)
            if isinstance(c, channels.ChannelChange)]
    if not plan:
        return
    runner = openclaw_host.default_runner
    ruleset = None if dry_run else openclaw_reload_rules.current_ruleset(runner)
    cockpit = "openclaw" if "openclaw" in _APPLIED_TARGETS or (dry_run and "openclaw" in _members_of(s)) else ""
    interactive = ui.stdin_is_a_terminal() and not ui.is_non_interactive()
    ui.console().print()
    ui.info("Channel accounts")
    for change in plan:
        out = channels.execute(change, runner=runner, confirm=ui.confirm, tty=interactive, dry_run=dry_run,
                               yes=_ASSUME_YES, cockpit=cockpit, inventory=inventory, reload_rules=ruleset)
        show = ui.ok if out.status == channels.DONE else ui.warn if out.status in (channels.REFUSED, channels.FAILED) \
            else ui.info
        for line in out.lines:
            show(line)
        if out.ran:
            ui.record_action(f"turned off the channel account {change.key}")


def _apply_image_dry_run(s: state.SetupState) -> None:
    """The image answer under `--dry-run`: what it would do, in words, then each `config patch --dry-run` verdict. The
    runner sees only read-only reads and dry-run patches; nothing is written and nothing signs in (ADR-0011)."""
    if "openclaw" not in _members_of(s):
        return
    from .cockpits import _openclaw_image as _image
    from .cockpits import openclaw as _openclaw_cockpit
    ctx = getattr(s, _image.CTX_ATTR, None)
    lines = _image.dry_run_lines(s, ctx)
    ui.console().print()
    ui.info("Image generation")
    for line in lines:
        ui.info(line)
    _openclaw_cockpit.dry_run_image(s)


def _step9_apply(s: state.SetupState, dry_run: bool = False) -> int:
    if dry_run:
        return _step9_dry_run(s)
    if not _REVIEW_SHOWN:
        _section(9, "Apply")
    ui.console().print()
    ui.info("Generating configuration files...")

    # Build the final executors dict
    if s.mode == "multi-model":
        base = profiles.load_profile(profiles.saved_or_recommended(s.profile.name, s.backend), "multi-model")
        if s.profile.customized and s.profile.customizations:
            customizations = {k: v for k, v in s.profile.customizations.items()
                              if k in profiles.KNOWN_ROLES}
            base = profiles.merge_customizations(base, customizations)
        executors_doc = _routed_executors(s, profiles.to_executors(base, s.backend))

        # Write executors.yaml
        profiles.write_executors(executors_doc, state.executors_path())
        ui.ok(str(state.executors_path()))
        ui.record_action("wrote executors.yaml")

        # lanes.yaml sits between executors.yaml and the LiteLLM configs: it is a registered artifact, so a
        # LiteLLM write that fails puts the previous lanes.yaml back from its own backup (ADR-0002 s8/s10).
        lanes_payload = _apply_lanes_file(s, executors_doc)

        # Write LiteLLM configs (litellm.yaml + docker-compose.yaml)
        if s.backend == "litellm" and s.litellm.deployment == "local":
            try:
                paths = litellm.write_configs(executors_doc, {k: asdict(v) for k, v in s.providers.items()})
                ui.record_action("wrote the LiteLLM config files")
                for label, p in paths.items():
                    ui.ok(str(p))
                    if str(p) not in s.tracking.config_files_written:
                        s.tracking.config_files_written.append(str(p))
            except (RuntimeError, OSError) as e:
                ui.error(f"Failed to write LiteLLM config: {e}")
                _restore_lanes_file(s, lanes_payload)
                return 1
        # Always track the executors.yaml we just wrote
        if str(state.executors_path()) not in s.tracking.config_files_written:
            s.tracking.config_files_written.append(str(state.executors_path()))
    else:
        executors_doc = _single_model_executors(s)
        _apply_lanes_file(s, executors_doc)

    # Gateway endpoint + credential, per backend.
    if s.mode == "multi-model" and s.backend == "openrouter":
        gw = profiles.GATEWAYS["openrouter"]
        master_key = credentials.get_key(gw["api_key_env"])
        gateway_url = gw["url"]
    else:
        master_key = credentials.get_key("LITELLM_MASTER_KEY")
        gateway_url = (
            s.litellm.remote.url if s.litellm.deployment == "remote"
            else f"http://{s.litellm.local.bind_address}:{s.litellm.local.port}"
        )

    # Apply per-cockpit configurators
    targets = s.profile.customizations.get("__targets__", {}).get("selected", [])
    if not targets:
        targets = [cid for cid, cs in s.cockpits.items() if cs.installed]
    # Dict order, not selection order: OpenClaw's engines reuse what the other cockpits install.
    targets = [cid for cid in ALL_COCKPITS if cid in targets]
    global _APPLIED_TARGETS
    _APPLIED_TARGETS = list(targets)
    # The apply gate: with a recorded selection every cockpit gets its row of the compatibility
    # matrix (`ctx["route"]`); a cockpit whose row is "skip" keeps the kit content and gets no model
    # or endpoint setting (the summary said so before this step). Without a selection nothing changes.
    selection = s.get_selection()
    routes = {a.cockpit: a for a in model_selection.plan_table(
        selection, s.mode, s.backend, targets, engine=(selection.openclaw_engine or None) if selection else None)} \
        if selection is not None else {}
    for cid in targets:
        mod = ALL_COCKPITS.get(cid)
        if not mod:
            continue
        try:
            ctx = {
                "state": s,
                "executors": executors_doc,
                "master_key": master_key,
                "gateway_url": gateway_url,
                "detected_version": s.cockpits.get(cid, state.CockpitState()).version,
                "detected_path": s.cockpits.get(cid, state.CockpitState()).binary_path,
            }
            if s.mode == "multi-model" and s.backend in profiles.OPENAI_BASE:
                ctx["openai_base"] = profiles.OPENAI_BASE[s.backend]
            if cid in routes:
                ctx["route"] = routes[cid]
            if _COMPOSE.get("allow_unverified"):
                ctx["allow_unverified"] = True
            try:
                written = mod.configure(ctx)
            except (KeyboardInterrupt, ui.Cancelled):
                # A cockpit configurator writes several files in turn: an interrupt can leave some of them
                # written, so the attempt is recorded and the cancel block names it.
                ui.record_action(f"may be partially written: {cid}")
                raise
            for w in written:
                ui.ok(str(w))
            ui.record_action(f"configured {cid}")
        except _shared.SkipCockpit as e:
            ui.warn(f"{cid}: skipped: {e}")
        except Exception as e:
            ui.error(f"{cid}: {e}")

    # Channel accounts the operator unticked are turned off here, after the config patch and the lane writes of the
    # cockpits above and never in parallel with them (ADR-0010 N5). The intent is consumed: it is never saved (C6).
    _apply_channel_changes(s)
    s.openclaw.channels_seen, s.openclaw.channels_wanted = [], []

    # Cleanup: remove __targets__ stash
    s.profile.customizations.pop("__targets__", None)

    # Persist what the cockpits just installed (skill links, workflow scripts, env keys) before the
    # gateway steps below can fail: otherwise those files exist on disk with no tracking record,
    # and a later run would treat them as the user's and never clean them up.
    state.save(s)
    ui.record_action("saved setup state")

    # Local install path: write wrapper + lifecycle, start service
    if s.mode == "multi-model" and s.backend == "litellm" and s.litellm.deployment == "local":
        ui.console().print()

        if s.litellm.local.runtime in ("pipx", "pip-venv"):
            # 1. Write the wrapper script
            bin_path = Path(s.litellm.local.binary_path or "")
            if not bin_path.is_file():
                ui.error(f"litellm binary not found at {bin_path}")
                return 1
            wrapper = litellm.write_wrapper_script(
                bin_path,
                port=s.litellm.local.port,
                host=s.litellm.local.bind_address,
            )
            s.tracking.wrapper_path = str(wrapper)
            ui.ok(str(wrapper))
            ui.record_action("wrote the LiteLLM wrapper script")

            # 2. Install lifecycle (always — auto-start determines whether it actually starts)
            if s.litellm.local.auto_start:
                try:
                    p = litellm.install_lifecycle(wrapper)
                    s.litellm.local.lifecycle_path = str(p)
                    s.tracking.lifecycle_installed = True
                    s.tracking.lifecycle_path_written = str(p)
                    ui.ok(f"Service installed: {p}")
                    ui.record_action("installed the LiteLLM service")
                except (OSError, RuntimeError) as e:
                    ui.error(f"Lifecycle install failed: {e}")
                    return 1
            else:
                # Start once now, no auto-start
                import subprocess
                subprocess.Popen([str(wrapper)],
                                  stdout=open(litellm.log_dir() / "litellm.log", "ab"),
                                  stderr=open(litellm.log_dir() / "litellm.err", "ab"))
                ui.ok("LiteLLM started in background (will not auto-restart on logout)")
                ui.record_action("started LiteLLM in the background")

        elif litellm.is_container_mode(s.litellm.local.runtime):
            # Docker mode — write compose, start container. Auto-start is handled
            # entirely by the container's `restart: always` policy; no LaunchAgent
            # / systemd unit gets installed for this runtime.
            try:
                cp = litellm.write_compose_yaml()
                if str(cp) not in s.tracking.config_files_written:
                    s.tracking.config_files_written.append(str(cp))
                ui.ok(str(cp))
                ui.record_action("wrote the compose file")
            except Exception as e:
                ui.error(f"Compose file write failed: {e}")
                return 1
            with ui.spinner(f"Starting container via {s.litellm.local.runtime} compose up"):
                if not litellm.start_service():
                    ui.error("Container start failed")
                    ui.detail("Run `ai-resources daemon logs` to investigate")
                    return 1
            ui.record_action("started the LiteLLM container")
            ui.ok(f"Container running with restart policy — auto-restarts when "
                  f"{s.litellm.local.runtime} comes up.")

        # Wait for gateway health regardless of mode
        with ui.spinner("Waiting for gateway to become healthy"):
            healthy = litellm.wait_for_health(
                f"{gateway_url}/health/liveliness", max_attempts=60, delay=1.0,
            )
        if healthy:
            ui.ok(f"Gateway running at {gateway_url}")
        else:
            ui.error("Gateway didn't pass health check")
            ui.detail("Run `ai-resources daemon logs` to investigate")
            return 1

    # Smoke tests (multi-model only)
    if s.mode == "multi-model":
        ui.console().print()
        ui.info("Running smoke tests (~$0.001 of tokens)...")
        results = smoke.run_all(executors_doc, gateway_url, master_key, s.backend)

        # Persist full results so users can investigate truncated errors
        smoke_log = litellm.log_dir() / "smoke.log"
        _write_smoke_log(smoke_log, results)

        had_failures = _report_smoke_results(results)
        if had_failures:
            ui.detail(f"  Full smoke log: {smoke_log}")

        s.smoke_tests = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "status": "pass" if all(ok for _, ok, _ in results) else "partial",
        }

    line = _openclaw_status_line(s)
    if line:
        ui.console().print()
        ui.info(line)

    return 0


def _openclaw_status_line(s: state.SetupState) -> str:
    """One-line OpenClaw summary for the end of step 9, in either setup mode. "" if unapplied."""
    from .cockpits import _openclaw_host as _host_section

    host_line = _host_section.status_line(s)
    if not s.openclaw.applied:
        return host_line
    worker = f", worker {s.openclaw.worker_model}" if s.openclaw.engine == "antigravity" else ""
    line = (
        f"OpenClaw: engine {s.openclaw.engine} → {s.openclaw.model}{worker}  "
        f"(plugin linked: {s.openclaw.plugin_linked}, risk acknowledged: {s.openclaw.risk_acknowledged})"
    )
    return f"{line}\n{host_line}" if host_line else line


# --- Step 10 — Verification ------------------------------------------------------
def _step10_verify(s: state.SetupState, targets: list[str]) -> int:
    """Read-only check of every tooling step 9 applied. 1 iff a finding is an error, never for a warn."""
    from .. import verify

    _section(10, "Verification")
    if not targets:
        ui.info("Nothing was applied, so there is nothing to verify.")
        return 0
    findings = verify.run_all(s, targets)
    verify.print_findings(findings)
    ui.console().print()
    if verify.has_errors(findings):
        ui.error(verify.summary(findings, selected=len(targets)))
        return 1
    ui.ok(verify.summary(findings, selected=len(targets)))
    return 0


# --- Step 9 dry-run preview ------------------------------------------------------
def _print_plan_table(s: state.SetupState) -> None:
    """One line per detected cockpit: configure / through a gateway / instructions only / skipped and why.
    The same table the summary shows; read-only."""
    selection = s.get_selection()
    actions = model_selection.plan_table(selection, s.mode, s.backend, _detected_cockpit_ids(s),
                                         engine=(selection.openclaw_engine or None) if selection else None)
    ui.info("Tool plan" + ("" if selection is not None else " (no model selection recorded: the kit defaults)"))
    for a in actions:
        ui.detail("  " + model_selection.describe(a))


def _diff_lines(label: str, path: Path, new_text: str, old_text: str | None = None) -> list[str]:
    """A unified diff of what a real apply would write to `path`, secrets masked. `new file` when the target is
    missing, `unchanged` when the content is identical. Reads the target; writes nothing."""
    import difflib
    head = f"\u2500\u2500 {label}  ({path})"
    new_lines = ui.safe_text(new_text).splitlines()
    if old_text is None:
        try:
            old_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return [head, "  new file"] + [f"  + {l}" for l in new_lines]
    old_lines = ui.safe_text(old_text).splitlines()
    if old_text == new_text:
        return [head, "  unchanged"]
    if old_lines == new_lines:
        return [head, "  changed (masked)"]            # only values that are hidden differ
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"{path} (current)",
                                     tofile=f"{path} (after apply)", lineterm="", n=2))
    return [head] + [f"  {l}" for l in diff]


def _dry_run_targets(s: state.SetupState, executors_doc: dict, env_patch: dict | None,
                     existing_env: dict | None) -> list[tuple[str, Path, str, str | None]]:
    """(label, target path, text a real apply would write, current text or None to read the target) for the
    files a dry run can render offline."""
    import json
    import yaml as _yaml
    from . import lanes_render
    out: list[tuple[str, Path, str, str | None]] = []
    multi = s.mode == "multi-model"
    local_gw = multi and s.backend == "litellm" and s.litellm.deployment == "local"
    if multi:
        out.append(("executors.yaml", state.executors_path(),
                    _yaml.safe_dump(executors_doc, default_flow_style=False, sort_keys=False), None))
    if local_gw:
        try:
            out.append(("litellm.yaml", state.litellm_path(),
                        litellm._render_litellm_yaml(executors_doc, dict(s.providers), "LITELLM_MASTER_KEY"),
                        None))
            if litellm.is_container_mode(s.litellm.local.runtime):
                out.append(("docker-compose.yaml", state.compose_path(), litellm._render_compose_yaml(s), None))
        except Exception as e:  # noqa: BLE001 - a render problem is reported by the section above
            ui.warn(f"No diff for the gateway files: {e}")
    try:
        lanes_text = lanes_render.expected_text(s, executors_doc.get("by_role") or {})
    except Exception as e:  # noqa: BLE001
        ui.warn(f"No diff for lanes.yaml: {e}")
        lanes_text = None
    if lanes_text is not None:
        out.append(("lanes.yaml", state.lanes_path(), lanes_text, None))
    if env_patch is not None:
        from .cockpits import claude as _claude_cockpit
        merged = {**(existing_env or {}), **env_patch}
        # Compared as the parsed `env` object, not the raw file: a settings file holds much more than `env`.
        before = json.dumps({"env": existing_env or {}}, indent=2, sort_keys=True) \
            if _claude_cockpit.SETTINGS_PATH.is_file() else None
        out.append(("settings.json env", _claude_cockpit.SETTINGS_PATH,
                    json.dumps({"env": merged}, indent=2, sort_keys=True), before))
    return out


def _print_dry_run_diffs(s: state.SetupState, executors_doc: dict, env_patch: dict | None,
                         existing_env: dict | None) -> None:
    console = ui.console()
    console.print()
    ui.info("What a real run would change (masked; nothing is written):")
    for label, path, text, current in _dry_run_targets(s, executors_doc, env_patch, existing_env):
        for line in _diff_lines(label, path, text, current):
            console.print(line, markup=False, highlight=False)
    console.print()
    ui.info("DRY RUN: nothing was written")


def _step9_dry_run(s: state.SetupState) -> int:
    """Render all configs to a temp dir, print them, start the gateway, run smoke tests, tear down."""
    import json
    import os
    import shutil
    import subprocess
    import tempfile

    from .. import repo_root as _repo_root
    from .cockpits import claude as _claude_cockpit

    console = ui.console()
    _section(9, "Apply (DRY RUN)")
    console.print()
    console.print("[bold yellow]DRY RUN — configs shown below will NOT be written.[/]")
    if s.mode == "multi-model":
        console.print("[bold yellow]Gateway will be started temporarily for smoke tests, then stopped.[/]")
    console.print()
    _print_plan_table(s)
    _apply_channel_changes(s, dry_run=True)
    _apply_image_dry_run(s)

    # Build executors
    if s.mode == "multi-model":
        base = profiles.load_profile(profiles.saved_or_recommended(s.profile.name, s.backend), "multi-model")
        if s.profile.customized and s.profile.customizations:
            customizations = {k: v for k, v in s.profile.customizations.items()
                              if k in profiles.KNOWN_ROLES}
            base = profiles.merge_customizations(base, customizations)
        executors_doc = _routed_executors(s, profiles.to_executors(base, s.backend))
    else:
        executors_doc = _single_model_executors(s)

    bind = s.litellm.local.bind_address or "127.0.0.1"
    port = s.litellm.local.port or 4000
    if s.mode == "multi-model" and s.backend == "openrouter":
        gw = profiles.GATEWAYS["openrouter"]
        master_key = credentials.get_key(gw["api_key_env"]) or ""
        gateway_url = gw["url"]
    else:
        master_key = credentials.get_key("LITELLM_MASTER_KEY") or ""
        gateway_url = (
            s.litellm.remote.url if s.litellm.deployment == "remote"
            else f"http://{bind}:{port}"
        )
    # The path a real run writes (`claude.configure`): the version-independent opt/ link, not the
    # resolved Cellar directory. Showing the resolved one diffed against the stored opt/ value and
    # labelled an unchanged setting `(update)`.
    ak_path = str(_shared.stable_kit_root(_repo_root()))
    mode = s.mode

    # Role table
    ui.role_table(profiles.role_table(executors_doc), title=f"Profile: {profiles.saved_or_recommended(s.profile.name, s.backend)}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="ai-resources-dryrun-"))
    tmp_litellm_yaml = tmp_dir / "litellm.yaml"
    tmp_compose_yaml = tmp_dir / "docker-compose.yaml"
    proc = None
    started_docker = False
    env_patch_for_diff: dict | None = None
    existing_env_for_diff: dict | None = None

    try:
        # ── litellm.yaml ──────────────────────────────────────────────────────
        if (s.mode == "multi-model" and s.backend == "litellm"
                and s.litellm.deployment == "local"):
            try:
                yaml_content = litellm._render_litellm_yaml(
                    executors_doc, dict(s.providers), "LITELLM_MASTER_KEY",
                )
                tmp_litellm_yaml.write_text(yaml_content, encoding="utf-8")
                console.print(f"\n[bold cyan]── litellm.yaml  (would write → {ui.markup_safe(state.litellm_path())})[/]")
                _print_yaml(console, yaml_content)
            except Exception as e:
                ui.warn(f"Cannot render litellm.yaml: {e}")

        # ── docker-compose.yaml ───────────────────────────────────────────────
        runtime = s.litellm.local.runtime
        if (s.mode == "multi-model" and s.backend == "litellm"
                and s.litellm.deployment == "local"
                and litellm.is_container_mode(runtime) and tmp_litellm_yaml.is_file()):
            try:
                import yaml as _yaml
                image = s.litellm.local.image or litellm.DEFAULT_DOCKER_IMAGE
                compose_body = {
                    "services": {
                        "ai-resources-litellm-dryrun": {
                            "image": image,
                            "container_name": "ai-resources-litellm-dryrun",
                            "command": ["--config", "/app/config.yaml", "--port", "4000",
                                        "--host", "0.0.0.0"],
                            "ports": [f"{bind}:{port}:4000"],
                            "volumes": [
                                f"{tmp_litellm_yaml}:/app/config.yaml:ro",
                                f"{state.env_path()}:/app/.env:ro",
                            ],
                            "env_file": [str(state.env_path())],
                            "restart": "no",
                            "networks": ["ai-resources"],
                            # Use python3 (always available in Python-based images) instead of
                            # curl (absent from slim images) to avoid false unhealthy status.
                            "healthcheck": {
                                "test": ["CMD-SHELL",
                                         "python3 -c \"import urllib.request; "
                                         "urllib.request.urlopen("
                                         "'http://localhost:4000/health/liveliness', timeout=3)"
                                         "\" 2>/dev/null || exit 1"],
                                "interval": "30s",
                                "timeout": "5s",
                                "retries": 3,
                                "start_period": "30s",
                            },
                        }
                    },
                    "networks": {"ai-resources": {"driver": "bridge"}},
                }
                compose_content = _yaml.safe_dump(compose_body, default_flow_style=False, sort_keys=False)
                tmp_compose_yaml.write_text(compose_content, encoding="utf-8")
                console.print(f"\n[bold cyan]── docker-compose.yaml  (would write → {ui.markup_safe(state.compose_path())})[/]")
                _print_yaml(console, compose_content)
            except Exception as e:
                ui.warn(f"Cannot render compose YAML: {e}")

        # ── settings.json patch ───────────────────────────────────────────────
        console.print(f"\n[bold cyan]── settings.json patch  (→ {ui.markup_safe(_claude_cockpit.SETTINGS_PATH)})[/]")
        patch = _claude_cockpit._build_settings_patch(
            executors_doc, master_key, gateway_url, ak_path, mode,
            backend=s.backend,
        )
        env_patch = patch.get("env", {})
        existing_env: dict = {}
        if _claude_cockpit.SETTINGS_PATH.is_file():
            try:
                existing_env = json.loads(
                    _claude_cockpit.SETTINGS_PATH.read_text(encoding="utf-8")
                ).get("env", {})
            except (json.JSONDecodeError, OSError):
                pass
        env_patch_for_diff, existing_env_for_diff = dict(env_patch), dict(existing_env)
        for k, v in env_patch.items():
            display_v = ui.markup_safe(credentials.mask(v) if ("KEY" in k or "TOKEN" in k) else v)
            k_shown = ui.markup_safe(k)
            if k not in existing_env:
                console.print(f"  [green]+ {k_shown} = {display_v}  (new)[/]")
            elif existing_env[k] == v:
                console.print(f"  [dim]= {k_shown} = {display_v}  (unchanged)[/]")
            else:
                console.print(f"  [yellow]~ {k_shown} = {display_v}  (update)[/]")

        # ── start gateway for testing ─────────────────────────────────────────
        if s.mode == "multi-model" and tmp_litellm_yaml.is_file():
            console.print()
            ui.info("Starting gateway temporarily for smoke tests…")

            if runtime in ("pipx", "pip-venv"):
                # Resolve litellm binary
                bin_path = Path(s.litellm.local.binary_path or "")
                if not bin_path.is_file():
                    if runtime == "pip-venv":
                        bin_path = litellm.venv_dir() / "bin" / "litellm"
                    candidate = shutil.which("litellm")
                    if candidate and not bin_path.is_file():
                        bin_path = Path(candidate)
                if not bin_path.is_file():
                    ui.warn("litellm binary not found — skipping live gateway test")
                    ui.detail(f"Expected: {bin_path}")
                    ui.detail("Run `ai-resources setup` (without --dry-run) to install it first.")
                else:
                    env_for_proc = {**os.environ, **credentials.load_env()}
                    proc = subprocess.Popen(
                        [str(bin_path), "--config", str(tmp_litellm_yaml),
                         "--port", str(port), "--host", bind],
                        env=env_for_proc,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    with ui.spinner("Waiting for gateway health"):
                        healthy = litellm.wait_for_health(
                            f"{gateway_url}/health/liveliness",
                            max_attempts=60, delay=1.0,
                        )
                    if healthy:
                        ui.ok(f"Gateway healthy at {gateway_url}")
                    else:
                        ui.error("Gateway did not become healthy within 60 s")
                        ui.detail("Check logs above or try starting manually:")
                        ui.detail(f"  source {state.env_path()} && {bin_path} "
                                  f"--config {tmp_litellm_yaml} --port {port}")

            elif litellm.is_container_mode(runtime) and tmp_compose_yaml.is_file():
                # Drive through the runtime's CLI, not its name: `colima compose`
                # is not a command — colima is driven by docker's.
                compose_cli = litellm.container_cli(runtime)
                with ui.spinner(f"{compose_cli} compose up -d (dry-run container)"):
                    rc, _, err = litellm._run(
                        [compose_cli, "compose", "-f", str(tmp_compose_yaml), "up", "-d"],
                        timeout=120,
                    )
                if rc != 0:
                    ui.error(f"Container start failed: {ui.safe_excerpt(err, 200)}")
                else:
                    started_docker = True
                    with ui.spinner("Waiting for gateway health"):
                        healthy = litellm.wait_for_health(
                            f"{gateway_url}/health/liveliness",
                            max_attempts=60, delay=1.0,
                        )
                    if healthy:
                        ui.ok(f"Gateway healthy at {gateway_url}")
                    else:
                        ui.error("Gateway did not become healthy within 60 s")
                        ui.detail(f"  {runtime} logs ai-resources-litellm-dryrun")

        elif (s.mode == "multi-model" and s.backend == "litellm"
                and s.litellm.deployment == "remote"):
            with ui.spinner(f"Validating remote {s.litellm.remote.url}"):
                ok, msg = litellm.validate_remote(s.litellm.remote.url, master_key)
            if ok:
                ui.ok("Remote endpoint reachable")
            else:
                ui.error(f"Remote unreachable: {ui.safe_text(msg)}")

        # ── smoke tests ───────────────────────────────────────────────────────
        if s.mode == "multi-model":
            console.print()
            ui.info("Running smoke tests…")
            results = smoke.run_all(executors_doc, gateway_url, master_key, s.backend)
            _report_smoke_results(results)

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if started_docker and tmp_compose_yaml.is_file():
            litellm._run(
                [runtime, "compose", "-f", str(tmp_compose_yaml), "down"],
                timeout=60,
            )
        shutil.rmtree(tmp_dir, ignore_errors=True)

    _print_dry_run_diffs(s, executors_doc, env_patch_for_diff, existing_env_for_diff)
    console.print()
    _section(10, "Verification")
    ui.info("Skipped in a dry run: verification checks what a real apply left behind, and nothing was applied.")
    console.print()
    ui.info("Dry run complete — no permanent changes made.")
    ui.detail("Remove --dry-run to apply.")
    return 0


# --- Completion banner -----------------------------------------------------------
def _try_lines() -> str:
    """One line per tool the run configured (or the tools that were installed), each with the command that opens it."""
    ids = [c for c in _APPLIED_TARGETS if c in tools.TOOLS] or [c for c in ("claude",) if c in tools.TOOLS]
    lines = []
    for cid in ids:
        spec = tools.TOOLS[cid]
        binary = spec.login_cmd[0] if spec.login_cmd else cid
        lines.append(f"  {binary:<28} open {spec.label}\n")
    return "".join(lines)


def _print_completion(s: state.SetupState) -> None:
    # Multi-model + Claude Code OAuth session = gateway auth failures.
    # Surface this warning before the banner so users see it.
    if s.mode == "multi-model":
        claude_cs = s.cockpits.get("claude")
        if claude_cs and claude_cs.configured:
            from .cockpits import claude as _claude_cockpit
            if _claude_cockpit.is_logged_in_via_oauth():
                ui.console().print()
                ui.info("Claude Code is signed in via OAuth (claude.ai subscription).")
                if s.backend == "openrouter":
                    # Not optional here: OpenRouter rejects a request that carries
                    # both a bearer token and an OAuth login.
                    ui.warn("Log out before using Claude Code, or every request fails:")
                    ui.detail("  1. claude /logout")
                    ui.detail("  2. Open a new terminal")
                    ui.detail("  3. ai-resources doctor")
                else:
                    ui.detail("The gateway runs with allow_requests_on_db_unavailable=true,")
                    ui.detail("so your current setup works as-is.")
                    ui.detail("")
                    ui.detail("To switch to API key mode (cancel subscription):")
                    ui.detail("  1. claude /logout")
                    ui.detail("  2. Open a new terminal")
                    ui.detail("  3. ai-resources doctor")

    gateway_line = ("" if s.mode == "multi-model" and s.backend == "openrouter"
                    else "  ai-resources daemon logs     LiteLLM logs\n") if s.mode == "multi-model" else ""
    ui.console().print()
    ui.banner(
        "✓ Setup complete",
        subtitle="Try:\n"
                 f"{_try_lines()}"
                 "  ai-resources doctor          full health check\n"
                 "  ai-resources executors       show role → model map\n"
                 f"{gateway_line}"
                 "  ai-resources audit           cost report",
    )
