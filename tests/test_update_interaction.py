"""`models update` on a terminal asks; unattended it never does. The answers drive the same fan-out."""
from __future__ import annotations

import dataclasses
import json
import subprocess

import pytest

from ai_resources import audit, cli, models, models_cmd, models_interaction as mi
from ai_resources import model_pins as mp
from ai_resources.setup import ui
from test_models_update import FIX, Env, _provider_env

SENTINEL = "FAKEKEY-DO-NOT-LEAK"


def run(*argv):
    return cli.main(["models", *argv])


class Prompts:
    """A scripted `ui.select`: records every question, answers from a queue of button labels."""

    def __init__(self, *answers, side_effect=None):
        self.answers, self.asked, self.side_effect = list(answers), [], side_effect

    def __call__(self, message, choices, default=None, **kw):
        self.asked.append((message, list(choices)))
        if self.side_effect:
            self.side_effect()
        return self.answers.pop(0)


def tty(monkeypatch, select=None):
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    if select is not None:
        monkeypatch.setattr(ui, "select", select)


def no_prompt(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("prompted")
    monkeypatch.setattr(ui, "select", boom)


@pytest.fixture
def unpriced(tmp_path, monkeypatch):
    """Claude-only host, three proposals waiting for a human (opus, sonnet, haiku)."""
    e = Env(tmp_path, monkeypatch, priced=False)
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: e.overlay)
    return e


# --- AC-S8c-1: nothing prompts unattended ----------------------------------------------------------

@pytest.mark.parametrize("flags,stdin_tty", [([], False), (["--unattended"], True)])
def test_no_prompt_without_a_terminal_or_with_unattended(unpriced, monkeypatch, flags, stdin_tty):
    no_prompt(monkeypatch)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: stdin_tty)
    assert run("update", *flags) == 10
    pend = unpriced.state()["pending"]["anthropic:sonnet"]
    assert pend["to"] == "claude-sonnet-5-5" and pend["first_seen"]


def test_no_prompt_when_the_interactive_ui_is_disabled(unpriced, monkeypatch):
    no_prompt(monkeypatch)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    assert run("update") == 10
    assert unpriced.state()["pending"]


# --- AC-S8c-2: exactly the questions of the design --------------------------------------------------

def test_several_proposals_get_one_batch_question_then_per_model_buttons_only_on_request(unpriced, monkeypatch):
    p = Prompts(mi.NOT_NOW)
    tty(monkeypatch, p)
    run("update", "--no-restart")
    assert len(p.asked) == 1 and p.asked[0][1] == [mi.UPDATE_ALL, mi.CHOOSE, mi.NOT_NOW]
    p = Prompts(mi.CHOOSE, mi.NOT_NOW, mi.NOT_NOW, mi.NOT_NOW)
    tty(monkeypatch, p)
    run("update", "--no-restart")
    assert [c for _m, c in p.asked[1:]] == [[mi.UPDATE_NOW, mi.NOT_NOW, mi.ALWAYS, mi.NEVER]] * 3


def test_a_single_proposal_gets_the_four_buttons_and_never_asks_a_follow_up_unless_never(unpriced, monkeypatch):
    p = Prompts(mi.NOT_NOW)
    tty(monkeypatch, p)
    run("update", "--no-restart", "--class", "sonnet")
    assert [c for _m, c in p.asked] == [[mi.UPDATE_NOW, mi.NOT_NOW, mi.ALWAYS, mi.NEVER]]
    p = Prompts(mi.NEVER, mi.NEVER_MODEL)
    tty(monkeypatch, p)
    run("update", "--no-restart", "--class", "sonnet")
    assert [c for _m, c in p.asked][-1] == [mi.NEVER_MODEL, mi.NEVER_FAMILY]


# --- AC-S8c-4/5: Not now and Never --------------------------------------------------------------

def test_not_now_changes_no_policy_and_only_pending_is_written(unpriced, monkeypatch):
    tty(monkeypatch, Prompts(mi.NOT_NOW))
    run("update", "--no-restart", "--class", "sonnet")
    ov = unpriced.state()
    assert ov["pending"]["anthropic:sonnet"]["to"] == "claude-sonnet-5-5"
    assert not ov.get("approvals") and "anthropic:sonnet" not in ov["policy"]["slots"]
    assert unpriced.patches == []


def test_never_this_model_is_remembered_and_the_timer_never_lists_it_again(unpriced, monkeypatch, capsys):
    tty(monkeypatch, Prompts(mi.NEVER, mi.NEVER_MODEL))
    run("update", "--no-restart", "--class", "sonnet")
    ov = unpriced.state()
    assert ov["policy"]["slots"]["anthropic:sonnet"]["never_ids"] == ["claude-sonnet-5-5"]
    assert "anthropic:sonnet" not in ov.get("pending", {})
    no_prompt(monkeypatch)
    capsys.readouterr()
    run("update", "--unattended", "--json", "--class", "sonnet")
    out = json.loads(capsys.readouterr().out)
    assert [p["decision"] for p in out["proposals"] if p["slot"] == "anthropic:sonnet"] == ["suppressed"]
    assert "anthropic:sonnet" not in unpriced.state().get("pending", {})


def test_never_the_whole_family_suppresses_every_later_id(unpriced, monkeypatch):
    tty(monkeypatch, Prompts(mi.NEVER, mi.NEVER_FAMILY))
    run("update", "--no-restart", "--class", "sonnet")
    assert unpriced.state()["policy"]["families"]["anthropic:sonnet"] == {"never": True}
    props = models.propose(mp.effective({}), models.Discovery(best={"sonnet": "claude-sonnet-9-9"}), unpriced.state())
    assert [p.decision for p in props if p.cls == "sonnet"] == ["suppressed"]


# --- AC-S8c-3: Always, in the same run, then the unattended guardrails ------------------------------

def test_always_persists_the_answer_and_applies_in_the_same_run_then_the_timer_needs_price_and_smoke(tmp_path, monkeypatch):
    e = _provider_env(tmp_path, monkeypatch, "google")
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: e.overlay)
    tty(monkeypatch, Prompts(mi.ALWAYS))
    assert run("update") == 0
    ov = e.state()
    assert ov["policy"]["slots"]["google:gemini-flash"]["answer"] == "always"
    assert ov["pins"]["google:gemini-flash"] == "gemini-3.9-flash"
    assert e.gateway_hits and e.patches == [] and e.restarts == 0
    # the next proposal: unattended applies it only with a known, not higher price
    deps = e.deps()
    monkeypatch.setattr(audit, "PRICES", {**audit.PRICES, "gemini-3.9-flash": (1.0, 2.0), "gemini-4.0-flash": (1.0, 2.0)})
    import httpx
    hits = []

    def handler(req):
        body = json.loads(req.content)
        hits.append(body)
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": "OK"}}]})

    def runner(argv, **k):
        if "refresh" in argv:
            return 0, "ok"
        cat = json.loads((FIX / "catalog-google.json").read_text())
        cat["models"].append({"key": "google/gemini-4.0-flash", "available": True})
        return 0, json.dumps(cat)

    deps = dataclasses.replace(deps, runner=runner, http=models.make_http(httpx.MockTransport(handler)))
    e.now = e.now.replace(day=e.now.day + 1)
    deps = dataclasses.replace(deps, now=lambda: e.now)
    r = models.run_update(models.Options(unattended=True), deps)
    assert r.outcome == "switched" and e.state()["pins"]["google:gemini-flash"] == "gemini-4.0-flash"
    assert hits[-1]["model"] == "google/gemini-4.0-flash"


def test_the_timer_does_not_apply_an_always_slot_whose_price_is_unknown(tmp_path, monkeypatch):
    e = _provider_env(tmp_path, monkeypatch, "google")
    mp.save_overlay({"policy": {"slots": {"google:gemini-flash": {"answer": "always", "max_bump": "any"}}}}, e.overlay)
    r = e.run(unattended=True)
    assert r.rc == models.EXIT_APPROVAL_PENDING and e.gateway_hits == []


# --- AC-S8c-6: revision and lock -----------------------------------------------------------------

def test_an_overlay_change_while_the_prompt_is_open_aborts_with_no_write(unpriced, monkeypatch):
    tampered = {}

    def tamper():
        ov = mp.load_overlay(unpriced.overlay) or mp.empty_overlay()
        ov.setdefault("pins", {})["anthropic:opus"] = "claude-opus-5-5"
        ov.setdefault("policy", {}).setdefault("slots", {})["anthropic:opus"] = {"frozen": True}
        mp.save_overlay(ov, unpriced.overlay)
        tampered["bytes"] = unpriced.overlay.read_bytes()
        tampered["state"] = unpriced.state()

    tty(monkeypatch, Prompts(mi.UPDATE_ALL, side_effect=tamper))
    rc = run("update")
    assert rc == models.EXIT_ERROR and unpriced.patches == [] and unpriced.restarts == 0
    assert tampered, "the overlay was never tampered with"
    # AC-S8c-6: no write of any kind after the revision check: the overlay is exactly what the other run left
    assert unpriced.overlay.read_bytes() == tampered["bytes"]
    assert unpriced.state() == tampered["state"]
    after = unpriced.state()
    assert "anthropic:sonnet" not in (after.get("approvals") or {}) and "anthropic:sonnet" not in (after.get("pins") or {})
    assert "anthropic:sonnet" not in (after.get("pending") or {})


def test_a_lock_held_at_the_start_means_rc_73_and_no_prompt(unpriced, monkeypatch):
    unpriced.lock_free = False
    tty(monkeypatch)
    no_prompt(monkeypatch)
    assert run("update") == models.EXIT_LOCKED


def test_a_lock_taken_while_the_prompt_is_open_means_rc_73_without_waiting(unpriced, monkeypatch, capsys):
    def take():
        unpriced.lock_free = False            # another run takes the lock while the question is on screen

    tty(monkeypatch, Prompts(mi.UPDATE_ALL, side_effect=take))
    assert run("update") == models.EXIT_LOCKED
    assert "already running" in capsys.readouterr().out
    assert unpriced.patches == []


# --- AC-S8c-7: failed health after Update now ---------------------------------------------------------

def test_update_now_with_a_failed_health_check_restores_everything(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, priced=False, health=False)
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: e.overlay)
    tty(monkeypatch, Prompts(mi.UPDATE_NOW))
    assert run("update", "--class", "sonnet") == models.EXIT_ROLLED_BACK
    assert e.doc["agents"]["entries"]["main"]["model"]["primary"] == "anthropic/claude-sonnet-5"
    ov = e.state()
    assert "anthropic:sonnet" not in ov["pins"] and ov["state"]["last_result"] == "rolled_back"


# --- AC-S8c-10: the applied set is the answered set, from one discovery --------------------------------

def test_the_applied_set_equals_the_answered_set_and_discovery_runs_once(unpriced, monkeypatch):
    p = Prompts(mi.CHOOSE, mi.NOT_NOW, mi.UPDATE_NOW, mi.NEVER, mi.NEVER_MODEL)
    tty(monkeypatch, p)
    assert run("update", "--no-restart") == 0
    ov = unpriced.state()
    assert list(ov["pins"]) == ["anthropic:sonnet"]                   # opus: not now; haiku: never
    assert ov["policy"]["slots"]["anthropic:haiku"]["never_ids"] == ["claude-haiku-5-5"]
    lists = [a for a in unpriced.runner_calls if "list" in a]
    assert len(lists) == 1                                            # one discovery, not two


# --- AC-S8c-9: the sentinel key never reaches a prompt, a result or a notice ----------------------------

def test_the_key_never_appears_in_prompts_or_output(tmp_path, monkeypatch, capsys):
    e = _provider_env(tmp_path, monkeypatch, "google")
    deps = dataclasses.replace(e.deps(), key_for=lambda p: SENTINEL)
    monkeypatch.setattr(models_cmd, "get_deps", lambda: deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: e.overlay)
    p = Prompts(mi.UPDATE_NOW)
    tty(monkeypatch, p)
    run("update")
    out = capsys.readouterr().out
    assert SENTINEL not in out and all(SENTINEL not in m for m, _c in p.asked)
    assert SENTINEL not in json.dumps(e.state()) and all(SENTINEL not in json.dumps(ev) for ev in e.events)


# --- scripted apply and review ------------------------------------------------------------------------

def test_apply_proposals_approves_and_applies_without_a_prompt(unpriced, monkeypatch):
    tty(monkeypatch)
    no_prompt(monkeypatch)
    assert run("update", "--apply-proposals", "--no-restart") == 0
    assert set(unpriced.state()["pins"]) == {"anthropic:opus", "anthropic:sonnet", "anthropic:haiku"}


def test_review_needs_a_terminal(unpriced, monkeypatch, capsys):
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert run("update", "--review") == 2


def test_the_result_says_which_files_were_re_rendered_or_that_none_embedded_the_id(unpriced, monkeypatch, capsys):
    tty(monkeypatch, Prompts(mi.UPDATE_NOW))
    run("update", "--no-restart", "--class", "sonnet")
    assert "No other kit file (executors.yaml, litellm.yaml" in capsys.readouterr().out


def test_the_wrapper_parses():
    assert subprocess.run(["bash", "-n", "scripts/openclaw/openclaw-models-update.sh"]).returncode == 0


# --- the pure rules ---------------------------------------------------------------------------------

def _prop(slot="google:gemini-flash", decision="needs_approval", **kw):
    provider, cls = slot.split(":")
    return models.Proposal(cls, "a", "b", "minor", "unknown", decision, ["price unknown"], provider)


def test_resolve_never_asks_without_a_prompt():
    props = [_prop(decision="needs_approval"), _prop("anthropic:opus", "auto"), _prop("anthropic:haiku", "suppressed")]
    res = mi.resolve(props, lambda slot: {}, can_prompt=False)
    assert [p.slot for p in res.pending] == ["google:gemini-flash"] and res.ask == []
    assert [p.slot for p in res.apply] == ["anthropic:opus"] and [p.slot for p in res.suppressed] == ["anthropic:haiku"]
    assert [p.slot for p in mi.resolve(props, lambda slot: {}, can_prompt=True).ask] == ["google:gemini-flash"]


def test_apply_answers_changes_policy_only_for_always_and_never():
    ov = mp.empty_overlay()
    out = mi.apply_answers(ov, {"google:gemini-flash": mi.Answer(mi.KIND_NOW, "x")})
    assert out["approvals"] == {"google:gemini-flash": "x"} and out["policy"]["slots"] == {}
    out = mi.apply_answers(ov, {"google:gemini-flash": mi.Answer(mi.KIND_NOT_NOW, "x")})
    assert out == mp.migrate(ov)
    out = mi.apply_answers(ov, {"google:gemini-flash": mi.Answer(mi.KIND_ALWAYS, "x")})
    assert out["policy"]["slots"]["google:gemini-flash"] == {"answer": "always", "max_bump": "any"}


def test_the_revision_ignores_the_timers_bookkeeping():
    a = mp.empty_overlay()
    b = mp.empty_overlay()
    b["state"]["last_run"] = "2026-10-09T00:00:00+00:00"
    b["pending"]["google:gemini-flash"] = {"to": "x"}
    b["history"].append({"action": "noop"})
    assert mi.revision(a) == mi.revision(b)
    b["pins"]["google:gemini-flash"] = "x"
    assert mi.revision(a) != mi.revision(b)
