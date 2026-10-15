"""mask_secrets audit: unquoted passphrases, JSON bodies, headers, -u user:pass, YAML block scalars, PEM, base64."""
from __future__ import annotations

import pytest

from ai_resources.setup import ui


@pytest.mark.parametrize("raw,leaks", [
    ("PASSWORD=my pass phrase", ["pass", "phrase", "my"]),
    ("export DB_PASSWD=correct horse battery", ["horse", "battery"]),
    ("  GPG_PASSPHRASE=one two three # note", ["two", "three", "note"]),
    ('{"password": "my pass phrase", "user": "bob"}', ["pass phrase"]),
    ("{'api_key': 'abcdefghijkl'}", ["abcdefghijkl"]),
    ('{"token":"xyz12345"}', ["xyz12345"]),
    ("curl -H 'Authorization: Bearer abcdefghijklmnop' https://x", ["abcdefghijklmnop"]),
    ("curl -H 'Authorization: Token abcdefghijklmnop' https://x", ["abcdefghijklmnop"]),
    ("Authorization: s3cr3tvalue", ["s3cr3tvalue"]),
    ("curl -u admin:hunter2 https://x", ["hunter2"]),
    ("curl --user admin:hunter2 https://x", ["hunter2"]),
    ("curl --user=admin:hunter2 https://x", ["hunter2"]),
    ("OPENAI_API_KEY=sk-abcdefgh12345\nPATH=/usr/bin\nHOME_TOKEN=zzzz9999", ["sk-abcdefgh12345", "zzzz9999"]),
    ("api_key: |\n  line-one-secret\n  line-two-secret\nother: fine", ["line-one-secret", "line-two-secret"]),
    ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQE\nabcdef\n-----END PRIVATE KEY-----",
     ["MIIEvQIBADANBgkqhkiG9w0BAQE", "abcdef"]),
    ("blob dGhpcyBpcyBhIHNlY3JldC8rdmFsdWU=", ["dGhpcyBpcyBhIHNlY3JldC8rdmFsdWU"]),
    ("blob a+b/cdefghijklmnopqrstuvwxyz0123==", ["cdefghijklmnopqrstuvwxyz0123"]),
])
def test_secret_is_masked(raw, leaks):
    masked = ui.mask_secrets(raw)
    for leak in leaks:
        assert leak not in masked, masked
    assert "****" in masked


def test_unquoted_passphrase_masks_to_end_of_line_only_on_that_line():
    out = ui.mask_secrets("PASSWORD=my pass phrase\nnext line stays")
    assert out == "PASSWORD=****\nnext line stays"


def test_other_lines_around_a_block_scalar_survive():
    out = ui.mask_secrets("a: 1\napi_key: |\n  sec\nmodel: x")
    assert "a: 1" in out and "model: x" in out and "sec" not in out


@pytest.mark.parametrize("raw", [
    "PASSWORD=${DB_PASSWORD}",
    "PASSWORD=$DB_PASSWORD",
    "PASSWORD=<DB_PASSWORD>",
    "PASSWORD=os.environ/DB_PASSWORD",
    "Enter your password to continue",
    "The PASSWORD variable is required",
    "ssh -u root host",
    "/home/bitgandtter/Development/wildbit/ai-resources/scripts",
    "Authorization header is required",
    "-----BEGIN CERTIFICATE-----",
])
def test_ordinary_text_and_references_are_kept(raw):
    assert ui.mask_secrets(raw) == raw


def test_prose_line_keeps_its_words_after_the_value():
    out = ui.mask_secrets("set PASSWORD=x then run it again please")
    assert out.endswith("then run it again please")


@pytest.mark.parametrize("raw,leaks", [
    ("PASSWORD: correct horse battery staple", ["correct", "horse", "battery", "staple"]),
    ("passphrase: correct horse battery staple", ["correct", "horse", "staple"]),
    ("  db_passwd:   one two three", ["one", "two", "three"]),
    ("passphrase=correct horse", ["correct", "horse"]),
    ("export GPG_PASSPHRASE: one two", ["one", "two"]),
    ("curl --passphrase hunter22 x", ["hunter22"]),
])
def test_colon_form_and_passphrase_names_are_masked(raw, leaks):
    masked = ui.mask_secrets(raw)
    for leak in leaks:
        assert leak not in masked, masked


@pytest.mark.parametrize("raw", [
    "max_tokens: 4096",
    "tokenizer: cl100k_base",
    "api_key_env: OPENAI_API_KEY",
    "api_key_env: NAME",
    "token_count: 5",
    "secret_key_ref: some-ref",
    "keyboard: us",
    "password_file: /etc/ai/pw",
    "sha256: " + "ab12" * 16,
    "input_digest: " + "0f" * 32,
    "image: ghcr.io/berriai/litellm@sha256:" + "ab12" * 16,
    "checksum=" + "ab12" * 8,
])
def test_names_and_digests_that_are_not_secrets_are_kept(raw):
    assert ui.mask_secrets(raw) == raw


def test_a_bare_long_hex_value_is_still_masked():
    assert "ab12" * 16 not in ui.mask_secrets("value: " + "ab12" * 16)
    assert "ab12" * 16 not in ui.mask_secrets("api_key: " + "ab12" * 16)
