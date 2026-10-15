"""The masked dry-run diff per target (plan S10): nothing is written, secrets never appear."""
from __future__ import annotations

from ai_resources.setup import state, ui, wizard
from wizard_fakes import SENTINEL

TOKEN = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def test_a_missing_target_is_a_new_file(tmp_path):
    lines = wizard._diff_lines("litellm.yaml", tmp_path / "litellm.yaml", "a: 1\nb: 2\n")
    assert lines[1] == "  new file" and lines[2:] == ["  + a: 1", "  + b: 2"]
    assert not (tmp_path / "litellm.yaml").exists()


def test_a_changed_target_gets_a_unified_diff_and_is_not_touched(tmp_path):
    path = tmp_path / "executors.yaml"
    path.write_text("a: 1\nb: 2\n", encoding="utf-8")
    lines = wizard._diff_lines("executors.yaml", path, "a: 1\nb: 3\n")
    text = "\n".join(lines)
    assert "-b: 2" in text and "+b: 3" in text and "(current)" in text and "(after apply)" in text
    assert path.read_text(encoding="utf-8") == "a: 1\nb: 2\n"


def test_an_identical_target_is_unchanged(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("a: 1\n", encoding="utf-8")
    assert wizard._diff_lines("x", path, "a: 1\n")[1] == "  unchanged"


def test_secrets_are_masked_on_both_sides(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(f'{{"env": {{"ANTHROPIC_AUTH_TOKEN": "{SENTINEL}-old-value"}}}}\n', encoding="utf-8")
    new = f'{{"env": {{"ANTHROPIC_AUTH_TOKEN": "{TOKEN}"}}}}\n'
    text = "\n".join(wizard._diff_lines("settings.json", path, new))
    assert TOKEN not in text and SENTINEL not in text and "ABCDEFGHIJ" not in text
    missing = "\n".join(wizard._diff_lines("settings.json", tmp_path / "nope.json", new))
    assert TOKEN not in missing and "****" in missing


def test_the_targets_come_from_the_state_and_a_settings_env_is_masked(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "openrouter"
    executors = {"by_role": {"implementer": {"model": "openrouter/x/y"}}}
    targets = wizard._dry_run_targets(s, executors, {"ANTHROPIC_AUTH_TOKEN": TOKEN}, {"OTHER": "1"})
    labels = [t[0] for t in targets]
    assert labels == ["executors.yaml", "settings.json env"]
    out = "\n".join(l for label, path, text, cur in targets for l in wizard._diff_lines(label, path, text, cur))
    assert TOKEN not in out


def test_the_dry_run_prints_the_diffs_then_the_nothing_written_line(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    printed = []

    class C:
        def print(self, *a, **k):
            printed.append(" ".join(str(x) for x in a))
    monkeypatch.setattr(ui, "console", lambda: C())
    infos = []
    monkeypatch.setattr(ui, "info", lambda m: infos.append(m))
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "openrouter"
    before = sorted(p for p in tmp_path.rglob("*"))
    wizard._print_dry_run_diffs(s, {"by_role": {}}, {"ANTHROPIC_AUTH_TOKEN": TOKEN}, {})
    assert any("executors.yaml" in l for l in printed) and any("new file" in l for l in printed)
    assert TOKEN not in "\n".join(printed)
    assert infos[-1] == "DRY RUN: nothing was written"
    assert sorted(p for p in tmp_path.rglob("*")) == before


def test_an_unchanged_settings_env_names_no_key(tmp_path, monkeypatch):
    """The raw settings file holds more than `env`; the env object is what is compared."""
    from ai_resources.setup.cockpits import claude as claude_cockpit
    settings = tmp_path / "settings.json"
    settings.write_text('{"model": "x", "env": {"AGENT_SKILLS_ROOT": "/kit/skills"}}', encoding="utf-8")
    monkeypatch.setattr(claude_cockpit, "SETTINGS_PATH", settings)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    s = state.SetupState()
    s.mode = "single-model"
    env = {"AGENT_SKILLS_ROOT": "/kit/skills"}
    (label, path, text, cur), = wizard._dry_run_targets(s, {"by_role": {}}, env, dict(env))
    lines = wizard._diff_lines(label, path, text, cur)
    assert lines[1] == "  unchanged" and "AGENT_SKILLS_ROOT" not in "\n".join(lines)
