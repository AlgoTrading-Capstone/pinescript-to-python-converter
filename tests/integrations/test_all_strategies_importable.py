"""
Integration smoke test: every module in src/strategies/ must be importable
without raising an exception.

This is the minimum bar for RL-readiness — a strategy that crashes at import
time produces no feature vectors and breaks the entire inference pipeline.
"""

import importlib
import pathlib


def test_all_strategies_importable():
    strategies_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "src" / "strategies"
    modules = [
        f for f in strategies_dir.glob("*.py")
        if f.stem != "__init__"
    ]
    assert modules, "No strategy modules found in src/strategies/"

    for module_file in modules:
        module_name = f"src.strategies.{module_file.stem}"
        # Will raise ImportError / SyntaxError if the module is broken
        importlib.import_module(module_name)