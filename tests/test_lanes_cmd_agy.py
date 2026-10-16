"""Symmetric kit-side extensions of the lane commands (ADR-0007 section 11, P3-S6): extended tripwires, bootstrap
files, command snapshots, the run-pinned table hash and the quota circuit breaker.

Every case runs on both lane labels (claude-cli and agy-cli-lane) and must give the same verdict. Real git repos
under tmp_path, HOME redirected. The attribution forms and the quota texts are SYNTHETIC fixtures: V6 and V7
(the probe runs that would record the real ones) have not run.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

import lane_symmetry as sym
from ai_resources import agy_lane_gates as gates_mod, lanes_cmd, roles_policy as rp
from ai_resources.setup import lanes_render, state
from test_lanes_cmd import RUN, env, sh  # noqa: F401

LABELS = {
    "claude-cli": ("kit-lane-cli-sonnet55", "kit-lane-cli-opus55",
                   {"backend": "claude-cli", "model": "anthropic/claude-sonnet-5-5", "pool": "claude-subscription"},
                   {"backend": "claude-cli", "model": "anthropic/claude-opus-5-5", "pool": "claude-subscription"}),
    "agy-cli-lane": ("kit-lane-agy-sonnet55", "kit-lane-agy-opus55",
                     {"backend": "agy-cli-lane", "model": "agy-cli-lane/claude-sonnet-5-5", "pool": "agy-claude-gpt"},
                     {"backend": "agy-cli-lane", "model": "agy-cli-lane/claude-opus-5-5", "pool": "agy-claude-gpt"}),
}
BY_ROLE = {"implementer": {"model": "sonnet"}, "tester": {"model": "sonnet"}, "doc-writer": {"model": "sonnet"},
           "verifier": {"model": "opus"}}
ROLES = ["implementer", "tester", "doc-writer", "verifier"]


@pytest.fixture(autouse=True)
def _open(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setattr(lanes_render, "AGY_LANE_ADR_ACCEPTED", True)
    monkeypatch.setattr(gates_mod, "EVIDENCE",
                        {g.id: dataclasses.replace(g, status=gates_mod.PROVEN, evidence="test-fixture")
                         for g in gates_mod.GATES})


@pytest.fixture(params=sorted(LABELS))
def label(request):
    return request.param


def write_table(label, *, roles=ROLES, ack_roles=None, only_agent=True):
    sid, oid, sag, oag = LABELS[label]
    lanes = {"agents": {sid: sag, oid: oag},
             "roles": {r: {"candidates": [f"openclaw-agent:{oid if r == 'verifier' else sid}"]
                           + ([] if only_agent else ["claude-code"])} for r in roles},
             "second": {}, "orchestrators": [], "verify_capability": {}, "pool_caps": {},
             "max_remote_concurrency": 2, "workspace_root": ""}
    acks = {"anthropic": lanes_render.make_ack("anthropic", ROLES, "2026-10-16")}
    if label == "agy-cli-lane":
        lanes["gates"] = {"antigravity": True}
        acks["google"] = lanes_render.make_ack("google", ack_roles if ack_roles is not None else ROLES,
                                               "2026-10-16", account="op@example.test")
    doc = lanes_render.render_doc(lanes, BY_ROLE, "single-model", acks)
    path = state.lanes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lanes_render.dumps(doc))
    return doc


def lane_id(label, role):
    sid, oid, *_ = LABELS[label]
    return oid if role == "verifier" else sid


def prep(env, label, step, role, contract="report", *, model=None, scope=(), expect_head=None, run=None, lane=None,
         attempt=1):
    argv = ["prepare", "--step", step, "--role", role, "--contract", contract,
            "--model", model or ("opus" if role == "verifier" else "sonnet"),
            "--lane", lane or lane_id(label, role), "--attempt", str(attempt)]
    if scope:
        argv += ["--scope", *scope]
    if expect_head:
        argv += ["--expect-head", expect_head]
    if run:
        args = argv + ["--run", run, "--repo", str(env.repo)]
        rc = lanes_cmd.main(args, runner=env.runner)
        return rc, json.loads(env.capsys.readouterr().out.strip().splitlines()[-1])
    return env.run(*argv)


def verify(env, step, p, *extra, head=None):
    exp = f"{p['nonce']}:{p['snapshot_digest']}:{head or env.head}"
    return env.run("verify", "--step", step, "--expect", exp, *extra)


def flow_tester(env, label, mutate=None, step="tests"):
    rc, p = prep(env, label, step, "tester", "scope", scope=("tests/",))
    assert rc == 0, p
    wt = Path(p["worktree"])
    env.commit(wt, {"tests/test_b.py": "def test_b(): pass\n"})
    env.report("tester", step, base=wt)
    if mutate:
        mutate(env, wt)
    return p, wt


# --- tripwires (8.2) --------------------------------------------------------------------------------------

ADDED = ["~/.claude/CLAUDE.md", "~/.claude/agents/", "~/.claude/skills/", "~/.agents/", "~/.gitconfig",
         "~/.config/git/", "~/.ssh/", "~/.config/systemd/user/", "~/.local/bin/"]


def _touch(env, shown):
    rel = shown[2:]
    path = env.home / rel
    if shown.endswith("/"):
        path = path / "x.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("planted\n")


@pytest.mark.parametrize("shown", ADDED)
def test_a_change_to_an_added_persistence_channel_fails_verify_naming_the_path(env, label, shown):
    assert shown in rp.TRIPWIRE_PATHS or shown in rp.TRIPWIRE_STAT_PATHS
    write_table(label)
    p, _ = flow_tester(env, label, lambda e, wt: _touch(e, shown))
    rc, out = verify(env, "tests", p)
    assert rc == 1 and any(shown in r for r in out["reasons"]), out


def test_credentials_and_agy_state_changing_never_fail_a_step(env, label):
    write_table(label)

    def mutate(e, wt):
        for rel in (".gemini/antigravity-oauth-token", ".gemini/gemini-credentials.json",
                    ".gemini/antigravity-cli/state.json", ".gemini/conversations/a.pb"):
            e.write(rel, "changed\n", base=e.home)
    p, _ = flow_tester(env, label, mutate)
    rc, out = verify(env, "tests", p)
    assert rc == 0, out


def test_gemini_tripwires_are_derived_from_a_benign_turn_and_none_is_active_without_evidence():
    assert rp.gemini_tripwires() == ()
    assert not any(".gemini" in p for p in rp.TRIPWIRE_PATHS + rp.TRIPWIRE_STAT_PATHS)
    got = rp.gemini_tripwires({"~/.gemini/settings.json", "~/.gemini/config/"})
    assert "~/.gemini/settings.json" not in got and "~/.gemini/config/" not in got
    assert "~/.gemini/GEMINI.md" in got
    assert rp.OPENCLAW_CRON_STORE_PATH is None        # located by the harness, never guessed


# --- bootstrap files and attribution -----------------------------------------------------------------------

def test_an_untracked_bootstrap_file_in_the_worktree_passes(env, label):
    write_table(label)
    p, _ = flow_tester(env, label, lambda e, wt: e.write("SOUL.md", "soul\n", base=wt))
    rc, out = verify(env, "tests", p)
    assert rc == 0, out


@pytest.mark.parametrize("name", ["SOUL.md", "IDENTITY.md", "USER.md"])
def test_a_commit_adding_a_bootstrap_file_fails_even_for_the_integrating_writer(env, label, name):
    write_table(label)
    rc, p = prep(env, label, "impl", "implementer", "integrating")
    assert rc == 0, p
    wt = Path(p["worktree"])
    env.commit(wt, {"src/new.py": "x\n", name: "boot\n"})
    env.report("implementer", "impl", base=wt)
    rc, out = verify(env, "impl", p)
    assert rc == 1 and any(name in r and "bootstrap" in r for r in out["reasons"]), out


@pytest.mark.parametrize("msg", [
    "work\n\nCo-authored-by: Antigravity <noreply@google.com>",
    "work\n\nGenerated with Antigravity",
    "work \U0001F916",
])
def test_an_attribution_in_a_commit_message_fails_verify(env, label, msg):
    write_table(label)
    rc, p = prep(env, label, "tests", "tester", "scope", scope=("tests/",))
    wt = Path(p["worktree"])
    env.commit(wt, {"tests/test_b.py": "def test_b(): pass\n"}, msg=msg)
    env.report("tester", "tests", base=wt)
    rc, out = verify(env, "tests", p)
    assert rc == 1 and any("attribution" in r.lower() for r in out["reasons"]), out


# --- command snapshots (C3) --------------------------------------------------------------------------------

class Cmds:
    def __init__(self):
        self.crontab, self.timers, self.rc = "", "NEXT LEFT LAST PASSED UNIT ACTIVATES\n", 0

    def __call__(self, argv):
        if argv[0] == "crontab":
            return (0, self.crontab, "") if self.rc == 0 else (127, "", "")
        return (0, self.timers, "") if self.rc == 0 else (127, "", "")


def test_a_changed_crontab_or_timer_list_fails_verify_and_unavailable_at_both_ends_does_not(env, label, monkeypatch):
    write_table(label)
    cmds = Cmds()
    monkeypatch.setattr(lanes_cmd, "SNAPSHOT_RUNNER", cmds)
    p, _ = flow_tester(env, label, lambda e, wt: setattr(cmds, "crontab", "* * * * * evil\n"))
    rc, out = verify(env, "tests", p)
    assert rc == 1 and any("crontab -l" in r for r in out["reasons"]), out

    cmds2 = Cmds()
    monkeypatch.setattr(lanes_cmd, "SNAPSHOT_RUNNER", cmds2)
    p, _ = flow_tester(env, label, lambda e, wt: setattr(
        cmds2, "timers", "Thu 12:00 5min Thu 11:55 1min evil.timer evil.service\n"), step="tests2")
    rc, out = verify(env, "tests2", p)
    assert rc == 1 and any("list-timers" in r for r in out["reasons"]), out

    cmds3 = Cmds()
    cmds3.rc = 127                                              # both ends: the commands are unavailable
    monkeypatch.setattr(lanes_cmd, "SNAPSHOT_RUNNER", cmds3)
    p, _ = flow_tester(env, label, None, step="tests3")
    rc, out = verify(env, "tests3", p)
    assert rc == 0, out


def test_a_timer_that_only_moves_its_next_run_does_not_fail_a_step(env, label, monkeypatch):
    write_table(label)
    cmds = Cmds()
    cmds.timers = "Thu 12:00 5min Thu 11:55 1min backup.timer backup.service\n"
    monkeypatch.setattr(lanes_cmd, "SNAPSHOT_RUNNER", cmds)
    p, _ = flow_tester(env, label, lambda e, wt: setattr(
        cmds, "timers", "Thu 12:05 5min Thu 12:00 1min backup.timer backup.service\n"))
    rc, out = verify(env, "tests", p)
    assert rc == 0, out


# --- the run-pinned table hash (5.7) ----------------------------------------------------------------------

def _rewrite_resolved(label):
    path = state.lanes_path()
    import yaml
    doc = yaml.safe_load(path.read_text())
    doc["resolved"]["roles"]["implementer"]["reason"] = "forged"
    doc["digest"] = lanes_render.doc_digest(doc)
    path.write_text(lanes_render.dumps(doc))


def test_the_first_prepare_pins_the_table_and_a_forged_rewrite_is_refused_naming_the_run(env, label):
    write_table(label)
    rc, p = prep(env, label, "impl", "implementer", "report")
    assert rc == 0, p
    tables = [e for e in env.ledger_entries(env.state) if e["kind"] == "table"]
    assert len(tables) == 1
    assert tables[0]["data"]["sha256"] == hashlib.sha256(state.lanes_path().read_bytes()).hexdigest()
    _rewrite_resolved(label)                                    # digest recomputed: the file still validates
    rc, out = prep(env, label, "impl2", "implementer", "report")
    assert rc == 1 and any(RUN in r and "lanes.yaml changed" in r for r in out["reasons"]), out
    rc, out = prep(env, label, "impl3", "implementer", "report", run="run2")     # a new run binds to the new table
    assert rc == 0, out


def test_a_table_entry_after_the_witnessed_head_carries_no_authority(env, label):
    write_table(label)
    rc, first = prep(env, label, "impl", "implementer", "report")
    start_head = first["ledger_head"]
    assert rc == 0
    # only the entries at or before this head count: a table entry minted later is not consulted
    env.head = start_head
    _rewrite_resolved(label)
    rc, out = prep(env, label, "impl2", "implementer", "report", expect_head=start_head)
    assert rc == 1 and any("lanes.yaml changed" in r for r in out["reasons"]), out    # the pin before the head holds
    genesis = "0" * 64
    rc, out = prep(env, label, "impl3", "implementer", "report", expect_head=genesis, run="run3")
    assert rc == 0, out                                        # an empty witnessed chain: nothing pinned yet


# --- prepare --lane and the acknowledgement ------------------------------------------------------------------

def test_a_lane_for_a_role_the_acknowledgement_does_not_list_is_refused(env, label):
    # the agy table lists only the tester; claude-cli's Anthropic ack is not in lanes.yaml, so the resolved
    # table decides: the unlisted role has no admitted lane agent in `resolved:` either way
    write_table(label, ack_roles=["tester"])
    rc, out = prep(env, label, "impl", "implementer", "report")
    if label == "agy-cli-lane":
        assert rc == 1 and out["reasons"], out
    else:
        assert rc == 0, out


def test_an_agy_lane_prepare_records_the_gate_statuses_in_the_ledger(env):
    write_table("agy-cli-lane")
    rc, out = prep(env, "agy-cli-lane", "impl", "implementer", "report")
    assert rc == 0, out
    entry = env.ledger_entries(env.state)[0]["data"]["lane"]
    assert set(entry["agy_gates"]) == {f"G{i}" for i in range(8)}


def test_a_forged_table_that_lists_an_agy_agent_is_refused_without_the_flag_or_gates(env, monkeypatch):
    write_table("agy-cli-lane")
    unproven = {g.id: g for g in gates_mod.GATES}
    monkeypatch.setattr(gates_mod, "EVIDENCE", unproven)         # evidence back to not proven; HOME stays redirected
    rc, out = prep(env, "agy-cli-lane", "impl", "implementer", "report")
    assert rc == 1 and any("not writable" in r for r in out["reasons"]), out
    assert state.lanes_path().is_relative_to(env.tmp)


def test_a_forged_table_without_the_flag_or_the_role_in_its_google_ack_is_refused_even_when_writable(env):
    import yaml
    doc = write_table("agy-cli-lane")
    path = state.lanes_path()
    forged = yaml.safe_load(path.read_text())
    forged["gates"] = {"antigravity": False}
    forged["acks"]["google"]["roles"] = ["doc-writer"]
    forged["digest"] = lanes_render.doc_digest(forged)
    path.write_text(lanes_render.dumps(forged))
    rc, out = prep(env, "agy-cli-lane", "impl", "implementer", "report")
    assert rc == 1, out
    text = " ".join(out["reasons"])
    assert "--lanes-antigravity is off" in text and "does not list implementer" in text


# --- the circuit breaker (7.1, C5) ----------------------------------------------------------------------------

QUOTA = "Error: quota exhausted for model, retry after 5h"


@pytest.fixture
def patterns(monkeypatch):
    monkeypatch.setattr(rp, "QUOTA_PATTERNS", {"claude-subscription": (r"quota exhausted",),
                                               "agy-claude-gpt": (r"quota exhausted",), "agy-gemini": ()})


def _spawn_error(env, text=QUOTA, name="spawn-error.txt"):
    path = env.tmp / "orchestrator" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def _trip(env, label, *, text=QUOTA):
    write_table(label, only_agent=False)
    rc, p = prep(env, label, "tests", "tester", "scope", scope=("tests/",))
    assert rc == 0, p
    wt = Path(p["worktree"])
    env.commit(wt, {"tests/test_b.py": "def test_b(): pass\n"})
    env.report("tester", "tests", base=wt)
    rc, out = verify(env, "tests", p, "--spawn-error", str(_spawn_error(env, text)))
    return rc, out


def test_a_spawn_error_with_the_recorded_text_trips_the_pool_and_refuses_the_lane_for_the_run(env, label, patterns):
    rc, out = _trip(env, label)
    assert rc == 0 and out["breaker"]["pool"] == LABELS[label][2]["pool"], out
    breakers = [e for e in env.ledger_entries(env.state) if e["kind"] == "breaker"]
    assert len(breakers) == 1 and breakers[0]["data"]["evidence_sha256"]
    rc, out = prep(env, label, "impl", "implementer", "report")
    assert rc == 1 and any("exhausted" in r for r in out["reasons"]), out
    rc, out = prep(env, label, "ver", "verifier", "report")        # a checker fails with the reason, never waits
    assert rc == 1 and any("exhausted" in r for r in out["reasons"]), out


def test_a_writer_moves_to_the_next_admitted_candidate_with_the_recorded_reason(env, label, patterns):
    _trip(env, label)
    rc, out = prep(env, label, "impl", "implementer", "report", lane="claude-code")
    assert rc == 0, out
    lane = [e for e in env.ledger_entries(env.state) if e["kind"] == "prepare" and e["step"] == "impl"][0]["data"]["lane"]
    assert lane["source"] == "fallback" and lane["reason"].startswith("pool exhausted:")


def test_the_same_text_written_by_the_child_trips_nothing(env, label, patterns):
    write_table(label, only_agent=False)
    rc, p = prep(env, label, "tests", "tester", "scope", scope=("tests/",))
    wt = Path(p["worktree"])
    env.commit(wt, {"tests/test_b.py": "def test_b(): pass\n"})
    env.write(".agent-output/tester/tests.md", QUOTA + "\n", base=wt)
    rc, out = verify(env, "tests", p)
    assert rc == 0 and "breaker" not in out
    assert not [e for e in env.ledger_entries(env.state) if e["kind"] == "breaker"]


def test_a_spawn_error_path_inside_the_run_tree_or_a_worktree_is_refused(env, label, patterns):
    write_table(label, only_agent=False)
    rc, p = prep(env, label, "tests", "tester", "scope", scope=("tests/",))
    wt = Path(p["worktree"])
    env.commit(wt, {"tests/test_b.py": "def test_b(): pass\n"})
    env.report("tester", "tests", base=wt)
    inside_tree = env.write("note.txt", QUOTA)
    rc, out = verify(env, "tests", p, "--spawn-error", str(inside_tree))
    assert rc == 1 and any("lies inside" in r for r in out["reasons"])
    inside_wt = env.write("note2.txt", QUOTA, base=wt)
    rc, out = verify(env, "tests", p, "--spawn-error", str(inside_wt))
    assert rc == 1 and any("lies inside" in r for r in out["reasons"])


def test_a_breaker_after_the_expect_head_is_ignored_and_without_it_is_honoured(env, label, patterns):
    write_table(label, only_agent=False)
    rc, first = prep(env, label, "tests", "tester", "scope", scope=("tests/",))
    head_before = first["ledger_head"]
    wt = Path(first["worktree"])
    env.commit(wt, {"tests/test_b.py": "def test_b(): pass\n"})
    env.report("tester", "tests", base=wt)
    rc, out = verify(env, "tests", first, "--spawn-error", str(_spawn_error(env)))
    assert rc == 0 and out["breaker"]
    rc, out = prep(env, label, "impl", "implementer", "report", expect_head=head_before)
    assert rc == 0, out                                         # minted after the witnessed head: no authority
    rc, out = prep(env, label, "impl2", "implementer", "report")
    assert rc == 1                                              # without --expect-head every entry is honoured


def test_the_shipped_pattern_table_is_empty_until_a_probe_run_records_a_text():
    assert all(not v for v in rp.QUOTA_PATTERNS.values())
    assert rp.classify_quota(QUOTA, "agy-claude-gpt") is None


# --- AC3.9: every kit command differs by label only in the allowlisted fields ----------------------------------

# Per-run values: random or derived from the run id and the clock. They carry no label-specific meaning.
VOLATILE = frozenset({"nonce", "snapshot_digest", "ledger_head", "prev", "hash", "ts", "time", "record", "digest"})


def _cmd(env, run, *argv):
    rc = lanes_cmd.main([*argv, "--run", run, "--repo", str(env.repo)], runner=env.runner)
    return rc, json.loads(env.capsys.readouterr().out.strip().splitlines()[-1])


def _commands_for(env, label):
    run = "sym-" + label.split("-")[0]
    write_table(label)
    out = {}
    out["prepare_tester"] = prep(env, label, "tests", "tester", "scope", scope=("tests/",), run=run)
    rc, p = out["prepare_tester"]
    assert rc == 0, p
    wt = Path(p["worktree"])
    env.commit(wt, {"tests/test_b.py": "def test_b(): pass\n"})
    env.report("tester", "tests", base=wt)
    out["verify_tester"] = _cmd(env, run, "verify", "--step", "tests", "--expect",
                                f"{p['nonce']}:{p['snapshot_digest']}:{p['ledger_head']}")
    rc, v = out["prepare_verifier"] = prep(env, label, "check", "verifier", "report", run=run, expect_head=None)
    assert rc == 0, v
    env.report("verifier", "check", "PASS")
    out["count"] = _cmd(env, run, "count", "--pool", "claude-subscription" if label == "claude-cli"
                        else "agy-claude-gpt")
    return {k: {"rc": rc, "out": o} for k, (rc, o) in out.items()}, run


def test_ac3_9_every_kit_command_differs_by_label_only_in_the_allowlisted_fields(env):
    results = {}
    for label in ("claude-cli", "agy-cli-lane"):
        got, run = _commands_for(env, label)
        results[label] = sym.normalize(got, drop=VOLATILE, replace={run: "RUN", f"sym-{label.split('-')[0]}": "RUN"})
    cli, agy = results["claude-cli"], results["agy-cli-lane"]
    assert {k: v["rc"] for k, v in cli.items()} == {k: v["rc"] for k, v in agy.items()}
    assert sym.unexplained(cli, agy) == set(), sorted(sym.diff_paths(cli, agy))
