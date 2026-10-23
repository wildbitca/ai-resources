"""The dry run shows the rc files as a real apply leaves them: the operator's lines stay, only the managed block changes."""
from __future__ import annotations

from ai_resources.setup import shell, state, wizard


def _state_with_anthropic():
    s = state.SetupState()
    s.providers["anthropic"] = state.ProviderState(enabled=True)
    return s


def test_the_preview_keeps_every_operator_line_and_only_adds_the_managed_block(tmp_path, monkeypatch):
    rc = tmp_path / ".zshrc"
    rc.write_text("export ZSH=$HOME/.oh-my-zsh\nsource $ZSH/oh-my-zsh.sh\nalias k=kubectl\n", encoding="utf-8")
    monkeypatch.setattr(shell, "get_existing_rc_files", lambda: [rc])
    monkeypatch.setattr(wizard.credentials, "get_key",
                        lambda name: "sk-ant-api03-" + "x" * 40 if name == "ANTHROPIC_API_KEY" else None)

    [(label, path, text, current)] = wizard._dry_run_shell_targets(_state_with_anthropic())
    lines = wizard._diff_lines(label, path, text, current)

    removed = [l for l in lines if l.startswith("  -") and not l.startswith("  ---")]
    added = [l for l in lines if l.startswith("  +") and not l.startswith("  +++")]
    assert removed == []
    assert any(shell.BEGIN_MARKER in l for l in added)
    assert any("ANTHROPIC_API_KEY" in l for l in added)
    assert not any("x" * 40 in l for l in lines), "the key value is masked in the preview"
    assert rc.read_text(encoding="utf-8").count("alias k=kubectl") == 1, "the preview writes nothing"


def test_nothing_to_export_means_no_rc_file_in_the_preview(tmp_path, monkeypatch):
    rc = tmp_path / ".bashrc"
    rc.write_text("alias ll='ls -l'\n", encoding="utf-8")
    monkeypatch.setattr(shell, "get_existing_rc_files", lambda: [rc])
    monkeypatch.setattr(wizard.credentials, "get_key", lambda name: None)
    assert wizard._dry_run_shell_targets(_state_with_anthropic()) == []


def test_planned_content_replaces_an_existing_block_in_place():
    env = {"AGENT_KIT": "/kit"}
    first = shell.planned_content("a\nb\n", env)
    again = shell.planned_content(first.replace("/kit", "/old"), env)
    assert again == first
    assert again.startswith("a\nb\n")
