"""Unit tests for multi-shell environment and OpenClaw systemd env synchronization."""
import os
from pathlib import Path

from ai_resources.setup import shell


def test_render_shell_block():
    vars_dict = {
        "GEMINI_API_KEY": "test-key-123",
        "CLAUDE_CODE_USE_VERTEX": "1",
    }
    rendered = shell.render_shell_block(vars_dict)
    assert shell.BEGIN_MARKER in rendered
    assert shell.END_MARKER in rendered
    assert 'export CLAUDE_CODE_USE_VERTEX="1"' in rendered
    assert 'export GEMINI_API_KEY="test-key-123"' in rendered


def test_write_shell_managed_block_append_and_replace(tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text("# user config\nalias ll='ls -l'\n", encoding="utf-8")

    # Initial append
    vars_1 = {"GEMINI_API_KEY": "val1"}
    changed = shell.write_shell_managed_block(rc, vars_1)
    assert changed is True
    content = rc.read_text(encoding="utf-8")
    assert "alias ll='ls -l'" in content
    assert 'export GEMINI_API_KEY="val1"' in content

    # Idempotent write
    assert shell.write_shell_managed_block(rc, vars_1) is False

    # Replacement
    vars_2 = {"GEMINI_API_KEY": "val2", "NEW_VAR": "hello"}
    changed2 = shell.write_shell_managed_block(rc, vars_2)
    assert changed2 is True
    content2 = rc.read_text(encoding="utf-8")
    assert "alias ll='ls -l'" in content2
    assert 'export GEMINI_API_KEY="val2"' in content2
    assert 'export NEW_VAR="hello"' in content2
    # Ensure no duplicate BEGIN markers
    assert content2.count(shell.BEGIN_MARKER) == 1
    assert content2.count(shell.END_MARKER) == 1


def test_write_shell_managed_block_empty_removes(tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text("# header\n", encoding="utf-8")
    shell.write_shell_managed_block(rc, {"KEY": "VAL"})
    assert shell.BEGIN_MARKER in rc.read_text(encoding="utf-8")

    # Empty env removes block
    changed = shell.write_shell_managed_block(rc, {})
    assert changed is True
    res = rc.read_text(encoding="utf-8")
    assert shell.BEGIN_MARKER not in res
    assert "# header" in res


def test_sync_openclaw_systemd_env(tmp_path, monkeypatch):
    openclaw_dir = tmp_path / ".openclaw"
    openclaw_dir.mkdir(parents=True)
    env_file = openclaw_dir / "gateway.systemd.env"
    env_file.write_text("SLACK_BOT_TOKEN=token123\nOLD_KEY=remove_me\n", encoding="utf-8")

    monkeypatch.setattr(Path, "expanduser", lambda p: env_file if "gateway.systemd.env" in str(p) else p)

    res = shell.sync_openclaw_systemd_env(
        {"GEMINI_API_KEY": "gemini_val"},
        purge_keys=["OLD_KEY"],
        dry_run=False,
    )
    assert res == env_file
    content = env_file.read_text(encoding="utf-8")
    assert "SLACK_BOT_TOKEN=token123" in content
    assert "GEMINI_API_KEY=gemini_val" in content
    assert "OLD_KEY" not in content


def test_setup_keeps_the_agy_sign_in(tmp_path, monkeypatch):
    gemini_dir = tmp_path / ".gemini" / "antigravity-cli"
    gemini_dir.mkdir(parents=True)
    token_file = gemini_dir / "antigravity-oauth-token"
    token_file.write_text("oauth-token-content", encoding="utf-8")

    monkeypatch.setattr(Path, "expanduser", lambda p: token_file if "antigravity-oauth-token" in str(p) else p)

    for strategy in ("vertex", "google_ai_studio", "hybrid_direct"):
        purged = shell.purge_stale_credentials(strategy, dry_run=False)
        assert token_file.exists(), strategy
        assert str(token_file) not in purged
