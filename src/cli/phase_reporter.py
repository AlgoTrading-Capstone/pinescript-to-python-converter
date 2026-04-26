"""
Pipeline phase progress reporter.

Single-line summary printed at the end of each phase, e.g.

    [PASS] Phase 4b · Gate · variance=7.1% win_rate=52% trades=41 lane=strict

Style is driven by the Rich theme defined in `src.cli.ui`.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

from src.cli.ui import console

Status = Literal["ok", "warn", "fail"]

_STATUS_BADGE = {
    "ok":   ("PASS", "success"),
    "warn": ("WARN", "warning"),
    "fail": ("FAIL", "error"),
}


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if abs(value) < 1.0 and value != 0.0:
            return f"{value:.3f}"
        return f"{value:.2f}"
    return str(value)


def print_phase_summary(
    phase: str,
    kvs: Mapping[str, Any] | None = None,
    status: Status = "ok",
) -> None:
    """Print a single styled line summarising a phase's outcome."""
    badge, badge_style = _STATUS_BADGE.get(status, ("INFO", "info"))
    pairs = " ".join(f"[muted]{k}=[/muted]{_format_value(v)}" for k, v in (kvs or {}).items())
    line = f"[{badge_style}][{badge}][/{badge_style}] [accent]{phase}[/accent]"
    if pairs:
        line += f"  {pairs}"
    console.print(line)
