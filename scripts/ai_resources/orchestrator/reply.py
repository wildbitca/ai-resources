"""Read the return block a role ends its reply with (skill `kit-orchestration`, "Running a step") into a small record.

Pure text in, plain data out. Lines look like `Blocked: true`, `**Return_to_step:** implement`, `> Verdict: PASS`: markdown
decoration before the key and around the colon is tolerated; the key is case-insensitive and `_`, `-` and space are the
same. The last occurrence of a key wins. Unknown keys are ignored. A reply is data from another model: only the fields
below are read, each is cut to a short length, and nothing in it is ever executed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_REPLY = 200_000
MAX_VALUE = 400

KEYS = ("blocked", "return_to_step", "block_reason", "verdict", "status", "plan_ref", "research_ref", "research_needed",
        "architect_approval", "architecture_design_ref", "test_report", "code_review_verdict", "security_audit",
        "requires_tests", "security_critical_feature", "next", "changes_made")

# What a verdict word means to the engine.
BLOCKING_VERDICTS = frozenset({"FAIL", "FAILED", "REQUIRES_CHANGES", "REQUEST_CHANGES", "CHANGES_REQUESTED", "BLOCK",
                               "BLOCKED", "GAPS", "REJECTED", "NO"})
PASSING_VERDICTS = frozenset({"PASS", "PASSED", "APPROVE", "APPROVED", "OK", "SUCCESS", "YES", "CONDITIONAL", "N/A"})
# Only the verifier may sign a run off: these words from any other role are rejected.
SIGNOFF_WORDS = frozenset({"READY", "SIGNED_OFF", "SIGNOFF", "VERIFIED", "ACCEPTED"})

_LINE = re.compile(r"^[ \t>*_`#-]*\**[ \t]*([A-Za-z][A-Za-z _-]{1,40}?)[ \t]*\**[ \t]*:[ \t]*\**[ \t]*(.*?)[ \t]*\**[ \t]*$",
                   re.M)


@dataclass
class Reply:
    fields: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str:
        return self.fields.get(key, "")

    @property
    def blocked(self) -> bool:
        return self.get("blocked").lower() in ("true", "yes", "1")

    @property
    def return_to(self) -> str:
        return self.get("return_to_step")

    @property
    def reason(self) -> str:
        return self.get("block_reason")

    @property
    def verdict_word(self) -> str:
        """The first verdict-like word of the reply: Verdict, Code_review_verdict, Security_audit, Architect_approval."""
        for key in ("verdict", "code_review_verdict", "security_audit", "architect_approval"):
            word = _word(self.get(key))
            if word:
                return word
        return ""

    def words(self) -> set[str]:
        return {w for k in ("verdict", "status", "code_review_verdict", "security_audit", "architect_approval",
                            "test_report") if (w := _word(self.get(k)))}

    @property
    def blocking_verdict(self) -> bool:
        return bool(self.words() & BLOCKING_VERDICTS)

    @property
    def signoff(self) -> bool:
        return bool(self.words() & SIGNOFF_WORDS)


_PHRASES = ("REQUEST_CHANGES", "REQUESTS_CHANGES", "REQUIRES_CHANGES", "CHANGES_REQUESTED", "CHANGES_REQUIRED")


def _word(value: str) -> str:
    """The verdict word of a value; a multi-word phrase (`request changes`) is normalised whole, never cut at a space."""
    text = value.strip().strip("*_`").strip()
    norm = re.sub(r"[ \t_-]+", "_", text.upper())
    for phrase in _PHRASES:
        if norm == phrase or norm.startswith(phrase + "_") or norm.startswith(phrase + ":"):
            return "REQUIRES_CHANGES" if phrase != "CHANGES_REQUESTED" else phrase
    m = re.match(r"[A-Za-z][A-Za-z_/]*", text)
    return m.group(0).upper().replace("/", "_") if m else ""


def normalise_key(raw: str) -> str:
    return re.sub(r"[ -]+", "_", raw.strip().lower())


def parse(text: str) -> Reply:
    """The fields of a reply. Never raises; an empty or hostile text yields no fields."""
    out: dict[str, str] = {}
    if not isinstance(text, str):
        return Reply(out)
    for m in _LINE.finditer(text[-MAX_REPLY:]):
        key = normalise_key(m.group(1))
        if key in KEYS:
            out[key] = " ".join(m.group(2).split())[:MAX_VALUE]
    return Reply(out)
