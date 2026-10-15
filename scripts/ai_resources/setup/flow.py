"""Wizard control-flow primitives (stdlib only: no rich, no questionary)."""
from __future__ import annotations


class Cancelled(Exception):
    """The operator cancelled (Ctrl-C / EOF) at a wizard prompt."""

    def __init__(self, step: str = ""):
        super().__init__(step)
        self.step = step


class GoBack(Exception):
    """The operator asked to return to the previous step."""


BACK = object()
