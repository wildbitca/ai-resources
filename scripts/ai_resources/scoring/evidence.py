"""Evidence labels for the scores database.

Five labels, strongest first. A composed score carries the weakest contributing label. Importance and
tolerance are POLICY, never evidence, so they are not labels here.

    MEASURED-HERE  run by this kit's own harness (Phase B), with a run id and a corpus hash
    MEASURED-3P    run by a named third party (a public leaderboard), dated
    VENDOR         the vendor's own documentation or launch figures
    INFERRED       derived by reasoning or from a role-agnostic proxy; the row names its basis
    UNKNOWN        could not be confirmed

Pure: no I/O.
"""
from __future__ import annotations

from enum import Enum


class Label(str, Enum):
    MEASURED_HERE = "MEASURED-HERE"
    MEASURED_3P = "MEASURED-3P"
    VENDOR = "VENDOR"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        """Lower is stronger."""
        return _ORDER.index(self)


_ORDER = (Label.MEASURED_HERE, Label.MEASURED_3P, Label.VENDOR, Label.INFERRED, Label.UNKNOWN)

# The spellings the research notes and vendor pages use, folded onto the five labels. Case-insensitive.
_NORMALISE = {
    "MEASURED-HERE": Label.MEASURED_HERE,
    "MEASURED-REPO": Label.MEASURED_HERE,
    "MEASURED-3P": Label.MEASURED_3P,
    "MEASURED": Label.MEASURED_3P,          # a named board
    "THIRD-PARTY": Label.MEASURED_3P,
    "VENDOR": Label.VENDOR,
    "VENDOR-DOC": Label.VENDOR,
    "VENDOR-REPORTED": Label.VENDOR,
    "INFERRED": Label.INFERRED,
    "[I]": Label.INFERRED,
    "UNKNOWN": Label.UNKNOWN,
    "UNCONFIRMED": Label.UNKNOWN,
    "NOT CONFIRMED": Label.UNKNOWN,
    "CANNOT CONFIRM": Label.UNKNOWN,
    "USER-REPORTED": Label.UNKNOWN,
    "[?]": Label.UNKNOWN,
}


def normalise(text: object) -> Label:
    """Fold a source spelling onto a label. An unrecognised spelling is UNKNOWN: never a guess upward."""
    if isinstance(text, Label):
        return text
    if not isinstance(text, str):
        return Label.UNKNOWN
    return _NORMALISE.get(text.strip().upper(), Label.UNKNOWN)


def is_known_spelling(text: object) -> bool:
    return isinstance(text, str) and text.strip().upper() in _NORMALISE


def weakest(*labels: object) -> Label:
    """The weakest of the given labels (UNKNOWN when none is given)."""
    found = [normalise(x) for x in labels]
    if not found:
        return Label.UNKNOWN
    return max(found, key=lambda lb: lb.rank)


# Labels a row may carry in the shipped database.
ROW_LABELS = tuple(lb.value for lb in Label)
MEASURED_LABELS = (Label.MEASURED_HERE.value, Label.MEASURED_3P.value)
