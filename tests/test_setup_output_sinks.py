"""Every message the setup package echoes from a subprocess goes through ui.safe_text (clean, then mask)."""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ai_resources" / "setup"
# A slice of a command's output, or a captured stdout/stderr, placed inside a message.
_SINK = re.compile(r"(?:\bout|bridge_out|\.stdout|\.stderr|\bstderr|\bstdout)\b[^\n]*\[-\d+:\]|\(r\.stdout \+ r\.stderr\)")


def test_no_message_echoes_a_slice_of_command_output_unmasked():
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _SINK.search(line) and "safe_text" not in line and re.search(r"\b(error|warn|detail|info|ok)\(", line):
                offenders.append(f"{path.relative_to(ROOT)}:{no}: {line.strip()}")
    assert offenders == []


def test_the_lanes_module_masks_what_it_echoes():
    from ai_resources.setup.cockpits import openclaw_lanes
    assert hasattr(openclaw_lanes, "ui")
