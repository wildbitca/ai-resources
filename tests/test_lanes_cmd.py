"""`ai-resources lanes prepare|verify|integrate|land|count` (ADR-0007): offline, real git repos, a fake
`openclaw worktrees` runner that mirrors V10's branch naming (`openclaw/<name>`, branch kept on remove)."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from ai_resources import lanes_cmd

RUN = "run1"
SONNET = "sonnet"
OPUS = "opus"
ENV_GIT = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def sh(args, cwd, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode:
        raise AssertionError(f"{args}: {p.stderr}")
    return p.stdout.strip()


class FakeRunner:
    """Real git, fake `openclaw worktrees create|remove` (V10: branch openclaw/<name>, branch kept)."""

    def __init__(self, managed: Path):
        self.managed = managed
        self.log: list[list[str]] = []
        self.created: dict[str, Path] = {}
        self.fail_create = 0

    def __call__(self, argv, cwd=None):
        self.log.append(list(argv))
        if argv[0] == "openclaw":
            return self._openclaw(argv)
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def _openclaw(self, argv):
        assert argv[1] == "worktrees"
        if argv[2] == "create":
            if self.fail_create:
                self.fail_create -= 1
                return 1, "", "OpenClawStateLeaseError"
            repo, name, base = argv[3], argv[argv.index("--name") + 1], argv[argv.index("--base-ref") + 1]
            path = self.managed / name
            sh(["git", "worktree", "add", "-q", "-b", f"openclaw/{name}", str(path), base], repo)
            self.created[name] = path
            return 0, json.dumps({"id": name, "path": str(path), "branch": f"openclaw/{name}"}), ""
        if argv[2] == "remove":
            wid = argv[3]
            path = self.created.pop(wid)
            sh(["git", "worktree", "remove", "--force", str(path)], path.parent.parent / "repo", check=False)
            return 0, "", ""
        raise AssertionError(argv)


class Orch:
    """The orchestrator: keeps the last printed ledger head and passes `--expect` back."""

    def __init__(self, tmp_path, runner):
        self.repo = tmp_path / "repo"
        self.runner = runner
        self.head = None
        self.capsys = None

    def run(self, *argv):
        args = list(argv)
        args += ["--run", RUN, "--repo", str(self.repo)]
        rc = lanes_cmd.main(args, runner=self.runner)
        out = json.loads(self.capsys.readouterr().out.strip().splitlines()[-1])
        if out.get("ledger_head"):
            self.head = out["ledger_head"]
        return rc, out

    def prepare(self, step, role, contract, model=SONNET, *, scope=(), attempt=1, reenter=False, pool=None, cap=None):
        argv = ["prepare", "--step", step, "--role", role, "--contract", contract, "--model", model,
                "--attempt", str(attempt)]
        if scope:
            argv += ["--scope", *scope]
        if reenter:
            argv.append("--reenter")
        if pool:
            argv += ["--pool", pool]
        if cap is not None:
            argv += ["--cap", str(cap)]
        return self.run(*argv)

    def expect(self, prep):
        return f"{prep['nonce']}:{prep['snapshot_digest']}:{self.head}"

    def verify(self, step, prep, *, attempt=1, in_flight=(), subject=None, report=None, head=None):
        exp = f"{prep['nonce']}:{prep['snapshot_digest']}:{head or self.head}"
        argv = ["verify", "--step", step, "--attempt", str(attempt), "--expect", exp]
        if in_flight:
            argv += ["--in-flight", *in_flight]
        if subject:
            argv += ["--subject", subject]
        if report:
            argv += ["--report", report]
        return self.run(*argv)

    def integrate(self, step, src, prep, *, head=None):
        return self.run("integrate", "--step", step, "--from", src, "--expect",
                        f"{prep['nonce']}:{prep['snapshot_digest']}:{head or self.head}")

    def land(self, step, prep, iteration=None, *, head=None):
        argv = ["land", "--step", step, "--expect", f"{prep['nonce']}:{prep['snapshot_digest']}:{head or self.head}"]
        if iteration:
            argv += ["--iteration", str(iteration)]
        return self.run(*argv)

    def count(self, pool, role="", step="", cap=None):
        argv = ["count", "--pool", pool]
        if role:
            argv += ["--role", role]
        if step:
            argv += ["--step", step]
        if cap is not None:
            argv += ["--cap", str(cap)]
        return self.run(*argv)

    # -- child simulation --
    def write(self, rel, text="x\n", base=None):
        p = Path(base or self.repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def commit(self, wt, files: dict, msg="work"):
        for rel, text in files.items():
            self.write(rel, text, base=wt)
        sh(["git", "add", "-A"], wt)
        sh(["git", "commit", "-q", "-m", msg], wt)
        return sh(["git", "rev-parse", "HEAD"], wt)

    def report(self, role, step, verdict=None, base=None):
        body = "report\n" + (f"Verdict: {verdict}\n" if verdict else "")
        return self.write(f".agent-output/{role}/{step}.md", body, base=base)

    def verifier(self, step, subject_step, tip, verdict="PASS", model=OPUS, attempt=1):
        rc, prep = self.prepare(step, "verifier", "report", model, attempt=attempt)
        assert rc == 0, prep
        self.report("verifier", step, verdict)
        return self.verify(step, prep, subject=f"{subject_step}@{tip}", attempt=attempt)

    def ledger_entries(self, state):
        path = state / "ai-resources" / "lanes" / RUN / "ledger.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture()
def env(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    for k, v in ENV_GIT.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(lanes_cmd, "RETRY_DELAY_S", 0)
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(["git", "init", "-q", "-b", "main"], repo)
    for rel, text in {"src/app.py": "print('app')\n", "tests/test_a.py": "def test_a(): pass\n",
                      "docs/a.md": "# a\n", "README.md": "readme\n", ".gitignore": "__pycache__/\n"}.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-q", "-m", "base"], repo)
    (repo / "handoff.md").write_text("handoff\n")           # untracked, like the real workflow's handoff
    runner = FakeRunner(tmp_path / "managed")
    (tmp_path / "managed").mkdir()
    o = Orch(tmp_path, runner)
    o.capsys = capsys
    o.state = state
    o.home = home
    o.tmp = tmp_path
    return o


def head_of(repo):
    return sh(["git", "rev-parse", "HEAD"], repo)


def tracked_state(repo):
    return sh(["git", "ls-files", "-s"], repo) + sh(["git", "diff", "HEAD"], repo) + head_of(repo)


# -- prepare -----------------------------------------------------------------------------------------------

def test_prepare_creates_a_chained_ledger_under_xdg_state_and_prints_the_triple(env):
    rc, out = env.prepare("explore1", "explore", "report", "haiku")
    assert rc == 0 and out["result"] == "PREPARED"
    assert re.fullmatch(r"[0-9a-f]{32}", out["nonce"])
    assert re.fullmatch(r"[0-9a-f]{64}", out["snapshot_digest"]) and re.fullmatch(r"[0-9a-f]{64}", out["ledger_head"])
    ledger = env.state / "ai-resources" / "lanes" / RUN / "ledger.jsonl"
    assert ledger.is_file() and not str(ledger).startswith("/tmp/")
    entries = env.ledger_entries(env.state)
    assert entries[0]["prev"] == "0" * 64 and entries[-1]["hash"] == out["ledger_head"]
    for prev, cur in zip(entries, entries[1:]):
        assert cur["prev"] == prev["hash"]
    assert entries[0]["data"]["model"] == "haiku" and entries[0]["data"]["family"] == "haiku"
    rec = env.state / "ai-resources" / "lanes" / RUN / "records" / entries[0]["record"]
    assert rec.is_file()


def test_every_git_call_neutralises_fsmonitor_and_hooks(env):
    env.prepare("s1", "tester", "scope", scope=["tests/"])
    git_calls = [a for a in env.runner.log if a[0] == "git" and a[1] != "worktree"]
    assert git_calls
    for argv in git_calls:
        assert "core.fsmonitor=false" in argv and "core.hooksPath=/dev/null" in argv, argv


def test_prepare_records_the_model_ref_and_its_family(env):
    rc, _ = env.prepare("s1", "tester", "report", "anthropic/claude-sonnet-5-5")
    assert rc == 0
    d = env.ledger_entries(env.state)[0]["data"]
    assert d["model"] == "anthropic/claude-sonnet-5-5" and d["family"] == "sonnet"


def test_prepare_refuses_a_dirty_tracked_tree_for_scope_and_integrating(env):
    env.write("src/app.py", "changed\n")
    rc, out = env.prepare("s1", "tester", "scope", scope=["tests/"])
    assert rc == 1 and "uncommitted tracked changes" in out["reasons"][0]
    rc, out = env.prepare("i1", "implementer", "integrating")
    assert rc == 1
    rc, _ = env.prepare("r1", "explore", "report", "haiku")     # report contract is read-only: allowed
    assert rc == 0


def test_prepare_validates_role_contract_and_scope(env):
    assert env.prepare("s1", "made-up", "report")[0] == 1
    assert env.prepare("s2", "verifier", "scope", scope=["tests/"])[0] == 1
    assert env.prepare("s3", "tester", "scope")[0] == 1
    assert env.prepare("s4", "tester", "scope", scope=["../x"])[0] == 1
    assert env.prepare("s5", "tester", "scope", scope=[".git/hooks/"])[0] == 1
    assert env.prepare("s6", "explore", "report", "haiku", scope=["tests/"])[0] == 1
    assert env.prepare("s7", "tester", "report", attempt=4)[0] == 1


def test_prepare_retries_the_worktree_create_once_after_a_lease_error(env):
    env.runner.fail_create = 1
    rc, out = env.prepare("s1", "tester", "scope", scope=["tests/"])
    assert rc == 0 and out["worktree"]
    creates = [a for a in env.runner.log if a[:3] == ["openclaw", "worktrees", "create"]]
    assert len(creates) == 2


def test_the_state_directory_is_never_under_tmp_when_xdg_is_set(env):
    assert lanes_cmd.state_root() == env.state / "ai-resources" / "lanes"


# -- report contract ---------------------------------------------------------------------------------------

def test_report_contract_passes_on_the_step_scoped_file(env):
    rc, prep = env.prepare("review", "code-reviewer", "report")
    env.report("code-reviewer", "review")
    rc, out = env.verify("review", prep)
    assert rc == 0 and out["result"] == "PASS", out


@pytest.mark.parametrize("mutate", [
    lambda o: o.write(".agent-output/code-reviewer/review-b.md"),
    lambda o: o.write(".agent-output/planner/review.md"),
    lambda o: o.write(".agent-output/planner/other.md"),
    lambda o: o.write("handoff.md", "tampered\n"),
    lambda o: o.write(".agent-output/handoff.md", "tampered\n"),
    lambda o: o.write("src/app.py", "tampered\n"),
    lambda o: o.write("src/brand_new.py"),
], ids=["same-role-other-step", "other-role-same-step", "other-step", "handoff", "agent-handoff", "code",
        "new-code"])
def test_report_contract_fails_on_any_other_write(env, mutate):
    rc, prep = env.prepare("review", "code-reviewer", "report")
    env.report("code-reviewer", "review")
    mutate(env)
    rc, out = env.verify("review", prep)
    assert rc == 1 and out["result"] == "FAIL" and out["reasons"], out


def test_report_contract_fails_when_another_steps_output_changes(env):
    env.write(".agent-output/tester/earlier.md", "old\n")
    rc, prep = env.prepare("review", "code-reviewer", "report")
    env.write(".agent-output/tester/earlier.md", "rewritten\n")
    rc, out = env.verify("review", prep)
    assert rc == 1 and any("another step" in r for r in out["reasons"])


def test_tolerance_list_is_ignored(env):
    rc, prep = env.prepare("review", "code-reviewer", "report")
    env.write("IDENTITY.md", "x\n")
    env.write("__pycache__/a.pyc", "x\n")
    env.write(".pytest_cache/v", "x\n")
    env.report("code-reviewer", "review")
    rc, out = env.verify("review", prep)
    assert rc == 0, out


def test_parallel_report_steps_do_not_fail_each_others_verify(env):
    rc, a = env.prepare("review", "code-reviewer", "report", OPUS)
    first = env.head
    rc, b = env.prepare("audit", "security-auditor", "report", OPUS)
    env.report("code-reviewer", "review")
    env.report("security-auditor", "audit")
    rc, out = env.verify("review", a, in_flight=["audit"], head=first)
    assert rc == 0, out
    rc, out = env.verify("audit", b, in_flight=["review"])
    assert rc == 0, out


def test_verify_never_writes_the_human_copy_through_a_symlink(env, tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("keep\n")
    rc, prep = env.prepare("review", "code-reviewer", "report")
    env.report("code-reviewer", "review")
    (env.repo / ".agent-output" / "code-reviewer" / "review.lane-verify.json").symlink_to(target)
    rc, out = env.verify("review", prep)
    assert target.read_text() == "keep\n"


def test_committed_pycache_outside_scope_fails_verify(env):
    a = scope_step(env, "tests", "tester", ("tests/",))
    wt = Path(a["worktree"])
    env.write("src/__pycache__/x.pyc", "x\n", base=wt)
    sh(["git", "add", "-f", "src/__pycache__/x.pyc"], wt)
    sh(["git", "commit", "-q", "-m", "work"], wt)
    rc, out = env.verify("tests", a)
    assert rc == 1 and any("__pycache__" in r for r in out["reasons"]), out


def test_writer_with_unknown_family_is_refused_for_a_checker_even_when_models_differ(env):
    rc, _ = env.prepare("impl", "tester", "report", "inherit")
    rc, out = env.prepare("v1", "verifier", "report", OPUS)
    assert rc == 1, out


# -- scope contract ----------------------------------------------------------------------------------------

def scope_step(env, step="tests", role="tester", scope=("tests/",), model=SONNET, attempt=1):
    rc, prep = env.prepare(step, role, "scope", model, scope=list(scope), attempt=attempt)
    assert rc == 0, prep
    return prep


def test_scope_writer_gets_its_own_worktree_and_passes_inside_scope(env):
    prep = scope_step(env)
    wt = Path(prep["worktree"])
    assert wt.is_dir() and wt != env.repo
    assert prep["branch"].startswith("openclaw/kit-lane-tester-tests-run1-i1-a1")
    tip = env.commit(wt, {"tests/test_b.py": "def test_b(): pass\n"})
    env.report("tester", "tests", base=wt)
    rc, out = env.verify("tests", prep)
    assert rc == 0 and out["tip"] == tip, out
    assert head_of(env.repo) != tip      # the run tree was not touched


def _mut_outside(env, prep, wt):
    env.commit(wt, {"docs/new.md": "x\n"})


def _mut_run_tree(env, prep, wt):
    env.write("src/app.py", "evil\n")


def _mut_ref(env, prep, wt):
    sh(["git", "update-ref", "refs/heads/evil", "HEAD"], env.repo)


def _mut_config(env, prep, wt):
    cfg = env.repo / ".git" / "config"
    cfg.write_text(cfg.read_text() + "[alias]\n\tx = status\n")


def _mut_hook(env, prep, wt):
    hook = env.repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")


def _mut_symlink(env, prep, wt):
    (wt / "tests").mkdir(exist_ok=True)
    os.symlink("/etc/passwd", wt / "tests" / "link")
    sh(["git", "add", "-A"], wt)
    sh(["git", "commit", "-q", "-m", "link"], wt)


def _mut_submodule(env, prep, wt):
    sh(["git", "update-index", "--add", "--cacheinfo", "160000,1111111111111111111111111111111111111111,tests/sub"], wt)
    sh(["git", "commit", "-q", "-m", "sub"], wt)


def _mut_exec(env, prep, wt):
    p = env.write("tests/run.sh", "#!/bin/sh\n", base=wt)
    p.chmod(0o755)
    sh(["git", "add", "-A"], wt)
    sh(["git", "commit", "-q", "-m", "exec"], wt)


def _mut_tripwire(env, prep, wt):
    env.write(".claude/settings.json", "{}\n", base=env.home)


def _mut_state_dir(env, prep, wt):
    rec = env.state / "ai-resources" / "lanes" / RUN / "records"
    (rec / "evil.json").write_text("{}")


def _mut_gitmodules(env, prep, wt):
    env.commit(wt, {".gitmodules": "[submodule]\n"})


def _mut_handoff_commit(env, prep, wt):
    env.commit(wt, {"handoff.md": "x\n"})


def _mut_uncommitted(env, prep, wt):
    env.write("tests/uncommitted.py", "x\n", base=wt)


def _mut_other_branch_commit(env, prep, wt):
    sh(["git", "checkout", "-q", "-b", "sneaky"], wt)
    env.commit(wt, {"tests/z.py": "x\n"})


@pytest.mark.parametrize("mutate", [
    _mut_outside, _mut_run_tree, _mut_ref, _mut_config, _mut_hook, _mut_symlink, _mut_submodule, _mut_exec,
    _mut_tripwire, _mut_state_dir, _mut_gitmodules, _mut_handoff_commit, _mut_uncommitted,
    _mut_other_branch_commit,
], ids=lambda f: f.__name__.removeprefix("_mut_"))
def test_scope_contract_fails_outside_scope_and_on_forbidden_effects(env, mutate):
    prep = scope_step(env)
    wt = Path(prep["worktree"])
    env.commit(wt, {"tests/ok.py": "ok\n"})
    mutate(env, prep, wt)
    rc, out = env.verify("tests", prep)
    assert rc == 1 and out["result"] == "FAIL" and out["reasons"], out


def test_failed_scope_attempt_removes_its_worktree_and_the_next_gets_a_new_one(env):
    prep = scope_step(env)
    wt = Path(prep["worktree"])
    env.commit(wt, {"docs/new.md": "x\n"})
    rc, out = env.verify("tests", prep)
    assert rc == 1 and out.get("worktree_removed") == prep["worktree_id"]
    removes = [a for a in env.runner.log if a[:3] == ["openclaw", "worktrees", "remove"]]
    assert removes == [["openclaw", "worktrees", "remove", prep["worktree_id"]]]
    prep2 = scope_step(env, attempt=2)
    assert prep2["worktree"] != prep["worktree"] and prep2["worktree_id"].endswith("-i1-a2")
    # no reset, clean or forced checkout was ever issued
    for argv in env.runner.log:
        joined = " ".join(argv)
        assert not re.search(r"\b(reset|clean)\b", joined), joined
        assert "checkout -f" not in joined and "--hard" not in joined, joined


def test_scope_disjoint_parallel_writers_each_have_a_worktree_and_pass(env):
    a = scope_step(env, "tests", "tester", ("tests/",))
    b = scope_step(env, "docs", "doc-writer", ("docs/",))
    assert a["worktree"] != b["worktree"]
    env.commit(Path(a["worktree"]), {"tests/t2.py": "x\n"})
    env.commit(Path(b["worktree"]), {"docs/b.md": "x\n"})
    rc, out = env.verify("tests", a, in_flight=["docs"], head=None)
    # a's expected head must be what the orchestrator last saw: b's prepare
    assert rc == 0, out
    rc, out = env.verify("docs", b, in_flight=["tests"])
    assert rc == 0, out


def test_scope_writer_cannot_touch_the_sibling_branch_outside_the_declared_in_flight(env):
    a = scope_step(env, "tests", "tester", ("tests/",))
    b = scope_step(env, "docs", "doc-writer", ("docs/",))
    env.commit(Path(a["worktree"]), {"tests/t2.py": "x\n"})
    env.commit(Path(b["worktree"]), {"docs/b.md": "x\n"})
    rc, out = env.verify("tests", a)         # b is running in parallel but was not declared
    assert rc == 1, out
    assert any("ledger written" in r or "ref" in r for r in out["reasons"])


# -- integrating contract ----------------------------------------------------------------------------------

def integrating_step(env, step="impl", role="implementer", model=SONNET, attempt=1, reenter=False):
    rc, prep = env.prepare(step, role, "integrating", model, attempt=attempt, reenter=reenter)
    assert rc == 0, prep
    return prep


def test_integrating_writer_works_in_its_own_worktree_and_passes(env):
    prep = integrating_step(env)
    wt = Path(prep["worktree"])
    assert wt != env.repo and prep["base"] == head_of(env.repo)
    env.commit(wt, {"src/new.py": "x\n", "tests/test_x.py": "x\n"})
    env.report("implementer", "impl", base=wt)
    rc, out = env.verify("impl", prep)
    assert rc == 0, out


@pytest.mark.parametrize("mutate", [
    lambda e, wt: e.commit(wt, {".claude/agents/x.md": "x\n"}),
    lambda e, wt: e.commit(wt, {".openclaw/x": "x\n"}),
    lambda e, wt: e.commit(wt, {".githooks/pre-commit": "x\n"}),
    lambda e, wt: e.commit(wt, {".agent-output/tester/other.md": "x\n"}),
    lambda e, wt: sh(["git", "update-ref", "refs/heads/evil", "HEAD"], e.repo),
    lambda e, wt: e.write("src/app.py", "x\n"),
], ids=["claude-dir", "openclaw-dir", "githooks", "other-step-output", "other-ref", "run-tree-write"])
def test_integrating_contract_fails_on_forbidden_paths_and_refs(env, mutate):
    prep = integrating_step(env)
    wt = Path(prep["worktree"])
    env.commit(wt, {"src/new.py": "x\n"})
    mutate(env, wt)
    rc, out = env.verify("impl", prep)
    assert rc == 1 and out["reasons"], out


@pytest.mark.parametrize("role,contract,kw", [("tester", "scope", {"scope": ["tests/"]}),
                                              ("implementer", "integrating", {})])
@pytest.mark.parametrize("message", ["fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
                                     "fix\n\n\U0001F916 Generated with Claude Code",
                                     "fix\n\nGenerated with Gemini"])
def test_commit_message_attribution_fails_verify(env, role, contract, kw, message):
    rc, prep = env.prepare("s1", role, contract, SONNET, **kw)
    wt = Path(prep["worktree"])
    env.commit(wt, {"tests/ok.py": "ok\n"}, msg=message)
    rc, out = env.verify("s1", prep)
    assert rc == 1 and any("attribution" in r for r in out["reasons"]), out


def test_clean_commit_message_passes(env):
    prep = scope_step(env)
    env.commit(Path(prep["worktree"]), {"tests/ok.py": "ok\n"}, msg="add a test")
    rc, out = env.verify("tests", prep)
    assert rc == 0, out


# -- integrate / land: the full chain ----------------------------------------------------------------------

class Flow:
    """scope writer -> verify -> Verifier -> integrating writer -> integrate -> verify -> Verifier -> land."""

    def __init__(self, env, model=SONNET):
        self.env, self.model = env, model

    def scope(self, *, files=None, verifier=True, verdict="PASS"):
        e = self.env
        self.sprep = scope_step(e, "tests", "tester", ("tests/",), model=self.model)
        self.stip = e.commit(Path(self.sprep["worktree"]), files or {"tests/test_b.py": "def test_b(): pass\n"})
        e.report("tester", "tests", base=Path(self.sprep["worktree"]))
        rc, self.sverify = e.verify("tests", self.sprep)
        assert rc == 0, self.sverify
        if verifier:
            rc, self.vverify = e.verifier("verify-tests", "tests", self.stip, verdict)
            assert rc == (0 if verdict == "PASS" else 1), self.vverify

    def integrating(self, *, files=None, integrate=True, reenter=False, attempt=1):
        e = self.env
        self.iprep = integrating_step(e, "impl", model=self.model, reenter=reenter, attempt=attempt)
        self.iwt = Path(self.iprep["worktree"])
        if integrate:
            rc, self.integ = e.integrate("impl", "tests", self.iprep)
            return rc, self.integ
        return 0, None

    def finish(self, *, files=None, verifier=True, verdict="PASS", step_v="verify-impl", attempt=1):
        e = self.env
        self.itip = e.commit(self.iwt, files or {"src/new.py": "x\n"})
        e.report("implementer", "impl", base=self.iwt)
        rc, self.iverify = e.verify("impl", self.iprep, attempt=attempt)
        assert rc == 0, self.iverify
        if verifier:
            rc, self.ivv = e.verifier(step_v, "impl", self.itip, verdict)
            assert rc == (0 if verdict == "PASS" else 1), self.ivv


def test_full_chain_lands_with_ff_only(env):
    f = Flow(env)
    f.scope()
    rc, _ = f.integrating()
    assert rc == 0
    f.finish()
    base = head_of(env.repo)
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 0 and out["result"] == "LANDED", out
    assert head_of(env.repo) == f.itip != base
    assert sh(["git", "rev-parse", "HEAD^{tree}"], env.repo) == sh(["git", "rev-parse", f"{f.itip}^{{tree}}"], env.repo)
    assert (env.repo / "tests" / "test_b.py").is_file() and (env.repo / "src" / "new.py").is_file()
    merges = [a for a in env.runner.log if "merge" in a and "--ff-only" in a]
    assert len(merges) == 1
    assert out["removed"], "recorded worktrees are cleaned up through openclaw worktrees remove"


def test_integrate_refuses_without_a_verifier_pass(env):
    f = Flow(env)
    f.scope(verifier=False)
    f.integrating(integrate=False)
    rc, out = env.integrate("impl", "tests", f.iprep)
    assert rc == 1 and any("Verifier PASS" in r for r in out["reasons"])


def test_integrate_refuses_without_the_scope_verify(env):
    f = Flow(env)
    f.sprep = scope_step(env)
    f.stip = env.commit(Path(f.sprep["worktree"]), {"tests/test_b.py": "x\n"})
    f.integrating(integrate=False)
    rc, out = env.integrate("impl", "tests", f.iprep)
    assert rc == 1 and any("scope verify PASS" in r for r in out["reasons"])


def test_a_forged_verifier_report_without_a_bound_record_is_refused(env):
    f = Flow(env)
    f.scope(verifier=False)
    env.write(".agent-output/verifier/verify-tests.md", "Verdict: PASS\n")      # never verified
    f.integrating(integrate=False)
    rc, out = env.integrate("impl", "tests", f.iprep)
    assert rc == 1 and any("Verifier PASS" in r for r in out["reasons"])


def test_a_verifier_record_bound_to_another_subject_does_not_count(env):
    f = Flow(env)
    f.scope(verifier=False)
    rc, _ = env.verifier("verify-tests", "tests", head_of(env.repo))             # subject is the base, not the tip
    assert rc == 0
    f.integrating(integrate=False)
    rc, out = env.integrate("impl", "tests", f.iprep)
    assert rc == 1 and any("Verifier PASS" in r for r in out["reasons"])


def test_a_verifier_requires_changes_verdict_does_not_count(env):
    f = Flow(env)
    f.scope(verifier=True, verdict="REQUIRES_CHANGES")
    f.integrating(integrate=False)
    rc, out = env.integrate("impl", "tests", f.iprep)
    assert rc == 1


def test_integrate_conflict_aborts_in_the_integration_worktree_and_blocks(env):
    f = Flow(env)
    f.scope(files={"tests/test_a.py": "def test_a(): return 'scope'\n"})
    rc, _ = f.integrating(integrate=False)
    env.commit(f.iwt, {"tests/test_a.py": "def test_a(): return 'impl'\n"})
    before = tracked_state(env.repo)
    rc, out = env.integrate("impl", "tests", f.iprep)
    assert rc == 3 and out["result"] == "BLOCKED" and "tests/test_a.py" in out["paths"]
    assert tracked_state(env.repo) == before
    assert sh(["git", "status", "--porcelain"], f.iwt) == ""            # merge aborted in the worktree
    assert not (f.iwt / ".git").is_dir()                                # still a linked worktree


def land_ready(env, **kw):
    f = Flow(env, **kw)
    f.scope()
    f.integrating()
    f.finish()
    return f


def test_land_refuses_when_the_integrating_verify_was_skipped(env):
    f = Flow(env)
    f.scope()
    f.integrating()
    f.itip = env.commit(f.iwt, {"src/new.py": "x\n"})
    env.report("implementer", "impl", base=f.iwt)
    rc, out = env.verifier("verify-impl", "impl", f.itip)               # a Verifier PASS exists, verify was skipped
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 1 and any("integrating verify PASS" in r for r in out["reasons"])


def test_land_refuses_without_a_verifier_pass_on_that_commit(env):
    f = Flow(env)
    f.scope()
    f.integrating()
    f.finish(verifier=False)
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 1 and any("Verifier PASS" in r for r in out["reasons"])
    # a verifier bound to an earlier commit does not count after the branch moved
    f.finish_tip2 = env.commit(f.iwt, {"src/more.py": "x\n"})
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 1


def test_land_refuses_when_the_branch_moved_after_verify(env):
    f = land_ready(env)
    env.commit(f.iwt, {"src/late.py": "x\n"})
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 1 and any("not the verified tip" in r for r in out["reasons"])


def test_land_refuses_when_base_is_not_run_head(env):
    f = land_ready(env)
    env.write("README.md", "advanced\n")
    sh(["git", "add", "README.md"], env.repo)
    sh(["git", "commit", "-q", "-m", "advance"], env.repo)
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 1
    assert any(re.search(r"base [0-9a-f]{40} is not run HEAD [0-9a-f]{40}", r) for r in out["reasons"]), out


def test_land_refuses_when_the_run_tree_changed_since_prepare(env):
    f = land_ready(env)
    env.write("README.md", "dirty\n")
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 1 and any("changed since the integrating prepare" in r for r in out["reasons"])


def test_land_twice_in_one_iteration_is_refused(env):
    f = land_ready(env)
    rc, _ = env.land("impl", f.iprep, 1)
    assert rc == 0
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 1 and any("already landed in iteration 1" in r for r in out["reasons"])


def test_loop_back_is_a_new_iteration_and_may_land_again(env):
    f = land_ready(env)
    rc, _ = env.land("impl", f.iprep, 1)
    assert rc == 0
    # REQUIRES_CHANGES: the orchestrator re-enters the integrating step (new iteration), no scope step this time
    prep2 = integrating_step(env, "impl", reenter=True)
    assert prep2["iteration"] == 2 and prep2["worktree_id"].endswith("-i2-a1")
    tip2 = env.commit(Path(prep2["worktree"]), {"src/fix.py": "x\n"})
    env.report("implementer", "impl", base=Path(prep2["worktree"]))
    rc, out = env.verify("impl", prep2)
    assert rc == 0, out
    rc, out = env.verifier("verify-impl-2", "impl", tip2)
    assert rc == 0
    rc, out = env.land("impl", prep2, 2)
    assert rc == 0 and out["result"] == "LANDED", out
    assert head_of(env.repo) == tip2
    rc, out = env.land("impl", prep2, 2)
    assert rc == 1


def test_a_stale_integration_branch_from_an_earlier_iteration_is_refused(env):
    f = land_ready(env)
    # the run tree moves on (a checkpoint), the loop re-enters, and iteration 1's branch is no longer landable
    env.write("README.md", "checkpoint\n")
    sh(["git", "add", "README.md"], env.repo)
    sh(["git", "commit", "-q", "-m", "checkpoint"], env.repo)
    prep2 = integrating_step(env, "impl", reenter=True)
    assert prep2["iteration"] == 2
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 1
    assert any(re.search(r"base [0-9a-f]{40} is not run HEAD [0-9a-f]{40}", r) for r in out["reasons"]), out


# -- verdict integrity --------------------------------------------------------------------------------------

def test_a_rewritten_snapshot_record_fails_the_expect_check(env):
    rc, prep = env.prepare("review", "code-reviewer", "report")
    rec_dir = env.state / "ai-resources" / "lanes" / RUN / "records"
    rec = next(rec_dir.glob("prepare-review-*.json"))
    rec.write_text(rec.read_text().replace('"handoff"', '"handoff"  '))
    env.report("code-reviewer", "review")
    rc, out = env.verify("review", prep)
    assert rc == 1 and any("rewritten" in r for r in out["reasons"])


def test_a_tampered_ledger_entry_breaks_the_chain(env):
    rc, prep = env.prepare("review", "code-reviewer", "report")
    ledger = env.state / "ai-resources" / "lanes" / RUN / "ledger.jsonl"
    ledger.write_text(ledger.read_text().replace('"role": "code-reviewer"', '"role": "verifier"'))
    env.report("code-reviewer", "review")
    rc, out = env.verify("review", prep)
    assert rc == 1 and any("chain broken" in r for r in out["reasons"])


def _rebuild(ledger: Path, edit):
    entries = [json.loads(line) for line in ledger.read_text().splitlines()]
    prev = "0" * 64
    out = []
    for e in entries:
        edit(e)
        e["prev"] = prev
        e["hash"] = lanes_cmd._entry_hash(e)
        prev = e["hash"]
        out.append(e)
    ledger.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in out))


def test_a_rewritten_self_consistent_ledger_is_refused_through_the_expected_head(env):
    f = Flow(env)
    f.scope()
    f.integrating(integrate=False)
    ledger = env.state / "ai-resources" / "lanes" / RUN / "ledger.jsonl"
    _rebuild(ledger, lambda e: e["data"].update(forged=True) if e["kind"] == "prepare" and e["step"] == "tests" else None)
    rc, out = env.integrate("impl", "tests", f.iprep)
    assert rc == 1 and any("not an ancestor" in r for r in out["reasons"]), out
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 1 and any("not an ancestor" in r for r in out["reasons"])
    rc, out = env.verify("impl", f.iprep)
    assert rc == 1 and any("not an ancestor" in r for r in out["reasons"])


def test_a_stale_but_genuine_head_is_accepted(env):
    f = Flow(env)
    f.scope()
    rc, prep_i = env.prepare("impl", "implementer", "integrating", SONNET)
    older = env.head                      # the head the orchestrator saw before an unrelated command
    env.count("claude-subscription", role="explore", step="impl")
    assert env.head != older
    rc, out = env.integrate("impl", "tests", prep_i, head=older)
    assert rc == 0 and out["result"] == "MERGED", out


def test_witnessed_authority_a_child_minted_verifier_pass_carries_none(env):
    prep = scope_step(env)
    wt = Path(prep["worktree"])
    tip = env.commit(wt, {"tests/test_b.py": "x\n"})
    env.report("tester", "tests", base=wt)
    orch_head = env.head
    # the lane child runs the kit commands itself: an invented chain-valid Verifier PASS for its own tip
    rc, fake = env.prepare("fake-verifier", "verifier", "report", OPUS)
    assert rc == 0
    env.report("verifier", "fake-verifier", "PASS")
    rc, out = env.verify("fake-verifier", fake, subject=f"tests@{tip}")
    assert rc == 0 and out["result"] == "PASS"                   # chain-valid
    # the orchestrator verifies the child's step with the head it saw before the child ran
    rc, out = env.verify("tests", prep, head=orch_head)
    assert rc == 1 and any("ledger written during the child's run" in r for r in out["reasons"])
    # integrate/land use the head printed by the failing verify: the minted entry is tainted and ignored
    rc, iprep = env.prepare("impl", "implementer", "integrating", SONNET)
    rc, out = env.integrate("impl", "tests", iprep)
    assert rc == 1
    text = " ".join(out["reasons"])
    assert "Verifier PASS" in text


def test_in_flight_steps_do_not_trigger_the_ledger_written_failure(env):
    a = scope_step(env, "tests", "tester", ("tests/",))
    first_head = env.head
    b = scope_step(env, "docs", "doc-writer", ("docs/",))
    env.commit(Path(a["worktree"]), {"tests/t2.py": "x\n"})
    env.commit(Path(b["worktree"]), {"docs/b.md": "x\n"})
    rc, out = env.verify("tests", a, in_flight=["docs"], head=first_head)     # b's prepare came after that head
    assert rc == 0, out
    rc, out = env.verify("tests", a, attempt=1, head=first_head)
    assert rc == 1                                                            # b not declared: fails


# -- checker independence at dispatch (C1) -------------------------------------------------------------------

def test_a_lane_checker_in_the_writer_family_is_refused(env):
    rc, _ = env.prepare("impl", "tester", "report", "sonnet")                      # a writer on claude-code
    assert rc == 0
    rc, out = env.prepare("v1", "verifier", "report", "anthropic/claude-sonnet-5-5")
    assert rc == 1 and "anthropic/claude-sonnet-5-5" in out["reasons"][0] and "sonnet" in out["reasons"][0]
    rc, out = env.prepare("v2", "verifier", "report", "claude-cli/claude-sonnet-5-5")
    assert rc == 1
    rc, out = env.prepare("v3", "verifier", "report", OPUS)
    assert rc == 0


def test_a_checker_with_an_unknown_family_is_refused(env):
    rc, out = env.prepare("v1", "verifier", "report", "inherit")
    assert rc == 1 and "unknown family" in out["reasons"][0]
    rc, out = env.prepare("v2", "security-auditor", "report", "totally-unknown-slug")
    assert rc == 1


def test_security_auditor_is_independent_of_scope_writers_too(env):
    rc, _ = env.prepare("docs", "doc-writer", "scope", "anthropic/claude-opus-5-5", scope=["docs/"])
    rc, out = env.prepare("sec", "security-auditor", "report", OPUS)
    assert rc == 1


def test_report_roles_do_not_count_as_writers(env):
    rc, _ = env.prepare("r1", "code-reviewer", "report", OPUS)
    rc, out = env.prepare("v1", "verifier", "report", OPUS)
    assert rc == 0


# -- count -----------------------------------------------------------------------------------------------------

def test_count_per_pool_with_retries_and_caps(env):
    for expected in (1, 2):
        rc, out = env.count("claude-subscription", role="tester", step="s1", cap=2)
        assert rc == 0 and out["count"] == expected
    rc, out = env.count("claude-subscription", role="tester", step="s1", cap=2)
    assert rc == 1 and any("cap reached" in r for r in out["reasons"])
    rc, out = env.count("agy-claude-gpt", role="tester", step="s1", cap=2)      # another pool
    assert rc == 0 and out["count"] == 1


def test_caps_never_apply_to_checker_roles(env):
    for _ in range(3):
        env.count("claude-subscription", role="tester", step="s1")
    rc, out = env.count("claude-subscription", role="verifier", step="v1", cap=1)
    assert rc == 0 and out["checker_dispatches"] == 1
    rc, out = env.count("claude-subscription", role="security-auditor", step="sec", cap=0)
    assert rc == 0


def test_prepare_counts_against_the_pool_and_honours_the_cap_for_writers_only(env):
    assert env.prepare("t1", "tester", "report", pool="claude-subscription", cap=1)[0] == 0
    rc, out = env.prepare("t2", "tester", "report", pool="claude-subscription", cap=1)
    assert rc == 1 and "cap reached" in out["reasons"][0]
    assert env.prepare("v1", "verifier", "report", OPUS, pool="claude-subscription", cap=1)[0] == 0


# -- symmetry --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["claude-cli/claude-sonnet-5-5", "agy-cli/claude-sonnet-5-5"])
def test_every_verdict_is_identical_whatever_the_lane_label(env, label):
    f = Flow(env, model=label)
    f.scope()
    rc, _ = f.integrating()
    f.finish()
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 0 and out["result"] == "LANDED"
    verdicts = [(e["kind"], e["data"].get("result")) for e in env.ledger_entries(env.state)
                if e["kind"] in ("verify", "integrate", "land")]
    assert verdicts == [("verify", "PASS"), ("verify", "PASS"), ("integrate", "MERGED"), ("verify", "PASS"),
                        ("verify", "PASS"), ("land", "LANDED")]


# -- CLI wiring ------------------------------------------------------------------------------------------------

def test_the_lanes_subcommand_is_wired_into_the_cli(env, capsys):
    from ai_resources import cli
    rc = cli.main(["lanes", "count", "--run", "wired", "--pool", "claude-subscription", "--role", "tester"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and out["result"] == "COUNTED" and out["count"] == 1
    for action in ("prepare", "verify", "integrate", "land", "count"):
        with pytest.raises(SystemExit):
            cli.main(["lanes", action, "--help"])
        capsys.readouterr()


def test_the_run_id_and_step_ids_are_validated(env):
    rc, out = env.prepare("../escape", "explore", "report", "haiku")
    assert rc == 2
    assert lanes_cmd.main(["count", "--run", "../x", "--pool", "p"], runner=env.runner) == 2
    env.capsys.readouterr()


# -- verifier gaps G1-G7 -------------------------------------------------------------------------------------

def _landable_chain(env):
    """A verified integrating step awaiting its Verifier: returns (Flow, itip)."""
    f = Flow(env)
    f.scope()
    f.integrating()
    f.finish(verifier=False)
    return f


def test_g1_a_parallel_member_cannot_overwrite_a_sealed_peer_report(env):
    f = _landable_chain(env)
    rc, p1 = env.prepare("v1", "verifier", "report", OPUS)
    first = env.head
    rc, p2 = env.prepare("v2", "security-auditor", "report", OPUS)
    env.report("verifier", "v1", "FAIL")
    rc, out = env.verify("v1", p1, in_flight=["v2"], head=first, subject=f"impl@{f.itip}")
    assert out["verdict"] == "FAIL"
    env.report("security-auditor", "v2")
    env.report("verifier", "v1", "PASS")                          # v2 forges its peer's verdict
    rc, out = env.verify("v2", p2, in_flight=["v1"])
    assert rc == 1 and any("v1.md" in r and "changed" in r for r in out["reasons"]), out
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 1 and out["result"] == "REFUSED", out


def test_g1_a_peer_report_present_at_prepare_may_not_change(env):
    env.report("verifier", "v1", "FAIL")
    rc, p2 = env.prepare("v2", "security-auditor", "report", OPUS)
    rc, p1 = env.prepare("v1", "verifier", "report", OPUS)
    env.report("security-auditor", "v2")
    env.report("verifier", "v1", "PASS")
    rc, out = env.verify("v2", p2, in_flight=["v1"], head=p2["ledger_head"])
    assert rc == 1 and any("v1.md" in r for r in out["reasons"]), out


def test_g1_a_report_is_write_once_for_its_own_step(env):
    rc, p = env.prepare("review", "code-reviewer", "report", OPUS)
    env.report("code-reviewer", "review")
    assert env.verify("review", p)[0] == 0
    env.write(".agent-output/code-reviewer/review.md", "rewritten\n")
    rc, out = env.verify("review", p)
    assert rc == 1 and any("sealed" in r for r in out["reasons"]), out


def test_g2_a_committed_tolerated_path_is_not_tolerated_by_a_dirty_edit(env):
    a = scope_step(env, "tests", "tester", ("tests/",))
    wt = Path(a["worktree"])
    env.commit(wt, {"SOUL.md": "evil\n"})
    env.write("SOUL.md", "evil again\n", base=wt)
    rc, out = env.verify("tests", a)
    assert rc == 1 and any("SOUL.md" in r for r in out["reasons"]), out


def test_g2_a_committed_pycache_then_edited_still_fails(env):
    a = scope_step(env, "tests", "tester", ("tests/",))
    wt = Path(a["worktree"])
    env.write("__pycache__/evil.py", "1\n", base=wt)
    sh(["git", "add", "-f", "__pycache__/evil.py"], wt)
    sh(["git", "commit", "-q", "-m", "work"], wt)
    env.write("__pycache__/evil.py", "2\n", base=wt)
    rc, out = env.verify("tests", a)
    assert rc == 1 and any("__pycache__/evil.py" in r for r in out["reasons"]), out


def test_g2_a_tracked_tolerated_path_edited_in_the_run_tree_fails(env):
    env.write("SOUL.md", "a\n")
    sh(["git", "add", "-f", "SOUL.md"], env.repo)
    sh(["git", "commit", "-q", "-m", "soul"], env.repo)
    rc, p = env.prepare("review", "code-reviewer", "report", OPUS)
    env.write("SOUL.md", "b\n")
    env.report("code-reviewer", "review")
    rc, out = env.verify("review", p)
    assert rc == 1, out


def test_g3_dotted_step_ids_are_refused_and_a_step_cannot_write_a_sibling_report(env):
    assert env.prepare("t.2", "tester", "report")[0] == 2
    rc, p = env.prepare("t", "tester", "report")
    env.report("tester", "t")
    env.write(".agent-output/tester/t.2.md", "sibling\n")
    rc, out = env.verify("t", p)
    assert rc == 1 and any("t.2.md" in r for r in out["reasons"]), out


def test_g4_ignored_writes_under_a_forbidden_root_fail_a_report_step(env):
    env.write(".gitignore", "__pycache__/\n.claude/\n")
    sh(["git", "add", ".gitignore"], env.repo)
    sh(["git", "commit", "-q", "-m", "ignore"], env.repo)
    rc, p = env.prepare("review", "code-reviewer", "report", OPUS)
    env.report("code-reviewer", "review")
    env.write(".claude/settings.local.json", "{}\n")
    assert sh(["git", "status", "--porcelain"], env.repo).count(".claude") == 0      # invisible to git status
    rc, out = env.verify("review", p)
    assert rc == 1 and any(".claude/settings.local.json" in r for r in out["reasons"]), out


def test_g5_a_later_fail_revokes_an_earlier_pass(env):
    f = Flow(env)
    f.scope()
    f.integrating()
    f.finish()
    env.write("src/evil.txt", "late\n")                          # the run tree changes after the PASS
    rc, out = env.verify("impl", f.iprep, in_flight=["verify-impl"])      # the Verifier ran in parallel
    assert rc == 1 and out["result"] == "FAIL", out
    rc, out = env.land("impl", f.iprep, 1)
    assert rc == 1 and out["result"] == "REFUSED", out
    assert not (env.repo / "src" / "new.py").exists()


def test_g5_a_child_cannot_present_the_nonce_it_reads_from_the_ledger(env):
    a = scope_step(env)
    env.commit(Path(a["worktree"]), {"tests/test_b.py": "x\n"})
    entry = env.ledger_entries(env.state)[0]
    assert a["nonce"] not in json.dumps(entry)
    forged = entry.get("nonce") or entry["nonce_sha256"]
    rc, out = env.run("verify", "--step", "tests", "--expect",
                      f"{forged}:{entry['record_sha256']}:{entry['hash']}")
    assert rc == 1 and out["result"] == "REFUSED", out
    assert len(env.ledger_entries(env.state)) == 1


def test_g6_the_run_directory_the_ledger_and_records_are_never_followed_through_links(env, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    run_dir = env.state / "ai-resources" / "lanes" / RUN
    run_dir.parent.mkdir(parents=True)
    run_dir.symlink_to(victim)
    rc, out = env.prepare("r1", "explore", "report", "haiku")
    assert rc == 2 and not list(victim.iterdir())
    run_dir.unlink()
    run_dir.mkdir()
    (run_dir / "ledger.jsonl").symlink_to(victim / "ledger.jsonl")
    rc, out = env.prepare("r1", "explore", "report", "haiku")
    assert rc == 2 and not list(victim.iterdir())
    (run_dir / "ledger.jsonl").unlink()
    (run_dir / "records").symlink_to(victim)
    rc, out = env.prepare("r1", "explore", "report", "haiku")
    assert rc == 2 and not list(victim.iterdir())


def test_g6_a_hardlinked_human_copy_is_not_truncated(env, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n")
    rc, prep = env.prepare("review", "code-reviewer", "report")
    env.report("code-reviewer", "review")
    os.link(victim, env.repo / ".agent-output" / "code-reviewer" / "review.lane-verify.json")
    env.verify("review", prep)
    assert victim.read_text() == "keep\n"


@pytest.mark.parametrize("rel", ["pkg/.claude/settings.json", "pkg/.githooks/pre-commit"])
def test_g7_forbidden_roots_match_at_any_depth(env, rel):
    a = scope_step(env, "tests", "tester", ("tests/", "pkg/"))
    env.commit(Path(a["worktree"]), {rel: "x\n"})
    rc, out = env.verify("tests", a)
    assert rc == 1 and any(rel in r for r in out["reasons"]), out


def test_g7_a_failing_verifier_verdict_exits_non_zero_and_the_result_agrees(env):
    rc, out = env.verifier("v1", "impl", head_of(env.repo), "FAIL")
    assert rc == 1 and out["verdict"] == "FAIL" and out["result"] == "FAIL", out
