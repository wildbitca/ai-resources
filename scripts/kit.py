#!/usr/bin/env python3
"""ai-resources kit — entry point shim.

Real implementation is in scripts/ai_resources/. This shim exists for
backwards compatibility with the Homebrew formula's bin wrapper, which
invokes scripts/kit.py directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable when run as a standalone script
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ai_resources.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
