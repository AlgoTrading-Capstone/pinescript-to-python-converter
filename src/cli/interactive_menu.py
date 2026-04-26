"""
Interactive CLI for plug-and-play conversion.

Two flows:
  * Manual — user drops a `.pine` into `input/manual/`, the menu identifies it,
    asks to start, then hands off to the existing manual conversion path.
  * Scrape — user delegates to the auto-scrape + evaluate + select flow.

This module owns presentation only. All business logic (manual ingestion,
conversion, gate, integration) stays in `src/pipeline/*` and `main.py`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from src.cli.ui import (
    build_table,
    console,
    print_banner,
    print_error,
    print_info,
    print_section,
    print_table,
    print_warning,
    status_panel,
)

Mode = Literal["manual", "scrape"]


def run_interactive_menu() -> Mode | None:
    """Top-level menu. Returns chosen mode, or None if the user quits."""
    print_banner("PineScript -> Python Converter")
    console.print()
    console.print("[accent]Choose a mode:[/accent]")
    console.print("  [success]1[/success]) Scrape bot — auto-fetch from TradingView, evaluate, pick the best")
    console.print("  [success]2[/success]) Manual     — convert a .pine you drop into [path]input/manual/[/path]")
    console.print("  [muted]q[/muted]) Quit")
    console.print()
    while True:
        try:
            choice = input("Mode [1/2/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return None
        if choice in ("1", "s", "scrape"):
            return "scrape"
        if choice in ("2", "m", "manual"):
            return "manual"
        if choice in ("q", "quit", "exit"):
            return None
        print_warning("Please enter 1, 2, or q.")


def _scan_manual_files(manual_dir: Path) -> list[Path]:
    if not manual_dir.exists():
        return []
    return sorted(
        (p for p in manual_dir.glob("*.pine") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.2f} MB"


def _format_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def pick_manual_file(manual_dir: Path) -> Path | None:
    """List `.pine` files in `manual_dir`, prompt the user to pick one.

    Behavior:
      * 0 files: print instructions, wait for Enter to rescan; 'q' aborts.
      * 1+ files: numbered table; user enters index, 'r' rescans, 'q' aborts.
    """
    print_section("Manual: pick a strategy")
    manual_dir = Path(manual_dir)
    manual_dir.mkdir(parents=True, exist_ok=True)

    while True:
        files = _scan_manual_files(manual_dir)

        if not files:
            status_panel(
                "No .pine files found",
                f"Drop your TradingView strategy file into:\n  [path]{manual_dir}[/path]\n\n"
                "Then press [success]Enter[/success] to rescan, or [muted]q[/muted] to quit.",
                style="warning",
            )
            try:
                ans = input("[Enter to rescan / q to quit]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return None
            if ans in ("q", "quit", "exit"):
                return None
            continue

        rows = [
            (
                str(i + 1),
                p.name,
                _format_size(p.stat().st_size),
                _format_mtime(p.stat().st_mtime),
            )
            for i, p in enumerate(files)
        ]
        table = build_table(
            f"Available .pine files in {manual_dir}",
            columns=[("#", "right"), ("File", "left"), ("Size", "right"), ("Modified", "right")],
            rows=rows,
        )
        print_table(table)
        console.print("[muted]Enter index to convert · 'r' to rescan · 'q' to quit[/muted]")
        try:
            ans = input("Pick: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return None
        if ans in ("q", "quit", "exit"):
            return None
        if ans in ("r", "rescan"):
            continue
        try:
            idx = int(ans)
        except ValueError:
            print_warning(f"'{ans}' is not a number. Enter an index, 'r', or 'q'.")
            continue
        if not (1 <= idx <= len(files)):
            print_warning(f"Out of range — pick 1..{len(files)}.")
            continue
        return files[idx - 1]


def confirm_run(metadata: dict, triage_reason: str) -> bool:
    """Show the identified strategy, ask whether to start the conversion."""
    print_section("Identified")
    name        = metadata.get("name", "?")
    safe_name   = metadata.get("safe_name", "?")
    timeframe   = metadata.get("timeframe", "?")
    lookback    = metadata.get("lookback_bars", "?")
    origin      = metadata.get("origin", "manual")

    body = (
        f"[muted]Name        :[/muted] [accent]{name}[/accent]\n"
        f"[muted]Safe name   :[/muted] [path]{safe_name}[/path]\n"
        f"[muted]Timeframe   :[/muted] {timeframe}\n"
        f"[muted]Lookback    :[/muted] {lookback} bars\n"
        f"[muted]Origin      :[/muted] {origin}\n"
        f"[muted]Triage      :[/muted] {triage_reason}"
    )
    status_panel("Strategy identified", body, style="info")

    console.print()
    while True:
        try:
            ans = input("Start conversion? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return False
        if ans in ("y", "yes"):
            print_info("Starting conversion...")
            return True
        if ans in ("", "n", "no"):
            return False
        print_warning("Please answer y or n.")
