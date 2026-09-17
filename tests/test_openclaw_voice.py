"""OpenClaw voice notes in setup: the question, the tools.media patch, snapshot and teardown.

Like test_openclaw_cockpit.py, `_openclaw` is a recorder, so these tests pin what the kit
hands to `openclaw config patch`. Nothing is installed or downloaded. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import hashlib
import io
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import state, ui  # noqa: E402
from ai_resources.setup.cockpits import _openclaw_voice as voice, openclaw  # noqa: E402

HAND_BUILT = {
    "models": [{"type": "cli", "command": "/home/me/.local/bin/openclaw-transcribe",
                "args": ["{{AttachmentPath}}"], "capabilities": ["audio"], "timeoutSeconds": 180}],
    "audio": {"enabled": True, "echoTranscript": True, "timeoutSeconds": 180},
}
IMAGE_MODEL = {"type": "provider", "provider": "openai", "model": "gpt-5", "capabilities": ["image"]}


class _Recorder:
    def __init__(self, config: pathlib.Path):
        self.config = config
        self.calls: list[tuple[list[str], dict | None]] = []

    def __call__(self, args, stdin=None, timeout=120):
        self.calls.append((args, json.loads(stdin) if stdin else None))
        if args[:2] == ["config", "file"]:
            return 0, str(self.config)
        return 0, "ok"

    def patches(self):
        return [(a, p) for a, p in self.calls if a[:2] == ["config", "patch"]]


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """A bot with the hand-built voice setup and a hand-written voice rule."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "AGENTS.md").write_text(
        "# AGENTS.md\n\n## Voice notes\n\nAct on transcripts.\n\n### Detail\n\nmore\n\n"
        "## Telegram topic routing\n\nRead TOPIC-ROUTING.md.\n", encoding="utf-8")
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({"agents": {"defaults": {"workspace": str(ws)}},
                               "tools": {"media": HAND_BUILT}}), encoding="utf-8")
    rec = _Recorder(cfg)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.setattr(openclaw, "_openclaw", rec)
    monkeypatch.setattr(openclaw._shared, "stable_kit_root", lambda _root: pathlib.Path("/kit"))
    monkeypatch.setattr(voice, "ensure_local_engine", lambda required: True)
    monkeypatch.setattr(voice, "ensure_glossary", lambda dest=None: False)
    monkeypatch.setattr(voice, "interpreter", lambda _ak: "/kit/venv/bin/python")
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: True)
    return cfg, ws, rec


def _state(mode: str, language: str = "es") -> state.SetupState:
    s = state.SetupState()
    s.cockpits["openclaw"] = state.CockpitState(installed=True)
    s.openclaw.engine, s.openclaw.voice, s.openclaw.voice_language = "keep", mode, language
    return s


# --- the question --------------------------------------------------------------------

def test_an_unattended_first_run_never_changes_how_a_live_bot_hears(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    assert voice.default_mode(state.SetupState()) == "keep"


@pytest.mark.parametrize("has_key, expected", [(True, "cloud"), (False, "local")])
def test_an_interactive_first_run_proposes_cloud_only_with_a_key(monkeypatch, has_key, expected):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(voice, "has_openrouter_key", lambda: has_key)
    assert voice.default_mode(state.SetupState()) == expected


def test_the_saved_answer_wins(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    s = state.SetupState()
    s.openclaw.voice = "local"
    assert voice.default_mode(s) == "local"


def test_openclaw_prompt_asks_about_voice_notes(monkeypatch):
    asked = []
    monkeypatch.setattr(openclaw, "_prompt_engine", lambda s: None)
    monkeypatch.setattr(ui, "select", lambda msg, choices, default=None, **_k: asked.append(msg) or "local")
    monkeypatch.setattr(ui, "text", lambda msg, default="", **_k: "es")
    s = state.SetupState()
    openclaw.prompt(s)
    assert asked == ["Voice notes: how should OpenClaw transcribe them?"]
    assert (s.openclaw.voice, s.openclaw.voice_language) == ("local", "es")


# --- tools.media -----------------------------------------------------------------------

def test_the_media_patch_runs_the_kit_transcriber_with_mode_and_language():
    media = voice.build_media("cloud", "/py", "/kit/t.py", "es", HAND_BUILT)
    assert media["models"] == [{
        "type": "cli", "command": "/py",
        "args": ["/kit/t.py", "--mode", "cloud", "--language", "es", "{{AttachmentPath}}"],
        "capabilities": ["audio"], "timeoutSeconds": 180,
    }], "the hand-built audio entry is replaced, not kept alongside"
    assert media["audio"] == {"enabled": True, "echoTranscript": True, "timeoutSeconds": 180}


def test_models_for_other_capabilities_survive():
    media = voice.build_media("local", "/py", "/kit/t.py", "auto",
                              {"models": [IMAGE_MODEL, *HAND_BUILT["models"]]})
    assert media["models"][1:] == [IMAGE_MODEL]


def test_off_only_disables_audio():
    assert voice.build_media("off", "/py", "/kit/t.py", "es", HAND_BUILT) == {"audio": {"enabled": False}}


def test_configure_patches_tools_media_and_snapshots_what_it_replaces(bot):
    cfg, _ws, rec = bot
    s = _state("cloud")
    openclaw.configure({"state": s})

    [(args, patch)] = rec.patches()
    entry = patch["tools"]["media"]["models"][0]
    assert entry["args"][0] == "/kit/scripts/ai_resources/voice/openclaw_transcribe.py"
    assert entry["args"][1:5] == ["--mode", "cloud", "--language", "es"]
    assert s.openclaw.voice_applied == "cloud"
    assert s.openclaw.voice_previous == HAND_BUILT
    assert json.loads(cfg.read_text())["tools"]["media"] == HAND_BUILT, \
        "the kit must never write openclaw.json itself"


def test_a_second_run_keeps_the_original_snapshot(bot):
    s = _state("cloud")
    openclaw.configure({"state": s})
    s.openclaw.voice = "local"
    openclaw.configure({"state": s})
    assert s.openclaw.voice_previous == HAND_BUILT and s.openclaw.voice_applied == "local"


def test_keep_changes_nothing(bot):
    _cfg, ws, rec = bot
    before = (ws / "AGENTS.md").read_text()
    s = _state("keep")
    openclaw.configure({"state": s})
    assert rec.patches() == [] and (ws / "AGENTS.md").read_text() == before


def test_a_local_engine_that_cannot_be_installed_leaves_voice_untouched(bot, monkeypatch):
    _cfg, _ws, rec = bot
    monkeypatch.setattr(voice, "ensure_local_engine", lambda required: not required)
    s = _state("local")
    openclaw.configure({"state": s})
    assert rec.patches() == [] and not s.openclaw.voice_applied


def test_a_rejected_voice_patch_is_not_recorded(bot, monkeypatch):
    monkeypatch.setattr(openclaw, "apply_patch", lambda *_a, **_k: (False, "invalid"))
    s = _state("cloud")
    openclaw.configure({"state": s})
    assert not s.openclaw.voice_applied and s.openclaw.voice_previous is None


# --- workspace AGENTS.md -----------------------------------------------------------------

def test_the_voice_rule_moves_into_the_kit_block_once(bot):
    _cfg, ws, _rec = bot
    s = _state("cloud")
    openclaw.configure({"state": s})
    openclaw.configure({"state": s})
    text = (ws / "AGENTS.md").read_text()
    assert text.count("## Voice notes") == 1
    assert text.index("## Voice notes") < text.index("<!-- END ai-resources -->")
    assert "Act on transcripts." not in text and "### Detail" not in text
    assert "## Telegram topic routing" in text
    assert list(ws.glob("AGENTS.md.ai-resources-backup-*")), "the hand-written original is backed up"


def test_a_declined_handover_keeps_the_users_section_and_no_duplicate(bot, monkeypatch):
    _cfg, ws, _rec = bot
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: False)
    openclaw.configure({"state": _state("cloud")})
    text = (ws / "AGENTS.md").read_text()
    assert text.count("## Voice notes") == 1 and "Act on transcripts." in text


def test_unattended_runs_never_take_over_the_users_section(bot, monkeypatch):
    _cfg, ws, _rec = bot
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    openclaw.configure({"state": _state("local")})
    text = (ws / "AGENTS.md").read_text()
    assert text.count("## Voice notes") == 1 and "Act on transcripts." in text


def test_off_takes_the_voice_rule_out_of_the_block(bot):
    _cfg, ws, _rec = bot
    s = _state("cloud")
    openclaw.configure({"state": s})
    s.openclaw.voice = "off"
    openclaw.configure({"state": s})
    assert "## Voice notes" not in (ws / "AGENTS.md").read_text()


def test_the_engine_block_and_the_voice_rule_share_one_block(bot, monkeypatch):
    _cfg, ws, _rec = bot
    s = _state("cloud")
    s.cockpits["claude"] = state.CockpitState(installed=True)
    s.openclaw.engine, s.openclaw.model = "claude-code", "anthropic/claude-sonnet-5"
    openclaw.configure({"state": s})
    text = (ws / "AGENTS.md").read_text()
    assert text.count("<!-- BEGIN ai-resources") == 1
    assert "Engram is the memory of record" in text and "## Voice notes" in text

    # A later run that keeps the engine (and declines restoring it) still rebuilds the block with it.
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: False)
    s.openclaw.engine, s.openclaw.voice = "keep", "local"
    openclaw.configure({"state": s})
    assert "Engram is the memory of record" in (ws / "AGENTS.md").read_text()


# --- teardown ----------------------------------------------------------------------------

def test_teardown_restores_the_previous_tools_media_exactly(bot):
    _cfg, ws, rec = bot
    s = _state("cloud")
    openclaw.configure({"state": s})

    openclaw.teardown(s)

    args, patch = rec.patches()[-1]
    assert patch == {"tools": {"media": HAND_BUILT}}
    assert args[-2:] == ["--replace-path", "tools.media"]
    assert "ai-resources" not in (ws / "AGENTS.md").read_text()
    assert s.openclaw == state.OpenClawState()


def test_teardown_deletes_tools_media_the_kit_created(bot):
    cfg, ws, rec = bot
    cfg.write_text(json.dumps({"agents": {"defaults": {"workspace": str(ws)}}}))
    s = _state("local")
    openclaw.configure({"state": s})
    openclaw.teardown(s)
    assert rec.patches()[-1][1] == {"tools": {"media": None}}


def test_keep_offers_to_restore_voice_without_touching_the_engine(bot):
    _cfg, _ws, rec = bot
    s = _state("cloud")
    openclaw.configure({"state": s})
    s.openclaw.voice = "keep"
    openclaw.configure({"state": s})
    assert rec.patches()[-1][1] == {"tools": {"media": HAND_BUILT}}
    assert not s.openclaw.voice_applied


def test_voice_state_survives_a_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")
    s = state.SetupState()
    s.openclaw.voice, s.openclaw.voice_language, s.openclaw.voice_applied = "cloud", "es", "cloud"
    s.openclaw.voice_previous = HAND_BUILT
    state.save(s)
    loaded = state.load().openclaw
    assert (loaded.voice, loaded.voice_language, loaded.voice_applied) == ("cloud", "es", "cloud")
    assert loaded.voice_previous == HAND_BUILT


# --- local engine files --------------------------------------------------------------------

def test_an_existing_glossary_is_never_overwritten(tmp_path):
    dest = tmp_path / "glossary.txt"
    dest.write_text("my terms")
    assert voice.ensure_glossary(dest) is False
    assert dest.read_text() == "my terms"


def test_a_missing_glossary_gets_the_kit_default(tmp_path):
    dest = tmp_path / "voice" / "glossary.txt"
    assert voice.ensure_glossary(dest) is True
    assert "OpenClaw" in dest.read_text()


class _Download(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_the_model_lands_only_after_its_checksum_matches(tmp_path, monkeypatch):
    payload = b"model bytes"
    monkeypatch.setattr(voice.urllib.request, "urlopen", lambda *_a, **_k: _Download(payload))
    dest = tmp_path / "ggml.bin"
    assert voice.ensure_model(dest, "https://x", hashlib.sha256(payload).hexdigest())
    assert dest.read_bytes() == payload and not (tmp_path / "ggml.bin.part").exists()


def test_a_truncated_download_leaves_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(voice.urllib.request, "urlopen", lambda *_a, **_k: _Download(b"trunc"))
    dest = tmp_path / "ggml.bin"
    assert not voice.ensure_model(dest, "https://x", hashlib.sha256(b"full model").hexdigest())
    assert not dest.exists() and not (tmp_path / "ggml.bin.part").exists()


def test_a_verified_model_is_not_downloaded_again(tmp_path, monkeypatch):
    dest = tmp_path / "ggml.bin"
    dest.write_bytes(b"ok")

    def no_network(*_a, **_k):
        raise AssertionError("must not download")

    monkeypatch.setattr(voice.urllib.request, "urlopen", no_network)
    assert voice.ensure_model(dest, "https://x", hashlib.sha256(b"ok").hexdigest())


def test_cloud_tolerates_a_missing_local_fallback_but_local_does_not(monkeypatch):
    monkeypatch.setattr(voice, "ensure_binaries", lambda: False)
    assert voice.ensure_local_engine(required=False) is True
    assert voice.ensure_local_engine(required=True) is False
