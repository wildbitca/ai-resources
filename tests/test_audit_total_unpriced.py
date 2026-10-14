"""The audit TOTAL footer must say when unpriced models contribute nothing to the cost total (G1)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from ai_resources import audit


def test_total_label_is_plain_when_every_model_is_priced():
    assert audit._total_label(0) == "TOTAL"


def test_total_label_counts_unpriced_models():
    assert audit._total_label(2) == "TOTAL (excludes 2 unpriced model(s))"


def _write_session(d: Path, model: str) -> None:
    line = {"message": {"model": model, "usage": {"input_tokens": 10, "output_tokens": 5}}}
    (d / f"{model}.jsonl").write_text(json.dumps(line) + "\n")


def test_audit_footer_marks_one_unpriced_model(tmp_path, monkeypatch, capsys):
    pytest.importorskip("rich")
    _write_session(tmp_path, "claude-sonnet-5")
    _write_session(tmp_path, "mystery-model-x")
    monkeypatch.setattr(audit, "_project_jsonl_dir", lambda: tmp_path)
    rc = audit.cmd_audit(argparse.Namespace(days=0, verbose=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "excludes 1 unpriced" in out
