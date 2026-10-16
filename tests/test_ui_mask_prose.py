"""mask_secrets leftovers: `password is X` prose and a value alone on the line after an empty `KEY=` / `KEY:`."""
from __future__ import annotations

import time

import pytest

from ai_resources.setup import ui


@pytest.mark.parametrize("raw,leak", [
    ("my password is Tr0ub4dor&3xyz please", "Tr0ub4dor&3xyz"),
    ("the token is a1B2c3D4e5F6", "a1B2c3D4e5F6"),
    ("The API key is 9f8e7d6c5b4a3921", "9f8e7d6c5b4a3921"),
    ("passphrase = Correct-Horse-9", "Correct-Horse-9"),
    ("secret is 'hunter2hunter2'", "hunter2hunter2"),
])
def test_prose_credential_is_masked(raw, leak):
    out = ui.mask_secrets(raw)
    assert leak not in out, out
    assert "****" in out


@pytest.mark.parametrize("raw", [
    "the password is required",                    # a word
    "the token is optional here",                  # a word
    "the secret is /run/secrets/db_pass",          # a path
    "the token is claude-sonnet-5-6",              # a model id
    "my password is short1",                       # under 8 characters
    "the api key is $OPENAI_API_KEY",              # a reference
    "the password is ~/.config/x/pw",              # a path
])
def test_prose_false_positives_are_kept(raw):
    assert ui.mask_secrets(raw) == raw


@pytest.mark.parametrize("raw,leak", [
    ("API_TOKEN=\nAb3dEf6hIj9kLm2\nnext", "Ab3dEf6hIj9kLm2"),
    ("export DB_PASSWORD=\nS3cr3t!Value99\n", "S3cr3t!Value99"),
    ("secret:\nQ7w8E9r0T1y2U3i4\nother: fine", "Q7w8E9r0T1y2U3i4"),
    ("api_key:\n  Zx9Cv8Bn7Mm6Aa5\n", "Zx9Cv8Bn7Mm6Aa5"),
])
def test_value_alone_after_empty_key_is_masked(raw, leak):
    out = ui.mask_secrets(raw)
    assert leak not in out, out
    assert "****" in out


@pytest.mark.parametrize("raw", [
    "API_TOKEN=\nPATH=/usr/bin\n",
    "TOKEN=\nthe-next-line-is-prose\n",
    "TOKEN=\n\nnext\n",
    "KEY=\n$HOME\n",
])
def test_empty_key_followed_by_non_credential_is_kept(raw):
    assert ui.mask_secrets(raw) == raw


def test_new_passes_are_linear():
    hostile = ("password is " * 4000) + "\n" + ("TOKEN=\n" * 2000)
    t0 = time.monotonic()
    ui.mask_secrets(hostile)
    assert time.monotonic() - t0 < 5
