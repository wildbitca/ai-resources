"""A bare pytest run keeps its temp tree under the user cache, never /tmp."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import basetemp_default as bt


def _config(basetemp=None):
    return SimpleNamespace(option=SimpleNamespace(basetemp=basetemp))


def _env(tmp_path, **extra):
    return {"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "cache"), **extra}


def test_default_goes_under_cache_root(tmp_path):
    env = _env(tmp_path)
    config = _config()
    target = bt.apply_default(config, env)
    assert target is not None and config.option.basetemp == str(target)
    assert target.parent == tmp_path / "cache" / "ai-resources-pytest-runs"
    assert not str(target).startswith("/tmp/pytest-of-")


def test_home_cache_used_without_xdg(tmp_path):
    env = {"HOME": str(tmp_path / "home")}
    assert bt.cache_root(env) == tmp_path / "home" / ".cache" / "ai-resources-pytest-runs"


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


def test_run_ids_differ_between_concurrent_runs(tmp_path):
    root = tmp_path / "root"
    assert bt.run_dir(root, pid=100, now=1.0) != bt.run_dir(root, pid=101, now=1.0)
    assert bt.run_dir(root, pid=100, now=1.0) != bt.run_dir(root, pid=100, now=1.0)


def test_prune_removes_only_old_run_dirs_under_root(tmp_path):
    root = tmp_path / "root"
    old, new, other = root / "run-old", root / "run-new", root / "keep-me"
    for d in (old, new, other):
        (d / "x").mkdir(parents=True)
    outside = tmp_path / "run-outside"
    outside.mkdir()
    past = time.time() - 10 * 3600
    for d in (old, other, outside):
        os.utime(d, (past, past))
    removed = bt.prune(root)
    assert removed == [old]
    assert not old.exists() and new.exists() and other.exists() and outside.exists()
