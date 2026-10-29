"""The minimal OpenClaw backup (scripts/openclaw/openclaw-backup.sh, _backup_lib.sh).

Everything runs in a throwaway HOME against a fake `openclaw` that writes the snapshot layout of
`openclaw backup sqlite` (`<repository>/<timestamp>-<id>/{database.sqlite,manifest.json}`) and verifies
it with PRAGMA integrity_check. Nothing touches the host's state, units, gateway or chat channels.

  * allowlist and exclude rules, against a fixture home;
  * an end-to-end restore: build, check the sha256, extract, verify EVERY snapshot, check engram and key files;
  * lock contention: a second run waits, then fails loudly; it never exits 0 without a backup;
  * the failure assertions, the legacy `full` mode, retention, and the timer schedule.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts" / "openclaw"
LIB = SCRIPTS / "_backup_lib.sh"
TEMPLATES = REPO / "templates" / "systemd"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("sqlite3") is None or shutil.which("flock") is None,
    reason="bash, sqlite3 and flock are required")

FAKE_OPENCLAW = r'''#!/usr/bin/env python3
import json, os, sqlite3, sys, time, uuid
a = sys.argv[1:]
log = os.environ["FAKE_LOG"]
open(log, "a").write("openclaw " + " ".join(a) + "\n")
home = os.environ["HOME"]
if a[:1] == ["--version"]:
    print("OpenClaw 0.0.0-test"); sys.exit(0)
if a[:3] == ["agents", "list", "--json"]:
    print(open(os.environ["FAKE_AGENTS"]).read()); sys.exit(0)
if a[:3] == ["backup", "sqlite", "create"]:
    if os.environ.get("FAKE_CREATE_FAIL") and os.environ["FAKE_CREATE_FAIL"] in " ".join(a):
        print("boom", file=sys.stderr); sys.exit(1)
    if os.environ.get("FAKE_CREATE_SILENT") and os.environ["FAKE_CREATE_SILENT"] in " ".join(a):
        sys.exit(0)                                     # exits 0 and leaves no snapshot
    repo = a[a.index("--repository") + 1]
    if "--global" in a:
        src = os.path.join(home, ".openclaw", "state", "openclaw.sqlite")
    else:
        agent = a[a.index("--agent") + 1]
        src = os.path.join(home, ".openclaw", "agents", agent, "agent", "openclaw-agent.sqlite")
    snap = os.path.join(repo, time.strftime("%Y-%m-%dT%H-%M-%S-000Z-") + str(uuid.uuid4()))
    os.makedirs(snap, mode=0o700)
    conn = sqlite3.connect(src)
    conn.execute("VACUUM INTO ?", (os.path.join(snap, "database.sqlite"),))
    conn.close()
    open(os.path.join(snap, "manifest.json"), "w").write(json.dumps({"source": src}))
    print("SQLite snapshot created: " + snap); sys.exit(0)
if a[:3] == ["backup", "sqlite", "verify"]:
    snap = a[3]
    if not os.path.isfile(os.path.join(snap, "manifest.json")):
        print("no manifest", file=sys.stderr); sys.exit(1)
    ok = sqlite3.connect(os.path.join(snap, "database.sqlite")).execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        print(ok, file=sys.stderr); sys.exit(1)
    print("SQLite snapshot verified: " + snap); sys.exit(0)
if a[:2] == ["backup", "create"]:                        # the legacy full archive
    out = a[a.index("--output") + 1]
    import tarfile
    p = os.path.join(out, "2026-01-01T00-00-00.000Z-openclaw-backup.tar.gz")
    with tarfile.open(p, "w:gz") as t:
        t.add(os.path.join(home, ".openclaw", "openclaw.json"), arcname="openclaw.json")
    print("Archive verification: passed"); sys.exit(0)
sys.exit(0)
'''


def _db(path: pathlib.Path, rows: int = 2000):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(os.urandom(64).hex(),) for _ in range(rows)])
    conn.commit()
    conn.close()


def _put(root: pathlib.Path, rel: str, text: str = "x\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class Fixture:
    """A home with state to keep, state to leave out, and a fake openclaw/systemctl on PATH."""

    def __init__(self, tmp: pathlib.Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.bin = tmp / "stubs"
        self.run_dir = tmp / "run"
        self.backups = tmp / "backups"
        self.agents = tmp / "agents.json"
        self.log = tmp / "calls.log"
        for d in (self.home, self.bin, self.run_dir, self.backups):
            d.mkdir(parents=True, exist_ok=True)
        self.backups.chmod(0o755)
        self.log.write_text("", encoding="utf-8")
        h = self.home
        # --- to keep
        _put(h, ".openclaw/openclaw.json", '{"agents":{"defaults":{"model":{"primary":"m"}},"entries":{}}}')
        _put(h, ".openclaw/.env", "SECRET=1\n")
        _put(h, ".openclaw/credentials/token.json", "{}\n")
        _put(h, ".openclaw/gateway.systemd.env", "A=1\n")
        _put(h, ".openclaw/kit-host.env", "OPENCLAW_OWNER_TELEGRAM_ID=42\n")
        _put(h, ".openclaw/kit-host-overrides.json5", "{}\n")
        _put(h, ".openclaw/config-journal-fingerprint.key", "k\n")
        _put(h, ".openclaw/telegram.token", "t\n")
        _put(h, ".openclaw/extensions/e/index.js", "1\n")
        _put(h, ".openclaw/bin/tool", "#!/bin/sh\n")
        _put(h, ".openclaw/agents/main/agent/workshop-skills/s.md", "s\n")
        _put(h, ".openclaw/agents/main/sessions/s1.jsonl", "{}\n")
        _put(h, ".openclaw/agents/a1/sessions/s2.jsonl", "{}\n")
        _put(h, ".openclaw/workspace/AGENTS.md", "a\n")
        _put(h, ".openclaw/workspace/MEMORY.md", "m\n")
        _put(h, ".openclaw/workspace/USER.md", "u\n")
        _put(h, ".openclaw/workspace/SOUL.md", "s\n")
        _put(h, ".openclaw/workspace/IDENTITY.md", "i\n")
        _put(h, ".openclaw/workspace/TOOLS.md", "t\n")
        _put(h, ".openclaw/workspace/memory/2026-01-01.md", "n\n")
        _put(h, ".openclaw/workspace/monitors/m.json", "{}\n")
        _put(h, "Development/a1ws/AGENTS.md", "a1\n")
        _put(h, "Development/a1ws/MEMORY.md", "a1m\n")
        _put(h, ".claude/CLAUDE.md", "c\n")
        _put(h, ".claude/settings.json", "{}\n")
        _put(h, ".claude/hooks/h.sh", "#!/bin/sh\n")
        _put(h, ".config/shell/rc", "export A=1\n")
        for f in ("model-pins.json", "operator-rules.md", "lanes.yaml", "offbox-wif-cred.json", "setup-state.yaml"):
            _put(h, f".config/ai-resources/{f}", "x\n")
        _put(h, ".config/systemd/user/openclaw-backup-weekly.timer", "[Timer]\n")
        _put(h, ".config/systemd/user/openclaw-backup-weekly.timer.d/override.conf", "[Timer]\n")
        _put(h, ".local/bin/openclaw-backup.sh", "#!/bin/sh\n")
        _put(h, ".local/bin/openclaw-watchdog.sh", "#!/bin/sh\n")
        # --- to leave out: not allowlisted, regenerated by ai-resources setup, or noise
        _put(h, ".local/bin/bigbinary", "B" * 1000)
        _put(h, ".openclaw/tmp/junk", "j\n")
        _put(h, ".openclaw/gocache/junk", "j\n")
        _put(h, ".openclaw/browser/profile", "j\n")
        _put(h, ".openclaw/worktrees/w/file", "j\n")
        _put(h, ".openclaw/kit-roles/kit-role-x/AGENTS.md", "regenerated\n")
        _put(h, ".openclaw/kit-lanes/kit-lane-y/AGENTS.md", "regenerated\n")
        _put(h, ".claude/skills/s/SKILL.md", "regenerated\n")
        _put(h, ".claude/projects/p/t.jsonl", "history\n")
        _put(h, ".config/ai-resources/roles/r.md", "regenerated\n")
        _put(h, ".openclaw/workspace/node_modules/m/index.js", "j\n")
        for noise in (".openclaw/openclaw.json.bak", ".openclaw/openclaw.json.bak-123", ".openclaw/openclaw.json.bak.2",
                      ".openclaw/extensions/e/x.reconcile-1", ".openclaw/credentials/x.lock",
                      ".openclaw/agents/main/sessions/s1.jsonl-wal", ".openclaw/agents/main/sessions/s1.jsonl-shm"):
            _put(h, noise, "noise\n")
        # --- databases
        _db(h / ".openclaw/state/openclaw.sqlite")
        _db(h / ".openclaw/agents/main/agent/openclaw-agent.sqlite")
        _db(h / ".openclaw/agents/a1/agent/openclaw-agent.sqlite")
        (h / ".openclaw/agents/openclaw").mkdir(parents=True, exist_ok=True)       # an unconfigured directory
        _db(h / ".engram/engram.db")
        self.set_agents(("main", h / ".openclaw/workspace"), ("a1", h / "Development/a1ws"), ("ghost", None),
                        ("kit-role-x", h / ".openclaw/kit-roles/kit-role-x"))
        (self.bin / "openclaw").write_text(FAKE_OPENCLAW, encoding="utf-8")
        (self.bin / "openclaw").chmod(0o755)

    def set_agents(self, *rows):
        import json
        self.agents.write_text(json.dumps(
            [{"id": i, **({"workspace": str(w)} if w else {})} for i, w in rows]), encoding="utf-8")

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "HOME": str(self.home), "USER": "tester", "LOGNAME": "tester",
            "XDG_RUNTIME_DIR": str(self.run_dir), "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
            "OPENCLAW_EXTRA_PATH": str(self.bin), "OPENCLAW_BACKUP_DIR": str(self.backups),
            "OPENCLAW_HOST_ENV": str(self.home / ".openclaw/kit-host.env"),
            "OPENCLAW_BACKUP_MIN_BYTES": "1000",
            "FAKE_LOG": str(self.log), "FAKE_AGENTS": str(self.agents),
            "PATH": os.environ["PATH"],
        }
        env.update(extra)
        return env

    def run(self, tier: str = "manual", timeout: int = 120, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPTS / "openclaw-backup.sh"), tier], env=self.env(**extra),
                              capture_output=True, text=True, timeout=timeout)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def archives(self, tier: str = "manual") -> list[pathlib.Path]:
        return sorted((self.backups / tier).glob("openclaw-*.tar.gz"))

    def sent(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("openclaw message send")]


@pytest.fixture
def fx(tmp_path):
    return Fixture(tmp_path)


def _bash(script: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'. "{LIB}"; {script}'], env={"PATH": os.environ["PATH"], **env},
                          capture_output=True, text=True)


# --- the allowlist and the exclude rules -------------------------------------------------------------------------

def test_allowlist_has_every_required_path(fx):
    r = _bash(f'backup_allowlist "{fx.home}" "{fx.home}/.openclaw/workspace" "{fx.home}/Development/a1ws"')
    assert r.returncode == 0, r.stderr
    got = set(r.stdout.split())
    for want in (".openclaw/openclaw.json", ".openclaw/.env", ".openclaw/credentials", ".openclaw/gateway.systemd.env",
                 ".openclaw/kit-host.env", ".openclaw/kit-host-overrides.json5",
                 ".openclaw/config-journal-fingerprint.key", ".openclaw/telegram.token", ".openclaw/extensions",
                 ".openclaw/bin", ".openclaw/agents/main/agent/workshop-skills",
                 ".openclaw/agents/main/sessions", ".openclaw/agents/a1/sessions",
                 ".openclaw/workspace/AGENTS.md", ".openclaw/workspace/MEMORY.md", ".openclaw/workspace/USER.md",
                 ".openclaw/workspace/SOUL.md", ".openclaw/workspace/IDENTITY.md", ".openclaw/workspace/TOOLS.md",
                 ".openclaw/workspace/memory", ".openclaw/workspace/monitors",
                 "Development/a1ws/AGENTS.md", "Development/a1ws/MEMORY.md",
                 ".claude/CLAUDE.md", ".claude/settings.json", ".claude/hooks", ".config/shell",
                 ".config/ai-resources/model-pins.json", ".config/ai-resources/operator-rules.md",
                 ".config/ai-resources/lanes.yaml", ".config/ai-resources/offbox-wif-cred.json",
                 ".config/ai-resources/setup-state.yaml", ".config/systemd/user",
                 ".local/bin/openclaw-backup.sh", ".local/bin/openclaw-watchdog.sh"):
        assert want in got, f"missing from the allowlist: {want}"


def test_allowlist_leaves_out_what_the_kit_regenerates_and_what_is_noise(fx):
    r = _bash(f'backup_allowlist "{fx.home}" "{fx.home}/.openclaw/workspace"')
    got = r.stdout.split()
    for top in (".openclaw/tmp", ".openclaw/gocache", ".openclaw/browser", ".openclaw/worktrees", ".claude/skills",
                ".claude/projects", ".config/ai-resources/roles", ".local/bin/bigbinary", ".local/bin",
                ".openclaw/workspace/node_modules", ".openclaw/workspace", ".openclaw"):
        assert top not in got, f"{top} must not be allowlisted"
    assert not any(p.startswith((".claude/skills", ".openclaw/tmp", ".openclaw/gocache")) for p in got)


def test_allowlist_skips_the_workspaces_the_kit_regenerates(fx):
    r = _bash(f'backup_allowlist "{fx.home}" "{fx.home}/.openclaw/kit-roles/kit-role-x" '
              f'".openclaw/kit-lanes/kit-lane-y" "{fx.home}/.openclaw/workspace"')
    got = r.stdout.split()
    assert not any("kit-roles" in p or "kit-lanes" in p for p in got)
    assert ".openclaw/workspace/AGENTS.md" in got


def test_allowlist_skips_paths_that_do_not_exist_and_workspaces_outside_home(fx):
    shutil.rmtree(fx.home / ".openclaw/extensions")
    r = _bash(f'backup_allowlist "{fx.home}" "/somewhere/else/ws" "" "{fx.home}/nope"')
    assert r.returncode == 0
    assert ".openclaw/extensions" not in r.stdout.split()
    assert not any("somewhere" in ln or "nope" in ln for ln in r.stdout.splitlines())


def test_allowlist_has_no_duplicates(fx):
    r = _bash(f'backup_allowlist "{fx.home}" "{fx.home}/.openclaw/workspace" ".openclaw/workspace"')
    lines = r.stdout.split()
    assert len(lines) == len(set(lines))


def test_exclude_rules_drop_backups_locks_and_wal_sidecars(fx):
    out = fx.tmp / "x.tar"
    script = (f'backup_allowlist "{fx.home}" "{fx.home}/.openclaw/workspace" > "{fx.tmp}/l"; '
              f'ex=(); for p in "${{BACKUP_TAR_EXCLUDES[@]}}"; do ex+=("--exclude=$p"); done; '
              f'tar -C "{fx.home}" "${{ex[@]}}" -T "{fx.tmp}/l" -cf "{out}"')
    assert _bash(script).returncode == 0
    names = set(tarfile.open(out).getnames())
    assert ".openclaw/openclaw.json" in names
    assert ".openclaw/agents/main/sessions/s1.jsonl" in names
    assert ".config/systemd/user/openclaw-backup-weekly.timer.d/override.conf" in names
    for noise in (".openclaw/openclaw.json.bak", ".openclaw/openclaw.json.bak-123", ".openclaw/openclaw.json.bak.2",
                  ".openclaw/extensions/e/x.reconcile-1", ".openclaw/credentials/x.lock",
                  ".openclaw/agents/main/sessions/s1.jsonl-wal", ".openclaw/agents/main/sessions/s1.jsonl-shm"):
        assert noise not in names, noise


def test_pigz_is_used_when_present_and_gzip_otherwise(tmp_path):
    stub = tmp_path / "pigz"
    stub.write_text('#!/bin/sh\necho "pigz $*" >> "$FAKE_LOG"; exec gzip\n', encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "log"
    r = _bash(f'echo hi | backup_gzip | gzip -dc', PATH=f"{tmp_path}:{os.environ['PATH']}", FAKE_LOG=str(log))
    assert r.stdout.strip() == "hi" and "pigz -p 2" in log.read_text()
    only_gzip = tmp_path / "only"
    only_gzip.mkdir()
    for tool in ("gzip", "cat", "echo", "bash", "env", "sh"):
        w = shutil.which(tool)
        if w:
            (only_gzip / tool).symlink_to(w)
    r = _bash("echo hi | backup_gzip | gzip -dc", PATH=str(only_gzip))
    assert r.stdout.strip() == "hi"


# --- end to end: build, check, restore ---------------------------------------------------------------------------

def _extract(archive: pathlib.Path, into: pathlib.Path) -> pathlib.Path:
    into.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as t:
        t.extractall(into, filter="data")
    return into


def test_end_to_end_build_checksum_extract_verify_every_snapshot(fx):
    r = fx.run("manual")
    assert r.returncode == 0, r.stderr
    [archive] = fx.archives()
    assert re.fullmatch(r"openclaw-\d{8}-\d{6}\.tar\.gz", archive.name)
    sha = archive.with_name(archive.name + ".sha256")
    assert sha.is_file()
    assert subprocess.run(["sha256sum", "-c", sha.name], cwd=archive.parent, capture_output=True).returncode == 0
    # the sha256 is written last: nothing in the tier is newer than it, and no partial file is left behind
    assert sha.stat().st_mtime_ns >= archive.stat().st_mtime_ns
    assert not list((fx.backups / "manual").glob("*.part"))
    assert not list(fx.backups.glob("stage.*"))

    restore = _extract(archive, fx.tmp / "restore")
    # every snapshot: the verify command takes the timestamped subdirectory, not the repository
    snaps = sorted(p for p in (restore / "db").glob("*/*") if p.is_dir())
    assert {p.parent.name for p in snaps} == {"openclaw-global", "agent-main", "agent-a1"}
    env = fx.env()
    for snap in snaps:
        v = subprocess.run(["openclaw", "backup", "sqlite", "verify", str(snap)], capture_output=True, text=True,
                           env={**env, "PATH": f"{fx.bin}:{env['PATH']}"})
        assert v.returncode == 0, (snap, v.stderr)
    assert not (restore / "db" / "agent-ghost").exists()
    assert not (restore / "db" / "agent-openclaw").exists()

    engram = restore / "engram" / "engram.db"
    assert sqlite3.connect(engram).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert sqlite3.connect(engram).execute("SELECT count(*) FROM t").fetchone()[0] == 2000

    home2 = fx.tmp / "home2"
    home2.mkdir()
    subprocess.run(["tar", "xpf", str(restore / "files.tar"), "-C", str(home2)], check=True)
    for key in (".openclaw/openclaw.json", ".openclaw/.env", ".openclaw/credentials/token.json",
                ".openclaw/workspace/AGENTS.md", ".openclaw/workspace/MEMORY.md", ".openclaw/workspace/USER.md",
                ".openclaw/workspace/memory/2026-01-01.md", "Development/a1ws/AGENTS.md",
                ".config/ai-resources/offbox-wif-cred.json", ".config/systemd/user/openclaw-backup-weekly.timer",
                ".config/systemd/user/openclaw-backup-weekly.timer.d/override.conf", ".openclaw/bin/tool",
                ".local/bin/openclaw-backup.sh", ".claude/hooks/h.sh"):
        assert (home2 / key).is_file(), key
    for left_out in (".openclaw/kit-roles", ".openclaw/kit-lanes", ".local/bin/bigbinary", ".claude/skills", ".claude/projects", ".openclaw/tmp",
                     ".openclaw/openclaw.json.bak"):
        assert not (home2 / left_out).exists(), left_out
    assert (restore / "INVENTORY.txt").read_text().count("mode:        minimal") == 1


def test_the_archive_is_private_and_the_snapshots_are_taken_with_the_native_command(fx):
    assert fx.run("manual").returncode == 0
    creates = [c for c in fx.calls() if c.startswith("openclaw backup sqlite create")]
    assert any("--global" in c for c in creates)
    assert {m for c in creates for m in re.findall(r"--agent (\S+)", c)} == {"main", "a1"}   # ghost/openclaw skipped
    assert not any("openclaw backup create" in c for c in fx.calls())
    assert (fx.archives()[0].stat().st_mode & 0o077) == 0


def test_the_stage_is_private_and_on_the_backup_disk(fx):
    # a snapshot repository must be inside a 0700 stage that sits directly under the backup dir
    stub = fx.bin / "openclaw"
    text = stub.read_text().replace('os.makedirs(snap, mode=0o700)',
        'open(os.environ["FAKE_LOG"], "a").write("stage-mode %o parent %s\\n" % (os.stat(os.path.dirname(os.path.dirname(repo))).st_mode & 0o777, os.path.dirname(os.path.dirname(os.path.dirname(repo)))))\n    os.makedirs(snap, mode=0o700)')
    stub.write_text(text)
    assert fx.run("manual").returncode == 0
    modes = [c for c in fx.calls() if c.startswith("stage-mode")]
    assert modes and all(c.split()[1] == "700" and c.split()[3] == str(fx.backups) for c in modes)


def test_unconfigured_agent_directories_are_not_snapshotted(fx):
    fx.set_agents(("main", fx.home / ".openclaw/workspace"))
    assert fx.run("manual").returncode == 0
    creates = " ".join(c for c in fx.calls() if "sqlite create" in c)
    assert "--agent main" in creates and "--agent a1" not in creates and "--agent openclaw" not in creates


# --- lock handling -----------------------------------------------------------------------------------------------

def _hold_lock(fx: Fixture, seconds: float) -> subprocess.Popen:
    lock = fx.run_dir / "openclaw-backup.lock"
    p = subprocess.Popen(["flock", str(lock), "sleep", str(seconds)])
    for _ in range(100):
        if lock.exists():
            break
        time.sleep(0.02)
    time.sleep(0.2)
    return p


def test_a_run_that_cannot_take_the_lock_fails_loudly_and_notifies(fx):
    holder = _hold_lock(fx, 30)
    try:
        r = fx.run("weekly", OPENCLAW_BACKUP_LOCK_WAIT="1")
    finally:
        holder.kill()
        holder.wait()
    assert r.returncode == 75, (r.returncode, r.stdout, r.stderr)           # never 0 without a backup
    assert not fx.archives("weekly")
    assert not any("skipping" in line for line in r.stderr.splitlines())
    assert "waiting" in r.stderr and "could not take" in r.stderr
    sent = fx.sent()
    assert len(sent) == 1 and "weekly" in sent[0] and "failed" in sent[0]       # exactly one notification


def test_a_run_waits_for_the_lock_and_then_makes_its_backup(fx):
    holder = _hold_lock(fx, 2)
    try:
        r = fx.run("daily", OPENCLAW_BACKUP_LOCK_WAIT="30")
    finally:
        holder.kill()
        holder.wait()
    assert r.returncode == 0, r.stderr
    assert "waiting" in r.stderr
    assert len(fx.archives("daily")) == 1 and not fx.sent()


# --- the post-build assertions -----------------------------------------------------------------------------------

def test_a_failing_snapshot_command_fails_the_run_and_notifies(fx):
    r = fx.run("manual", FAKE_CREATE_FAIL="--agent a1")
    assert r.returncode != 0
    assert not fx.archives() and not list(fx.backups.glob("stage.*"))
    assert len(fx.sent()) == 1


def test_a_missing_global_snapshot_fails_the_run(fx):
    r = fx.run("manual", FAKE_CREATE_FAIL="--global")
    assert r.returncode != 0 and not fx.archives()


def test_a_snapshot_command_that_exits_zero_without_a_snapshot_is_caught(fx):
    r = fx.run("manual", FAKE_CREATE_SILENT="--agent main")
    assert r.returncode != 0 and not fx.archives()
    assert "agent-main" in r.stderr


def test_an_archive_below_the_minimum_size_is_refused(fx):
    r = fx.run("manual", OPENCLAW_BACKUP_MIN_BYTES="900000000")
    assert r.returncode != 0 and not fx.archives()
    assert not list((fx.backups / "manual").glob("*.part")) and "weighs only" in r.stderr
    assert len(fx.sent()) == 1


def test_an_archive_above_the_ceiling_is_refused(fx):
    r = fx.run("manual", OPENCLAW_BACKUP_MAX_BYTES="2000")
    assert r.returncode != 0 and not fx.archives() and "ceiling" in r.stderr


def test_a_missing_engram_database_is_caught_when_the_source_exists(fx):
    # engram.db vanishing between the snapshot and the listing is simulated by a sqlite3 that makes no file
    shim = fx.bin / "sqlite3"
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)
    r = fx.run("manual")
    assert r.returncode != 0 and not fx.archives() and "engram" in r.stderr


def test_a_broken_agent_list_fails_the_run(fx):
    fx.agents.write_text("not json", encoding="utf-8")
    r = fx.run("manual")
    assert r.returncode != 0 and not fx.archives()


def test_a_bad_tier_mode_or_retention_is_a_usage_error(fx):
    assert fx.run("hourly").returncode == 2
    assert fx.run("manual", OPENCLAW_BACKUP_MODE="huge").returncode == 2
    assert fx.run("manual", OPENCLAW_BACKUP_KEEP_MANUAL="0").returncode == 2
    assert fx.run("manual", OPENCLAW_BACKUP_KEEP_MANUAL="x").returncode == 2


# --- retention ---------------------------------------------------------------------------------------------------

def _seed(fx: Fixture, tier: str, count: int):
    d = fx.backups / tier
    d.mkdir(exist_ok=True)
    now = time.time()
    for i in range(count):
        t = d / f"openclaw-2026010{i + 1}-000000.tar.gz"
        t.write_bytes(b"old")
        t.with_name(t.name + ".sha256").write_text("x\n")
        os.utime(t, (now - 86400 * (count - i + 1),) * 2)


@pytest.mark.parametrize("tier,env,default", [("daily", "OPENCLAW_BACKUP_KEEP_DAILY", 7),
                                              ("weekly", "OPENCLAW_BACKUP_KEEP_WEEKLY", 4),
                                              ("monthly", "OPENCLAW_BACKUP_KEEP_MONTHLY", 3)])
def test_retention_defaults(fx, tier, env, default):
    _seed(fx, tier, default + 2)
    assert fx.run(tier).returncode == 0
    assert len(fx.archives(tier)) == default
    assert len(list((fx.backups / tier).glob("*.sha256"))) == default


def test_retention_is_configurable_and_the_new_archive_survives(fx):
    _seed(fx, "daily", 5)
    assert fx.run("daily", OPENCLAW_BACKUP_KEEP_DAILY="2").returncode == 0
    left = fx.archives("daily")
    assert len(left) == 2
    assert any(not p.name.startswith("openclaw-20260") for p in left)      # the fresh archive is among them


# --- the legacy full mode (rollback) -----------------------------------------------------------------------------

def test_full_mode_keeps_the_legacy_layout(fx):
    r = fx.run("manual", OPENCLAW_BACKUP_MODE="full")
    assert r.returncode == 0, r.stderr
    [archive] = fx.archives()
    names = set(tarfile.open(archive).getnames())
    assert {"openclaw-state.tar.gz", "ai-config.tar.gz", "INVENTORY.txt", "engram.db"} <= names
    assert not any(n.startswith("db/") for n in names)
    assert any("backup create" in c for c in fx.calls()) and not any("sqlite create" in c for c in fx.calls())
    cfg = tarfile.open(fileobj=tarfile.open(archive).extractfile("ai-config.tar.gz"), mode="r:gz").getnames()
    assert ".openclaw/openclaw.json" in cfg and ".openclaw/openclaw.json.bak" not in cfg


def test_minimal_is_the_default_mode(fx):
    assert fx.run("manual").returncode == 0
    assert any("sqlite create" in c for c in fx.calls())
    assert 'MODE="${OPENCLAW_BACKUP_MODE:-minimal}"' in (SCRIPTS / "openclaw-backup.sh").read_text()


# --- the schedule ------------------------------------------------------------------------------------------------

def _timer(name: str) -> dict[str, str]:
    out = {}
    for line in (TEMPLATES / f"openclaw-backup-{name}.timer.template").read_text().splitlines():
        if "=" in line and not line.startswith("["):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _minutes(spec: str) -> int:
    m = re.search(r"(\d\d):(\d\d):\d\d$", spec)
    return int(m.group(1)) * 60 + int(m.group(2))


def _delay(t: dict[str, str]) -> int:
    m = re.fullmatch(r"(\d+)min", t.get("RandomizedDelaySec", "0min"))
    return int(m.group(1)) if m else 0


def test_weekly_and_monthly_run_clear_of_the_daily_window():
    daily, weekly, monthly = _timer("daily"), _timer("weekly"), _timer("monthly")
    d_start = _minutes(daily["OnCalendar"])
    d_end = d_start + _delay(daily) + 30          # generous: a minimal run takes minutes, the old full one half an hour
    for name, t in (("weekly", weekly), ("monthly", monthly)):
        start = _minutes(t["OnCalendar"])
        assert start > d_end or start + _delay(t) < d_start, f"{name} overlaps the daily window"
    assert weekly["OnCalendar"] == "Sun *-*-* 05:00:00"
    assert weekly["OnCalendar"] != daily["OnCalendar"]
    # weekly and monthly stay clear of each other too, even on a Sunday the 1st
    assert abs(_minutes(weekly["OnCalendar"]) - _minutes(monthly["OnCalendar"])) >= 30


def test_the_template_agrees_with_the_known_local_weekly_dropin():
    """The operator's override.conf resets OnCalendar to Sun 05:00; the template now says the same."""
    assert _timer("weekly")["OnCalendar"] == "Sun *-*-* 05:00:00"


def test_install_units_leaves_a_local_dropin_alone(tmp_path):
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    from ai_resources import openclaw_host
    dest = tmp_path / "units"
    dropin = dest / "openclaw-backup-weekly.timer.d" / "override.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text("[Timer]\nOnCalendar=\nOnCalendar=Sun *-*-* 05:00:00\n")
    before = dropin.read_text()
    openclaw_host.install_units(dest, markers={"LIBEXEC": "/kit"}, runner=lambda *a, **k: (0, ""))
    assert dropin.read_text() == before
    assert "Sun *-*-* 05:00:00" in (dest / "openclaw-backup-weekly.timer").read_text()


# --- robustness: live files, atomic checksum, stale parts ---------------------------------------------------------

def test_a_file_that_changes_while_tar_reads_it_does_not_fail_the_backup(fx):
    shim = fx.bin / "tar"
    real = shutil.which("tar")
    shim.write_text(f'#!/bin/sh\n{real} "$@"; rc=$?\ncase "$*" in *--verbatim-files-from*) exit 1 ;; esac\nexit $rc\n')
    shim.chmod(0o755)
    r = fx.run("manual")
    assert r.returncode == 0, r.stderr
    assert len(fx.archives()) == 1


def test_a_real_tar_error_fails_the_backup(fx):
    shim = fx.bin / "tar"
    real = shutil.which("tar")
    shim.write_text(f'#!/bin/sh\n{real} "$@"; rc=$?\ncase "$*" in *--verbatim-files-from*) exit 2 ;; esac\nexit $rc\n')
    shim.chmod(0o755)
    r = fx.run("manual")
    assert r.returncode != 0 and not fx.archives()


def test_a_failing_sha256sum_leaves_no_tarball_and_no_empty_checksum(fx):
    shim = fx.bin / "sha256sum"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(0o755)
    r = fx.run("manual")
    assert r.returncode != 0
    assert not list((fx.backups / "manual").glob("*"))


def test_a_stale_part_file_is_swept_and_a_fresh_one_is_kept(fx):
    d = fx.backups / "manual"
    d.mkdir()
    old, new = d / "openclaw-20200101-000000.tar.gz.part", d / "openclaw-20200102-000000.tar.gz.part"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    t = time.time() - 7200
    os.utime(old, (t, t))
    assert fx.run("manual").returncode == 0
    assert not old.exists() and new.exists()


def test_a_future_dated_old_archive_cannot_push_out_the_fresh_one(fx):
    _seed(fx, "daily", 7)
    future = time.time() + 86400 * 30
    for p in fx.archives("daily"):
        os.utime(p, (future, future))
    assert fx.run("daily").returncode == 0
    names = [p.name for p in fx.archives("daily")]
    assert len(names) == 7 and any(not n.startswith("openclaw-20260") for n in names)
