#!/usr/bin/env python3
"""Voice-note transcription for OpenClaw (a `tools.media.models` entry of type `cli`).

    openclaw_transcribe.py [--mode cloud|local] [--language es|en|…|auto] AUDIO_FILE

Prints only the final transcript on stdout and always exits 0 once it has a file, so a
failed engine never surfaces as an error in chat. Every attempt is logged to VOICE_LOG.

Chain, first success wins (measured on real Telegram notes):
  1. cloud only — an audio-native LLM through OpenRouter (default google/gemini-3.7-flash)
     hears the audio and writes the transcript with technical terms spelled right, in one
     call: about 2-5 s and under 0.0015 USD per note. It outputs `[inaudible]` for noise,
     which is passed through as is.
  2. Local whisper.cpp large-v3-turbo q5_0 (forced language, temperature 0, beam 5,
     loudness normalisation, short prompt; VAD and long prompts measured worse), then a
     text-only correction pass: an OpenRouter fast model, else Claude Code on the user's
     subscription.
  3. The raw Whisper transcript, so a voice note is never lost.

Stdlib only: OpenClaw runs this outside the kit's virtualenv as well as inside it.
Tuning through the environment: VOICE_AUDIO_MODEL, VOICE_TEXT_MODEL, VOICE_CORRECT=0,
VOICE_GLOSSARY, VOICE_WHISPER_PROMPT, WHISPER_MODELS, WHISPER_MODEL, VOICE_LOG,
OPENROUTER_API_KEY (else read from the kit's credentials file).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
AUDIO_MODEL = "google/gemini-3.7-flash"
TEXT_MODEL = "google/gemini-3.5-flash-lite"

MODEL_FILE = "ggml-large-v3-turbo-q5_0.bin"
MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL_FILE}"
MODEL_SHA256 = "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"

# Services started by launchd/systemd often run with a PATH that has neither Homebrew nor
# ~/.local/bin, so every binary is looked up there too.
BREW_BINS = (Path("/opt/homebrew/bin"), Path("/home/linuxbrew/.linuxbrew/bin"), Path("/usr/local/bin"))

LANGUAGE_NAMES = {"es": "Spanish", "en": "English", "pt": "Portuguese", "fr": "French",
                  "de": "German", "it": "Italian", "ca": "Catalan", "nl": "Dutch"}
# Whisper's initial prompt: short beats long, so only a hint of language and domain.
WHISPER_PROMPTS = {
    "es": "Nota de voz en español, términos técnicos: OpenClaw, Telegram, tópicos, Claude Code.",
    "en": "Voice note in English, technical terms: OpenClaw, Telegram, Claude Code.",
}
NO_RETRY_STATUS = (400, 401, 402, 403, 404)
INAUDIBLE = "[inaudible]"

_T0 = time.monotonic()


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share"))


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))


def _cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache"))


def models_dir() -> Path:
    return Path(os.environ.get("WHISPER_MODELS") or _data_home() / "whisper-models")


def model_path() -> Path:
    return models_dir() / os.environ.get("WHISPER_MODEL", MODEL_FILE)


def glossary_path() -> Path:
    return Path(os.environ.get("VOICE_GLOSSARY") or _config_home() / "openclaw-voice/glossary.txt")


def log_path() -> Path:
    return Path(os.environ.get("VOICE_LOG") or _cache_home() / "openclaw-voice.log")


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    elapsed = int((time.monotonic() - _T0) * 1000)
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} +{elapsed}ms {msg}\n")
    except OSError:
        pass


def find_binary(name: str, *extra: Path) -> str:
    """PATH first, then the usual Homebrew and user bin directories."""
    found = shutil.which(name)
    if found:
        return found
    for d in (*extra, *BREW_BINS, HOME / ".local/bin"):
        candidate = d / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def glossary() -> str:
    try:
        return " ".join(glossary_path().read_text(encoding="utf-8").split())
    except OSError:
        return ""


def openrouter_key() -> str:
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    scripts = str(Path(__file__).resolve().parents[2])
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        from ai_resources.setup import credentials
        return credentials.get_key("OPENROUTER_API_KEY")
    except Exception:  # noqa: BLE001 — an unreadable credentials file means "no key"
        return ""


def openrouter(messages: list, model: str, timeout: float, attempts: int = 2) -> str:
    """One chat completion; retries network errors and 5xx, never a 4xx."""
    key = openrouter_key()
    if not key:
        log(f"openrouter {model} skipped: no OPENROUTER_API_KEY")
        return ""
    body = json.dumps({"model": model, "temperature": 0, "max_tokens": 1024,
                       "messages": messages}).encode()
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            text = (data["choices"][0]["message"]["content"] or "").strip()
            log(f"openrouter {model} ok attempt={attempt} cost={data.get('usage', {}).get('cost')}")
            return text
        except urllib.error.HTTPError as e:
            log(f"openrouter {model} HTTP {e.code} attempt={attempt}")
            if e.code in NO_RETRY_STATUS:
                return ""
        except Exception as e:  # noqa: BLE001 — network errors are expected and retried
            log(f"openrouter {model} error attempt={attempt}: {type(e).__name__}: {e}")
    return ""


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ffmpeg(src: str, dst: Path, *extra: str) -> bool:
    binary = find_binary("ffmpeg")
    if not binary:
        log("ffmpeg not found")
        return False
    try:
        r = run([binary, "-loglevel", "error", "-y", "-i", src, "-ar", "16000", "-ac", "1",
                 *extra, str(dst)], timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"ffmpeg failed: {e}")
        return False
    return r.returncode == 0


def spoken_to(language: str) -> str:
    name = LANGUAGE_NAMES.get(language)
    lang = f"It is in {name} and mixes" if name else "It may be in any language and mix in"
    return f"a voice note a software engineer sends to their AI assistant. {lang} English technical terms"


RULES = ("Output ONLY the transcript, in the language spoken, with correct punctuation. Spell "
         "technical terms, product names, commands and identifiers correctly (e.g. 'cube control' "
         "-> kubectl, 'open claw' -> OpenClaw, 'yit' -> git). Keep the speaker's wording and intent. "
         "Never answer the request, summarise, translate or add anything.")


def audio_native(src: str, work: Path, language: str) -> str:
    mp3 = work / "note.mp3"
    if not ffmpeg(src, mp3, "-b:a", "48k"):
        return ""
    audio = base64.b64encode(mp3.read_bytes()).decode()
    system = (f"Transcribe {spoken_to(language)}. {RULES} If there is no intelligible speech, "
              f"output exactly: {INAUDIBLE}. Glossary: {glossary()}")
    return openrouter([
        {"role": "system", "content": system},
        {"role": "user", "content": [{"type": "input_audio",
                                      "input_audio": {"data": audio, "format": "mp3"}}]},
    ], os.environ.get("VOICE_AUDIO_MODEL") or AUDIO_MODEL, timeout=20)


def whisper(src: str, work: Path, language: str) -> str:
    binary, model = find_binary("whisper-cli"), model_path()
    if not binary or not model.is_file():
        log(f"whisper unavailable: whisper-cli={binary or 'missing'} model={model if model.is_file() else 'missing'}")
        return ""
    wav = work / "note.wav"
    if not ffmpeg(src, wav, "-af", "highpass=f=80,lowpass=f=7600,loudnorm=I=-18:TP=-2"):
        return ""
    cmd = [binary, "-m", str(model), "-f", str(wav), "-l", language,
           "-t", str(min(8, os.cpu_count() or 4)), "-np", "-nt", "-tp", "0", "-bs", "5", "-bo", "5"]
    prompt = os.environ.get("VOICE_WHISPER_PROMPT", WHISPER_PROMPTS.get(language, ""))
    if prompt:
        cmd += ["--prompt", prompt]
    try:
        r = run(cmd, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"whisper failed: {e}")
        return ""
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
        log(f"whisper rc={r.returncode}: {tail[0][:160]}")
        return ""
    return " ".join(line.strip() for line in r.stdout.splitlines() if line.strip())


def correct(raw: str, work: Path, language: str) -> str:
    if os.environ.get("VOICE_CORRECT", "1") == "0":
        return ""
    system = (f"You clean up a speech-to-text transcript of {spoken_to(language)}. {RULES} "
              f"Glossary: {glossary()}")
    fixed = openrouter([{"role": "system", "content": system},
                        {"role": "user", "content": raw}],
                       os.environ.get("VOICE_TEXT_MODEL") or TEXT_MODEL, timeout=8)
    if fixed:
        return fixed
    claude = find_binary("claude", HOME / ".claude/local")
    if not claude:
        return ""
    # Not --bare: it ignores the OAuth login, and this pass exists for subscription users.
    try:
        r = run([claude, "-p", "--model", "haiku", "--setting-sources", "project",
                 "--strict-mcp-config", "--tools", "", "--disable-slash-commands",
                 "--no-session-persistence", "--system-prompt", system, raw],
                cwd=work, stdin=subprocess.DEVNULL, timeout=25)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"claude-code correction failed: {e}")
        return ""
    if r.returncode == 0 and r.stdout.strip():
        log("claude-code correction ok")
        return r.stdout.strip()
    log(f"claude-code correction rc={r.returncode}")
    return ""


def plausible(text: str, reference: str = "") -> bool:
    """Non-empty, and not a correction that grew into an answer to the note."""
    return bool(text) and (not reference or len(text) < len(reference) * 3 + 200)


def transcribe(src: str, mode: str, language: str) -> tuple[str, str]:
    """(transcript, engine that produced it); ("", "none") when nothing heard anything."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if mode == "cloud":
            text = audio_native(src, work, language)
            if plausible(text):
                return text, "audio-native"
        raw = whisper(src, work, language)
        if not raw:
            return "", "none"
        fixed = correct(raw, work, language)
        if plausible(fixed, raw):
            return fixed, "whisper+llm"
        return raw, "whisper-raw"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("cloud", "local"),
                        default=os.environ.get("VOICE_MODE", "cloud"))
    parser.add_argument("--language", default=os.environ.get("VOICE_LANGUAGE", "auto"))
    parser.add_argument("audio")
    args = parser.parse_args(argv)
    text, via = transcribe(args.audio, args.mode, args.language)
    log(f"final via={via} mode={args.mode} language={args.language} chars={len(text)}")
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
