#!/usr/bin/env bash
# openclaw-verify.sh — checks in one pass that the setup is as expected.
#
# Two uses: by hand after a change, and as a monitor's condition (`--notify`, from the daily
# timer). Exits 0 when EVERYTHING is fine and 1 as soon as anything fails, and says what.
# It changes nothing: it only reads.
#
# Checks 1-4 and 9 run everywhere. Checks 5-8 need infrastructure a generic host does not have
# (an ingress, an OTLP receiver, an Alloy config, Flux) and are OFF until the matching
# OPENCLAW_VERIFY_* variable in ~/.openclaw/kit-host.env turns them on.
#
# `--notify` writes the run to verify.log and messages the operator ONLY when something failed.
# A check that writes every day stops being read, and then it is worth nothing: that is the
# lesson of the k3s backups, which were broken for a month with the failure written in a log
# nobody opened.
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

notify_mode=no
[ "${1:-}" = "--notify" ] && notify_mode=yes

failures=0
ok()  { printf "  [ok] %-34s %s\n" "$1" "$2"; }
bad() { printf "  [FAIL] %-34s %s\n" "$1" "$2"; failures=$((failures+1)); }

run_checks() {
  # 1. The gateway, which everything else hangs from.
  [ "$(systemctl --user is-active "$OPENCLAW_UNIT")" = active ] \
    && ok "gateway" "active" || bad "gateway" "NOT active"
  openclaw health >/dev/null 2>&1 && ok "gateway health" "answers" || bad "gateway health" "does not answer"

  # 2. That it still starts by itself after a machine reboot.
  [ "$(systemctl --user is-enabled "$OPENCLAW_UNIT" 2>/dev/null)" = enabled ] \
    && [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = yes ] \
    && ok "start at boot" "enabled + linger" || bad "start at boot" "check enabled/linger"

  # 3. The six timers: three backup tiers, maintenance, watchdog and this verifier. Without
  #    them there is no backup, no maintenance and no safety net.
  n=$(systemctl --user list-timers "openclaw-*" --no-pager 2>/dev/null | grep -c "openclaw-")
  [ "$n" -ge 6 ] && ok "timers" "$n armed" || bad "timers" "only $n (expected 6)"

  # 4. Backup: that one exists and is not stale. A stale backup lies worse than none.
  latest=$(ls -1t "$OPENCLAW_BACKUP_DIR"/*/*.tar.gz 2>/dev/null | head -1)
  if [ -n "$latest" ]; then
    h=$(( ( $(date +%s) - $(stat -c %Y "$latest") ) / 3600 ))
    [ -f "$latest.sha256" ] || bad "backup checksum" "missing .sha256 for $(basename "$latest")"
    [ "$h" -le 36 ] && ok "local backup" "${h}h ago" || bad "local backup" "${h}h ago (>36)"
  else
    bad "local backup" "there is none"
  fi

  # 5. Exposure: domain, certificate and the gateway answering behind the ingress.
  if [ "${OPENCLAW_VERIFY_INGRESS:-0}" = 1 ] && [ -n "${OPENCLAW_PUBLIC_URL:-}" ]; then
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 15 "$OPENCLAW_PUBLIC_URL" 2>/dev/null)
    [ "$code" = 200 ] && ok "$OPENCLAW_PUBLIC_URL" "200" || bad "$OPENCLAW_PUBLIC_URL" "http=$code"
  fi

  # 6. Telemetry: that the receiver still listens where the CLI thinks it is.
  if [ "${OPENCLAW_VERIFY_OTLP:-0}" = 1 ] && [ -n "${OPENCLAW_OTLP_ENDPOINT:-}" ]; then
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 15 -X POST \
      -H "Content-Type: application/json" -d '{"resourceLogs":[]}' \
      "http://$OPENCLAW_OTLP_ENDPOINT/v1/logs" 2>/dev/null)
    [ "$code" = 200 ] && ok "OTLP receiver" "200" || bad "OTLP receiver" "http=$code"
  fi

  # 7. That the content filter is still in place. It protects the separation between clients:
  #    if someone rewrites the Alloy config without it, prompts start leaving the node.
  #    The context is literal on purpose: the kit must never inherit an ambient one.
  if [ "${OPENCLAW_VERIFY_ALLOY_FILTER:-0}" = 1 ]; then
    if kubectl --context default get cm -n monitoring alloy-config -o jsonpath='{.data.config\.alloy}' 2>/dev/null \
       | grep -q 'delete_key(attributes, "tool_input")'; then
      ok "tool_input filter" "present in Alloy"
    else
      bad "tool_input filter" "ABSENT: prompts would leave the node"
    fi
  fi

  # 8. GitOps and providers: that nothing was left half done. A kustomization that is still
  #    reconciling does not say "True" and is not a failure: the first run of this monitor
  #    cried wolf because of it. Only `False` counts, and even then it is retried: a
  #    reconciliation in flight can pass through False for a moment.
  if [ "${OPENCLAW_VERIFY_FLUX:-0}" = 1 ]; then
    failing() { flux --context default get kustomizations --no-header 2>/dev/null | awk '$4=="False"' | wc -l; }
    k=$(failing)
    if [ "$k" != 0 ]; then sleep 20; k=$(failing); fi
    if [ "$k" = 0 ]; then ok "flux" "no kustomization is False"; else
      bad "flux" "$k False: $(flux --context default get kustomizations --no-header 2>/dev/null | awk '$4=="False"{print $1}' | tr '\n' ' ')"
    fi
  fi

  # 9. The kit's hooks and the operator's: if `ai-resources setup` overwrites them it shows here.
  expected="kit_handoff_guard"
  [ -n "${OPENCLAW_NARRATION:-}" ] && expected="$expected openclaw_team_progress"
  [ "${OPENCLAW_GUARD:-0}" = 1 ] && expected="$expected openclaw_gateway_guard"
  expected="$expected ${OPENCLAW_VERIFY_HOOKS:-}"
  python3 - "$expected" <<'PY' || failures=$((failures+1))
import json, os, sys
expected = set(sys.argv[1].split())
path = os.path.expanduser("~/.claude/settings.json")
try:
    doc = json.load(open(path))
except Exception as exc:
    print(f"  [FAIL] {'hooks':<34} cannot read {path}: {exc}")
    sys.exit(1)
seen = {c["command"].split("/")[-1].rstrip('"').replace(".py", "")
        for ev in doc.get("hooks", {}).values() for i in ev for c in i["hooks"]}
missing = {e for e in expected if not any(e in v for v in seen)}
detail = "all %d registered" % len(expected) if not missing else "missing: " + ",".join(sorted(missing))
print(f"  [{'ok' if not missing else 'FAIL'}] {'hooks':<34} {detail}")
sys.exit(1 if missing else 0)
PY
}

if [ "$notify_mode" = yes ]; then
  mkdir -p "$OPENCLAW_LOG_DIR"
  output="$(run_checks 2>&1)"
  printf "%s\n" "$output" >> "$OPENCLAW_LOG_DIR/verify.log"
  failed="$(printf "%s\n" "$output" | grep -E "^  \[FAIL\]")"
  [ -z "$failed" ] && exit 0
  notify "The setup verifier found failures:
$failed" || true
  exit 1
fi

run_checks
echo
if [ "$failures" -eq 0 ]; then echo "ALL OK"; else echo "FAILURES: $failures"; fi
exit $(( failures > 0 ? 1 : 0 ))
