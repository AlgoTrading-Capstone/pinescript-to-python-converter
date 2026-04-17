"""
Integration smoke test: every module in src/strategies/ must be importable
without raising an exception.

This is the minimum bar for RL-readiness — a strategy that crashes at import
time produces no feature vectors and breaks the entire inference pipeline.
"""

import importlib
import pathlib

import pytest


def test_all_strategies_importable():
    strategies_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "src" / "strategies"
    modules = [
        f for f in strategies_dir.glob("*.py")
        if f.stem != "__init__"
    ]
    if not modules:
        pytest.skip("src/strategies/ is empty — no generated strategies to import yet")

    for module_file in modules:
        module_name = f"src.strategies.{module_file.stem}"
        importlib.import_module(module_name)