"""One secret scrubber for text that leaves a tool call: acceptance reasons, `agy run` errors and stderr."""
from __future__ import annotations

import re
from typing import Any

SECRET = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|ya29\.[\w-]+|1//[\w-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|Bearer\s+\S+|eyJ[\w-]{20,}\.[\w.-]*)")


def scrub(text: Any) -> str:
    return SECRET.sub("***", str(text))
