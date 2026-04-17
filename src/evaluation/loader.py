"""
Strategy Loader — Dynamic Import by safe_name

Given a safe_name (e.g. "kama_trend_strategy"), import the module at
`src.strategies.{safe_name}_strategy` (or `src.strategies.{safe_name}` if the
name already ends in `_strategy`), walk its attributes, and return an
instantiated subclass of BaseStrategy.

Strategy subclasses self-construct with no constructor arguments — they pass
their own name/description/timeframe/lookback_hours to `super().__init__` — so
the loader can instantiate without any external config.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Type

from src.base_strategy import BaseStrategy


class StrategyLoadError(RuntimeError):
    """Raised when a safe_name cannot be resolved to a BaseStrategy subclass."""


def _module_path(safe_name: str) -> str:
    stem = safe_name if safe_name.endswith("_strategy") else f"{safe_name}_strategy"
    return f"src.strategies.{stem}"


def _find_strategy_class(module) -> Type[BaseStrategy]:
    candidates = [
        obj for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, BaseStrategy)
        and obj is not BaseStrategy
        and obj.__module__ == module.__name__
    ]
    if not candidates:
        raise StrategyLoadError(
            f"No BaseStrategy subclass found in module '{module.__name__}'"
        )
    if len(candidates) > 1:
        names = ", ".join(c.__name__ for c in candidates)
        raise StrategyLoadError(
            f"Multiple BaseStrategy subclasses in '{module.__name__}': {names}. "
            f"Expected exactly one per file."
        )
    return candidates[0]


def load_strategy_by_safe_name(safe_name: str) -> BaseStrategy:
    """
    Dynamically import and instantiate a strategy by its safe_name.

    Raises StrategyLoadError if the module is missing, contains zero or multiple
    BaseStrategy subclasses, or the class cannot be instantiated without args.
    """
    module_path = _module_path(safe_name)
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise StrategyLoadError(
            f"Cannot import '{module_path}': {e}"
        ) from e

    strategy_cls = _find_strategy_class(module)
    try:
        return strategy_cls()
    except TypeError as e:
        raise StrategyLoadError(
            f"'{strategy_cls.__name__}' cannot be instantiated with no args: {e}. "
            f"Strategy classes must self-construct (hardcode name/description/"
            f"timeframe/lookback_hours in their __init__)."
        ) from e