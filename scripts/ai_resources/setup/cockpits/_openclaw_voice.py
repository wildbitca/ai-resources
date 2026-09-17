"""OpenClaw voice notes: which engine transcribes audio attachments before the agent sees them.

    cloud   an audio-native model through OpenRouter, with local whisper.cpp as the fallback
    local   whisper.cpp only; audio never leaves the machine (the transcript's text-only
            correction pass still uses OpenRouter or Claude Code when available)
    off     audio transcription disabled
    keep    change nothing

Both transcribing modes run the kit's `voice/openclaw_transcribe.py` as a `tools.media`
CLI model, so the chain and its measured defaults live in one place.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from .. import credentials, state, ui
from ...voice import openclaw_transcribe as transcriber

MODES = {
    "cloud": "Cloud — OpenRouter audio model (needs OPENROUTER_API_KEY), local whisper.cpp fallback",
    "local": "Local only — whisper.cpp on this machine",
    "off": "Off — do not transcribe voice notes",
    "keep": "Keep the current voice-note setup — change nothing",
}
# Homebrew formula → the binary the transcriber looks for.
LOCAL_FORMULAS = {"ffmpeg": "ffmpeg", "whisper-cpp": "whisper-cli"}
TIMEOUT_SECONDS = 180
DEFAULT_GLOSSARY = Path(transcriber.__file__).with_name("glossary.txt")

VOICE_MD = """## Voice notes

The user may talk to you with voice notes. An `[Audio]` block with a `Transcript:` is the user's
message, already transcribed: treat it exactly like typed text and act on it immediately — answer
the question, run the task, make the change.

- Do not ask the user to confirm what the transcript says, and do not repeat it back before acting.
- Do not re-transcribe the audio yourself with whisper or any other tool; the gateway already did.
- Transcripts can contain small errors (a misheard word, a wrong technical term). Infer the most
  likely intent from context and proceed; mention the assumption in one short clause only if it
  changes what you did.
- Ask only when the transcript is `[inaudible]`, empty, or genuinely unintelligible, and then ask in
  one short line for the user to repeat it.
"""
VOICE_HEADING = "## Voice notes"


def has_openrouter_key() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY") or credentials.has_key("OPENROUTER_API_KEY"))


def default_mode(s: state.SetupState) -> str:
    """The saved answer; unattended first runs keep; otherwise cloud when a key exists."""
    if s.openclaw.voice in MODES:
        return s.openclaw.voice
    if ui.is_non_interactive():
        # Never change how a live bot hears its user from an unattended run.
        return "keep"
    return "cloud" if has_openrouter_key() else "local"


def prompt(s: state.SetupState) -> None:
    mode = ui.select(
        "Voice notes: how should OpenClaw transcribe them?",
        [ui.Choice(label, value=mid) for mid, label in MODES.items()],
        default=default_mode(s),
    )
    s.openclaw.voice = mode
    if mode == "cloud" and not has_openrouter_key():
        ui.warn("No OPENROUTER_API_KEY in the environment or the kit credentials: "
                "every note will fall back to local whisper.cpp until one is added.")
    if mode in ("cloud", "local"):
        s.openclaw.voice_language = ui.text(
            "Language spoken in voice notes (ISO code such as es or en, or auto):",
            default=s.openclaw.voice_language or "auto",
        ).strip() or "auto"


# --- tools.media -------------------------------------------------------------------

def _is_audio_only_cli(entry: Any) -> bool:
    return (isinstance(entry, dict) and entry.get("type") == "cli"
            and entry.get("capabilities") == ["audio"])


def build_media(mode: str, python: str, script: str, language: str, current: Any) -> dict:
    """The tools.media value for `mode`. Models for other capabilities are kept."""
    if mode == "off":
        return {"audio": {"enabled": False}}
    others = [m for m in ((current or {}).get("models") or []) if not _is_audio_only_cli(m)]
    entry = {
        "type": "cli",
        "command": python,
        "args": [script, "--mode", mode, "--language", language, "{{AttachmentPath}}"],
        "capabilities": ["audio"],
        "timeoutSeconds": TIMEOUT_SECONDS,
    }
    return {
        "models": [entry, *others],
        "audio": {"enabled": True, "echoTranscript": True, "timeoutSeconds": TIMEOUT_SECONDS},
    }


def interpreter(ak_path: str) -> str:
    """The kit's own Python when installed by Homebrew (stable across upgrades), else this one."""
    venv = Path(ak_path) / "venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


# --- local engine ------------------------------------------------------------------

def missing_formulas() -> list[str]:
    return [f for f, binary in LOCAL_FORMULAS.items() if not transcriber.find_binary(binary)]


def ensure_binaries() -> bool:
    missing = missing_formulas()
    if not missing:
        return True
    brew = transcriber.find_binary("brew")
    if not brew:
        ui.error(f"Voice notes need {', '.join(missing)}; install them (Homebrew not found).")
        return False
    ui.info(f"Installing {', '.join(missing)} with Homebrew…")
    try:
        r = subprocess.run([brew, "install", *missing], capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        ui.error(f"brew install {' '.join(missing)} failed: {e}")
        return False
    if r.returncode != 0 or missing_formulas():
        ui.error(f"brew install {' '.join(missing)} failed: {(r.stdout + r.stderr)[-400:]}")
        return False
    return True


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_model(dest: Path, url: str = transcriber.MODEL_URL,
                 expected: str = transcriber.MODEL_SHA256) -> bool:
    """Download the Whisper model once. It lands under its final name only when complete and
    verified: a truncated file under that name would make every local transcription fail."""
    if dest.is_file():
        if sha256(dest) == expected:
            return True
        ui.warn(f"{dest} does not match the expected checksum; downloading it again.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    ui.info(f"Downloading {dest.name} (~550 MB) to {dest.parent}…")
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, part.open("wb") as out:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                h.update(chunk)
                out.write(chunk)
    except OSError as e:
        part.unlink(missing_ok=True)
        ui.error(f"Whisper model download failed: {e}")
        return False
    if h.hexdigest() != expected:
        part.unlink(missing_ok=True)
        ui.error(f"Whisper model checksum mismatch (got {h.hexdigest()}); nothing installed.")
        return False
    os.replace(part, dest)
    return True


def ensure_glossary(dest: Path | None = None) -> bool:
    """Install the default glossary; a file that exists is the user's and is never replaced."""
    dest = dest or transcriber.glossary_path()
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_GLOSSARY, dest)
    return True


def ensure_local_engine(required: bool) -> bool:
    """whisper.cpp, ffmpeg and the model. `required` is False for cloud, where they are the fallback."""
    ok = ensure_binaries() and ensure_model(transcriber.models_dir() / transcriber.MODEL_FILE)
    if not ok and not required:
        ui.warn("Voice notes: the local fallback is not ready; cloud transcription still works.")
        return True
    return ok


# --- workspace AGENTS.md -----------------------------------------------------------

def hand_written_section(text: str) -> bool:
    """A `## Voice notes` section outside the kit's managed block."""
    from ._shared import _managed_block_span
    lines = text.splitlines(keepends=True)
    begin, end = _managed_block_span(lines)
    if begin is not None and end is not None:
        lines = lines[:begin] + lines[end + 1:]
    return any(line.rstrip("\r\n") == VOICE_HEADING for line in lines)


def remove_hand_written_section(text: str) -> str:
    """Drop a `## Voice notes` section outside the managed block, up to the next heading."""
    from ._shared import _is_fence, _managed_block_span
    lines = text.splitlines(keepends=True)
    begin, end = _managed_block_span(lines)
    kept: list[str] = []
    skipping = in_fence = False
    for i, line in enumerate(lines):
        inside_block = begin is not None and end is not None and begin <= i <= end
        bare = line.rstrip("\r\n")
        level = len(bare) - len(bare.lstrip("#"))
        if not inside_block and not in_fence and level in (1, 2) and bare[level:level + 1] == " ":
            skipping = bare == VOICE_HEADING
        if _is_fence(bare):
            in_fence = not in_fence
        if inside_block or not skipping:
            kept.append(line)
    return "".join(kept)
