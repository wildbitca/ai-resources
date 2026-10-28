# shellcheck shell=bash
# scratch.sh - the one place shell scripts get scratch space (ADR-0020). Source it; never execute it.
#
#   . "$KIT/scripts/scratch.sh"
#   scratch_begin <role> <slug> [--keep-on-fail] [--no-trap]  # exports AI_RESOURCES_SCRATCH, TMPDIR and the shared caches; traps EXIT INT TERM
#   scratch_tmpfile [-d] [template]  # a temp file/dir inside $AI_RESOURCES_SCRATCH/tmp
#   scratch_tmp_beside [-d] <dir> <tmpl>  # a temp entry beside its destination (staging, write-then-rename), not scratch
#
# Success (exit 0) removes the run; so does a failure unless the caller passed --keep-on-fail (then logs/ stays 3 days). Never write directly under
# ~/.cache or /tmp.

_SCRATCH_KIT="${AGENT_KIT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# PYTHONSAFEPATH (3.11+): do not put the current directory on sys.path, or a json.py planted in a repo would run.
_scratch_py() { PYTHONSAFEPATH=1 PYTHONPATH="$_SCRATCH_KIT/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 -m ai_resources.scratch "$@"; }

scratch_begin() {
  local role="${1:-script}" slug="${2:-}" notrap="" keep="" out
  shift 2 2>/dev/null || shift $#
  for a in "$@"; do
    case "$a" in --no-trap) notrap=1 ;; --keep-on-fail) keep="--keep-on-fail" ;; esac
  done
  out="$(_scratch_py new --role "$role" --slug "$slug" --owner-pid "$$" $keep --shell)" || return 1
  eval "$out"
  [ -n "$notrap" ] && return 0
  trap 'scratch_end $?' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

# For a script that owns its EXIT trap: call scratch_begin <role> <slug> --no-trap, then scratch_end "$rc" from it.
scratch_end() {
  local rc="${1:-0}"
  trap - EXIT INT TERM
  [ -n "${AI_RESOURCES_SCRATCH:-}" ] && _scratch_py done "$AI_RESOURCES_SCRATCH" --status "$rc" >/dev/null 2>&1
  return 0
}

scratch_tmpfile() {
  [ -n "${AI_RESOURCES_SCRATCH:-}" ] || { echo "scratch_tmpfile: call scratch_begin first" >&2; return 1; }
  mktemp "$@" -p "$AI_RESOURCES_SCRATCH/tmp"
}

scratch_tmp_beside() {
  local d=""
  [ "${1:-}" = "-d" ] && { d="-d"; shift; }
  mktemp ${d:+"$d"} -p "$1" "${2:-tmp.XXXXXXXX}"
}
