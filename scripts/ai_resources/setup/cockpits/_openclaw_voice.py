"""OpenClaw voice notes: which engine transcribes audio attachments before the agent sees them.

    agy     Antigravity CLI on the user's own Google account: the model hears the note,
            nothing is billed per token and no audio leaves for a gateway key
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
    "agy": "Antigravity CLI (agy) — your own Google account, no OpenRouter key, no whisper",
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


def has_agy() -> bool:
    return bool(os.environ.get("AGY_BIN") or transcriber.find_binary("agy", Path.home() / ".local" / "bin"))


def default_mode(s: state.SetupState) -> str:
    """The saved answer; unattended first runs keep; else agy, then cloud, then local.

    agy comes first because it needs no per-token key and no local model: it runs on a
    Google subscription the user already has."""
    if s.openclaw.voice in MODES:
        return s.openclaw.voice
    if ui.is_non_interactive():
        # Never change how a live bot hears its user from an unattended run.
        return "keep"
    if has_agy():
        return "agy"
    return "cloud" if has_openrouter_key() else "local"


LANGUAGES = {
    "es": "Spanish", "en": "English", "pt": "Portuguese", "fr": "French", "de": "German",
    "it": "Italian", "ca": "Catalan", "nl": "Dutch",
}
AUTO_LABEL = ("auto — detect per note (short clips can be misdetected: a real "
              "\"¿Puedes escucharme?\" came out in Russian)")
CORRECTION = {
    "llm": "Correct with an LLM (recommended)",
    "offline": "Fully offline (no network)",
}


def locale_language() -> str:
    """The ISO 639-1 code of the system locale (LC_ALL, then LANG); "" for C/POSIX or unset."""
    for var in ("LC_ALL", "LANG"):
        value = os.environ.get(var, "").strip()
        if not value:
            continue
        code = value.split(".")[0].split("@")[0].split("_")[0].lower()
        if code in ("c", "posix") or not code.isalpha() or len(code) not in (2, 3):
            return ""
        return code
    return ""


def default_language(s: state.SetupState) -> str:
    """The saved answer, else the system locale's language, else Spanish."""
    return s.openclaw.voice_language or locale_language() or "es"


def prompt(s: state.SetupState) -> None:
    mode = ui.select(
        "Voice notes: how should OpenClaw transcribe them?",
        [ui.Choice(label, value=mid) for mid, label in MODES.items()],
        default=default_mode(s),
    )
    s.openclaw.voice = mode
    if mode == "agy" and not has_agy():
        ui.warn("No agy binary found (install Antigravity CLI and run `agy` once to sign in): "
                "every note will report that it could not be understood until then.")
    if mode == "cloud" and not has_openrouter_key():
        ui.warn("No OPENROUTER_API_KEY in the environment or the kit credentials: "
                "every note will fall back to local whisper.cpp until one is added.")
    if mode not in ("agy", "cloud", "local"):
        return
    default = default_language(s)
    codes = {default: LANGUAGES.get(default, default), **LANGUAGES}
    choices = [ui.Choice(f"{code} — {name}", value=code) for code, name in codes.items() if code != "auto"]
    s.openclaw.voice_language = ui.select(
        "Language spoken in voice notes (a fixed language transcribes short notes more reliably):",
        [*choices, ui.Choice(AUTO_LABEL, value="auto")],
        default=default,
    )
    if mode == "local":
        s.openclaw.voice_correction = ui.select(
            "Correct local transcripts with an LLM? It fixes misheard technical terms and "
            "punctuation. Only the transcript text is sent, never the audio: to OpenRouter "
            "(about 1 s) or to Claude Code on your subscription (about 10 s), about $0.0001 per note.",
            [ui.Choice(label, value=cid) for cid, label in CORRECTION.items()],
            default=s.openclaw.voice_correction or "llm",
        )


# --- tools.media -------------------------------------------------------------------

def _is_audio_only_cli(entry: Any) -> bool:
    return (isinstance(entry, dict) and entry.get("type") == "cli"
            and entry.get("capabilities") == ["audio"])


def build_media(mode: str, python: str, script: str, language: str, current: Any,
                correction: str = "llm") -> dict:
    """The tools.media value for `mode`. Models for other capabilities are kept.

    OpenClaw's CLI model entry has no env field, so offline correction travels as a flag."""
    if mode == "off":
        return {"audio": {"enabled": False}}
    others = [m for m in ((current or {}).get("models") or []) if not _is_audio_only_cli(m)]
    entry = {
        "type": "cli",
        "command": python,
        "args": [script, "--mode", mode, "--language", language,
                 *(["--no-correct"] if mode == "local" and correction == "offline" else []),
                 "{{AttachmentPath}}"],
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


def ensure_binaries(s: state.SetupState) -> bool:
    """Install missing formulas, recording in state the ones setup installed itself."""
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
    still_missing = missing_formulas()
    for formula in missing:
        if formula not in still_missing and formula not in s.openclaw.voice_formulas_installed:
            s.openclaw.voice_formulas_installed.append(formula)
    if r.returncode != 0 or still_missing:
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
                 expected: str = transcriber.MODEL_SHA256, s: state.SetupState | None = None) -> bool:
    """Download the Whisper model once. It lands under its final name only when complete and
    verified: a truncated file under that name would make every local transcription fail.
    A download is recorded in `s` so teardown removes it; a model already in place is not."""
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
    if s is not None and str(dest) not in s.openclaw.voice_models_downloaded:
        s.openclaw.voice_models_downloaded.append(str(dest))
    return True


def ensure_glossary(dest: Path | None = None) -> bool:
    """Install the default glossary; a file that exists is the user's and is never replaced."""
    dest = dest or transcriber.glossary_path()
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_GLOSSARY, dest)
    return True


def ensure_local_engine(s: state.SetupState, required: bool) -> bool:
    """whisper.cpp, ffmpeg and the model. `required` is False for cloud, where they are the fallback."""
    ok = ensure_binaries(s) and ensure_model(transcriber.models_dir() / transcriber.MODEL_FILE, s=s)
    if not ok and not required:
        ui.warn("Voice notes: the local fallback is not ready; cloud transcription still works.")
        return True
    return ok


def remove_installed(s: state.SetupState) -> None:
    """Undo what setup installed for voice notes: downloaded models always, formulas only when
    setup installed them (and, interactively, only after the user agrees)."""
    for model in s.openclaw.voice_models_downloaded:
        path = Path(model)
        if path.is_file():
            path.unlink()
            ui.ok(f"Removed {path}")
    s.openclaw.voice_models_downloaded = []

    formulas = list(s.openclaw.voice_formulas_installed)
    brew = transcriber.find_binary("brew") if formulas else ""
    if formulas and not brew:
        ui.warn(f"Homebrew not found; uninstall {', '.join(formulas)} yourself if unused.")
    elif formulas and ui.confirm(
            f"Setup installed {', '.join(formulas)} for voice notes. Uninstall with Homebrew?",
            default=True):
        for formula in formulas:
            try:
                r = subprocess.run([brew, "uninstall", formula], capture_output=True, text=True,
                                   timeout=600)
                failed = r.returncode != 0
                detail = (r.stdout + r.stderr).strip()[-300:]
            except (OSError, subprocess.TimeoutExpired) as e:
                failed, detail = True, str(e)
            if failed:
                ui.warn(f"brew uninstall {formula} failed (another formula may need it): {detail}")
            else:
                ui.ok(f"Uninstalled {formula}")
    s.openclaw.voice_formulas_installed = []


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
