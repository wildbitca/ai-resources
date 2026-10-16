"""Offline repros of the independent security audit of the setup wizard (findings F1-F8).

Every case was a live leak, a ReDoS, a MarkupError or an escape-sequence injection when the audit ran.
"""
from __future__ import annotations

import io
import os
import re
import stat
import subprocess
import time
import types
import urllib.error
from contextlib import contextmanager

import pytest

from ai_resources.setup import credentials, litellm, smoke, state, ui, wizard

S = "s3cr3tV4lue9"          # a plain secret with no recognisable shape


# --- F5: pattern gaps -------------------------------------------------------------------------
LEAKY = {
    "json_escaped_quote": '{"api_key": "ab\\"%s"}' % S,
    "json_single_quote_escape": "{'api_key': 'x\\'%s'}" % S,
    "settings_env_json": '{"env": {"ANTHROPIC_AUTH_TOKEN": "a\\"b%s"}}' % S,
    "cookie": "Cookie: session=%s" % S,
    "cookie_second_pair": "Cookie: a=b; session=%s" % S,
    "set_cookie": "Set-Cookie: sid=%s; HttpOnly" % S,
    "x_auth": "X-Auth: %s" % S,
    "anthropic_auth": "ANTHROPIC_AUTH=%s" % S,
    "pgpass": "PGPASS=%s" % S,
    "db_pwd": "DB_PWD=%s" % S,
    "gh_pat": "GH_PAT=%s" % S,
    "value_with_comma": "API_KEY=abc,%s" % S,
    "value_with_brace": "API_KEY=abc}%s" % S,
    "value_after_ref": "API_KEY=${X}%s" % S,
    "value_after_true": "API_KEY=true %s" % S,
    "value_after_home": "API_KEY=$HOME %s" % S,
    "inner_single_quote": "API_KEY=\"ab'%s\"" % S,
    "url_at_in_password": "https://user:p@ss%s@host/" % S,
    "url_slash_in_password": "https://user:ab/%s@host/" % S,
    "url_empty_user": "https://:%s@host/" % S,
    "url_token_as_user": "https://%s@github.com/x.git" % S,
    "url_amz_signature": "https://bucket.s3/x?X-Amz-Signature=%s" % S,
    "pem_lowercase": "-----BEGIN private key-----\n%s\n" % S,
    "pem_pgp": "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\n%s\n-----END PGP PRIVATE KEY BLOCK-----" % S,
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.%s" % S,
    "yaml_list": "api_keys:\n  - %s" % S,
    "yaml_list_same_indent": "api_keys:\n- %s\n- other%s" % (S, S),
    "yaml_next_line": "api_key:\n  %s" % S,
    "json_next_line": '{\n  "api_key":\n    "%s"\n}' % S,
    "block_comment": "api_key: | # comment\n  %s" % S,
    "block_lower_indent": "  api_key: |\n %s" % S,
    "block_same_indent": "api_key: >-\n%s" % S,
    "block_crlf": "api_key: |\r\n  %s\r\n" % S,
    "flag_dash_value": "--password -%s" % S,
    "mysql_p": "mysql -u root -p%s db" % S,
    "fullwidth_equals": "API_KEY\uff1d%s" % S,
    "unicode_colon": "API_KEY\u2236 %s" % S,
    "greek_kappa": "API_\u039aEY=%s" % S,
    "zero_width_in_name": "API_K\u200bEY=%s" % S,
    "zero_width_after_eq": "API_KEY=\u200b%s" % S,
    "bidi_in_name": "API_\u202eKEY=%s" % S,
}


@pytest.mark.parametrize("name", sorted(LEAKY))
def test_f5_secret_is_masked(name):
    raw = LEAKY[name]
    assert S not in ui.mask_secrets(raw), ui.mask_secrets(raw)
    assert S not in ui.safe_text(raw), ui.safe_text(raw)


@pytest.mark.parametrize("raw,expected_kept", [
    ("model: anthropic/claude-sonnet-4-5-20250929", "anthropic/claude-sonnet-4-5-20250929"),
    ("planner: openrouter/anthropic/claude-3.5-sonnet", "openrouter/anthropic/claude-3.5-sonnet"),
    ("max_tokens: 4096", "max_tokens: 4096"),
    ("api_key_env: OPENAI_API_KEY", "api_key_env: OPENAI_API_KEY"),
    ("api_key: os.environ/OPENAI_API_KEY", "api_key: os.environ/OPENAI_API_KEY"),
    ("api_key: ${OPENAI_API_KEY}", "api_key: ${OPENAI_API_KEY}"),
    ("API_KEY=${X}", "API_KEY=${X}"),
    ("api_key: null  # not set", "api_key: null  # not set"),
    ("PWD=/home/user/project", "PWD=/home/user/project"),
    ("PATH=/usr/bin:/bin", "PATH=/usr/bin:/bin"),
    ("author: Jane Doe", "author: Jane Doe"),
    ("https://github.com/org/repo@v1.2", "https://github.com/org/repo@v1.2"),
    ("pip install git+https://github.com/org/repo@main", "git+https://github.com/org/repo@main"),
    ("ssh://git@github.com/org/repo.git", "ssh://git@github.com/org/repo.git"),
    ("http://localhost:4000/v1/chat", "http://localhost:4000/v1/chat"),
    ("https://host:8080/path@ref", "https://host:8080/path@ref"),
    ("docker run -p8080:80 img", "docker run -p8080:80 img"),
    ("mkdir -p /tmp/x", "mkdir -p /tmp/x"),
    ("The quick brown fox jumps over the lazy dog.", "The quick brown fox jumps over the lazy dog."),
    ("Authentication succeeded for the author", "Authentication succeeded for the author"),
])
def test_f5_ordinary_text_is_kept(raw, expected_kept):
    out = ui.mask_secrets(raw)
    assert expected_kept in out, out


# --- F1: whole text first, then lines ---------------------------------------------------------
@pytest.fixture
def shown(monkeypatch):
    events: list[str] = []
    for name in ("info", "ok", "warn", "error", "detail"):
        monkeypatch.setattr(ui, name, lambda msg, _n=name: events.append(f"{_n}: {msg}"))
    return events


@pytest.mark.parametrize("text", [
    "-----BEGIN PRIVATE KEY-----\nMIIEv%s+abc/def\n-----END PRIVATE KEY-----" % S,
    "api_key: |\n  %s" % S,
    '{"api_key":\n "%s"}' % S,
    "master_key:\n  %s" % S,
    "API_KEY\x1c=%s" % S,
    "API_KEY\x1d=%s" % S,
    "export DB_PASSWORD=\x85%s" % S,
    "API_KEY=\x1e%s" % S,
    "PASSWORD\u2028=%s" % S,
])
def test_f1_log_tail_masks_the_whole_text_before_splitting(shown, text):
    ui.log_tail(text)
    assert S not in "\n".join(shown), shown


@pytest.mark.parametrize("text", [
    "-----BEGIN PRIVATE KEY-----\nMIIEv%s+abc/def\n-----END PRIVATE KEY-----" % S,
    "api_key: |\n  %s" % S,
    "master_key:\n  %s" % S,
])
def test_f1_log_tail_given_pre_split_lines_still_masks_multiline_secrets(shown, text):
    ui.log_tail(text.splitlines())
    assert S not in "\n".join(shown), shown


@pytest.mark.parametrize("text", [
    "-----BEGIN PRIVATE KEY-----\nMIIEv%s+abc/def\n-----END PRIVATE KEY-----" % S,
    "api_key: |\n  %s" % S,
])
def test_f1_run_with_spinner_failure_masks_multiline_output(shown, monkeypatch, text):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, text, ""))
    ui.run_with_spinner("install", ["x"])
    assert S not in "\n".join(shown), shown


def test_f1_safe_lines_splits_after_masking():
    lines = ui.safe_lines("ok line\napi_key: |\n  %s\nnext: fine" % S)
    assert S not in "\n".join(lines)
    assert lines[0] == "ok line" and lines[-1] == "next: fine"


# --- F2: mask, then truncate ------------------------------------------------------------------
def test_f2_head_cut_inside_an_url_credential_does_not_leak():
    msg = "x" * 280 + " https://user:%s@host/" % S
    for limit in range(285, 310):
        assert S[:5] not in ui.safe_excerpt(msg, limit), limit


def test_f2_tail_cut_inside_a_name_does_not_leak():
    msg = "noise " * 20 + "DB_PASS" + "WORD=" + S + "\n"
    for limit in range(len(S) + 5, len(S) + 40):
        assert S not in ui.safe_excerpt(msg, limit, tail=True), limit


def test_f2_the_tail_of_a_secret_that_starts_before_the_window_is_masked():
    msg = ("y" * 287) + "OPENAI_API_KEY=%s" % S
    assert S not in ui.safe_excerpt(msg, 300, tail=True)


def test_f2_excerpt_is_bounded_and_marks_the_cut():
    out = ui.safe_excerpt("word " * 500, 100)
    assert len(out) <= 110 and out.endswith("...")


def test_f2_tools_install_failure_masks_before_cutting(monkeypatch, shown):
    from ai_resources.setup import tools
    out = "y" * 290 + "OPENAI_API_KEY=%s" % S
    monkeypatch.setattr(tools, "_run_install", lambda cmd, timeout=300: (False, out))
    s = state.SetupState() if hasattr(state, "SetupState") else None
    tools.install_one("agy", s)
    assert S not in "\n".join(shown), shown


# --- F3: ReDoS --------------------------------------------------------------------------------
_N = 100_000
ADVERSARIAL = {
    "a_dash": "a-" * (_N // 2),
    "dash_only": "-" * _N,
    "key_dash": "KEY-" * (_N // 4),
    "key_run": "KEY" * (_N // 3),
    "password_run": "PASSWORD" * (_N // 8),
    "a_dot": "a." * (_N // 2),
    "word_32_run": "a" * _N + "!",
    "flag_key": "-key" * (_N // 4),
    "url_like": "a" * _N,
    "spaces_after_key": "KEY" + " " * _N + "x",
    "esc_csi": "\x1b[" + "0" * _N,
    "esc_osc": "\x1b]" + "a" * _N,
    "b64_eq": "A" * _N + "!",
    "keyword_lines": "API_KEY\n" * (_N // 8),
    "colon_spaces": "token:" + " \n" * (_N // 2),
    "key_eq_many": "KEY=" * (_N // 4),
    "userpass": "-u " * (_N // 3),
    "bearer_run": "bearer " * (_N // 7),
    "scheme_run": "a://" * (_N // 4),
    "at_run": "https://" + "a@" * (_N // 2),
    "pem_begin_run": "-----BEGIN PRIVATE KEY-----" * (_N // 27),
    "colon_key_lines": "api_key:\n  " * (_N // 11),
    "block_lines": "api_key: |\n" + "  x\n" * (_N // 4),
    "digest_run": "digest" * (_N // 6),
    "jwt_run": "eyJ" * (_N // 3),
    "mysql_p": "mysql " + "-pa " * (_N // 4),
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_f3_adversarial_input_is_masked_in_bounded_time(name):
    text = ADVERSARIAL[name]
    t = time.perf_counter()
    ui.mask_secrets(text)
    ui.safe_text(text)
    assert time.perf_counter() - t < 3.0, name


def test_f3_a_line_past_the_cap_is_cut_to_a_marker():
    out = ui.mask_secrets("x " * 10_000 + "tail-secret")
    assert "tail-secret" not in out
    assert "line truncated" in out
    assert len(out) < 6000


def test_f3_lines_under_the_cap_are_untouched():
    line = "word " * 700
    assert ui.mask_secrets(line) == line


# --- F4: URL credentials and markup ------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://user:%s@gw.example.com" % S,
    "http://:%s@gw.example.com" % S,
    "https://%s@gw.example.com" % S,
])
def test_f4_validate_url_rejects_embedded_credentials(url):
    res = wizard._validate_url(url)
    assert res is not True and "credential" in res.lower()


def test_f4_validate_url_still_accepts_plain_urls():
    assert wizard._validate_url("https://litellm.example.com") is True
    assert wizard._validate_url("http://localhost:4000/") is True
    assert wizard._validate_url("https://host/path@ref") is True


def _rich_console(force_terminal=True):
    from rich.console import Console
    buf = io.StringIO()
    return Console(file=buf, force_terminal=force_terminal, color_system="truecolor", width=160), buf


@pytest.fixture
def rich_ui(monkeypatch):
    con, buf = _rich_console()
    monkeypatch.setattr(ui, "_console", con)
    return buf


def test_f4_spinner_label_with_markup_close_tag_does_not_raise(rich_ui):
    with ui.spinner("Validating https://gw/[/]"):
        pass
    assert "\x1b]8" not in rich_ui.getvalue()


def test_f4_spinner_label_cannot_inject_a_hyperlink(rich_ui):
    with ui.spinner("Validating [link=https://evil.example]click[/link]"):
        pass
    assert "\x1b]8" not in rich_ui.getvalue()


def test_f4_spinner_label_masks_url_credentials(rich_ui):
    with ui.spinner("Validating https://user:%s@gw.example.com" % S):
        pass
    assert S not in rich_ui.getvalue()


def test_f4_spinner_fallback_line_is_masked(monkeypatch, capsys):
    class Double:
        def print(self, *a, **k):
            pass
    monkeypatch.setattr(ui, "_console", Double())
    with ui.spinner("Validating https://user:%s@gw/\x1b]0;pwn\x07" % S):
        pass
    out = capsys.readouterr().out
    assert S not in out and "\x1b" not in out


@pytest.mark.parametrize("fn", ["info", "ok", "warn", "error", "detail"])
def test_f4_message_sinks_mask_url_credentials(rich_ui, fn):
    getattr(ui, fn)("Gateway healthy at https://user:%s@gw.example.com" % S)
    assert S not in rich_ui.getvalue()


def test_f4_tables_are_cleaned_and_escaped(rich_ui):
    hostile = "[link=https://evil]x[/link] \x1b]0;pwn\x07 \x1b[2J [/]"
    ui.role_table([("planner", "openrouter", hostile)], title="t [/] \x1b]0;pwn\x07")
    ui.review_table([("Gateway", hostile)], title="[bold]x")
    ui.detected_table([("\u2714", "litellm", hostile), ("?", "x", hostile)])
    out = rich_ui.getvalue()
    assert "\x1b]" not in out
    assert "\x1b[2J" not in out


def test_f4_table_cells_mask_secrets(rich_ui):
    ui.review_table([("Gateway", "https://user:%s@gw" % S)])
    ui.role_table([("a", "b", "API_KEY=%s" % S)])
    assert S not in rich_ui.getvalue()


# --- F6: section titles and rendered YAML --------------------------------------------------------
def test_f6_section_title_is_cleaned(rich_ui):
    ui.section(1, 10, "x[/] \x1b]0;pwn\x07 \u202e")
    out = rich_ui.getvalue()
    assert "\x1b]" not in out and "\u202e" not in out


def test_f6_section_title_plain_print(monkeypatch, capsys):
    monkeypatch.setattr(ui, "_HAS_RICH", False)
    ui.section(1, 2, "t \x1b]0;pwn\x07")
    assert "\x1b" not in capsys.readouterr().out


def test_f6_dry_run_yaml_is_printed_without_markup():
    import inspect
    src = inspect.getsource(wizard._step9_dry_run)
    for m in re.finditer(r"console\.print\(f?\"  \{line\}\"[^)]*\)", src):
        assert "markup=False" in m.group(0), m.group(0)
    assert src.count("markup=False") >= 2


# --- F7: bidi and format characters ---------------------------------------------------------------
@pytest.mark.parametrize("ch", ["\u200b", "\u200e", "\u200f", "\u202a", "\u202e", "\u2066", "\u2069",
                                "\u2060", "\ufeff", "\u061c"])
def test_f7_clean_text_strips_format_characters(ch):
    assert ui.clean_text(f"safe{ch}exe.txt") == "safeexe.txt"


def test_f7_clean_text_keeps_ordinary_unicode():
    assert ui.clean_text("caf\u00e9 \u2714 \u2014 \u65e5\u672c\u8a9e") == "caf\u00e9 \u2714 \u2014 \u65e5\u672c\u8a9e"


# --- F8: smoke errors, smoke.log mode, credentials.mask ----------------------------------------------
def test_f8_credentials_mask_never_reveals_most_of_a_secret():
    for n in range(1, 40):
        secret = "".join(chr(ord("a") + i % 26) for i in range(n))
        shown = len(re.sub(r"[*\u2022]", "", credentials.mask(secret)))
        assert shown * 4 <= n, (n, credentials.mask(secret))
    assert credentials.mask("123456789") .count("*") + credentials.mask("123456789").count("\u2022") >= 7
    assert credentials.mask("") == "(empty)"


def test_f8_long_secret_mask_keeps_a_short_hint():
    out = credentials.mask("sk-" + "a" * 61)
    assert out.startswith("sk-") or out.startswith("sk")
    assert len(out) == 64


def test_f8_anthropic_round_trip_masks_the_error_body(monkeypatch):
    body = ('{"error": {"message": "invalid key %s for API_KEY=%s"}}' % ("sk-ant-api03-ABCDEFGHIJKLMNOP", S)).encode()

    def boom(req, timeout=0):
        raise urllib.error.HTTPError("u", 401, "bad", {}, io.BytesIO(body))
    monkeypatch.setattr(smoke.urllib.request, "urlopen", boom)
    ok, msg = smoke._anthropic_round_trip("m", "https://gw", "k")
    assert not ok and S not in msg and "sk-ant-api03-ABCDEFGHIJKLMNOP" not in msg


def test_f8_transport_error_is_masked(monkeypatch):
    def boom(req, timeout=0):
        raise urllib.error.URLError("failed https://user:%s@gw/" % S)
    monkeypatch.setattr(smoke.urllib.request, "urlopen", boom)
    ok, msg = smoke._anthropic_round_trip("m", "https://gw", "k")
    assert not ok and S not in msg


def test_f8_smoke_log_is_written_masked_with_mode_0600(tmp_path):
    path = tmp_path / "logs" / "smoke.log"
    wizard._write_smoke_log(path, [("Model round-trip  m", False, "bad API_KEY=%s" % S), ("ok one", True, "")])
    text = path.read_text()
    assert S not in text and "FAIL" in text and "PASS" in text
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    wizard._write_smoke_log(path, [("again", True, "")])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_f8_existing_loose_smoke_log_is_tightened(tmp_path):
    path = tmp_path / "smoke.log"
    path.write_text("old\n")
    os.chmod(path, 0o644)
    wizard._write_smoke_log(path, [("x", True, "")])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# --- wizard sinks (F1, F2, F4) ------------------------------------------------------------------------
@contextmanager
def _noop_spinner(label):
    yield None


@pytest.fixture
def docker_step(monkeypatch, shown):
    from ai_resources.setup import detection
    monkeypatch.setattr(wizard, "_DRY_RUN", False)
    monkeypatch.setattr(detection, "detect_container_runtime",
                        lambda: ("docker", types.SimpleNamespace(installed=True, version="1", binary_path="/x")))
    monkeypatch.setattr(detection, "detect_compose", lambda cli: True)
    monkeypatch.setattr(wizard, "_ask_bind_and_port", lambda s: None)
    monkeypatch.setattr(ui, "text", lambda *a, **k: "img")
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "spinner", _noop_spinner)
    monkeypatch.setattr(ui, "record_action", lambda m: None)
    monkeypatch.setattr(state, "save", lambda s: None)
    return shown


def _state():
    return state.SetupState()


def test_wizard_pull_failure_masks_head_and_multiline(monkeypatch, docker_step):
    msg = "x" * 290 + " https://user:%s@reg/\napi_key: |\n  %s\n" % (S, S)
    monkeypatch.setattr(litellm, "pull_image", lambda image: (False, msg))
    rc = wizard._step3_docker(_state())
    assert rc == 1
    assert S not in "\n".join(docker_step), docker_step


def test_wizard_remote_validation_failure_masks(monkeypatch, shown):
    monkeypatch.setattr(ui, "text", lambda *a, **k: "https://gw.example.com")
    monkeypatch.setattr(ui, "password", lambda *a, **k: "k")
    monkeypatch.setattr(ui, "spinner", _noop_spinner)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(ui, "record_action", lambda m: None)
    monkeypatch.setattr(wizard.credentials, "update_env_tracked", lambda d: ({}, []))
    monkeypatch.setattr(litellm, "validate_remote", lambda url, key: (False, "boom API_KEY=%s" % S))
    wizard._step3_remote(_state(), dry_run=False)
    assert S not in "\n".join(shown), shown


def test_wizard_remote_rejects_credentials_in_a_url_returned_by_a_double(monkeypatch, shown):
    # defence in depth: even if the prompt validator was bypassed, a URL with credentials is not stored
    monkeypatch.setattr(ui, "text", lambda *a, **k: "https://user:%s@gw.example.com" % S)
    s = _state()
    rc = wizard._step3_remote(s, dry_run=True)
    assert rc == 1
    assert S not in s.litellm.remote.url
    assert S not in "\n".join(shown), shown


def test_already_masked_values_are_kept_and_a_planted_bullet_is_not_a_bypass():
    assert ui.mask_secrets("OPENROUTER_API_KEY: ******  (saved)").startswith("OPENROUTER_API_KEY: ******")
    assert credentials.mask("sk-" + "a" * 40) in ui.mask_secrets("K_KEY: " + credentials.mask("sk-" + "a" * 40))
    assert S not in ui.mask_secrets("API_KEY=•" + S)
