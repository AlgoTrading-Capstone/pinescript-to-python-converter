"""
Variance Filter for Strategy Signals

Determines whether a strategy's signal series is statistically alive — i.e.,
whether LONG + SHORT signals cover at least `min_active_pct` of all bars.

This is the first statistical gate a converted strategy must pass before it is
promoted to the RL-training project. Strategies that fail here are "dead on BTC"
regardless of conversion correctness.

Public API
----------
signal_activity_pct(signals: pd.Series) -> float
passes_variance(signals: pd.Series, min_active_pct: float = 0.05) -> bool
evaluate_strategies(signals_by_name: dict[str, pd.Series], min_active_pct: float = 0.05)
    -> dict[str, dict]  (activity_pct, passed per strategy)
"""

from __future__ import annotations

from typing import Dict

import pandas as pd


ACTIVE_SIGNALS = frozenset({"LONG", "SHORT"})


def signal_activity_pct(signals: pd.Series) -> float:
    """Fraction of bars where the signal is LONG or SHORT."""
    if len(signals) == 0:
        return 0.0
    return float(signals.isin(ACTIVE_SIGNALS).mean())


def passes_variance(signals: pd.Series, min_active_pct: float = 0.05) -> bool:
    """True when at least min_active_pct of bars are LONG or SHORT."""
    return signal_activity_pct(signals) >= min_active_pct


def evaluate_strategies(
    signals_by_name: Dict[str, pd.Series],
    min_active_pct: float = 0.05,
) -> Dict[str, dict]:
    """
    Evaluate many strategies in one call.

    Returns a verdict dict per strategy:
        {name: {"activity_pct": float, "passed": bool}}
    """
    verdicts: Dict[str, dict] = {}
    for name, signals in signals_by_name.items():
        pct = signal_activity_pct(signals)
        verdicts[name] = {
            "activity_pct": pct,
            "passed": pct >= min_active_pct,
        }
    return verdicts