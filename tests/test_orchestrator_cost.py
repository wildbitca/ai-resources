"""Cost reporting per run (ADR-0015 A6; the operator dropped the spend cap, so this only reports)."""
from __future__ import annotations

from datetime import datetime, timezone

from ai_resources import audit_sources
from ai_resources.orchestrator import cost


def u(agent, session, ts, inp=1000, out=500, model="claude-sonnet-5-5"):
    return audit_sources.Usage(ts=datetime.fromtimestamp(ts, timezone.utc), source="openclaw", project="", agent=agent,
                               model=model, input=inp, output=out, cache_read=0, cache_write_5m=0, cache_write_1h=0,
                               msg_id=f"{agent}{session}{ts}", session=session, origin="openclaw-api")


RUN = {"id": "r1", "started_at": 100.0, "finished_at": 500.0,
       "steps": {"plan": {"role": "planner", "session": "s-plan", "agent": "kit-role-planner"},
                 "implement": {"role": "implementer", "session": "s-impl", "agent": "kit-role-implementer"}}}


def price(usage):
    return None if usage.model == "mystery" else (usage.input + usage.output * 5) / 1_000_000


def test_calls_are_attributed_by_session_or_by_agent_inside_the_window():
    usages = [u("kit-role-planner", "s-plan", 200), u("kit-role-implementer", "s-impl", 300, inp=2000),
              u("kit-role-implementer", "other-session", 400),            # same agent, inside the window: counted
              u("kit-role-implementer", "other-session", 900),            # outside the window: not this run's
              u("main", "chat", 250),                                       # not a role agent of this run
              u("kit-role-planner", "s-plan", 5000)]                        # known session wins even outside the window
    out = cost.summarize(RUN, usages, price)
    assert out["total"]["calls"] == 4
    assert out["by_role"]["planner"]["calls"] == 2 and out["by_role"]["implementer"]["calls"] == 2
    assert out["total"]["usd"] == round(sum(price(x) for x in (usages[0], usages[1], usages[2], usages[5])), 4)
    assert out["label"].startswith("USD API-equivalent estimate")


def test_unpriced_calls_are_counted_not_guessed():
    out = cost.summarize(RUN, [u("kit-role-planner", "s-plan", 200, model="mystery")], price)
    assert out["total"]["unpriced_calls"] == 1 and out["total"]["usd"] == 0.0


def test_an_empty_history_is_an_empty_report():
    out = cost.summarize(RUN, [], price)
    assert out["total"] == {"calls": 0, "input": 0, "output": 0, "usd": 0.0, "unpriced_calls": 0} and out["by_role"] == {}


def test_run_cost_reads_through_the_loader_read_only(tmp_path):
    seen = {}

    def loader(agents_dir, since=None):
        seen["dir"], seen["since"] = agents_dir, since
        return [u("kit-role-planner", "s-plan", 200)], audit_sources.LoadStats(databases=2)
    out = cost.run_cost(RUN, tmp_path, loader=loader)
    assert seen["dir"] == tmp_path and seen["since"].timestamp() == 100.0
    assert out["databases"] == 2 and out["missing"] is False and out["total"]["calls"] == 1


def test_the_cost_module_never_pauses_or_caps_anything():
    assert not any(w in " ".join(dir(cost)).lower() for w in ("cap", "pause", "budget", "limit"))
