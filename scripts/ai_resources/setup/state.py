"""setup-state.yaml — persisted wizard answers + detection results."""
from __future__ import annotations

import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # handled by ui.require_deps()

from .. import model_pins  # noqa: E402

SCHEMA_VERSION = "1.0"


def config_root() -> Path:
    """Return ~/.config/ai-resources (or $XDG_CONFIG_HOME/ai-resources)."""
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "ai-resources"


def state_path() -> Path:
    return config_root() / "setup-state.yaml"


def env_path() -> Path:
    return config_root() / ".env"


def executors_path() -> Path:
    return config_root() / "executors.yaml"


def litellm_path() -> Path:
    return config_root() / "litellm.yaml"


def compose_path() -> Path:
    return config_root() / "docker-compose.yaml"


def lanes_path() -> Path:
    """The rendered subscription-lanes table (ADR-0007): setup-owned, written only when lanes are non-empty."""
    return config_root() / "lanes.yaml"


@dataclass
class CockpitState:
    installed: bool = False
    version: str = ""
    binary_path: str = ""
    config_root: str = ""
    configured: bool = False
    last_configured_at: str = ""


@dataclass
class LiteLLMLocal:
    runtime: str = "pip-venv"         # pip-venv | pipx | docker | podman
    binary_path: str = ""             # absolute path to litellm executable
    venv_path: str = ""               # for pip-venv mode
    python_path: str = ""             # python interpreter used for the install (3.10-3.13)
    image: str = "ghcr.io/berriai/litellm:main-stable"  # docker mode only
    bind_address: str = "127.0.0.1"
    port: int = 4000
    auto_start: bool = True
    lifecycle_path: str = ""
    log_dir: str = ""


@dataclass
class LiteLLMRemote:
    url: str = ""
    master_key_env: str = "LITELLM_MASTER_KEY"


@dataclass
class LiteLLMState:
    deployment: str = ""              # local | remote | skipped
    local: LiteLLMLocal = field(default_factory=LiteLLMLocal)
    remote: LiteLLMRemote = field(default_factory=LiteLLMRemote)
    health_check_at: str = ""


@dataclass
class ProviderState:
    enabled: bool = False
    auth_method: str = "api_key"      # api_key | vertex_adc | oauth
    env_var: str = ""
    last_validated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProfileState:
    # Empty until a profile is chosen (step 6 or --profile). A saved name is never replaced; an empty one lets
    # the wizard offer the recommended profile instead of a stale hard-coded default.
    name: str = ""
    customized: bool = False
    customizations: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Provenance of a team proposed by `scoring.compose` (mode, input digest, dataset version, labels, headroom
    # per pool). None until a composed team is accepted; then it is not written (see `_to_dict`).
    composed: Any = None
    # The operator's explicit `--allow-unverified` opt-in, persisted so a later re-render (models update/approve)
    # keeps the unverified ids that setup wrote instead of gating them back to `inherit`.
    allow_unverified: bool = False
    # Subscription lanes (ADR-0007, WHAT not HOW). None until the operator enables them; then
    # {agents, roles, second, orchestrators, verify_capability, pool_caps, max_remote_concurrency,
    # workspace_root}. Both fields are additive: absent from the file while empty, so an old file
    # round-trips byte-identically.
    lanes: Any = None
    # {anthropic?: Ack, google?: Ack}, Ack = {date, text_sha256, text, roles}.
    lane_acks: Any = None
    # The role -> {model, fallbacks} table of the saved profile as it was last applied (ADR-0014 D1). A profile whose
    # table changed in a kit update shows the old -> new diff once (ADR-0001), then this record is updated. Additive:
    # absent from the file while empty.
    roles_seen: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstallTracking:
    """Audit trail of artifacts the wizard created — used to cleanly undo on mode switch.

    Each flag/path records something the wizard was responsible for installing.
    Pre-existing artifacts (e.g. a litellm already on PATH before setup) are NOT
    tracked here, so teardown only removes what we put there.
    """
    # LiteLLM Python package
    litellm_installed_by_us: bool = False
    litellm_install_method: str = ""           # "pipx" | "pip-venv" | ""
    # Docker image
    docker_image_pulled_by_us: bool = False
    docker_image: str = ""
    # Background runner (launchd plist / systemd unit)
    lifecycle_installed: bool = False
    lifecycle_path_written: str = ""
    # Wrapper script
    wrapper_path: str = ""
    # Generated YAMLs (litellm.yaml, docker-compose.yaml, executors.yaml, …)
    config_files_written: list[str] = field(default_factory=list)
    # .env keys added by the wizard (only those not pre-existing)
    env_keys_added: list[str] = field(default_factory=list)
    # Per-cockpit settings.json env keys we inserted
    cockpit_env_keys_added: dict[str, list[str]] = field(default_factory=dict)
    # Workflow script file names we copied into the cockpit's workflows dir (e.g. kit-plan.js).
    # Only these may be pruned later: a user's own kit-*.js is never touched.
    workflow_scripts_installed: list[str] = field(default_factory=list)
    # Subagent names we generated into the cockpit's agents dir. Same contract as
    # above: only these may be pruned, so an agent the user wrote by hand survives
    # even when its name matches one the kit used to ship. Without this record a
    # persona that is renamed or removed upstream would leave its subagent behind
    # forever, and the cockpit would keep offering an agent with no definition.
    subagent_files_installed: list[str] = field(default_factory=list)
    # External tools (claude, agy, openclaw) the wizard installed via their own official
    # installer — never a pre-existing binary. Teardown only ever touches these ids.
    tools_installed_by_us: list[str] = field(default_factory=list)
    # tool id -> the exact install command that was run, for audit / re-display.
    tool_install_methods: dict[str, str] = field(default_factory=dict)
    # tool id -> rc-file lines its installer appended (across ~/.bashrc, ~/.zshrc,
    # ~/.zprofile, ~/.profile), minus any that duplicated a pre-existing line. Only
    # these exact lines are ever removed on teardown.
    rc_lines_added: dict[str, list[str]] = field(default_factory=dict)
    # Retired in kit 2.2b: install consent is per run and is never stored or replayed (an old "yes" must not
    # authorise a later run). The key is still read and written (always None) so an old file loads.
    install_tools_answer: str | None = None
    # Ids of one-time cost migrations already applied (a key removed or a value replaced on the operator's
    # confirmation). A re-run skips an id that is recorded, so a value the operator changed back stays theirs.
    cost_migrations_applied: list[str] = field(default_factory=list)
    # Round 2 (ADR-0014). All additive: absent from the file while empty, so a state saved before round 2 round-trips.
    # Hygiene: user-level MCP servers and plugins the operator disabled on confirmation, each
    # {kind: mcp|plugin, name, scope, previous: <full config as read>, timestamp, secrets_recoverable}. Restore re-adds
    # exactly these; a re-run skips an id that is recorded.
    hygiene_disabled: list[dict] = field(default_factory=list)
    # Global CLAUDE.md blocks moved out on confirmation, each {block_id, sha256, destination, backup, pointer,
    # kind: skill|rule}. Restore re-inlines a block only while its destination still hashes to `sha256`.
    claude_md_migrations: list[dict] = field(default_factory=list)
    # The persona domains whose subagents were rendered on the last run (domains.active_domains()).
    persona_domains_active: list[str] = field(default_factory=list)
    # agy MCP servers the kit added to ~/.gemini/config/mcp_config.json: {name: definition written}. Teardown removes
    # only these, and only while the entry still equals what was written.
    agy_mcp_added: dict[str, Any] = field(default_factory=dict)


IMAGE_STATE_DEFAULTS: dict[str, Any] = {
    "image_mode": "", "image_model": "", "image_account": "", "image_previous": None, "image_previous_absent": False,
    "image_applied": False, "image_auth_profile_created": "", "image_deny_added": False,
    "image_deny_key_absent": False, "image_applied_ref": "",
}


@dataclass
class OpenClawState:
    """Which engine the kit pointed OpenClaw's default agent at, and what it replaced."""
    # Ids of the confirmed cost migrations already applied to openclaw.json (see openclaw_cost_migrations).
    cost_migrations_applied: list[str] = field(default_factory=list)
    # What the narrow Slack cost writer set (openclaw_slack_cost): change records, so teardown deletes exactly those.
    slack_cost_changes: list[dict] = field(default_factory=list)
    # What the Haiku notification route set (openclaw_notification_route): change records, so teardown removes exactly
    # those. Additive: absent while empty.
    notification_route_changes: list[dict] = field(default_factory=list)
    engine: str = ""                  # antigravity | claude-code | codex | gemini-cli | keep
    model: str = ""                   # OpenClaw model ref (bare id for antigravity, e.g.
                                       # gemini-3.8-flash-low, claude-sonnet-4-6 or
                                       # gpt-oss-120b-medium — antigravity's two weekly
                                       # quota pools, see _agy_quota.py; namespaced
                                       # otherwise, e.g. anthropic/claude-sonnet-5)
    applied: bool = False             # the kit has written openclaw.json at least once
    config_path: str = ""
    # openclaw.json values before the kit's first write (model, models, engram,
    # extra_dirs, plus the antigravity-only keys), restored verbatim on teardown.
    # None means the key was absent. Also carries the one-off marker
    # "agy_mcp_bridge_preexisted" recorded when the antigravity engine registers the
    # agy MCP bridge, so teardown only removes an entry the kit itself created.
    previous: dict[str, Any] = field(default_factory=dict)
    # Voice notes. `voice` is the last answer (cloud | local | off | keep); `voice_applied`
    # is the mode the kit last wrote into tools.media, "" when it never did.
    voice: str = ""
    voice_language: str = ""          # ISO code passed to the transcriber, or auto
    voice_correction: str = ""        # local mode only: llm | offline
    voice_applied: str = ""
    # tools.media before the kit's first voice write, restored on teardown. None: absent.
    voice_previous: Any = None
    # What setup itself installed for voice notes, and only that, is removed on teardown:
    # Homebrew formulas that were missing before, and Whisper model files it downloaded.
    voice_formulas_installed: list[str] = field(default_factory=list)
    voice_models_downloaded: list[str] = field(default_factory=list)
    # The default glossary the kit created (path -> sha256 of what it wrote); teardown removes it only
    # while it is still byte-identical, because the operator is told to edit it.
    voice_glossary_created: dict[str, str] = field(default_factory=dict)
    # Image generation (ADR-0011). All optional, none secret-bearing: names and model refs only. `image_mode` is the last
    # answer ("" never answered; else keep | disable | a route id of _openclaw_image.CATALOG). `image_account` is a
    # profile id or an env var NAME, never a value. `image_previous` is agents.defaults.mediaModels.image before the kit's
    # first write (`image_previous_absent`: the key did not exist). `image_deny_added` is True only after the kit's own
    # `image_generate` token landed in the global tools.deny; `image_deny_key_absent` that the key did not exist before.
    image_mode: str = ""
    image_model: str = ""
    image_account: str = ""
    image_previous: Any = None
    image_previous_absent: bool = False
    image_applied: bool = False
    image_auth_profile_created: str = ""
    image_deny_added: bool = False
    image_deny_key_absent: bool = False
    # The model ref the kit last wrote to mediaModels.image (`image_model` is also the next answer, so it can differ at
    # Apply). The kit treats the live value as its own only when it is kit-shaped AND names this ref (review L2).
    image_applied_ref: str = ""
    # MCP servers mirrored from Claude Code. `mcp` is the last answer (mirror | keep);
    # `mcp_skipped` the servers the user unticked. `mcp_mirrored` holds each server the kit
    # manages: {name: {"previous": definition before the kit (None: absent), "applied": ...}}.
    mcp: str = ""
    mcp_skipped: list[str] = field(default_factory=list)
    mcp_mirrored: dict[str, Any] = field(default_factory=dict)
    # Per-agent `tools.deny` tokens (`<server>__*`) the kit added for mirrored servers outside the opt-in list:
    # {agent id: [tokens]}. Only these are ever removed again (an operator's own token stays).
    mcp_deny_added: dict[str, list[str]] = field(default_factory=dict)
    # antigravity engine only, below:
    orchestrator_model: str = "gemini-3.8-flash-low"
    # Bare model id: the ref is built as `claude-kit/<worker_model>`, and OpenClaw
    # only resolves two-segment refs (`claude-kit/anthropic/claude-sonnet-5` is
    # rejected by `config patch --dry-run`).
    worker_model: str = field(default_factory=lambda: model_pins.effective()["sonnet"])
    plugin_linked: bool = False       # the kit ran `openclaw plugins install --link ...`
    risk_acknowledged: bool = False   # explicit consent to unrestricted code execution
    # True once `configure()` has successfully applied the antigravity engine's own
    # patch (agents.entries.claude, subagents.allowAgents, tools.media and
    # plugins.entries.ai-resources), independent of `engine` above.
    # `engine` reflects the *current* choice and is overwritten as soon as the wizard
    # picks a different one; this flag is what gates restoring antigravity-only keys
    # (in `configure()` when switching away, and in `teardown()`), so a switch from
    # antigravity to any other engine is never mistaken for "nothing to restore".
    antigravity_applied: bool = False
    # OpenClaw host section (cockpits/_openclaw_host.py). Every answer defaults to "no": a first
    # unattended run never changes how a live host behaves.
    host: bool = False                # the master answer: this machine runs the gateway
    host_narration: str = ""          # team narration into Telegram: "" (off) | milestones | every-step
    host_guard: bool = False          # the gateway guard hook (denies an undrained gateway stop)
    host_units: bool = False          # the openclaw-* systemd units (backup, watchdog, ...)
    host_config: bool = False         # the canonical config block (profiles/openclaw-host.json5)
    host_workboard: bool = True       # `openclaw plugins enable workboard` (asked; defaults to yes)
    # ADR-0014 D5: the operator's answer to "turn the main heartbeat off?" (default No keeps the 2h isolated light
    # heartbeat). Additive: absent from the file while False.
    host_heartbeat_off: bool = False
    host_agents_md: bool = False      # AGENTS.md templates for workspaces that have none
    host_check: bool = False          # report (and offer to fix) what `openclaw bootstrap` finds
    # ADR-0004. "" means "keep whatever kit-host.env holds, else the shipped default".
    host_graceful_restart: str = ""   # off | notify | on: what the health check does under memory pressure
    host_resource_guards: str = ""    # on | off: fill mcp.sessionIdleTtlMs and agents.defaults.timeoutSeconds
    host_domain: str = ""             # public host name of the control UI (no scheme)
    host_pod_cidr: str = ""           # CIDR of the ingress that reaches the gateway
    host_operator_id: str = ""        # numeric Telegram id that failure notices go to
    host_backup_dir: str = ""         # where the backup tiers live
    # When `ai-resources doctor` reports a gateway running older plugin code than the kit on disk:
    # linked (the kit's plugin is linked) | engine (only when the kit applied antigravity) | off.
    # "linked" is the default because the consequence is a warning, not an action: it never
    # changes how a live host behaves, the rule the host answers above follow. It does change
    # doctor's issue count, and so its exit status.
    stale_plugin_check: str = "linked"
    # What the last configure() applied, so teardown removes exactly that and nothing else.
    host_hooks_applied: bool = False
    host_units_previous: dict[str, Any] = field(default_factory=dict)  # unit file -> text before (None: absent)
    host_units_applied: bool = False
    # Timers the kit enabled (they were not enabled before), so teardown disables exactly those.
    host_timers_enabled: list[str] = field(default_factory=list)
    # Hand-installed health-restart files the kit moved aside when it adopted that timer:
    # original path -> where it was moved. Teardown moves them back; nothing is ever deleted.
    host_hand_moved: dict[str, str] = field(default_factory=dict)
    # original path -> sha256 of the kit's file the kit wrote at that path after the hand copy was moved
    # aside, so teardown replaces exactly that file and never one the operator changed since.
    host_hand_kit_sha: dict[str, str] = field(default_factory=dict)
    # original path -> the link target of a hand file that was a SYMLINK when the kit moved it aside. Teardown
    # puts back a moved-aside link only when it still has that target: a link planted in the backup directory is
    # never moved onto a unit path.
    host_hand_links: dict[str, str] = field(default_factory=dict)
    # Reasons an earlier teardown already reported as unfinished (teardown names each reason once, not on every
    # run); cleared when a teardown finishes.
    host_teardown_reported: list[str] = field(default_factory=list)
    # Hand-installed legacy team-hook registrations the kit replaced: [{"event", "entry"}], put back
    # by teardown.
    host_legacy_hooks: list[Any] = field(default_factory=list)
    host_config_changes: list[Any] = field(default_factory=list)       # leaves changed, with their old values
    host_workboard_applied: str = ""  # "" | enabled-by-kit | already-enabled
    host_agents_md_written: dict[str, str] = field(default_factory=dict)  # path -> sha256 of what was written
    # The kit block in agent workspace AGENTS.md files (independent of the host answers above):
    # path -> sha256 of a file the kit CREATED with only the block, "" when the file already
    # existed (teardown removes the block and leaves every other byte).
    agent_kit_blocks: dict[str, str] = field(default_factory=dict)
    # Telegram group routing (root `bindings` only; `channels` is never written). `host_group_ids` is
    # the last answer per agent; `bindings_previous` the array before the kit's first write (None:
    # absent), put back on teardown; `bindings_written` the digest of what the kit last wrote, so
    # teardown never overwrites an array the operator changed since.
    host_group_ids: dict[str, str] = field(default_factory=dict)
    bindings_applied: bool = False
    bindings_previous: Any = None
    bindings_written: str = ""
    host_env_previous: dict[str, Any] = field(default_factory=dict)    # kit-host.env key -> value before (None: absent)
    host_env_created: bool = False
    # A gateway restart setup DEFERRED because agent turns were in flight: {reason, command,
    # main_pid, since}. Empty when nothing is owed. While it is set setup reports OpenClaw as
    # not complete and `ai-resources openclaw status` names the command; `main_pid` (the unit's
    # MainPID at deferral) is how a restart done by hand is recognised.
    gateway_restart_pending: dict[str, Any] = field(default_factory=dict)
    # Restart-required openclaw.json keys setup (or a teardown, or the post-setup watch) did NOT apply
    # because that would restart a live gateway (ADR-0003). Each entry:
    #   {"op": "profile", "path": dotted, "action": "filled"|"forced", "source", "at"}   re-derived at apply time
    #   {"op": "restore", "path": dotted, "change": <host_config_changes record>, "source", "at"}
    # Never holds a secret value. `ai-resources openclaw apply-pending` applies and clears them.
    host_restart_pending: list[Any] = field(default_factory=list)
    # The post-setup health watch (`openclaw-config-watch-<run_id>`, a transient user unit):
    # {run_id, unit, started_at, changes, done, result}. `changes` are this run's written keys with
    # their previous values (never a secret value). Cleared by a newer run or a teardown.
    config_watch: Any = None
    # The opt-in named gcloud configuration for the off-box uploader (2.1.1, `openclaw offbox-identity`):
    # the answer, and what the kit CREATED ({name, account, impersonate, project}; empty when it created
    # nothing). Teardown deletes only a configuration recorded here that still matches.
    host_offbox_identity: bool = False
    host_offbox_config_created: dict[str, str] = field(default_factory=dict)
    # What the lanes writer created (ADR-0007), so teardown removes exactly that and nothing else:
    # {created_agent_ids, created_workspaces, allow_agents_added: {orchestrator: [ids]},
    #  allow_agents_absent: [orchestrator], catalog_added: [model refs], deferred: [paths]}.
    # Additive: absent from the file while empty.
    lanes_applied: dict[str, Any] = field(default_factory=dict)
    # Channel intent (ADR-0010 N5, plan S11): `channels_seen` are the "<channel>:<account>" keys that were enabled
    # when setup last listed them; `channels_wanted` the ones the operator left ticked. A key seen and not wanted is
    # a planned DISABLE. Never written to the file (`_to_dict` always strips both); no schema bump.
    channels_wanted: list[str] = field(default_factory=list)
    channels_seen: list[str] = field(default_factory=list)


@dataclass
class SetupState:
    schema_version: str = SCHEMA_VERSION
    last_run: str = ""
    mode: str = "single-model"        # single-model | multi-model
    # Which gateway serves multi-model routing. Meaningless in single-model.
    # litellm    = self-hosted gateway this kit installs and supervises
    # openrouter = hosted gateway; nothing to install, one key, no lifecycle
    backend: str = "litellm"          # litellm | openrouter
    # "multi-model via your CLI subscriptions" (ADR-0008): several models spread across the subscription CLIs
    # (Claude Code, Antigravity, Codex when allowed), no gateway. `mode` stays `single-model` so no gateway code
    # path can engage; this flag only names the team. Additive: absent from the file while False, so an old file
    # loads and round-trips unchanged and keeps its meaning.
    cli_team: bool = False
    cli_team_api: bool = False
    # Round 3 (ADR-0015): each cockpit's own auth, {cockpit: {"method": str, "providers": {provider: method}}}. Method
    # names and provider ids only, never a key or a token. Additive: absent from the file while empty, no schema bump.
    cockpit_auth: dict[str, Any] = field(default_factory=dict)
    cockpits: dict[str, CockpitState] = field(default_factory=dict)
    litellm: LiteLLMState = field(default_factory=LiteLLMState)
    providers: dict[str, ProviderState] = field(default_factory=dict)
    profile: ProfileState = field(default_factory=ProfileState)
    smoke_tests: dict[str, Any] = field(default_factory=lambda: {"last_run": "", "status": "unknown"})
    tracking: InstallTracking = field(default_factory=InstallTracking)
    openclaw: OpenClawState = field(default_factory=OpenClawState)
    # What the user wants (providers, slots, primary), as a plain dict (see ai_resources.selection).
    # None on a host that never made a selection: readers derive the implicit four Claude slots, so
    # existing setups keep producing byte-identical output, and the key is not written at all.
    selection: Any = None

    def get_selection(self):
        """The recorded Selection, or None (callers use `selection.implicit_selection()` for the default)."""
        from .. import selection as _sel
        return _sel.Selection.from_dict(self.selection)

    def set_selection(self, sel) -> None:
        self.selection = sel.to_dict() if sel is not None else None


def _to_dict(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        doc = {k: _to_dict(v) for k, v in asdict(obj).items()}
        if isinstance(obj, SetupState) and not doc.get("cli_team"):
            doc.pop("cli_team", None)                      # additive field: absent until the CLI mode is chosen
        if isinstance(obj, SetupState) and not doc.get("cli_team_api"):
            doc.pop("cli_team_api", None)                  # additive field: absent until the CLI API mode is chosen
        if isinstance(obj, SetupState) and not doc.get("cockpit_auth"):
            doc.pop("cockpit_auth", None)                  # round 3: absent until a cockpit answered its auth
        if isinstance(obj, SetupState) and isinstance(doc.get("profile"), dict) \
                and doc["profile"].get("composed") is None:
            doc["profile"].pop("composed", None)       # additive field: absent until a composed team exists
        if isinstance(obj, SetupState) and isinstance(doc.get("profile"), dict) \
                and not doc["profile"].get("allow_unverified"):
            doc["profile"].pop("allow_unverified", None)   # additive field: absent until the opt-in is recorded
        if isinstance(obj, SetupState) and isinstance(doc.get("tracking"), dict) \
                and not doc["tracking"].get("cost_migrations_applied"):
            doc["tracking"].pop("cost_migrations_applied", None)   # additive field: absent until a migration ran
        if isinstance(obj, SetupState) and isinstance(doc.get("profile"), dict):
            for key in ("lanes", "lane_acks", "roles_seen"):   # additive fields: absent until lanes / a table are recorded
                if not doc["profile"].get(key):
                    doc["profile"].pop(key, None)
        if isinstance(obj, SetupState) and isinstance(doc.get("openclaw"), dict) \
                and not doc["openclaw"].get("cost_migrations_applied"):
            doc["openclaw"].pop("cost_migrations_applied", None)   # additive field: absent until a migration ran
        if isinstance(obj, SetupState) and isinstance(doc.get("openclaw"), dict) \
                and not doc["openclaw"].get("slack_cost_changes"):
            doc["openclaw"].pop("slack_cost_changes", None)        # additive field: absent until the writer ran
        if isinstance(obj, SetupState) and isinstance(doc.get("tracking"), dict):
            for key in ("hygiene_disabled", "claude_md_migrations", "persona_domains_active", "agy_mcp_added"):
                if not doc["tracking"].get(key):
                    doc["tracking"].pop(key, None)         # round 2 (ADR-0014): absent until the writer ran
        if isinstance(obj, SetupState) and isinstance(doc.get("openclaw"), dict) \
                and not doc["openclaw"].get("host_heartbeat_off"):
            doc["openclaw"].pop("host_heartbeat_off", None)           # round 2: absent until the answer is Yes
        if isinstance(obj, SetupState) and isinstance(doc.get("openclaw"), dict) \
                and not doc["openclaw"].get("notification_route_changes"):
            doc["openclaw"].pop("notification_route_changes", None)    # round 2: absent until the route ran
        if isinstance(obj, SetupState) and isinstance(doc.get("openclaw"), dict) \
                and not doc["openclaw"].get("mcp_deny_added"):
            doc["openclaw"].pop("mcp_deny_added", None)            # additive field: absent until the opt-in scoping ran
        if isinstance(obj, SetupState) and isinstance(doc.get("openclaw"), dict) \
                and not doc["openclaw"].get("lanes_applied"):
            doc["openclaw"].pop("lanes_applied", None)
        if isinstance(obj, SetupState) and isinstance(doc.get("openclaw"), dict):
            # Channel intent lives for one run only (ADR-0010 C6): never serialized, so no save, mid-run or final, can
            # persist it, whatever the in-memory lists hold.
            for key in ("channels_wanted", "channels_seen"):
                doc["openclaw"].pop(key, None)
            # Image fields are additive: absent while they hold their defaults, so a state saved before this change
            # round-trips byte for byte.
            for key, default in IMAGE_STATE_DEFAULTS.items():
                if doc["openclaw"].get(key) == default:
                    doc["openclaw"].pop(key, None)
        return doc
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_dict(v) for v in obj]
    return obj


def _from_dict(cls: type, data: dict) -> Any:
    """Recursively rebuild a dataclass instance from a dict.

    Note: with `from __future__ import annotations`, fld.type is a string,
    not the actual class. We must resolve it via the dataclass's default
    factory, or hardcode known nested types. Below uses an explicit map
    for the known dataclasses — the simplest and most reliable approach.
    """
    if not isinstance(data, dict):
        return cls()

    NESTED: dict[type, dict[str, type]] = {
        SetupState: {
            "litellm": LiteLLMState,
            "profile": ProfileState,
            "tracking": InstallTracking,
            "openclaw": OpenClawState,
        },
        LiteLLMState: {
            "local": LiteLLMLocal,
            "remote": LiteLLMRemote,
        },
    }
    DICT_OF: dict[type, dict[str, type]] = {
        SetupState: {
            "cockpits": CockpitState,
            "providers": ProviderState,
        },
    }

    nested_map = NESTED.get(cls, {})
    dict_map = DICT_OF.get(cls, {})

    fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
    kwargs = {}
    for name in fields:
        if name not in data:
            continue
        val = data[name]
        if name in nested_map:
            kwargs[name] = _from_dict(nested_map[name], val) if isinstance(val, dict) else nested_map[name]()
        elif name in dict_map and isinstance(val, dict):
            sub_cls = dict_map[name]
            kwargs[name] = {k: _from_dict(sub_cls, v) for k, v in val.items()}
        else:
            kwargs[name] = val
    return cls(**kwargs)


class StateCorrupt(RuntimeError):
    """The state file exists and is not valid YAML (or not a mapping): a save would replace it with an empty
    record. Unattended writers refuse; `setup` says so and, interactively, goes on from the backup (3d)."""


_replace_corrupt_ok: str | None = None   # the state path `allow_replacing_corrupt()` was called for
_corrupt_notice: str | None = None   # the backup path of the corrupt file `load()` found, until it is taken


def _is_copy_of(target: Path, data: bytes) -> bool:
    """True when `target` is a regular file (not a link, a FIFO, a device or a directory) holding exactly `data`.
    It is opened `O_NOFOLLOW|O_NONBLOCK` and its type and size are read from the descriptor first, so a FIFO planted
    at a backup name cannot block the reader (2.2e, 4b)."""
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(data):
            return False
        chunks: list[bytes] = []
        remaining = len(data) + 1                   # one byte more than expected: a file that grew is not a copy
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks) == data
    except OSError:
        return False
    finally:
        os.close(fd)


def _backup_corrupt(path: Path) -> str | None:
    """Copy the unreadable state file beside itself, once: `<name>.corrupt`, `.corrupt-1`, ... created
    exclusively (`O_EXCL|O_NOFOLLOW`, 0600). A copy with the same bytes that is already there is reused. A write
    that fails (a full disk) removes the partial copy and answers None: `load()` and the refusals say that no copy
    could be made, they do not raise."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    for n in range(20):
        target = path.with_name(path.name + ".corrupt" + (f"-{n}" if n else ""))
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            if _is_copy_of(target, data):
                return str(target)
            continue
        except OSError:
            return None
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except OSError:
            try:
                os.unlink(target)
            except OSError:
                pass
            return None
        return str(target)
    return None


def _corrupt_state(path: Path) -> bool:
    """True when `path` is a regular file that does not parse as a YAML mapping."""
    if yaml is None or not path.is_file():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError):
        return True
    except OSError:
        return False
    return data is not None and not isinstance(data, dict)


def save_would_refuse() -> bool:
    """True when `save()` would raise StateCorrupt right now (the state file is unreadable and replacing it was
    not allowed). Writers that have side effects before their save check this first."""
    real = os.path.realpath(state_path())
    return _replace_corrupt_ok != real and _corrupt_state(Path(real))


def _copy_sentence() -> str:
    """The sentence a refusal says about the copy of the unreadable file, made right now (the verbs that refuse
    do so before any `load()`, so nobody else has copied it): exclusive and 0600, once per distinct content."""
    backup = _backup_corrupt(state_path())
    if backup:
        return f"a copy was made beside it as {Path(backup).name}"
    return "no copy could be made beside it"


def refusal_text() -> str:
    return (f"refusing: {state_path()} is not valid YAML ({_copy_sentence()}); nothing was changed. Fix or "
            "remove the file, or run `ai-resources setup` interactively.")


def take_corrupt_notice() -> str | None:
    """The backup path of a corrupt state file `load()` found since the last call (None when there was none).
    `setup` prints it once; the file itself stays where it is until a save is allowed to replace it."""
    global _corrupt_notice
    notice, _corrupt_notice = _corrupt_notice, None
    return notice


def allow_replacing_corrupt() -> None:
    """The operator was told that the state file is unreadable and its content is backed up: a save may now
    replace it. Only an interactive `setup` calls this."""
    global _replace_corrupt_ok
    _replace_corrupt_ok = os.path.realpath(state_path())


def load() -> SetupState:
    """Load setup state from disk; return empty SetupState if absent.

    A file that is not valid YAML is copied aside once (0600) and read as empty; a later `save()` refuses to
    replace it (StateCorrupt) until `allow_replacing_corrupt()`, so a writer nobody is watching cannot turn
    the operator's record into an empty one."""
    global _corrupt_notice
    path = state_path()
    if not path.is_file() or yaml is None:
        return SetupState()
    # One predicate for "corrupt" with `save()` (`_corrupt_state`): anything that is not a mapping, whatever its
    # truthiness (`[]`, `0`, `false`, `''` included). Only an empty document (None) is an empty record.
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError):
        data = False
    if data is None:
        data = {}
    if not isinstance(data, dict):
        backup = _backup_corrupt(path)
        _corrupt_notice = backup or "(no backup could be written)"
        return SetupState()
    loaded = _from_dict(SetupState, data)
    loaded.tracking.install_tools_answer = None      # 2.2b: a saved install answer is never replayed
    return loaded


def save(state: SetupState) -> None:
    """Persist state to disk."""
    if yaml is None:
        return
    state.last_run = datetime.now(timezone.utc).isoformat()
    path = state_path()
    if _replace_corrupt_ok != os.path.realpath(path) and _corrupt_state(Path(os.path.realpath(path))):
        raise StateCorrupt(f"{path} is not valid YAML; {_copy_sentence()}. Nothing was written. Fix or remove "
                           "the file, or run `ai-resources setup` interactively.")
    # A state file that is a symlink (a dotfile manager) is replaced at its target, not turned into a plain file.
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = _to_dict(state)
    if doc.get("selection") is None:
        doc.pop("selection", None)
    text = yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)
    # The state records what the kit replaced on the host (previous config values, unit texts),
    # so it is owner-only. It is written to a temp file in the same directory (created 0600, so there is no
    # window where it is world-readable) and moved over the old one with os.replace: a concurrent reader gets
    # the previous file or the new one, never an empty or half-written file, and an interrupted save leaves
    # the previous file as it was.
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _remove_stale_temp_files(path)
    _settle_watch_marker(state)   # a save that records the marker's run closes the gap it was written for


def watch_marker_path() -> Path:
    """The result marker the post-setup watch leaves when it could not record its result (2.2c, 6c)."""
    path = state_path()
    return path.with_name(path.stem + ".watch-result.json")


def write_watch_marker(info: dict) -> bool:
    """Atomically write a tiny `{run_id, outcome, reason, at}` record next to the state file: a temp file in the
    same directory (created 0600) and `os.replace` over the marker's own name. The name is never resolved: a
    symlink planted there is REPLACED by the marker, never written through to its target. False when it could
    not be written. It is cleared by the save that records the same run (see `_settle_watch_marker`)."""
    import json
    path = watch_marker_path()
    keep = {k: str(info.get(k, ""))[:300] for k in ("run_id", "outcome", "reason")}
    keep["at"] = datetime.now(timezone.utc).isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(keep))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return False
    _remove_stale_temp_files(path)
    return True


_MARKER_MAX_BYTES = 8192


def read_watch_marker() -> dict | None:
    """The marker, or None when there is none, it is not a regular file this user owns (a symlink, a directory,
    a file of another user), or it is not a record of strings. Read only; never raises."""
    import json
    path = watch_marker_path()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None
        raw = os.read(fd, _MARKER_MAX_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # the file is plain text next to the state: nothing but printable characters is ever shown to the operator
    return {k: "".join(ch for ch in str(data.get(k, ""))[:300] if ch.isprintable())
            for k in ("run_id", "outcome", "reason", "at")}


def clear_watch_marker() -> None:
    """Remove the marker itself: a symlink at its name is unlinked, never its target (a directory is left)."""
    path = watch_marker_path()
    try:
        if stat.S_ISDIR(os.lstat(path).st_mode):
            return
        os.unlink(path)
    except OSError:
        pass


def _settle_watch_marker(state: "SetupState") -> None:
    """Called by a successful `save()`: the marker goes away only when this save RECORDS the run it describes
    (the saved `config_watch` has the marker's run id and a result). Any other save (an executors
    regeneration, a setup step) leaves it: it reports a revert that was never recorded, and a save that did not
    record it must not hide it. Whatever is not a marker (a link, a directory, a file that is not a record) is
    junk and goes at the next save."""
    path = watch_marker_path()
    try:
        os.lstat(path)
    except OSError:
        return
    marker = read_watch_marker()
    if marker is None:
        clear_watch_marker()
        return
    cw = getattr(getattr(state, "openclaw", None), "config_watch", None) or {}
    if marker.get("run_id") and cw.get("run_id") == marker["run_id"] and cw.get("done") and cw.get("result"):
        clear_watch_marker()


STALE_TEMP_AGE_S = 3600


def remove_stale_temp_files(path: Path) -> None:
    """The cleanup `save()` runs, for any file the kit writes through `mkstemp` beside it (`kit-host.env`, an
    `AGENTS.md`): `.<name>.*.tmp` next to `path` that a SIGKILLed write left behind."""
    _remove_stale_temp_files(path)


def _remove_stale_temp_files(path: Path) -> None:
    """Remove `.<state file>.*.tmp` files a SIGKILLed save left behind: regular files this user owns, untouched
    for over an hour (a younger one may be a save in flight). Never a link, never anything else, never the
    state file itself."""
    import time
    prefix, now = f".{path.name}.", time.time()
    try:
        names = os.listdir(path.parent)
    except OSError:
        return
    for name in names:
        if not (name.startswith(prefix) and name.endswith(".tmp")) or len(name) <= len(prefix) + 4:
            continue
        candidate = path.parent / name
        try:
            st = os.lstat(candidate)
            if (stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and now - st.st_mtime > STALE_TEMP_AGE_S):
                os.unlink(candidate)
        except OSError:
            continue


class StateLockUnusable(RuntimeError):
    """The lock file exists but cannot be opened (a directory, no permission, a dangling link)."""


class StateLockBusy(RuntimeError):
    """Another run holds the setup-state lock."""


_held = 0          # how many `exclusive()` blocks of THIS process are open (the lock is not re-entrant)


@contextmanager
def exclusive():
    """Hold an advisory lock on the setup state across load -> modify -> save.

    Not blocking: a second run that finds it held is refused at once (it would otherwise load a stale record and
    overwrite what the first run writes). The lock file sits next to the state file and is never removed. It is
    opened with O_NOFOLLOW: a symlink at that name is refused (StateLockUnusable), never followed."""
    import fcntl
    global _held
    path = state_path()
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as e:
        raise StateLockUnusable(f"{lock} is a symlink" if lock.is_symlink() else f"{lock}: {type(e).__name__}"
                                ) from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StateLockBusy(str(lock)) from None
        _held += 1
        try:
            yield
        finally:
            _held -= 1
    finally:
        os.close(fd)


def update(mutator, *, wait_s: float = 10.0, poll_s: float = 0.2, sleep=None, monotonic=None) -> bool:
    """Re-load the state under the lock, let `mutator(fresh)` change ONLY the fields its caller owns, and save.

    For code that holds a state object for a long time (a watch that reverts for minutes, a regeneration): its
    own copy is stale by the time it wants to write, and saving it would overwrite what another run wrote in
    between. The lock is held for the load -> modify -> save only, never for the minutes before it. `mutator`
    may return False to say there is nothing to write.

    A process that already holds the lock (a setup run) just does it. Returns False when the lock could not be
    taken within `wait_s` or cannot be used: nothing was written."""
    import time as _time
    sleep = sleep or _time.sleep
    monotonic = monotonic or _time.monotonic

    def apply() -> bool:
        fresh = load()
        real = os.path.realpath(state_path())
        if _replace_corrupt_ok != real and _corrupt_state(Path(real)):
            return False                 # an unattended writer never replaces an unreadable record
        if mutator(fresh) is False:
            return True
        save(fresh)
        return True

    if _held:
        return apply()
    deadline = monotonic() + wait_s
    while True:
        try:
            with exclusive():
                return apply()
        except StateLockBusy:
            if monotonic() >= deadline:
                return False
            sleep(poll_s)
        except StateLockUnusable:
            return False


BUSY_LINE = ("refusing: another ai-resources run holds the setup-state lock; wait for it to finish and run this "
             "again")


def locked_run(fn, *, out=print, enabled: bool = True):
    """Run `fn()` under the setup-state lock; a busy or unusable lock is ONE line and rc 1, never a hang.

    `enabled=False` (a dry run) runs `fn()` as it is and creates no lock file."""
    if not enabled:
        return fn()
    try:
        with exclusive():
            return fn()
    except StateLockBusy:
        out(BUSY_LINE)
        return 1
    except StateLockUnusable as e:
        out(f"refusing: the setup-state lock file cannot be used ({e}); remove it or fix its permissions and "
            "run this again")
        return 1


def lanes_enabled(s: "SetupState") -> bool:
    """True when the state holds a non-empty lanes table (a fresh install never does)."""
    lanes = getattr(getattr(s, "profile", None), "lanes", None)
    return isinstance(lanes, dict) and bool(lanes.get("agents") or lanes.get("roles"))


def is_first_run() -> bool:
    return not state_path().is_file()
