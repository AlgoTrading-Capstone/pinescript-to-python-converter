"""
Rich-powered terminal UI helpers for the pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

_THEME = Theme(
    {
        "info": "cyan",
        "success": "green",
        "warning": "yellow",
        "error": "bold red",
        "muted": "dim",
        "title": "bold bright_white",
        "accent": "bold cyan",
        "good": "green",
        "ok": "yellow",
        "complex": "magenta",
        "skip": "red",
        "path": "cyan",
    }
)

console = Console(theme=_THEME, safe_box=True, legacy_windows=False)

_VERDICT_STYLES = {
    "[RECOMMENDED]": "good",
    "[GOOD]": "good",
    "[OK]": "ok",
    "[COMPLEX]": "complex",
    "[SKIP]": "skip",
}


def verdict_text(label: str) -> Text:
    return Text(label, style=_VERDICT_STYLES.get(label, "muted"))


def print_banner(title: str) -> None:
    console.print()
    console.print(
        Panel.fit(
            Text(title, style="title"),
            border_style="accent",
            padding=(0, 2),
        )
    )


def print_section(title: str) -> None:
    console.print()
    console.print(Rule(Text(title, style="accent"), style="accent"))


def print_info(message: str) -> None:
    console.print(f"[info]{message}[/info]")


def print_success(message: str) -> None:
    console.print(f"[success]{message}[/success]")


def print_warning(message: str) -> None:
    console.print(f"[warning]{message}[/warning]")


def print_error(message: str) -> None:
    console.print(f"[error]{message}[/error]")


def print_kv(label: str, value: str | Path, value_style: str = "default") -> None:
    console.print(f"[muted]{label:<14}[/muted] [{value_style}]{value}[/{value_style}]")


def build_table(
    title: str,
    columns: Sequence[tuple[str, str]],
    rows: Iterable[Sequence[object]],
) -> Table:
    table = Table(
        title=title,
        box=box.SIMPLE_HEAVY,
        header_style="bold",
        show_lines=False,
        expand=True,
    )
    for column, justify in columns:
        table.add_column(column, justify=justify)
    for row in rows:
        table.add_row(*[cell if isinstance(cell, Text) else str(cell) for cell in row])
    return table


def print_table(table: Table) -> None:
    console.print(table)


def print_artifact_summary(title: str, rows: Iterable[tuple[str, object]]) -> None:
    table = Table(
        title=title,
        box=box.SIMPLE_HEAVY,
        header_style="bold",
        show_lines=False,
        expand=True,
    )
    table.add_column("Item", style="muted", no_wrap=True)
    table.add_column("Location / Value", style="path")
    for label, value in rows:
        if value is None or value == "":
            continue
        table.add_row(str(label), str(value))
    console.print()
    console.print(table)


def status_panel(title: str, body: str, style: str = "info") -> None:
    console.print(
        Panel(
            body,
            title=title,
            title_align="left",
            border_style=style,
            padding=(0, 1),
        )
    )


def truncate(text: str, limit: int = 90) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."
