"""A fake `agy` for `agy run` tests: mode from FAKE_AGY_MODE, writes the files listed in FAKE_AGY_WRITE (relative to cwd)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

mode = os.environ.get("FAKE_AGY_MODE", "ok")
argv = sys.argv[1:]
Path(os.environ["FAKE_AGY_LOG"]).write_text(json.dumps({"argv": argv, "cwd": os.getcwd()}))
for rel in filter(None, os.environ.get("FAKE_AGY_WRITE", "").split(",")):
    Path(rel).parent.mkdir(parents=True, exist_ok=True)
    Path(rel).write_text("written by agy\n")


def emit(**ev):
    print(json.dumps(ev), flush=True)


if mode == "timeout":
    time.sleep(30)
emit(event="init", conversation_id="c-1")
if mode == "crash":
    print("boom: unexpected", file=sys.stderr)
    sys.exit(3)
if mode == "leak":
    print("auth failed: ghp_" + "A" * 30 + " and Bearer abc.def and sk-ant-" + "b" * 20, file=sys.stderr)
    sys.exit(3)
if mode == "quota":
    emit(result={"status": "ERROR", "response": "", "error": {"message": "You've hit your limit, resets 5pm"}})
    sys.exit(1)
if mode == "empty":
    emit(result={"status": "SUCCESS", "response": "", "usage": {"input_tokens": 4, "output_tokens": 0}})
    sys.exit(0)
body = "Looked at it.\nStatus: success\nBlocked: false\nVerdict: PASS\n"
emit(step_update={"step_type": "agent_response", "text_delta": "Looked at it."})
emit(result={"status": "SUCCESS", "response": body, "usage": {"input_tokens": 120, "output_tokens": 17}})
