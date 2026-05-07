"""ai-resources kit — Python package."""
from __future__ import annotations

__version__ = "1.1.3"

import os
from pathlib import Path


def repo_root() -> Path:
    """Return the kit's repository root (one level above scripts/)."""
    env = os.environ.get("AGENT_KIT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]
