#!/usr/bin/env python3
"""StopFailure hook (ai-resources): record a provider quota block so headroom readers can use it.

Claude Code fires StopFailure instead of Stop when an API error ended the turn; for `rate_limit` and `billing_error` this
writes the block to the quota-block sidecar (scripts/ai_resources/scoring/quota_signal.py documents the format and what the
payload does and does not carry). Fire-and-forget: output and exit code are ignored by Claude Code, so this never blocks, never
prints, and swallows every error. Nothing leaves the machine.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from ai_resources.scoring import quota_signal
        now = int(time.time())
        block = quota_signal.block_from_payload(payload, now)
        if block is not None:
            pool = os.environ.get("AI_RESOURCES_QUOTA_POOL") or quota_signal.DEFAULT_POOL
            quota_signal.record(quota_signal.default_path(), pool, block, now)
    except Exception:  # noqa: BLE001 - a hook that cannot record must stay silent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
