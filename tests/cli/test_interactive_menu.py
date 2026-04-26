"""Tests for the interactive CLI menu helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.cli import interactive_menu


def _drop_pine(directory: Path, name: str, content: str = "// minimal pine") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


# -----------------------------------------------------------------------------
# run_interactive_menu
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("answer,expected", [
    ("1", "scrape"),
    ("scrape", "scrape"),
    ("S", "scrape"),
    ("2", "manual"),
    ("manual", "manual"),
    ("M", "manual"),
])
def test_menu_returns_chosen_mode(monkeypatch, answer, expected):
    monkeypatch.setattr("builtins.input", lambda _prompt="": answer)
    assert interactive_menu.run_interactive_menu() == expected


def test_menu_quits_returns_none(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "q")
    assert interactive_menu.run_interactive_menu() is None


def test_menu_eof_returns_none(monkeypatch):
    def _eof(_prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert interactive_menu.run_interactive_menu() is None


def test_menu_retries_on_invalid_input(monkeypatch):
    answers = iter(["zzz", "5", "manual"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert interactive_menu.run_interactive_menu() == "manual"


# -----------------------------------------------------------------------------
# pick_manual_file
# -----------------------------------------------------------------------------
def test_pick_manual_file_zero_then_quit(monkeypatch, tmp_path):
    # Empty manual dir: first prompt should be "press Enter to rescan, q to quit".
    monkeypatch.setattr("builtins.input", lambda _prompt="": "q")
    assert interactive_menu.pick_manual_file(tmp_path / "manual") is None


def test_pick_manual_file_zero_then_drop_then_pick(monkeypatch, tmp_path):
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()

    answers_iter = iter(["", "1"])  # press Enter to rescan, then pick #1

    def _input(_prompt=""):
        ans = next(answers_iter)
        if ans == "":
            # Simulate the user dropping a file before pressing Enter again.
            _drop_pine(manual_dir, "alpha.pine")
        return ans

    monkeypatch.setattr("builtins.input", _input)
    picked = interactive_menu.pick_manual_file(manual_dir)
    assert picked is not None
    assert picked.name == "alpha.pine"


def test_pick_manual_file_picks_index(monkeypatch, tmp_path):
    manual_dir = tmp_path / "manual"
    _drop_pine(manual_dir, "alpha.pine")
    _drop_pine(manual_dir, "bravo.pine")
    _drop_pine(manual_dir, "charlie.pine")

    monkeypatch.setattr("builtins.input", lambda _prompt="": "2")
    picked = interactive_menu.pick_manual_file(manual_dir)
    assert picked is not None
    assert picked.suffix == ".pine"
    assert picked.parent == manual_dir


def test_pick_manual_file_quit_with_files(monkeypatch, tmp_path):
    manual_dir = tmp_path / "manual"
    _drop_pine(manual_dir, "alpha.pine")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "q")
    assert interactive_menu.pick_manual_file(manual_dir) is None


def test_pick_manual_file_invalid_index_then_valid(monkeypatch, tmp_path):
    manual_dir = tmp_path / "manual"
    _drop_pine(manual_dir, "alpha.pine")
    _drop_pine(manual_dir, "bravo.pine")
    answers = iter(["abc", "9", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    picked = interactive_menu.pick_manual_file(manual_dir)
    assert picked is not None
    assert picked.parent == manual_dir


# -----------------------------------------------------------------------------
# confirm_run
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("answer,expected", [
    ("y", True),
    ("yes", True),
    ("Y", True),
    ("n", False),
    ("no", False),
    ("", False),
])
def test_confirm_run(monkeypatch, answer, expected):
    monkeypatch.setattr("builtins.input", lambda _prompt="": answer)
    metadata = {
        "name": "Demo",
        "safe_name": "demo",
        "timeframe": "15m",
        "lookback_bars": 100,
        "origin": "manual",
    }
    assert interactive_menu.confirm_run(metadata, "looks ok") is expected


def test_confirm_run_eof_returns_false(monkeypatch):
    def _eof(_prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert interactive_menu.confirm_run({}, "ok") is False


def test_confirm_run_retries_on_invalid(monkeypatch):
    answers = iter(["maybe", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert interactive_menu.confirm_run({}, "ok") is True
