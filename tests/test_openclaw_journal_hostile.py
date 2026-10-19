"""A journal line is untrusted text: the host token is validated and what is printed is cleaned (2.2b section 6)."""
from __future__ import annotations

import pathlib
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources import openclaw_pressure as pr  # noqa: E402

TLS = ("2026-10-11T19:38:10+00:00 box node[1]: [secrets/egress-proxy] secret egress TLS certificate unavailable; "
       "check OpenSSL and the system clock, then retry kind={kind} host={host} substituted=false")


class Runner:
    def __init__(self, text):
        self.text, self.calls = text, []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return 0, self.text


def _hosts(text):
    return pr.parse_journal_signals(text)["tls_hosts"]


@pytest.mark.parametrize("name", [
    "api.example.com", "a", "xn--bcher-kva.example", "a-b.c-d.example.org", "x" * 63 + ".example", "1.2.3.example",
    ".".join(["a" * 61] * 4),                                  # 247 characters
])
def test_a_valid_host_name_is_kept(name):
    assert _hosts(TLS.format(kind="refused", host=name)) == [name]


@pytest.mark.parametrize("name", [
    "-bad.example", "bad-.example", "a..b", ".a.example", "a.example.", "a_b.example", "API.EXAMPLE.COM",
    "x" * 64 + ".example", ".".join(["a" * 63] * 4) + ".b", "café.example", "a.example:443", "a/b",
    "a\x1b[31m.example", "a‮.example", "[::1]", "a%2eexample", "*.example.com",
])
def test_anything_that_is_not_a_lowercase_rfc_1123_name_is_dropped(name):
    assert _hosts(TLS.format(kind="refused", host=name)) == []


def test_only_lines_that_say_kind_refused_count():
    text = "\n".join([TLS.format(kind="allowed", host="allowed.example"),
                      TLS.format(kind="refused", host="refused.example"),
                      "secret egress TLS certificate unavailable host=nokind.example"])
    assert _hosts(text) == ["refused.example"]


def test_a_hostile_line_prints_nothing_hostile_and_costs_linear_time():
    bad_host = "a" * 10_000 + "\x1b[31m" + "‮" * 100
    lines = [TLS.format(kind="refused", host="good.example")] + [TLS.format(kind="refused", host=bad_host)] * 2000
    text = "\n".join(lines)
    t = time.time()
    found = host.journal_findings(Runner(text), None)
    assert time.time() - t < 5
    [(level, message, remedy)] = found
    assert "good.example" in message and "1 host(s)" in message
    for printed in (message, remedy):
        assert "\x1b" not in printed and "‮" not in printed and "aaaa" not in printed


def test_the_journal_read_has_a_byte_cap_as_well():
    cap = pr.JOURNAL_SIGNAL_BYTES
    assert 0 < cap <= 16 * 1024 * 1024
    filler = ("x" * 999 + "\n") * (cap // 1000 + 10)
    text = TLS.format(kind="refused", host="early.example") + "\n" + filler + TLS.format(kind="refused",
                                                                                          host="late.example")
    assert len(text) > cap
    runner = Runner(text)
    sig = pr.read_journal_signals(runner)
    assert sig["tls_hosts"] == ["early.example"], "what lies past the cap is not read"
    [(_argv, kw)] = runner.calls
    assert kw.get("max_bytes") == cap, "the reader is told the cap, so a huge journal is never held in memory"


def test_printable_text_only_comes_from_the_stability_reason_too():
    stab = {"present": True, "reason": "gateway.restart_close_failed\x1b[2J‮", "age_hours": 1.0}
    text = "subagent completion deferred during gateway restart source=x"
    [(_lv, message, _r)] = host.journal_findings(Runner(text), stab)
    assert "\x1b" not in message and "‮" not in message and "restart_close_failed" in message


def test_status_and_verify_never_name_a_host_that_failed_validation(capsys):
    text = TLS.format(kind="refused", host="evil‮.example") + "\n" + TLS.format(kind="refused",
                                                                                   host="BAD.EXAMPLE")
    assert host.journal_findings(Runner(text), None) == []


def test_the_default_runner_stops_a_process_at_the_byte_cap():
    import sys as _sys
    code = "import sys\nsys.stdout.write('x' * 5_000_000)\nsys.stdout.flush()\n"
    rc, out = host.default_runner([_sys.executable, "-c", code], max_bytes=100_000, timeout=20, truncate_ok=True)
    assert rc == 0 and len(out) == 100_000                  # the journal read: a truncated answer is acceptable
    rc, out = host.default_runner([_sys.executable, "-c", "print('short')"], max_bytes=100_000, timeout=20)
    assert rc == 0 and out == "short"
    rc, out = host.default_runner([_sys.executable, "-c", "import time; time.sleep(30)"], max_bytes=1000, timeout=1)
    assert rc == 124
    assert host.default_runner(["/no/such/binary"], max_bytes=10)[0] == 127


def test_a_capped_output_is_a_failure_unless_the_caller_accepts_truncation():
    import sys as _sys
    code = "import sys\nsys.stdout.write('x' * 5_000_000)\nsys.stdout.flush()\n"
    rc, out = host.default_runner([_sys.executable, "-c", code], max_bytes=100_000, timeout=20)
    assert rc != 0 and len(out) == 100_000


def test_a_timeout_kills_the_whole_process_group_and_returns_promptly(tmp_path):
    """A shell that backgrounds a grandchild holding the pipe: killing only the child would leave it alive."""
    import os as _os
    import signal as _signal
    import sys as _sys
    import time as _time
    pidfile = tmp_path / "grandchild.pid"
    script = f"sleep 60 & echo $! > {pidfile}; wait"
    start = _time.monotonic()
    rc, _out = host.default_runner(["/bin/sh", "-c", script], max_bytes=1000, timeout=1)
    assert rc == 124 and _time.monotonic() - start < 15
    pid = int(pidfile.read_text())
    deadline = _time.monotonic() + 5
    while _time.monotonic() < deadline:
        try:
            _os.kill(pid, 0)
        except ProcessLookupError:
            break
        _time.sleep(0.1)
    else:
        _os.kill(pid, _signal.SIGKILL)
        raise AssertionError("the grandchild survived the timeout")


def test_the_reap_wait_is_bounded(monkeypatch):
    import subprocess as _sp

    class Stuck:
        pid = 2 ** 22 + 12345
        calls = 0

        def wait(self, timeout=None):
            Stuck.calls += 1
            assert timeout is not None, "an unbounded wait"
            raise _sp.TimeoutExpired("x", timeout)

        def kill(self):
            pass

    assert host._reap(Stuck(), 0.01) is None and Stuck.calls == 2


# --- 2.2c, 6d / 6e ----------------------------------------------------------------------------------------------

class ReversingRunner(Runner):
    """A journalctl that honours `--reverse`: newest line first, as the real one prints it."""

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        lines = self.text.splitlines()
        return 0, "\n".join(reversed(lines) if "--reverse" in argv else lines)


def test_the_byte_cap_keeps_the_newest_refusals_not_the_oldest(monkeypatch):
    monkeypatch.setattr(pr, "JOURNAL_SIGNAL_BYTES", 3000)
    old = [TLS.format(kind="refused", host=f"old{i}.example") for i in range(60)]
    newest = TLS.format(kind="refused", host="newest.example")
    runner = ReversingRunner("\n".join([*old, newest]))
    sig = pr.read_journal_signals(runner)
    [(argv, _kw)] = runner.calls
    assert "--reverse" in argv and "-n" in argv
    assert sig["tls_hosts"][0] == "newest.example", "the newest refusal is read first and survives the cap"
    assert "old59.example" in sig["tls_hosts"] and "old0.example" not in sig["tls_hosts"]
    [(_lv, message, _remedy)] = host.journal_findings(runner, None)
    assert "newest.example" in message


@pytest.mark.parametrize("raw", ["good.example\x1fevil", "good.example\x0bevil", "good.example\x85evil",
                                 "good.example\u2028evil", "good.example\x00evil", "good.example\x1b[0m"])
def test_a_control_character_inside_a_host_drops_the_host_instead_of_truncating_it(raw):
    assert _hosts(TLS.format(kind="refused", host=raw)) == []


def test_a_blank_still_ends_the_host_token():
    text = TLS.format(kind="refused", host="good.example") + ""
    assert _hosts(text) == ["good.example"]


# --- kit 2.2d, 3e: a CR or a TAB inside the token drops the host, it does not cut it to a valid prefix ----------------

@pytest.mark.parametrize("raw", ["good.example\revil", "good.example\tevil", "good.example\r", "good.example\t",
                                 "good.example\r\tevil.example", "\tgood.example"])
def test_a_carriage_return_or_tab_inside_the_host_token_drops_the_host(raw):
    assert _hosts(TLS.format(kind="refused", host=raw)) == []


def test_a_host_after_a_dropped_one_on_the_next_line_is_still_read():
    text = (TLS.format(kind="refused", host="good.example\rcut.example") + "\n"
            + TLS.format(kind="refused", host="fine.example"))
    assert _hosts(text) == ["fine.example"]


# --- kit 2.2e, 4d: `host=` is a field of its own, not the tail of another one ----------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("secret egress TLS certificate unavailable; retry kind=refused url=https://a.example/?host=wrong.example "
     "host=right.example substituted=false", ["right.example"]),
    ("secret egress TLS certificate unavailable; retry kind=refused proxy-host=wrong.example host=right.example",
     ["right.example"]),
    ("secret egress TLS certificate unavailable; retry kind=refused proxy-host=wrong.example", []),
    ("secret egress TLS certificate unavailable; retry kind=refused x?host=wrong.example", []),
    ("secret egress TLS certificate unavailable; retry kind=refused host=right.example", ["right.example"]),
    ("host=first.example secret egress TLS certificate unavailable kind=refused", ["first.example"]),
    ("secret egress TLS certificate unavailable; retry kind=refused\thost=tab.example", ["tab.example"]),
])
def test_the_host_field_is_anchored_at_a_word_start(line, expected):
    assert _hosts(line) == expected
