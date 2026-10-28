# A setup run ended INCOMPLETE (exit 75) or listed skipped changes

`ai-resources setup` prints a "change(s) were not made" block at the end of every run: what, why, and the command that finishes it.
Policy: [ADR-0023](../decisions/0023-auto-gates-for-kit-owned-changes.md).

- **Exit 75, "setup is INCOMPLETE ... config patch(es) were refused"**: OpenClaw answered "OpenClaw state at ...openclaw.sqlite is undergoing
  offline maintenance; retry when it finishes" for the whole wait budget (default 180 s). That text means no gateway holds the state database:
  it is stopped or restarting (a drained window of an earlier run, a watchdog or systemd restart, OpenClaw's own startup maintenance). The
  patch is stored in the setup state (`config_patch_pending`). Check `openclaw health` and `ls ~/.openclaw/watchdog.off`, wait until the gateway
  answers, then run `ai-resources openclaw apply-pending` (or `ai-resources setup` again). A longer wait: `ai-resources setup --patch-wait 600`
  or `AI_RESOURCES_PATCH_WAIT_S=600`.
- **A leftover `~/.openclaw/watchdog.off`** is a maintenance window nobody closed. If no `ai-resources openclaw ...` / `models` / `reconcile`
  process is running (`pgrep -af 'ai-resources|openclaw gateway'`), remove it by hand; the watchdog resumes. Setup never removes a marker it did not create.
- **"Not applied (operator-owned or risky)"** entries: the kit left a value you own, the bindings, the workboard plugin or the off-box identity
  alone. Each is listed with its own command; `--ask` walks through them, `--accept-gates` says yes to all that do not restart the gateway.
- **Restart-required keys** are never applied unattended: `ai-resources openclaw apply-pending` from a terminal outside the gateway, when idle.
- CI: `ai-resources setup --non-interactive --accept-gates` applies the kit-owned and operator-confirmed non-restarting changes of a saved
  answer set; exit 75 means a patch is pending, any other non-zero exit is an error-level finding.
