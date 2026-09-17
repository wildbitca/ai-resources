"""OpenClaw cockpit: engine choice, the openclaw.json patch, and teardown.

No OpenClaw binary is needed: `_openclaw` is replaced by a recorder, so these
tests pin the patch the kit hands to `openclaw config patch`, not OpenClaw's
own validation of it. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import state, ui  # noqa: E402
from ai_resources.setup.cockpits import ALL, openclaw  # noqa: E402

KIT_SKILLS = "/kit/skills"


def _state(*installed: str) -> state.SetupState:
    s = state.SetupState()
    for cid in installed:
        s.cockpits[cid] = state.CockpitState(installed=True)
    return s


class _Recorder:
    """Stands in for the openclaw binary and keeps every patch it was given."""

    def __init__(self, config: pathlib.Path):
        self.config = config
        self.calls: list[tuple[list[str], dict | None]] = []

    def __call__(self, args, stdin=None, timeout=120):
        self.calls.append((args, json.loads(stdin) if stdin else None))
        if args[:2] == ["config", "file"]:
            return 0, str(self.config)
        return 0, "ok"

    def patches(self):
        return [p for a, p in self.calls if a[:2] == ["config", "patch"]]


@pytest.fixture
def jarvis(tmp_path, monkeypatch):
    """A config shaped like a real bot: OpenRouter auto, Engram already declared."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# Jarvis rules\n", encoding="utf-8")
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({
        "agents": {"defaults": {
            "workspace": str(ws),
            "model": {"primary": "openrouter/auto"},
            "models": {"openrouter/auto": {"alias": "OpenRouter"}},
        }},
        "mcp": {"servers": {"engram": {"command": "/usr/bin/engram", "args": ["mcp"]}}},
    }), encoding="utf-8")
    rec = _Recorder(cfg)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.setattr(openclaw, "_openclaw", rec)
    monkeypatch.setattr(openclaw._shared, "stable_kit_root", lambda _root: pathlib.Path("/kit"))
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: False)
    # A live gateway would answer this; without the stub every antigravity test would
    # wait out the real backend poll (see test_the_engine_patch_waits_for_the_backend).
    monkeypatch.setattr(openclaw, "backend_registered", lambda _backend: True)
    return cfg, ws, rec


@pytest.fixture
def jarvis_with_existing_extras(tmp_path, monkeypatch):
    """A config shaped like a bot that already has its own openrouter/llama-cpp plugin
    config, a hand-set-up media transcriber, another agent entry, non-default
    a restricted default allowAgents list, and a second OpenRouter
    model alias — all BEFORE the kit ever touches it.

    The `jarvis` fixture above has none of these keys, so a teardown test built on it
    only proves restoration writes `null` — the same broken restore-to-null code path
    would also pass trivially, and a broken snapshot that never captured a prior value
    in the first place would too. This fixture exists so AC-13b/AC-16's 'restored
    exactly' claim is tested against real, non-empty prior values.
    """
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# Jarvis rules\n", encoding="utf-8")
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({
        "agents": {
            "defaults": {
                "workspace": str(ws),
                "model": {"primary": "openrouter/auto"},
                "models": {
                    "openrouter/auto": {"alias": "OpenRouter"},
                    "openrouter/claude-3": {"alias": "Claude via OR"},
                },
                "subagents": {"allowAgents": ["research"]},
            },
            "entries": {"support": {"workspace": "/srv/support"}},
        },
        "mcp": {"servers": {"engram": {"command": "/usr/bin/engram", "args": ["mcp"]}}},
        "plugins": {"entries": {
            "openrouter": {"enabled": True, "apiKey": "sk-or-xxx"},
            "llama-cpp": {"enabled": True, "modelPath": "/models/llama.gguf"},
        }},
        "commands": {"plugins": {"enabled": False}},
        "tools": {"media": {
            "audio": {"enabled": False},
            "models": [{"type": "cli", "command": "/usr/local/bin/my-transcribe",
                        "args": ["{{AttachmentPath}}"]}],
        }},
    }), encoding="utf-8")
    rec = _Recorder(cfg)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.setattr(openclaw, "_openclaw", rec)
    monkeypatch.setattr(openclaw._shared, "stable_kit_root", lambda _root: pathlib.Path("/kit"))
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: False)
    # A live gateway would answer this; without the stub every antigravity test would
    # wait out the real backend poll (see test_the_engine_patch_waits_for_the_backend).
    monkeypatch.setattr(openclaw, "backend_registered", lambda _backend: True)
    return cfg, ws, rec


def test_openclaw_is_registered_last_so_its_engines_are_configured_first():
    assert list(ALL)[-1] == "openclaw"


def test_only_engines_with_an_installed_cli_are_offered():
    ids = [e.id for e in openclaw.available_engines(_state("claude"))]
    assert ids == ["claude-code", "keep"]


def test_antigravity_is_offered_once_agy_is_installed():
    ids = [e.id for e in openclaw.available_engines(_state("agy"))]
    assert ids == ["antigravity", "keep"]


def test_gemini_cli_is_never_offered_even_when_gemini_is_installed():
    ids = [e.id for e in openclaw.available_engines(_state("gemini"))]
    assert "gemini-cli" not in ids


def test_an_unattended_first_run_never_repoints_a_live_bot(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    assert openclaw.default_engine(_state("claude")) == "keep"


def test_an_interactive_run_proposes_claude_code_when_installed(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    assert openclaw.default_engine(_state("claude")) == "claude-code"


def test_claude_code_engine_pins_the_model_to_the_claude_cli_runtime():
    doc = {"mcp": {"servers": {"engram": {"command": "engram"}}}}
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 doc, KIT_SKILLS, "/bin/engram")
    assert patch["agents"]["defaults"]["model"] == {"primary": "anthropic/claude-sonnet-5"}
    assert patch["agents"]["defaults"]["models"]["anthropic/claude-sonnet-5"] == {
        "agentRuntime": {"id": "claude-cli"}}


def test_engram_is_added_when_missing_because_cli_runtimes_run_with_strict_mcp_config():
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 {}, KIT_SKILLS, "/bin/engram")
    assert patch["mcp"]["servers"]["engram"]["command"] == "/bin/engram"


def test_an_existing_engram_server_is_left_alone():
    doc = {"mcp": {"servers": {"engram": {"command": "/custom/engram"}}}}
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 doc, KIT_SKILLS, "/bin/engram")
    assert "mcp" not in patch


def test_kit_skills_left_over_from_an_old_direct_setup_are_cleaned_up():
    """The "direct" engine that used to add kit_skills to extraDirs was removed; no
    remaining engine adds it, but claude-code still cleans up a stale entry."""
    back = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                {"skills": {"load": {"extraDirs": ["/mine", KIT_SKILLS]}}},
                                KIT_SKILLS, "e")
    assert back["skills"]["load"]["extraDirs"] == ["/mine"]


def test_claude_code_never_adds_kit_skills_to_extradirs():
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 {"skills": {"load": {"extraDirs": ["/mine"]}}}, KIT_SKILLS, "e")
    assert "skills" not in patch


def test_a_runtime_pin_from_a_previous_engine_is_cleared():
    doc = {"agents": {"defaults": {"models": {
        "google/gemini-3.7-flash": {"agentRuntime": {"id": "google-gemini-cli"}},
        "openrouter/auto": {"alias": "OpenRouter"},
    }}}}
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 doc, KIT_SKILLS, "e")
    models = patch["agents"]["defaults"]["models"]
    assert models["google/gemini-3.7-flash"] == {"agentRuntime": None}
    assert "openrouter/auto" not in models


def test_configure_goes_through_openclaw_and_snapshots_what_it_replaces(jarvis):
    cfg, ws, rec = jarvis
    s = _state("claude", "openclaw")
    s.openclaw.engine, s.openclaw.model = "claude-code", "anthropic/claude-sonnet-5"

    openclaw.configure({"state": s})

    assert len(rec.patches()) == 1
    assert s.openclaw.applied
    assert s.openclaw.previous["model"] == {"primary": "openrouter/auto"}
    assert json.loads(cfg.read_text())["agents"]["defaults"]["model"]["primary"] == "openrouter/auto", \
        "the kit must never write openclaw.json itself"
    text = (ws / "AGENTS.md").read_text()
    assert "Engram is the memory of record" in text and "# Jarvis rules" in text
    assert "## Using the kit" not in text, "Claude Code already loads the kit block from CLAUDE.md"


def _antigravity_state(*extra_installed: str) -> state.SetupState:
    s = _state("agy", "claude", "openclaw", *extra_installed)
    s.openclaw.engine = "antigravity"
    s.openclaw.risk_acknowledged = True
    return s


def test_a_second_run_keeps_the_original_snapshot(jarvis, monkeypatch):
    _cfg, _ws, _rec = jarvis
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    s = _antigravity_state()
    openclaw.configure({"state": s})
    first = dict(s.openclaw.previous)
    s.openclaw.model = "gemini-3.8-flash-high"  # a second run, e.g. a different model choice
    openclaw.configure({"state": s})
    assert s.openclaw.previous == first


def test_antigravity_engine_writes_the_kit_block_into_the_workspace_agents_md(jarvis, monkeypatch):
    _cfg, ws, _rec = jarvis
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    s = _antigravity_state()
    openclaw.configure({"state": s})
    text = (ws / "AGENTS.md").read_text()
    assert "You (agy) are the orchestrator" in text and "# Jarvis rules" in text
    assert "Engram is the memory of record" in text
    assert "sessions_spawn agentId=claude" in text
    assert "--context" in text


def test_a_rejected_patch_is_not_recorded_as_applied(jarvis, monkeypatch):
    _cfg, _ws, rec = jarvis
    monkeypatch.setattr(openclaw, "apply_patch", lambda *_a, **_k: (False, "invalid"))
    s = _state("claude", "openclaw")
    s.openclaw.engine = "claude-code"
    openclaw.configure({"state": s})
    assert not s.openclaw.applied


def test_teardown_restores_the_snapshot_and_removes_the_kit_block(jarvis, monkeypatch):
    _cfg, ws, rec = jarvis
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    s = _antigravity_state()
    openclaw.configure({"state": s})

    openclaw.teardown(s)

    restore = rec.patches()[-1]
    assert restore["agents"]["defaults"]["model"] == {"primary": "openrouter/auto"}
    assert "mcp" not in restore, "Engram existed before the kit and must survive teardown"
    assert "ai-resources" not in (ws / "AGENTS.md").read_text()
    assert not s.openclaw.applied


def test_openclaw_state_survives_a_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")
    s = state.SetupState()
    s.openclaw.engine, s.openclaw.applied = "claude-code", True
    s.openclaw.previous = {"model": {"primary": "openrouter/auto"}}
    state.save(s)
    loaded = state.load()
    assert isinstance(loaded.openclaw, state.OpenClawState)
    assert loaded.openclaw.engine == "claude-code"
    assert loaded.openclaw.previous == {"model": {"primary": "openrouter/auto"}}



def test_every_engine_makes_engram_the_memory_of_record(jarvis, monkeypatch):
    _cfg, ws, _rec = jarvis
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    for engine in ("claude-code", "antigravity"):
        s = _state("claude", "openclaw", "agy")
        s.openclaw.engine = engine
        s.openclaw.risk_acknowledged = True
        openclaw.configure({"state": s})
        assert "Engram is the memory of record" in (ws / "AGENTS.md").read_text(), engine


def test_the_chosen_model_joins_an_existing_allowlist():
    doc = {"agents": {"defaults": {"modelPolicy": {"allow": ["openrouter/auto"]}}}}
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 doc, KIT_SKILLS, "e")
    assert patch["agents"]["defaults"]["modelPolicy"] == {
        "allow": ["openrouter/auto", "anthropic/claude-sonnet-5"]}


@pytest.mark.parametrize("doc", [
    {},
    {"agents": {"defaults": {"modelPolicy": {"allow": ["anthropic/*"]}}}},
])
def test_no_allowlist_change_when_absent_or_already_covered(doc):
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 doc, KIT_SKILLS, "e")
    assert "modelPolicy" not in patch["agents"]["defaults"]


# --- antigravity engine (S5) ------------------------------------------------------

def test_never_offers_the_flash_medium_model():
    assert "gemini-3.8-flash-medium" not in openclaw.ENGINES["antigravity"].models


# --- S2: the two antigravity quota pools ------------------------------------------

def test_antigravity_tuple_holds_at_least_one_id_from_each_pool():
    models = openclaw.ENGINES["antigravity"].models
    assert models[0] == "gemini-3.8-flash-low", "default is unchanged"
    assert any(m.startswith("gemini-") for m in models)
    assert any(m.startswith("claude-") or m.startswith("gpt-") for m in models)


def test_build_patch_with_a_claude_gpt_pool_model_yields_a_two_segment_ref():
    gemini_patch = openclaw.build_patch(
        openclaw.ENGINES["antigravity"], "gemini-3.8-flash-low", {}, KIT_SKILLS, "e",
        worker_model="claude-sonnet-5",
    )
    claude_patch = openclaw.build_patch(
        openclaw.ENGINES["antigravity"], "claude-sonnet-4-6", {}, KIT_SKILLS, "e",
        worker_model="claude-sonnet-5",
    )
    assert claude_patch["agents"]["defaults"]["model"]["primary"] == "agy-cli/claude-sonnet-4-6"
    # Rest of the patch is byte-identical to the gemini case once the primary ref differs.
    gemini_patch["agents"]["defaults"]["model"]["primary"] = "SENTINEL"
    claude_patch["agents"]["defaults"]["model"]["primary"] = "SENTINEL"
    assert gemini_patch == claude_patch


def test_applied_engine_resolves_a_bare_claude_sonnet_4_6_to_antigravity_never_claude_code():
    """Regression test only (no code change): claude-code and codex ids are namespaced
    (`anthropic/...`, `openai/...`), so a bare vendor id landing in the antigravity tuple
    is safe today only because of that. Pins the invariant `applied_engine()` relies on
    (its model-string-membership scan, `openclaw.py`) for whoever changes it next."""
    s = state.SetupState()
    s.openclaw.applied = True
    s.openclaw.model = "claude-sonnet-4-6"
    assert openclaw.applied_engine(s) is openclaw.ENGINES["antigravity"]


def test_non_antigravity_model_labels_are_byte_identical_to_today(monkeypatch):
    seen = {}

    def select(_msg, choices, default=None, **_k):
        seen["choices"] = [(c.title if hasattr(c, "title") else c["title"]) for c in choices]
        return default

    monkeypatch.setattr(ui, "select", select)
    monkeypatch.setattr(ui, "text", lambda _msg, default="", **_k: default)
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    s = _state("claude")
    s.openclaw.engine = "claude-code"
    openclaw._prompt_engine(s)
    assert seen["choices"] == list(openclaw.ENGINES["claude-code"].models)


def test_antigravity_model_labels_carry_the_pool_name_value_stays_bare_id(monkeypatch):
    seen = {}

    def select(_msg, choices, default=None, **_k):
        if "engine" in _msg.lower():
            return "antigravity"
        seen["choices"] = choices
        return default

    monkeypatch.setattr(ui, "select", select)
    monkeypatch.setattr(ui, "text", lambda _msg, default="", **_k: default)
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    monkeypatch.setattr(openclaw.detection, "_which_extra", lambda _b: "")
    s = _state("agy", "claude")
    s.openclaw.engine = "antigravity"
    openclaw._prompt_engine(s)
    labels = {c.value: c.title for c in seen["choices"]}
    assert labels["gemini-3.8-flash-low"] == "gemini-3.8-flash-low — Gemini Models pool"
    assert labels["claude-sonnet-4-6"] == "claude-sonnet-4-6 — Claude and GPT models pool"
    assert [c.value for c in seen["choices"]] == list(openclaw.ENGINES["antigravity"].models)


# --- S3: live pool state at antigravity model-choice time -------------------------

def test_s3_warns_once_for_a_zero_percent_pool_and_still_offers_all_choices(monkeypatch):
    from ai_resources.setup.cockpits import _agy_quota

    warnings, details = [], []
    monkeypatch.setattr(ui, "warn", lambda msg: warnings.append(msg))
    monkeypatch.setattr(ui, "detail", lambda msg: details.append(msg))
    monkeypatch.setattr(ui, "text", lambda _msg, default="", **_k: default)
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(openclaw.detection, "_which_extra", lambda _b: "/usr/local/bin/agy")

    pools = [
        _agy_quota.Pool(_agy_quota.POOL_GEMINI, 0, "2026-09-24T15:45:01Z"),
        _agy_quota.Pool(_agy_quota.POOL_CLAUDE_GPT, 81, "2026-09-24T21:01:59Z"),
    ]
    monkeypatch.setattr(_agy_quota, "read_usage", lambda **_kw: (pools, ""))

    offered = {}

    def select(_msg, choices, default=None, **_k):
        if "engine" in _msg.lower():
            return "antigravity"
        offered["values"] = [c.value for c in choices]
        return default

    monkeypatch.setattr(ui, "select", select)
    s = _state("agy", "claude")
    s.openclaw.engine = "antigravity"
    openclaw._prompt_engine(s, dry_run=False)

    # Only the quota warning below — not the unrelated risk-acknowledgement warning
    # `_prompt_engine` also emits for the antigravity engine.
    quota_warnings = [w for w in warnings if "remaining" in w]
    assert len(quota_warnings) == 1
    assert "Gemini Models" in quota_warnings[0] and "Claude and GPT" in quota_warnings[0]
    assert offered["values"] == list(openclaw.ENGINES["antigravity"].models)


def test_s3_skips_the_read_under_non_interactive(monkeypatch):
    from ai_resources.setup.cockpits import _agy_quota

    calls = []
    monkeypatch.setattr(_agy_quota, "read_usage", lambda **_kw: calls.append(1) or ([], ""))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    monkeypatch.setattr(openclaw.detection, "_which_extra", lambda _b: "/usr/local/bin/agy")
    monkeypatch.setattr(ui, "select", lambda _msg, choices, default=None, **_k: default)
    monkeypatch.setattr(ui, "text", lambda _msg, default="", **_k: default)
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    s = _state("agy", "claude")
    s.openclaw.engine = "antigravity"
    openclaw._prompt_engine(s, dry_run=False)
    assert calls == []


def test_s3_skips_the_read_under_dry_run(monkeypatch):
    from ai_resources.setup.cockpits import _agy_quota

    calls = []
    monkeypatch.setattr(_agy_quota, "read_usage", lambda **_kw: calls.append(1) or ([], ""))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(openclaw.detection, "_which_extra", lambda _b: "/usr/local/bin/agy")
    monkeypatch.setattr(ui, "select", lambda _msg, choices, default=None, **_k: default)
    monkeypatch.setattr(ui, "text", lambda _msg, default="", **_k: default)
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    s = _state("agy", "claude")
    s.openclaw.engine = "antigravity"
    openclaw._prompt_engine(s, dry_run=True)
    assert calls == []


def test_s3_agy_absent_prints_nothing_extra_and_completes(monkeypatch):
    from ai_resources.setup.cockpits import _agy_quota

    calls = []
    monkeypatch.setattr(_agy_quota, "read_usage", lambda **_kw: calls.append(1) or ([], ""))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(openclaw.detection, "_which_extra", lambda _b: "")
    monkeypatch.setattr(ui, "select", lambda _msg, choices, default=None, **_k: default)
    monkeypatch.setattr(ui, "text", lambda _msg, default="", **_k: default)
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    s = _state("agy", "claude")
    s.openclaw.engine = "antigravity"
    openclaw._prompt_engine(s, dry_run=False)
    assert calls == []


def test_a_saved_direct_engine_is_not_offered_and_falls_back_to_keep_with_a_warning(monkeypatch):
    warnings = []
    monkeypatch.setattr(ui, "warn", lambda msg: warnings.append(msg))
    s = _state("claude")
    s.openclaw.engine = "direct"
    assert openclaw.default_engine(s) == "keep"
    assert warnings and "direct" in warnings[0].lower()


def test_ac13_antigravity_build_patch_shape():
    """Given engine antigravity and a config with several topic-bound agents."""
    doc = {"agents": {"entries": {
        "main": {}, "snoutzone": {"subagents": {"allowAgents": ["codex"]}},
        "elinvo": {}, "devops": {}, "X": {},
    }}}
    patch = openclaw.build_patch(
        openclaw.ENGINES["antigravity"], "gemini-3.8-flash-low", doc, KIT_SKILLS, "e",
        openrouter_enabled=False, worker_model="claude-sonnet-5",
        python3_bin="/usr/bin/python3", agy_bin="/kit/bin/agy",
    )
    assert patch["agents"]["defaults"]["model"] == {"primary": "agy-cli/gemini-3.8-flash-low"}
    entries = patch["agents"]["entries"]
    for name in ("main", "elinvo", "devops", "X"):
        assert entries[name]["subagents"]["allowAgents"] == ["claude"], name
    assert entries["snoutzone"]["subagents"]["allowAgents"] == ["codex", "claude"]

    claude_entry = entries["claude"]
    # `openclaw config patch --dry-run` rejects an agent-level `agentRuntime`, a
    # three-segment model ref, and `tools.exec.enabled`/`ask: false` (verified against
    # the live 2026.9.4 schema); the backend comes from the ref's first segment.
    assert "agentRuntime" not in claude_entry
    assert claude_entry["model"] == {"primary": "claude-kit/claude-sonnet-5"}
    assert claude_entry["tools"]["exec"] == {"mode": "full"}
    assert claude_entry["workspace"] == str(pathlib.Path.home() / "Development")

    # Voice notes are NOT part of the engine patch: `_openclaw_voice` owns tools.media
    # for every engine, so the two never fight over the same key.
    assert "tools" not in patch

    assert patch["plugins"]["entries"]["openrouter"]["enabled"] is False
    assert "channels" not in patch



def test_ac13b_openrouter_plugin_toggle_applies_to_every_engine():
    doc = {"agents": {"defaults": {"models": {"openrouter/auto": {"alias": "x"}}}}}

    enabled = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                   doc, KIT_SKILLS, "e", openrouter_enabled=True)
    assert enabled["plugins"]["entries"]["openrouter"]["enabled"] is True
    assert "openrouter/auto" not in enabled["agents"]["defaults"].get("models", {})

    disabled = openclaw.build_patch(
        openclaw.ENGINES["antigravity"], "gemini-3.8-flash-low", doc, KIT_SKILLS, "e",
        openrouter_enabled=False, worker_model="claude-sonnet-5",
    )
    assert disabled["plugins"]["entries"]["openrouter"]["enabled"] is False
    assert disabled["agents"]["defaults"]["models"]["openrouter/auto"] is None


def test_ac14_no_patch_ever_mentions_channels_or_the_legacy_voice_chain():
    doc = {}
    for engine_id in ("claude-code", "codex", "antigravity"):
        kwargs = {"openrouter_enabled": True}
        if engine_id == "antigravity":
            kwargs.update(worker_model="claude-sonnet-5")
        model = openclaw.ENGINES[engine_id].models[0]
        patch = openclaw.build_patch(openclaw.ENGINES[engine_id], model, doc, KIT_SKILLS, "e", **kwargs)
        assert "channels" not in patch, engine_id
        dumped = json.dumps(patch)
        assert "openclaw-transcribe" not in dumped
        assert "whisper" not in dumped
        assert "{{MediaPath}}" not in dumped


def test_ac15_configure_skips_the_patch_when_risk_is_not_acknowledged(jarvis, monkeypatch):
    _cfg, _ws, rec = jarvis
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    s = _state("agy", "claude", "openclaw")
    s.openclaw.engine = "antigravity"
    assert s.openclaw.risk_acknowledged is False

    openclaw.configure({"state": s})

    assert rec.patches() == []
    assert not s.openclaw.applied


def test_ac16_teardown_restores_plugins_subagents_and_the_claude_agent(jarvis, monkeypatch):
    _cfg, ws, rec = jarvis
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    s = _antigravity_state()
    openclaw.configure({"state": s})
    assert s.openclaw.plugin_linked is True

    removed = openclaw.teardown(s)

    restore = rec.patches()[-1]
    assert restore["plugins"]["entries"]["openrouter"] is None
    assert restore["plugins"]["entries"]["llama-cpp"] is None
    assert restore["agents"]["entries"] is None
    assert "plugin:ai-resources" in removed
    assert not s.openclaw.applied
    assert s.openclaw.plugin_linked is False

    plugin_calls = [a for a, _p in rec.calls if a[:2] == ["plugins", "uninstall"]]
    assert plugin_calls == [["plugins", "uninstall", "ai-resources"]]


def test_ac13b_and_ac16_preexisting_config_is_restored_exactly_by_teardown(
        jarvis_with_existing_extras, monkeypatch):
    """AC-13b + AC-16, against real prior values (see jarvis_with_existing_extras):
    the openrouter plugin config (including its apiKey), the untouched llama-cpp entry,
    another agent's entry, the restricted default allowAgents list, both OpenRouter
    model aliases, and the hand-set-up media transcriber must all come
    back byte-for-byte, not as nulls."""
    _cfg, _ws, rec = jarvis_with_existing_extras
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    s = _antigravity_state()  # single-model default -> openrouter_selected(s) is False

    openclaw.configure({"state": s})
    assert s.openclaw.applied

    openclaw.teardown(s)
    restore = rec.patches()[-1]

    assert restore["plugins"]["entries"]["openrouter"] == {"enabled": True, "apiKey": "sk-or-xxx"}
    assert restore["plugins"]["entries"]["llama-cpp"] == {"enabled": True, "modelPath": "/models/llama.gguf"}
    assert restore["agents"]["entries"] == {"support": {"workspace": "/srv/support"}}
    assert restore["agents"]["defaults"]["subagents"] == {"allowAgents": ["research"]}
    assert restore["agents"]["defaults"]["models"] == {
        "openrouter/auto": {"alias": "OpenRouter"},
        "openrouter/claude-3": {"alias": "Claude via OR"},
    }
    assert restore["agents"]["defaults"]["model"] == {"primary": "openrouter/auto"}
    # The antigravity patch no longer writes agents.defaults.agentRuntime (the schema
    # rejects it), so teardown has nothing of ours to restore there.
    assert "agentRuntime" not in restore["agents"]["defaults"]


def test_ac16_teardown_only_unlinks_the_plugin_if_the_kit_linked_it(jarvis, monkeypatch):
    _cfg, _ws, rec = jarvis
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    s = _state("claude", "openclaw")
    s.openclaw.engine = "claude-code"
    openclaw.configure({"state": s})
    assert s.openclaw.plugin_linked is False

    openclaw.teardown(s)
    plugin_calls = [a for a, _p in rec.calls if a[:2] == ["plugins", "uninstall"]]
    assert plugin_calls == []


def test_configure_registers_the_agy_mcp_bridge_once_when_no_entry_preexisted(jarvis, monkeypatch):
    _cfg, _ws, _rec = jarvis
    calls = []
    monkeypatch.setattr(openclaw, "_agy", lambda args, timeout=30: (calls.append(args), (1, ""))[1]
                        if args[:2] == ["mcp", "list"] else (calls.append(args), (0, ""))[1])
    s = _antigravity_state()
    openclaw.configure({"state": s})
    add_calls = [c for c in calls if c[:2] == ["mcp", "add"]]
    assert len(add_calls) == 1
    assert add_calls[0][2] == "openclaw"
    assert s.openclaw.previous["agy_mcp_bridge_preexisted"] is False


def test_configure_does_not_overwrite_a_preexisting_agy_mcp_entry(jarvis, monkeypatch):
    _cfg, _ws, _rec = jarvis
    calls = []

    def fake_agy(args, timeout=30):
        calls.append(args)
        if args[:2] == ["mcp", "list"]:
            return 0, "openclaw  python3  /some/bridge.py"
        return 0, ""

    monkeypatch.setattr(openclaw, "_agy", fake_agy)
    s = _antigravity_state()
    openclaw.configure({"state": s})
    add_calls = [c for c in calls if c[:2] == ["mcp", "add"]]
    assert add_calls == []
    assert s.openclaw.previous["agy_mcp_bridge_preexisted"] is True

    calls.clear()
    openclaw.teardown(s)
    remove_calls = [c for c in calls if c[:2] == ["mcp", "remove"]]
    assert remove_calls == [], "a pre-existing agy MCP entry must never be removed by teardown"


def test_configure_resolves_agy_and_claude_bins_via_which_extra_not_plain_which(jarvis, monkeypatch):
    """Finding 4: shutil.which alone misses ~/.local/bin and fnm shims — the same dirs
    detect_agy() already checks via detection._which_extra."""
    _cfg, _ws, rec = jarvis
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    monkeypatch.setattr(openclaw.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        openclaw.detection, "_which_extra",
        lambda name: {"agy": "/home/user/.local/bin/agy", "claude": "/home/user/.local/bin/claude"}.get(name, ""),
    )
    s = _antigravity_state()
    openclaw.configure({"state": s})
    patch = rec.patches()[0]
    plugin_config = patch["plugins"]["entries"]["ai-resources"]["config"]
    assert plugin_config["agyBin"] == "/home/user/.local/bin/agy"
    assert plugin_config["claudeBin"] == "/home/user/.local/bin/claude"


def test_configure_does_not_record_the_bridge_marker_on_registration_failure_and_warns(jarvis, monkeypatch):
    """Finding 4: a failed registration (e.g. agy not resolvable) must not be recorded
    as 'preexisted', or the next run's guard would skip retrying forever, silently."""
    _cfg, _ws, _rec = jarvis
    warnings = []
    monkeypatch.setattr(ui, "warn", lambda msg: warnings.append(msg))

    def failing_agy(args, timeout=30):
        if args[:2] == ["mcp", "list"]:
            return 1, ""  # no existing "openclaw" entry
        return 127, "agy not found on PATH"  # `mcp add` itself fails

    monkeypatch.setattr(openclaw, "_agy", failing_agy)
    s = _antigravity_state()
    openclaw.configure({"state": s})
    assert "agy_mcp_bridge_preexisted" not in s.openclaw.previous
    assert any("agy MCP bridge" in w for w in warnings)

    # A second run must retry registration, not skip it because of a stale marker.
    add_calls = []

    def now_working_agy(args, timeout=30):
        if args[:2] == ["mcp", "list"]:
            return 1, ""
        add_calls.append(args)
        return 0, ""

    monkeypatch.setattr(openclaw, "_agy", now_working_agy)
    openclaw.configure({"state": s})
    assert len(add_calls) == 1
    assert s.openclaw.previous["agy_mcp_bridge_preexisted"] is False


def test_switching_away_from_antigravity_restores_its_keys_unlinks_plugin_and_unregisters_bridge(jarvis, monkeypatch):
    """Finding 5: a re-run that picks a different engine after antigravity must not
    leave agents.entries.claude, the ai-resources plugin
    config, the linked plugin or the registered agy MCP bridge behind — restoration
    must be keyed on what was actually applied, not on the *current* engine choice."""
    _cfg, ws, rec = jarvis
    bridge_calls = []

    def fake_agy(args, timeout=30):
        bridge_calls.append(args)
        if args[:2] == ["mcp", "list"]:
            return 1, ""
        return 0, ""

    monkeypatch.setattr(openclaw, "_agy", fake_agy)
    s = _antigravity_state()
    openclaw.configure({"state": s})
    assert s.openclaw.antigravity_applied is True
    assert s.openclaw.plugin_linked is True
    assert len([c for c in bridge_calls if c[:2] == ["mcp", "add"]]) == 1

    bridge_calls.clear()
    s.openclaw.engine = "claude-code"
    openclaw.configure({"state": s})

    restore = rec.patches()[-1]
    assert restore["agents"]["entries"] is None
    assert restore["plugins"]["entries"]["ai-resources"] is None
    # The newly selected engine's own patch still applies alongside the restore.
    assert restore["agents"]["defaults"]["model"] == {"primary": "anthropic/claude-sonnet-5"}

    assert s.openclaw.antigravity_applied is False
    assert s.openclaw.plugin_linked is False
    assert [c for c in bridge_calls if c[:2] == ["mcp", "remove"]] == [["mcp", "remove", "openclaw"]]
    plugin_calls = [a for a, _p in rec.calls if a[:2] == ["plugins", "uninstall"]]
    assert plugin_calls == [["plugins", "uninstall", "ai-resources"]]

    # A later teardown() must not restore the antigravity-only keys a second time —
    # that already happened during the switch above.
    rec.calls.clear()
    openclaw.teardown(s)
    final = rec.patches()[-1]
    assert "entries" not in final["agents"]
    assert "commands" not in final
    assert "tools" not in final


def test_the_claude_worker_agent_never_gets_operator_admin():
    patch = openclaw.build_patch(
        openclaw.ENGINES["antigravity"], "gemini-3.8-flash-low", {}, KIT_SKILLS, "e",
        openrouter_enabled=False, worker_model="claude-sonnet-5",
    )
    dumped = json.dumps(patch)
    assert "operator.admin" not in dumped
    assert "operator" not in json.dumps(patch["agents"]["entries"]["claude"])


# --- S6: single-model / multi-model parity (AC-18) ---------------------------------
# Proof command per the plan: `pytest tests/test_openclaw_cockpit.py -k parity`. Every
# test name below carries "parity" so that selector actually picks them up — `-k ac18`
# selected zero tests before this fix (the word "parity" only appeared in a comment).

def test_ac18_parity_single_model_and_multimodel_litellm_produce_identical_antigravity_patches():
    doc = {}
    single = state.SetupState()
    single.mode = "single-model"
    multi = state.SetupState()
    multi.mode, multi.backend = "multi-model", "litellm"

    def patch_for(s):
        return openclaw.build_patch(
            openclaw.ENGINES["antigravity"], "gemini-3.8-flash-low", doc, KIT_SKILLS, "e",
            openrouter_enabled=openclaw.openrouter_selected(s),
            worker_model="claude-sonnet-5",
        )

    assert patch_for(single) == patch_for(multi)


def test_ac18_parity_openrouter_backend_alone_flips_the_openrouter_plugin_on():
    openrouter = state.SetupState()
    openrouter.mode, openrouter.backend = "multi-model", "openrouter"
    litellm_mode = state.SetupState()
    litellm_mode.mode, litellm_mode.backend = "multi-model", "litellm"
    assert openclaw.openrouter_selected(openrouter) is True
    assert openclaw.openrouter_selected(litellm_mode) is False


def _stub_ui_answers_for_prompt(monkeypatch):
    """Drive `openclaw.prompt()` without a terminal: every prompt just returns its own
    default, the way an operator accepting every suggested answer would."""
    monkeypatch.setattr(ui, "select", lambda _msg, _choices, default=None, **_k: default)
    monkeypatch.setattr(ui, "text", lambda _msg, default="", **_k: default)
    monkeypatch.setattr(ui, "confirm", lambda _msg, default=True, **_k: True)
    monkeypatch.setattr(ui, "detail", lambda *_a, **_k: None)
    monkeypatch.setattr(ui, "warn", lambda *_a, **_k: None)
    # S3's live quota read must never touch a real agy binary from a test.
    monkeypatch.setattr(openclaw.detection, "_which_extra", lambda _binary: "")


def test_ac18_parity_prompt_plus_configure_produce_the_same_patch_and_never_touch_claude_settings_json(
        monkeypatch, tmp_path):
    """Given single-model and multi-model:litellm modes, when prompt + build_patch run
    (through the real `openclaw.prompt()`, not just `build_patch` called by hand), then
    the resulting patches are identical and the OpenClaw configurator writes nothing to
    `~/.claude/settings.json` — that file belongs to the Claude Code cockpit, never to
    this one, in either mode."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    cfg_path = tmp_path / "openclaw.json"
    cfg_path.write_text("{}", encoding="utf-8")
    rec = _Recorder(cfg_path)
    monkeypatch.setattr(openclaw, "_openclaw", rec)
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    monkeypatch.setattr(openclaw._shared, "stable_kit_root", lambda _root: pathlib.Path("/kit"))
    # As in the other fixtures: a live gateway answers this, and without the stub the
    # backend poll would run for real in both modes.
    monkeypatch.setattr(openclaw, "backend_registered", lambda _backend: True)
    _stub_ui_answers_for_prompt(monkeypatch)

    patches = {}
    for mode in ("single-model", "multi-model"):
        s = _state("agy", "claude", "openclaw")
        s.mode = mode
        if mode == "multi-model":
            s.backend = "litellm"
        openclaw.prompt(s)
        assert s.openclaw.engine == "antigravity"
        assert s.openclaw.risk_acknowledged is True

        rec.calls.clear()
        openclaw.configure({"state": s})
        patches[mode] = rec.patches()[0]

    assert patches["single-model"] == patches["multi-model"]
    settings_path = tmp_path / ".claude" / "settings.json"
    assert not settings_path.exists()


def test_step7_cockpit_config_prompts_openclaw_in_both_modes(monkeypatch):
    from ai_resources.setup import wizard

    prompted = []
    monkeypatch.setattr(openclaw, "prompt", lambda s, **_k: prompted.append(s.mode))
    monkeypatch.setattr(ui, "checkbox", lambda *_a, **_k: ["openclaw"])

    for mode in ("single-model", "multi-model"):
        s = _state("openclaw")
        s.mode = mode
        wizard._step7_cockpit_config(s)

    assert prompted == ["single-model", "multi-model"]


class _FakeConsole:
    """Stand-in for `ui.console()`, which hard-requires rich (absent in CI)."""

    def print(self, *_a, **_k):
        pass


def test_teardown_multi_model_preserves_the_tool_install_record_and_openclaw_state(monkeypatch):
    from ai_resources.setup import wizard, litellm

    monkeypatch.setattr(ui, "console", lambda: _FakeConsole())
    monkeypatch.setattr(litellm, "plan_multi_model_teardown",
                        lambda _prev: [("pipx_uninstall", "Uninstall LiteLLM")])
    monkeypatch.setattr(litellm, "execute_multi_model_teardown",
                        lambda _prev: [("pipx_uninstall", True, "removed")])
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)

    prev = state.SetupState()
    prev.mode = "multi-model"
    prev.tracking.tools_installed_by_us = ["agy"]
    prev.tracking.rc_lines_added = {"agy": ['export AGY_HOME="$HOME/.agy"']}
    prev.tracking.install_tools_answer = "yes"
    prev.openclaw.engine = "antigravity"
    prev.openclaw.applied = True

    s = state.SetupState()
    s.mode = "single-model"
    s.tracking = state.InstallTracking(
        tools_installed_by_us=list(prev.tracking.tools_installed_by_us),
        rc_lines_added=dict(prev.tracking.rc_lines_added),
        install_tools_answer=prev.tracking.install_tools_answer,
    )
    s.openclaw = prev.openclaw

    rc = wizard._teardown_multi_model(prev, s)
    assert rc == 0
    # The LiteLLM-specific fields were reset (a fresh InstallTracking()'s defaults)...
    assert s.tracking.litellm_installed_by_us is False
    # ...but the tool-install record (S4, mode-agnostic) survived the reset.
    assert s.tracking.tools_installed_by_us == ["agy"]
    assert s.tracking.rc_lines_added == {"agy": ['export AGY_HOME="$HOME/.agy"']}
    assert s.tracking.install_tools_answer == "yes"
    # OpenClaw's own orchestration state is untouched by multi-model teardown entirely.
    assert s.openclaw.engine == "antigravity"
    assert s.openclaw.applied is True


def test_step9_openclaw_status_line_is_empty_when_not_applied():
    from ai_resources.setup import wizard
    assert wizard._openclaw_status_line(state.SetupState()) == ""


def test_step9_openclaw_status_line_shows_engine_model_and_flags():
    from ai_resources.setup import wizard
    s = state.SetupState()
    s.openclaw.applied = True
    s.openclaw.engine = "antigravity"
    s.openclaw.model = "gemini-3.8-flash-low"
    s.openclaw.worker_model = "claude-sonnet-5"
    s.openclaw.plugin_linked = True
    s.openclaw.risk_acknowledged = True
    line = wizard._openclaw_status_line(s)
    assert "antigravity" in line and "gemini-3.8-flash-low" in line
    assert "claude-sonnet-5" in line
    assert "plugin linked: True" in line and "risk acknowledged: True" in line


def test_step9_openclaw_status_line_omits_worker_for_non_antigravity_engines():
    from ai_resources.setup import wizard
    s = state.SetupState()
    s.openclaw.applied = True
    s.openclaw.engine = "claude-code"
    s.openclaw.model = "anthropic/claude-sonnet-5"
    line = wizard._openclaw_status_line(s)
    assert "worker" not in line


def test_the_engine_patch_waits_for_the_backend_and_restarts_the_gateway(jarvis, monkeypatch):
    """`config patch` validates model refs, so `agy-cli/<model>` must already be
    registered: a fresh run otherwise dies with "Unknown model: agy-cli/…" (seen on a
    real setup run, 2026-09-17). The plugin is linked, then the gateway restarted, before
    the patch goes out."""
    cfg, _ws, rec = jarvis
    seen = {"restarted": False, "checks": 0}

    def backend(_b):
        seen["checks"] += 1
        return seen["restarted"]

    monkeypatch.setattr(openclaw, "backend_registered", backend)
    monkeypatch.setattr(openclaw.time, "sleep", lambda _s: None)
    monkeypatch.setattr(openclaw, "register_agy_mcp_bridge", lambda *_a: (True, False, ""))
    def record(args, stdin=None, timeout=120):
        if args[:2] == ["gateway", "restart"]:
            seen["restarted"] = True
        return rec(args, stdin, timeout)

    monkeypatch.setattr(openclaw, "_openclaw", record)
    assert openclaw.configure({"state": _antigravity_state()})
    calls = [c[0] for c in rec.calls]
    link = next(i for i, c in enumerate(calls) if c[:2] == ["plugins", "install"])
    restart = next(i for i, c in enumerate(calls) if c[:2] == ["gateway", "restart"])
    patched = next(i for i, c in enumerate(calls) if c[:2] == ["config", "patch"])
    assert link < restart < patched, calls
    assert seen["checks"] >= 2


def test_the_engine_patch_is_skipped_when_the_backend_never_registers(jarvis, monkeypatch):
    monkeypatch.setattr(openclaw, "backend_registered", lambda _b: False)
    monkeypatch.setattr(openclaw.time, "sleep", lambda _s: None)
    monkeypatch.setattr(openclaw, "register_agy_mcp_bridge", lambda *_a: (True, False, ""))
    cfg, _ws, rec = jarvis
    openclaw.configure({"state": _antigravity_state()})
    assert not any(c[0][:2] == ["config", "patch"] for c in rec.calls), rec.calls


def test_backend_registered_reads_the_plugin_runtime_not_models_list(monkeypatch):
    """`openclaw models list` lists provider models and never prints CLI-backend refs, so
    it reports a loaded backend as missing (real run, 2026-09-17). The readiness check
    must come from the plugin's own runtime inspection."""
    seen: list[list[str]] = []

    def fake(args, stdin=None, timeout=120):
        seen.append(args)
        if args[:2] == ["plugins", "inspect"]:
            return 0, json.dumps({"plugin": {"status": "loaded",
                                             "cliBackendIds": ["agy-cli", "claude-kit"]}})
        return 0, "anthropic/claude-sonnet-5\ngoogle/gemini-3.8-flash\n"

    monkeypatch.setattr(openclaw, "_openclaw", fake)
    assert openclaw.backend_registered("agy-cli") is True
    assert not any(a[:2] == ["models", "list"] for a in seen), seen


@pytest.mark.parametrize("payload", [
    {"plugin": {"status": "loaded", "cliBackendIds": ["claude-kit"]}},   # other backend only
    {"plugin": {"status": "error", "cliBackendIds": ["agy-cli"]}},       # loaded it is not
    {"plugin": {}},
    {},
])
def test_backend_registered_is_false_for_anything_short_of_a_loaded_backend(monkeypatch, payload):
    monkeypatch.setattr(openclaw, "_openclaw",
                        lambda *_a, **_k: (0, json.dumps(payload)))
    assert openclaw.backend_registered("agy-cli") is False


def test_backend_registered_survives_junk_output(monkeypatch):
    for rc, out in ((1, "gateway down"), (0, "not json at all"), (0, "")):
        monkeypatch.setattr(openclaw, "_openclaw", lambda *_a, rc=rc, out=out, **_k: (rc, out))
        assert openclaw.backend_registered("agy-cli") is False


def _inspect_reply(monkeypatch, plugin: dict, restart_rc: int = 0, on_restart=None):
    """Stub `_openclaw` so `plugins inspect` answers with `plugin` and restarts are seen."""
    calls: list[list[str]] = []

    def fake(args, stdin=None, timeout=120):
        calls.append(args)
        if args[:2] == ["plugins", "inspect"]:
            return 0, json.dumps({"plugin": plugin() if callable(plugin) else plugin})
        if args[:2] == ["gateway", "restart"]:
            if on_restart:
                on_restart()
            return restart_rc, "restarted" if restart_rc == 0 else "boom"
        return 0, "ok"

    monkeypatch.setattr(openclaw, "_openclaw", fake)
    monkeypatch.setattr(openclaw.time, "sleep", lambda _s: None)
    return calls


def test_plugin_is_stale_when_the_gateway_runs_another_directory(monkeypatch, tmp_path):
    """`brew upgrade` moves the kit; a gateway still running the previous Cellar path
    answers "Unknown CLI backend" to every message until it restarts (2026-09-17)."""
    linked = tmp_path / "1.7.2" / "openclaw-plugin" / "ai-resources"
    stale = tmp_path / "1.7.1" / "openclaw-plugin" / "ai-resources"
    for d in (linked, stale):
        d.mkdir(parents=True)
    _inspect_reply(monkeypatch, {"status": "loaded", "cliBackendIds": ["agy-cli"],
                                 "rootDir": str(stale)})
    assert openclaw.plugin_is_stale(str(linked)) is True
    _inspect_reply(monkeypatch, {"status": "loaded", "cliBackendIds": ["agy-cli"],
                                 "rootDir": str(linked)})
    assert openclaw.plugin_is_stale(str(linked)) is False


def test_plugin_is_stale_is_false_when_the_runtime_says_nothing(monkeypatch):
    _inspect_reply(monkeypatch, {})
    assert openclaw.plugin_is_stale("/kit/openclaw-plugin/ai-resources") is False


def test_restart_gateway_reports_success_once_the_backend_is_back(monkeypatch):
    state_ = {"up": False}
    calls = _inspect_reply(
        monkeypatch,
        lambda: {"status": "loaded", "cliBackendIds": ["agy-cli"]} if state_["up"] else {},
        on_restart=lambda: state_.__setitem__("up", True),
    )
    assert openclaw.restart_gateway() is True
    assert ["gateway", "restart"] in calls


def test_restart_gateway_reports_failure_when_the_command_fails(monkeypatch):
    _inspect_reply(monkeypatch, {}, restart_rc=1)
    assert openclaw.restart_gateway() is False


def test_restart_gateway_reports_failure_when_the_backend_never_returns(monkeypatch):
    _inspect_reply(monkeypatch, {"status": "loaded", "cliBackendIds": []})
    assert openclaw.restart_gateway() is False


def test_a_stale_plugin_triggers_a_restart_even_though_the_backend_answers(jarvis, monkeypatch):
    """The stale gateway still reports the backend, so readiness alone is not enough."""
    cfg, _ws, rec = jarvis
    seen = {"restarts": 0}
    monkeypatch.setattr(openclaw, "backend_registered", lambda _b: True)
    monkeypatch.setattr(openclaw, "plugin_is_stale", lambda _d: seen["restarts"] == 0)
    monkeypatch.setattr(openclaw, "register_agy_mcp_bridge", lambda *_a: (True, False, ""))

    def restart(_backend="agy-cli"):
        seen["restarts"] += 1
        return True

    monkeypatch.setattr(openclaw, "restart_gateway", restart)
    assert openclaw._register_plugin_and_backends(_antigravity_state(), "/kit") is True
    assert seen["restarts"] == 1


def test_s3_passes_the_resolved_agy_path_not_a_bare_name(monkeypatch):
    """agy commonly lives only in ~/.local/bin, which _which_extra() finds but PATH
    may not carry. Falling back to a bare "agy" would make the annotation vanish
    silently on exactly the machines the extra-bin lookup exists for."""
    from ai_resources.setup.cockpits import _agy_quota

    seen = {}

    def read_usage(agy_bin=None, **_kw):
        seen["agy_bin"] = agy_bin
        return [], ""

    monkeypatch.setattr(_agy_quota, "read_usage", read_usage)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "warn", lambda _msg: None)
    monkeypatch.setattr(ui, "detail", lambda _msg: None)
    monkeypatch.setattr(ui, "text", lambda _msg, default="", **_k: default)
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    monkeypatch.setattr(openclaw.detection, "_which_extra",
                        lambda _b: "/home/someone/.local/bin/agy")

    def select(_msg, choices, default=None, **_k):
        return "antigravity" if "engine" in _msg.lower() else default

    monkeypatch.setattr(ui, "select", select)
    s = _state("agy", "claude")
    s.openclaw.engine = "antigravity"
    openclaw._prompt_engine(s, dry_run=False)
    assert seen["agy_bin"] == "/home/someone/.local/bin/agy"


def test_s3_zero_percent_warning_never_points_at_an_equally_dead_pool(monkeypatch):
    from ai_resources.setup.cockpits import _agy_quota

    warnings = []
    pools = [
        _agy_quota.Pool(_agy_quota.POOL_GEMINI, 0, "2026-09-24T15:45:01Z"),
        _agy_quota.Pool(_agy_quota.POOL_CLAUDE_GPT, 0, "2026-09-24T21:01:59Z"),
    ]
    monkeypatch.setattr(_agy_quota, "read_usage", lambda **_kw: (pools, ""))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "warn", lambda msg: warnings.append(msg))
    monkeypatch.setattr(ui, "detail", lambda _msg: None)
    monkeypatch.setattr(ui, "text", lambda _msg, default="", **_k: default)
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    monkeypatch.setattr(openclaw.detection, "_which_extra", lambda _b: "/usr/local/bin/agy")

    def select(_msg, choices, default=None, **_k):
        return "antigravity" if "engine" in _msg.lower() else default

    monkeypatch.setattr(ui, "select", select)
    s = _state("agy", "claude")
    s.openclaw.engine = "antigravity"
    openclaw._prompt_engine(s, dry_run=False)

    quota_warnings = [w for w in warnings if "remaining" in w]
    assert len(quota_warnings) == 2
    for w in quota_warnings:
        assert "Every other pool is spent too" in w
        assert "Re-run `ai-resources setup` and pick" not in w
