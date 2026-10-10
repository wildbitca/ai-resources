"""Memory-pressure detection for the OpenClaw gateway cgroup (ADR-0004).

Pure of side effects: it READS cgroup and /proc files and runs three read-only commands through an
injected `runner` (`systemctl --user show`, `openclaw health`, `free -b`). It never restarts, stops,
signals or writes anything, so the health timer can call it every tick and a test can drive it from a
fixture tree with no host at all.

The whole module is the SOLE owner of one concern: turning raw signals into one classification.

    healthy | pressure | hard | frozen | refused-probe | health-fail

* `pressure`: in BOTH samples of one run, the cgroup is at or above PRESSURE_PCT of MemoryHigh, swap is at
  or above SWAP_PCT, and `memory.events high` grew between the samples. The counter is cumulative since the
  cgroup was created (it read 23,235,996 on the live host), so only its delta means anything.
* `hard`: pressure that is also at or above HARD_PCT of MemoryHigh in both samples.
* `frozen`: the gateway cannot restart gracefully. Its main thread sits in state D with a `wchan` naming
  `mem_cgroup_handle_over_high` (the MemoryHigh throttle), or `openclaw health` says "still starting" for
  longer than FROZEN_MIN_S, or health fails while the cgroup is over the pressure threshold. Never restarted.
* `refused-probe`: a required signal could not be read. Fail closed: nothing acts.
* `health-fail`: health fails and there is no memory pressure (the legacy trigger).

Restart GATES (window, cooldown, cap, flock) are NOT here: they live next to the action in
`openclaw_host.graceful_restart`. Journal parsing for the recovery report is `parse_recovery_report`.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

Runner = Callable[..., "tuple[int, str]"]

UNIT = "openclaw-gateway.service"
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")

PRESSURE_PCT = 90
HARD_PCT = 105
SWAP_PCT = 90
FROZEN_MIN_S = 600
CONFIRM_S = 60
THROTTLE_WCHAN = "mem_cgroup_handle_over_high"
HEALTH_TIMEOUT_S = 45

CLASSIFICATIONS = ("healthy", "pressure", "hard", "frozen", "refused-probe", "health-fail")


class ProbeError(Exception):
    """A REQUIRED signal could not be read. The caller fails closed."""


# --- raw readers -----------------------------------------------------------------------------------

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ProbeError(f"{path.name}: {e.strerror or 'unreadable'}") from None


def _read_opt(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _kv(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def _pressure_line(text: str | None, kind: str) -> float | None:
    for line in (text or "").splitlines():
        if line.startswith(kind + " "):
            m = re.search(r"\bavg10=([0-9.]+)", line)
            return float(m.group(1)) if m else None
    return None


def read_cgroup(control_group: str, cgroup_root: Path = CGROUP_ROOT) -> dict:
    """The gateway cgroup's memory numbers.

    `current`, `high` and `events` are REQUIRED (ProbeError when unreadable or malformed); the rest is
    None when its file is missing. `high` is None when MemoryHigh is `max` (no throttle configured).
    """
    d = cgroup_root / control_group.lstrip("/")
    cur = _read(d / "memory.current")
    high = _read(d / "memory.high")
    events = _kv(_read(d / "memory.events"))
    if not cur.isdigit():
        raise ProbeError("memory.current: not a number")
    if high != "max" and not high.isdigit():
        raise ProbeError("memory.high: not a number")
    if "high" not in events:
        raise ProbeError("memory.events: no `high` counter")
    stat = _kv(_read_opt(d / "memory.stat"))
    psi = _read_opt(d / "memory.pressure")
    pids = _read_opt(d / "pids.current")
    procs = _read_opt(d / "cgroup.procs")
    return {
        "current": int(cur),
        "high": None if high == "max" else int(high),
        "max": _read_opt(d / "memory.max"),
        "events": {k: events.get(k) for k in ("high", "max", "oom", "oom_kill")},
        "anon": stat.get("anon"), "file": stat.get("file"),
        "psi_some_avg10": _pressure_line(psi, "some"), "psi_full_avg10": _pressure_line(psi, "full"),
        "pids": int(pids) if pids and pids.isdigit() else None,
        "procs": len(procs.split()) if procs is not None else None,
    }


def read_proc(pid: str | int, proc_root: Path = PROC_ROOT) -> dict:
    """Scheduler state letter (from `stat`) and `wchan` of `pid`. Either is None when unreadable."""
    base = proc_root / str(pid)
    state = None
    stat = _read_opt(base / "stat")
    if stat:
        # "pid (comm) S ...": comm may hold spaces and parentheses, so cut at the LAST ")".
        tail = stat.rsplit(")", 1)[-1].split()
        state = tail[0] if tail else None
    return {"state": state, "wchan": _read_opt(base / "wchan")}


def parse_swap(text: str) -> dict | None:
    """`free -b` output -> {"total": n, "used": n}; None when there is no Swap line."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "Swap:" and len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            return {"total": int(parts[1]), "used": int(parts[2])}
    return None


def _unit_props(runner: Runner, unit: str, env: dict | None = None) -> dict[str, str]:
    rc, out = runner(["systemctl", "--user", "show", unit, "-p", "ControlGroup", "-p", "MainPID",
                      "-p", "ActiveState", "-p", "ActiveEnterTimestamp", "--timestamp=unix"], env=env)
    if rc != 0:
        raise ProbeError("systemctl show failed")
    props: dict[str, str] = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        props[k.strip()] = v.strip()
    return props


def sample(*, runner: Runner, openclaw_bin: str = "openclaw", unit: str = UNIT,
           cgroup_root: Path = CGROUP_ROOT, proc_root: Path = PROC_ROOT,
           clock: Callable[[], float] = time.time, health: "tuple[int, str] | None" = None,
           env: dict | None = None) -> dict:
    """One reading of everything the classifier needs. Raises ProbeError on a required signal.

    `health` is an `(rc, text)` the caller already measured (status does), so a gateway that does not
    answer is not asked twice."""
    props = _unit_props(runner, unit, env)
    cg = props.get("ControlGroup", "")
    if not cg:
        raise ProbeError("the unit has no ControlGroup (not running)")
    entered = props.get("ActiveEnterTimestamp", "")
    m = re.fullmatch(r"@(\d+)", entered)
    rc, text = health if health is not None else runner([openclaw_bin, "health"], env=env,
                                                        timeout=HEALTH_TIMEOUT_S)
    lowered = (text or "").lower()
    pid = props.get("MainPID", "")
    swap_rc, swap_text = runner(["free", "-b"], env=env)
    return {
        "ts": clock(),
        "unit": {"active": props.get("ActiveState", ""), "main_pid": pid,
                 "active_enter": int(m.group(1)) if m else None},
        "cgroup": read_cgroup(cg, cgroup_root),
        "proc": read_proc(pid, proc_root) if pid.isdigit() and int(pid) > 0 else {"state": None, "wchan": None},
        "health": {"rc": rc, "starting": "still starting" in lowered},
        "swap": parse_swap(swap_text) if swap_rc == 0 else None,
    }


# --- classification --------------------------------------------------------------------------------

def _pct(cur: int, high: int | None) -> float | None:
    return None if not high else cur * 100.0 / high


def _swap_pct(sw: dict | None) -> float | None:
    if not sw or not sw.get("total"):
        return None
    return sw["used"] * 100.0 / sw["total"]


def classify(samples: list[dict], *, pressure_pct: float = PRESSURE_PCT, hard_pct: float = HARD_PCT,
             swap_pct: float = SWAP_PCT, frozen_min_s: float = FROZEN_MIN_S,
             now: float | None = None) -> dict:
    """{"classification": one of CLASSIFICATIONS, "evidence": {...}} from two (or more) samples.

    Every condition must hold in EVERY sample, so a spike that clears in the second reading is
    `healthy`. The evidence carries numbers only: no process arguments, no session text.
    """
    if len(samples) < 2:
        return {"classification": "refused-probe", "evidence": {"reason": "fewer than two samples"}}
    first, last = samples[0], samples[-1]
    now = last["ts"] if now is None else now
    ev: dict = {"samples": len(samples)}

    if last["unit"].get("active") != "active":
        ev["reason"] = f"unit is {last['unit'].get('active') or 'unknown'}"
        return {"classification": "refused-probe", "evidence": ev}

    pcts = [_pct(s["cgroup"]["current"], s["cgroup"]["high"]) for s in samples]
    ev["memory_pct"] = [None if p is None else round(p, 1) for p in pcts]
    ev["memory_current"] = last["cgroup"]["current"]
    ev["memory_high"] = last["cgroup"]["high"]
    ev["anon"], ev["file"] = last["cgroup"].get("anon"), last["cgroup"].get("file")
    ev["psi_full_avg10"] = last["cgroup"].get("psi_full_avg10")
    ev["pids"] = last["cgroup"].get("pids")
    ev["high_events_delta"] = last["cgroup"]["events"]["high"] - first["cgroup"]["events"]["high"]
    sw = [_swap_pct(s.get("swap")) for s in samples]
    ev["swap_pct"] = [None if p is None else round(p, 1) for p in sw]
    ev["health_rc"] = last["health"]["rc"]
    ev["still_starting"] = all(s["health"]["starting"] for s in samples)
    ev["proc_state"], ev["wchan"] = last["proc"]["state"], last["proc"]["wchan"]
    entered = last["unit"].get("active_enter")
    ev["active_age_s"] = None if entered is None else max(0, int(now - entered))

    throttled = all(s["proc"]["state"] == "D" and THROTTLE_WCHAN in (s["proc"]["wchan"] or "") for s in samples)
    starting_long = ev["still_starting"] and ev["active_age_s"] is not None and ev["active_age_s"] >= frozen_min_s
    if throttled or starting_long:
        ev["frozen_by"] = "memory-throttle" if throttled else "still-starting"
        return {"classification": "frozen", "evidence": ev}

    if ev["memory_high"] is None:
        ev["reason"] = "MemoryHigh is not set: no throttle to confirm against"
        return {"classification": "healthy", "evidence": ev}

    over = all(p is not None and p >= pressure_pct for p in pcts)
    swapped = all(p is not None and p >= swap_pct for p in sw)
    growing = ev["high_events_delta"] > 0
    confirmed = over and swapped and growing

    if confirmed and last["health"]["rc"] != 0:
        # Over the limit AND not answering: a graceful restart would be refused ("database is locked"),
        # so this is the frozen path (notify only), never a restart attempt.
        ev["frozen_by"] = "unresponsive-over-limit"
        return {"classification": "frozen", "evidence": ev}
    if confirmed:
        hard = all(p >= hard_pct for p in pcts)
        return {"classification": "hard" if hard else "pressure", "evidence": ev}
    if last["health"]["rc"] != 0:
        return {"classification": "health-fail", "evidence": ev}
    return {"classification": "healthy", "evidence": ev}


def evaluate(*, runner: Runner, openclaw_bin: str = "openclaw", unit: str = UNIT,
             cgroup_root: Path = CGROUP_ROOT, proc_root: Path = PROC_ROOT, confirm_seconds: float = CONFIRM_S,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
             pressure_pct: float = PRESSURE_PCT, hard_pct: float = HARD_PCT, swap_pct: float = SWAP_PCT,
             frozen_min_s: float = FROZEN_MIN_S, health: "tuple[int, str] | None" = None,
             env: dict | None = None) -> dict:
    """Sample, wait `confirm_seconds`, sample again, classify. Any ProbeError is `refused-probe`."""
    kw = dict(runner=runner, openclaw_bin=openclaw_bin, unit=unit, cgroup_root=cgroup_root,
              proc_root=proc_root, clock=clock, health=health, env=env)
    try:
        a = sample(**kw)
        if confirm_seconds > 0:
            sleep(confirm_seconds)
        b = sample(**kw)
    except ProbeError as e:
        return {"classification": "refused-probe", "evidence": {"reason": str(e)},
                "ts": clock(), "confirm_seconds": confirm_seconds}
    out = classify([a, b], pressure_pct=pressure_pct, hard_pct=hard_pct, swap_pct=swap_pct,
                   frozen_min_s=frozen_min_s, now=b["ts"])
    out["ts"] = b["ts"]
    out["confirm_seconds"] = confirm_seconds
    out["thresholds"] = {"pressure_pct": pressure_pct, "hard_pct": hard_pct, "swap_pct": swap_pct,
                         "frozen_min_s": frozen_min_s}
    return out


# --- restart window --------------------------------------------------------------------------------

DEFAULT_TZ = "America/Guayaquil"
_WINDOW = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


def parse_window(window: str) -> tuple[int, int] | None:
    """"02:00-05:00" -> (start, end) in minutes from midnight. None for empty or malformed."""
    m = _WINDOW.match((window or "").strip())
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
        return None
    return h1 * 60 + m1, h2 * 60 + m2


def local_minutes(now: float, tz: str = DEFAULT_TZ) -> int | None:
    """Minutes since local midnight in `tz` (an IANA name); None when the zone cannot be resolved."""
    try:
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(now, tz=ZoneInfo(tz))
    except Exception:  # noqa: BLE001  (unknown zone, no tzdata): fail closed
        return None
    return local.hour * 60 + local.minute


def window_open(now: float, window: str, tz: str = DEFAULT_TZ) -> bool:
    """True when `now` (epoch) falls inside `window` on the clock of `tz`.

    The host clock is UTC while the operator lives in America/Guayaquil (UTC-5), so the zone is an
    explicit knob (OPENCLAW_RESTART_TZ) and never the host's local time. A window that wraps past
    midnight ("22:00-04:00") is handled. Empty, malformed or an unresolvable zone: closed.

    Both ends are INCLUSIVE at minute granularity: "00:00-23:59" is the whole day and "02:00-05:00"
    is open through 05:00:59. "HH:MM-HH:MM" with equal ends is closed (use "00:00-23:59" for a day).
    """
    span = parse_window(window)
    if span is None:
        return False
    minute = local_minutes(now, tz)
    if minute is None:
        return False
    start, end = span
    if start == end:
        return False
    if start < end:
        return start <= minute <= end
    return minute >= start or minute <= end


# --- the recovery report ---------------------------------------------------------------------------

_MARKED = re.compile(r"marked (\d+) interrupted main session", re.IGNORECASE)
_ABORTED = re.compile(r"aborted (\d+) active run", re.IGNORECASE)


def parse_recovery_report(text: str) -> dict:
    """Counts out of a gateway journal excerpt covering one restart.

    Reads only the structural markers OpenClaw writes; never the session text.
    """
    # A journal line never starts with "#": comment lines (a fixture's header) are not log lines.
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    text = "\n".join(lines)
    marked = sum(int(m.group(1)) for m in _MARKED.finditer(text))
    aborted = sum(int(m.group(1)) for m in _ABORTED.finditer(text))
    return {
        "marked_interrupted": marked,
        "aborted_runs": aborted,
        "shutdown_deadline": sum(1 for ln in lines if "shutdown deadline reached" in ln),
        "recovery_started": sum(1 for ln in lines if "started interrupted main session" in ln),
        "tombstoned": sum(1 for ln in lines if re.search(r"tombston", ln, re.IGNORECASE)),
        "use_resume_true": len(re.findall(r"\buseResume=true\b", text)),
        "use_resume_false": len(re.findall(r"\buseResume=false\b", text)),
        "restart_refused": text.count("GATEWAY_RESTART_PREPARATION_REFUSED"),
    }


def journal_since_arg(since: str) -> str:
    """A `journalctl --since` value from an epoch number or an ISO timestamp."""
    since = since.strip()
    if re.fullmatch(r"\d+(\.\d+)?", since):
        return "@" + str(int(float(since)))
    iso = since.replace("T", " ")
    if iso.endswith("Z"):
        iso = iso[:-1] + " UTC"
    return iso


def read_report(since: str, runner: Runner, unit: str = UNIT) -> dict:
    """Run `journalctl --user -u <unit> --since ...` (read-only) and parse it. `available` is False when
    journalctl could not be read, so a caller never mistakes an empty read for "nothing happened"."""
    rc, out = runner(["journalctl", "--user", "-u", unit.removesuffix(".service"), "--since",
                      journal_since_arg(since), "--no-pager", "-o", "short-iso"], timeout=60)
    if rc != 0:
        return {"available": False, **parse_recovery_report("")}
    return {"available": True, **parse_recovery_report(out)}


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
