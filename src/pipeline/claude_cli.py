"""
Helpers for validating Claude CLI availability.
"""

from __future__ import annotations

from pathlib import Path
from shutil import which


def get_claude_cli_path() -> Path | None:
    resolved = which("claude")
    return Path(resolved) if resolved else None


def has_claude_cli() -> bool:
    return get_claude_cli_path() is not None
