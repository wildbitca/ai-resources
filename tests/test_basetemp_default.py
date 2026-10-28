"""A bare pytest run keeps its temp tree in the kit's scratch root, never /tmp."""
from __future__ import annotations

from types import SimpleNamespace

import basetemp_default as bt
from ai_resources import scratch


def _config(basetemp=None):
    return SimpleNamespace(option=SimpleNamespace(basetemp=basetemp))


def _env(tmp_path, **extra):
    return {"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "cache"), **extra}


def test_default_goes_into_a_scratch_run(tmp_path):
    env = _env(tmp_path)
    config = _config()
    target = bt.apply_default(config, env)
    assert target is not None and config.option.basetemp == str(target)
    assert target.name == "pytest" and target.parent.parent == tmp_path / "cache" / "ai-resources" / "runs"
    assert target.parent.name.startswith("pytest-t-") and target.is_dir()
    assert not str(target).startswith("/tmp/pytest-of-")
    bt.finish_default(config, ok=True)


def test_green_and_red_runs_both_leave_nothing(tmp_path):
    for status in (None, 1):
        config = _config()
        run = bt.apply_default(config, _env(tmp_path)).parent
        config._ai_exit = status
        assert bt.finish_default(config, ok=status is None) == "removed" and not run.exists()
        assert bt.finish_default(config) is None          # idempotent


def test_home_cache_used_without_xdg(tmp_path):
    env = {"HOME": str(tmp_path / "home")}
    assert scratch.runs_dir(env) == tmp_path / "home" / ".cache" / "ai-resources" / "runs"


def test_explicit_basetemp_wins(tmp_path):
    config = _config(str(tmp_path / "mine"))
    assert bt.apply_default(config, _env(tmp_path)) is None
    assert config.option.basetemp == str(tmp_path / "mine")
    assert not (tmp_path / "cache").exists()


def test_temproot_and_explicit_tmpdir_are_honoured(tmp_path):
    for extra in ({"PYTEST_DEBUG_TEMPROOT": str(tmp_path / "root")}, {"TMPDIR": str(tmp_path / "t")}):
        config = _config()
        assert bt.apply_default(config, _env(tmp_path, **extra)) is None
        assert config.option.basetemp is None


def test_system_tmp_tmpdir_does_not_count_as_explicit(tmp_path):
    config = _config()
    assert bt.apply_default(config, _env(tmp_path, TMPDIR="/tmp")) is not None
    bt.finish_default(config, ok=True)


def test_a_cache_root_inside_tmp_is_refused():
    assert bt.apply_default(_config(), {"HOME": "/tmp/h", "XDG_CACHE_HOME": "/tmp/c"}) is None


def test_an_inherited_scratch_run_is_reused_not_owned(tmp_path):
    env = _env(tmp_path)
    run = scratch.new("verifier", "x", env=env)
    config = _config()
    target = bt.apply_default(config, {**env, scratch.ENV_SCRATCH: str(run), "TMPDIR": str(run / "tmp")})
    assert target == run / "pytest" and config.option.basetemp == str(target)
    assert bt.finish_default(config, ok=True) is None and run.is_dir()   # the owner cleans up
