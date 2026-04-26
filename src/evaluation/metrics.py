"""
Strategy Performance Metrics — pure, testable primitives.

Inputs are signal/price series (or pre-computed per-trade PnL); outputs are
scalar floats. No I/O, no plotting, no state. The statistical gate consumes
these to drive its 7-dimension lane assignment (Profit Factor, Win Rate,
Max Drawdown, Trade Count, Sharpe, Sortino, Expectancy).

Sharpe and Sortino are annualized from bar-level returns. With a 15m
timeframe and 24/7 markets, that's 365 * 24 * 4 = 35040 bars per year. The
position used for bar t is the position resolved at bar t-1, so every
return is strictly causal — no lookahead.

Public API
----------
compute_bar_returns(closes, signals) -> pd.Series
compute_equity_curve(bar_returns) -> pd.Series
compute_max_drawdown(equity) -> float
compute_sharpe(bar_returns, bars_per_year=BARS_PER_YEAR_15M) -> float
compute_sortino(bar_returns, bars_per_year=BARS_PER_YEAR_15M) -> float
compute_profit_factor(trade_pnl) -> float
compute_expectancy(trade_pnl) -> float
"""

from __future__ import annotations

import math

import pandas as pd

from src.evaluation.winrate import resolve_effective_positions


BARS_PER_YEAR_15M = 365 * 24 * 4  # 35040


_POSITION_MAP = {"LONG": 1, "SHORT": -1, "FLAT": 0}


def compute_bar_returns(closes: pd.Series, signals: pd.Series) -> pd.Series:
    """Per-bar strategy return for the position held entering each bar.

    `signals` is forward-filled across HOLD via `resolve_effective_positions`,
    then mapped to {+1, -1, 0}. The position is shifted by one bar before
    being multiplied with the close-to-close pct-change so bar t's return
    reflects the position decided on bar t-1 (strictly causal — no lookahead).
    """
    if len(closes) != len(signals):
        raise ValueError(
            f"closes ({len(closes)}) and signals ({len(signals)}) length mismatch"
        )
    positions = (
        resolve_effective_positions(signals)
        .map(_POSITION_MAP)
        .fillna(0)
        .astype(int)
    )
    bar_pct = closes.pct_change().fillna(0.0)
    return (positions.shift(1).fillna(0).astype(int) * bar_pct).astype(float)


def compute_equity_curve(bar_returns: pd.Series) -> pd.Series:
    """Cumulative equity starting at 1.0."""
    if bar_returns.empty:
        return pd.Series(dtype=float)
    return (1.0 + bar_returns).cumprod()


def compute_max_drawdown(equity: pd.Series) -> float:
    """Max drawdown as a positive fraction (0.25 == a 25% drop from peak)."""
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    return float(-drawdown.min())


def compute_sharpe(
    bar_returns: pd.Series,
    bars_per_year: int = BARS_PER_YEAR_15M,
) -> float:
    """Annualized Sharpe (zero risk-free rate) from bar-level returns."""
    if len(bar_returns) < 2:
        return 0.0
    std = float(bar_returns.std(ddof=1))
    if std == 0.0:
        return 0.0
    return float(bar_returns.mean() / std * math.sqrt(bars_per_year))


def compute_sortino(
    bar_returns: pd.Series,
    bars_per_year: int = BARS_PER_YEAR_15M,
) -> float:
    """Annualized Sortino (downside-only deviation, zero MAR)."""
    if len(bar_returns) < 2:
        return 0.0
    downside = bar_returns[bar_returns < 0]
    if len(downside) < 2:
        return 0.0
    downside_std = float(downside.std(ddof=1))
    if downside_std == 0.0:
        return 0.0
    return float(bar_returns.mean() / downside_std * math.sqrt(bars_per_year))


def compute_profit_factor(trade_pnl: pd.Series) -> float:
    """Sum of winning trade PnL divided by the absolute sum of losing PnL.

    Returns 0.0 when there are no trades, +inf when every trade wins.
    """
    if len(trade_pnl) == 0:
        return 0.0
    wins = float(trade_pnl[trade_pnl > 0].sum())
    losses = float(-trade_pnl[trade_pnl < 0].sum())
    if losses <= 0.0:
        return float("inf") if wins > 0.0 else 0.0
    return wins / losses


def compute_expectancy(trade_pnl: pd.Series) -> float:
    """Mean per-trade PnL (return per trade)."""
    if len(trade_pnl) == 0:
        return 0.0
    return float(trade_pnl.mean())