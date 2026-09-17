"""OpenClaw voice-note transcriber: the order of the chain and when each step gives up.

No network and no binaries: OpenRouter is a fake `urlopen`, and ffmpeg, whisper-cli and
claude are a fake `run`. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import urllib.error

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.voice import openclaw_transcribe as t  # noqa: E402


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _completion(text: str) -> _Response:
    return _Response(json.dumps({"choices": [{"message": {"content": text}}],
                                 "usage": {"cost": 0.0004}}).encode())


class _OpenRouter:
    """Answers queued per model; an exception in the queue is raised instead."""

    def __init__(self):
        self.queue: dict[str, list] = {}
        self.calls: list[dict] = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        self.calls.append(body)
        answer = self.queue[body["model"]].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _completion(answer)

    def models(self) -> list[str]:
        return [c["model"] for c in self.calls]


class _Proc:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


class _Binaries:
    """ffmpeg writes its output file; whisper-cli and claude print what the test set."""

    def __init__(self):
        self.whisper = "o sea que si te pido que añadiste los tópicos"
        self.claude = ""
        self.calls: list[str] = []

    def __call__(self, cmd, **_kw):
        name = pathlib.Path(cmd[0]).name
        self.calls.append(name)
        if name == "ffmpeg":
            pathlib.Path(cmd[-1]).write_bytes(b"audio")
            return _Proc()
        if name == "whisper-cli":
            return _Proc(self.whisper)
        if name == "claude":
            return _Proc(self.claude, 0 if self.claude else 1)
        raise AssertionError(f"unexpected binary {name}")


AUDIO, TEXT = t.AUDIO_MODEL, t.TEXT_MODEL


@pytest.fixture
def chain(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    (models / t.MODEL_FILE).write_bytes(b"model")
    monkeypatch.setenv("WHISPER_MODELS", str(models))
    monkeypatch.setenv("VOICE_LOG", str(tmp_path / "voice.log"))
    monkeypatch.setenv("VOICE_GLOSSARY", str(tmp_path / "missing-glossary.txt"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    for var in ("VOICE_AUDIO_MODEL", "VOICE_TEXT_MODEL", "VOICE_CORRECT", "VOICE_WHISPER_PROMPT",
                "WHISPER_MODEL"):
        monkeypatch.delenv(var, raising=False)
    router, bins = _OpenRouter(), _Binaries()
    monkeypatch.setattr(t.urllib.request, "urlopen", router)
    monkeypatch.setattr(t, "run", bins)
    monkeypatch.setattr(t, "find_binary", lambda name, *_extra: f"/bin/{name}")
    return router, bins


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(t.OPENROUTER_URL, code, "err", {}, io.BytesIO(b"{}"))


def test_cloud_mode_uses_the_audio_native_model_and_skips_whisper(chain):
    router, bins = chain
    router.queue[AUDIO] = ["¿Eres capaz de hacerlo?"]
    assert t.transcribe("note.ogg", "cloud", "es") == ("¿Eres capaz de hacerlo?", "audio-native")
    assert "whisper-cli" not in bins.calls
    audio = router.calls[0]["messages"][1]["content"][0]
    assert audio["type"] == "input_audio" and audio["input_audio"]["format"] == "mp3"


def test_inaudible_from_the_audio_model_is_passed_through_not_second_guessed(chain):
    router, bins = chain
    router.queue[AUDIO] = ["[inaudible]"]
    assert t.transcribe("note.ogg", "cloud", "es") == ("[inaudible]", "audio-native")
    assert "whisper-cli" not in bins.calls


def test_a_4xx_is_not_retried_and_falls_back_to_whisper_plus_correction(chain):
    router, bins = chain
    router.queue[AUDIO] = [_http(402)]
    router.queue[TEXT] = ["O sea, que si te pido que me listes los tópicos"]
    text, via = t.transcribe("note.ogg", "cloud", "es")
    assert via == "whisper+llm" and text.startswith("O sea, que si te pido")
    assert router.models() == [AUDIO, TEXT], "a 4xx must not be retried"


def test_a_network_error_is_retried_once(chain):
    router, _bins = chain
    router.queue[AUDIO] = [urllib.error.URLError("timed out"), "hola"]
    assert t.transcribe("note.ogg", "cloud", "es") == ("hola", "audio-native")
    assert router.models() == [AUDIO, AUDIO]


def test_a_5xx_is_retried_once_then_the_chain_moves_on(chain):
    router, _bins = chain
    router.queue[AUDIO] = [_http(502), _http(503)]
    router.queue[TEXT] = ["corrected"]
    assert t.transcribe("note.ogg", "cloud", "es") == ("corrected", "whisper+llm")
    assert router.models() == [AUDIO, AUDIO, TEXT]


def test_local_mode_never_sends_audio_anywhere(chain):
    router, bins = chain
    router.queue[TEXT] = ["corrected"]
    assert t.transcribe("note.ogg", "local", "es") == ("corrected", "whisper+llm")
    assert router.models() == [TEXT]
    assert all(not isinstance(c["messages"][1]["content"], list) for c in router.calls)
    assert bins.calls[:2] == ["ffmpeg", "whisper-cli"]


def test_correction_falls_back_to_claude_code_without_openrouter(chain, monkeypatch):
    router, bins = chain
    monkeypatch.setattr(t, "openrouter_key", lambda: "")
    bins.claude = "O sea, que si te pido que me listes los tópicos"
    assert t.transcribe("note.ogg", "cloud", "es") == (bins.claude, "whisper+llm")
    assert router.calls == []
    assert bins.calls == ["ffmpeg", "ffmpeg", "whisper-cli", "claude"]


def test_raw_whisper_is_returned_when_every_correction_fails(chain, monkeypatch):
    _router, bins = chain
    monkeypatch.setattr(t, "openrouter_key", lambda: "")
    assert t.transcribe("note.ogg", "local", "es") == (bins.whisper, "whisper-raw")


def test_a_correction_that_answers_instead_of_transcribing_is_discarded(chain):
    router, bins = chain
    router.queue[TEXT] = ["Sure! Here is a long answer. " * 40]
    assert t.transcribe("note.ogg", "local", "es") == (bins.whisper, "whisper-raw")


def test_nothing_is_printed_when_whisper_hears_nothing(chain, monkeypatch, capsys):
    _router, bins = chain
    monkeypatch.setattr(t, "openrouter_key", lambda: "")
    bins.whisper = ""
    assert t.main(["--mode", "local", "--language", "es", "note.ogg"]) == 0
    assert capsys.readouterr().out == ""


def test_whisper_runs_with_the_measured_settings(chain, monkeypatch):
    _router, bins = chain
    seen = {}

    def spy(cmd, **kw):
        if pathlib.Path(cmd[0]).name == "whisper-cli":
            seen["cmd"] = cmd
        return bins(cmd, **kw)

    monkeypatch.setattr(t, "run", spy)
    monkeypatch.setenv("VOICE_CORRECT", "0")
    t.transcribe("note.ogg", "local", "es")
    cmd = seen["cmd"]
    for flag, value in (("-l", "es"), ("-tp", "0"), ("-bs", "5"), ("-bo", "5")):
        assert cmd[cmd.index(flag) + 1] == value
    assert "--prompt" in cmd and "--vad" not in cmd


def test_no_correct_keeps_local_mode_fully_offline(chain, capsys):
    router, bins = chain
    assert t.main(["--mode", "local", "--language", "es", "--no-correct", "note.ogg"]) == 0
    assert capsys.readouterr().out == bins.whisper + "\n"
    assert router.calls == [] and "claude" not in bins.calls


def test_main_prints_only_the_transcript(chain, capsys):
    router, _bins = chain
    router.queue[AUDIO] = ["hola"]
    assert t.main(["--mode", "cloud", "--language", "es", "note.ogg"]) == 0
    assert capsys.readouterr().out == "hola\n"


def test_the_log_records_the_route_but_not_the_words(chain, tmp_path, capsys):
    router, _bins = chain
    router.queue[AUDIO] = ["contenido privado"]
    t.main(["note.ogg"])
    log = (tmp_path / "voice.log").read_text()
    assert "final via=audio-native" in log and "contenido privado" not in log
    assert "test-key" not in log


def test_the_key_falls_back_to_the_kit_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    env = tmp_path / "ai-resources" / ".env"
    env.parent.mkdir()
    env.write_text('OPENROUTER_API_KEY="from-kit"\n')
    assert t.openrouter_key() == "from-kit"


def test_binaries_are_found_in_homebrew_when_the_service_path_lacks_it(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(t, "BREW_BINS", (tmp_path,))
    assert t.find_binary("ffmpeg") == str(fake)
