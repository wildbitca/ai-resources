"""Stale stage.* cleanup in scripts/openclaw/openclaw-backup.sh.

The static test reads only the script text. The behavioural test runs the real script, so it is integration.
"""
from __future__ import annotations

import os
import pathlib
import re
import time

import pytest

from test_openclaw_backup_minimal import Fixture

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "openclaw" / "openclaw-backup.sh"


def test_stale_stage_cleanup_sits_between_the_lock_and_the_new_stage():
    text = SCRIPT.read_text(encoding="utf-8")
    lock = text.index("flock -w")
    cleanup = re.search(r"""find "\$BASE" -maxdepth 1 -type d -name 'stage\.\*' -mmin \+60 -print0""", text)
    assert cleanup, "the stale stage cleanup line is missing"
    stage = text.index('STAGE="$(scratch_tmp_beside')
    assert lock < cleanup.start() < stage


@pytest.mark.integration
def test_stale_stage_dirs_are_removed_and_everything_else_is_kept(tmp_path):
    fx = Fixture(tmp_path)
    b = fx.backups
    stale, fresh, other = b / "stage.AAAAAAAA", b / "stage.BBBBBBBB", b / "keepme"
    for d in (stale, fresh, other):
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "f").write_text("x")
    stale_file = b / "stage.file"          # not a directory: left alone
    stale_file.write_text("x")
    stale_link = b / "stage.link"          # a symlink to a dir outside $BASE: not followed, target intact
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "f").write_text("x")
    stale_link.symlink_to(outside)
    (b / "daily").mkdir()
    (b / "daily" / "openclaw-20260101-000000.tar.gz").write_bytes(b"old")
    old = time.time() - 7200
    for p in (stale, stale_file, other, b / "daily"):
        os.utime(p, (old, old))
    os.utime(stale_link, (old, old), follow_symlinks=False)
    r = fx.run("manual")
    assert r.returncode == 0, r.stderr
    assert not stale.exists()
    assert fresh.exists() and other.exists() and stale_file.exists()
    assert (b / "daily" / "openclaw-20260101-000000.tar.gz").exists()
    assert "removed 1 stale stage dir(s)" in r.stderr
    assert stale_link.is_symlink() and (outside / "sub" / "f").exists()
    assert {p.name for p in b.glob("stage.*")} == {"stage.BBBBBBBB", "stage.file", "stage.link"}   # the run's own stage is gone


@pytest.mark.integration
def test_no_stale_stage_means_no_log_line(tmp_path):
    fx = Fixture(tmp_path)
    r = fx.run("manual")
    assert r.returncode == 0, r.stderr
    assert "stale stage" not in r.stderr


@pytest.mark.integration
def test_the_logged_count_is_the_number_removed(tmp_path):
    fx = Fixture(tmp_path)
    old = time.time() - 7200
    for n in ("AAAAAAAA", "BBBBBBBB", "CCCCCCCC"):
        d = fx.backups / f"stage.{n}"
        d.mkdir()
        os.utime(d, (old, old))
    r = fx.run("manual")
    assert r.returncode == 0, r.stderr
    assert "removed 3 stale stage dir(s)" in r.stderr
