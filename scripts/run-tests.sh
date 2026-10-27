#!/usr/bin/env bash
# Run the Python suite the way CI does: everything in parallel except the `timing` tests, which assert on
# wall-clock budgets and run afterwards in one process so a loaded CPU cannot fail them.
#
#   scripts/run-tests.sh                 full suite (PYTHON picks the interpreter, default python3)
#   scripts/run-tests.sh tests/test_x.py extra pytest arguments pass through to both passes
#
# REAL_AGENT=1 also runs the tests marked `real_agent` (they need a live provider). One that cannot run is reported
# UNVERIFIED, never green, and fails that run (tests/real_agent_gate.py).
#
# Without pytest-xdist installed the parallel pass runs in one process; the result is the same, only slower.
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
ARGS=("$@")
[ ${#ARGS[@]} -eq 0 ] && ARGS=(tests/)
[ -n "${REAL_AGENT:-}" ] && ARGS+=(--real-agent)

PARALLEL=()
if "$PY" -c "import xdist" 2>/dev/null; then
  PARALLEL=(-n "${PYTEST_WORKERS:-auto}")
fi

status=0
"$PY" -m pytest -q -p no:cacheprovider "${PARALLEL[@]}" -m "not timing" "${ARGS[@]}" || status=$?
# Exit 5 is "no tests collected": a path with no timing tests is not a failure of the serial pass.
"$PY" -m pytest -q -p no:cacheprovider -m timing "${ARGS[@]}" || { rc=$?; [ $rc -eq 5 ] || status=$rc; }
[ $status -eq 5 ] && status=0
exit $status
