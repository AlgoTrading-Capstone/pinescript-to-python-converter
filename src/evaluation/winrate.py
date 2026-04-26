"""
Win-Rate Backtest for a Single Strategy Signal Series

Uses pandas run-length encoding to detect trades:
  - Entry price = close at the first bar of a LONG/SHORT run
  - Exit price  = close at the bar after the run ends (or last bar if still open)
  - PnL (LONG)  = (exit - entry) / entry
  - PnL (SHORT) = (entry - exit) / entry
  - Win         = PnL > 0

Public API
----------
compute_winrate(closes: pd.Series, signals: pd.Series) -> dict
    Returns {win_rate, total_trades, avg_pnl, trades}

passes_winrate(stats: dict, min_win_rate: float = 0.5, min_trades: int = 30) -> bool

compute_trades(closes: pd.Series, signals: pd.Series) -> pd.DataFrame
    Detects LONG/SHORT trades with timestamps, prices, PnL, and win flags.

Rendering helpers (`render_winrate_curve`, `create_winrate_barchart`) live in
`src.evaluation.plots.winrate_curve`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def resolve_effective_positions(signals: pd.Series) -> pd.Series:
    """Convert signal recommendations into evaluated target exposure.

    ``HOLD`` means "keep current exposure" in ``BaseStrategy``.  The statistical
    gate therefore needs to carry the previous LONG/SHORT/FLAT state forward
    before detecting trade runs.  Leading HOLD values have no prior exposure and
    resolve to FLAT.
    """
    return signals.replace("HOLD", pd.NA).ffill().fillna("FLAT")


def compute_winrate(closes: pd.Series, signals: pd.Series) -> dict:
    """
    Compute win rate + trade stats for a single strategy signal series.

    Args:
        closes: pd.Series of close prices (numeric).
        signals: pd.Series of string signals, aligned to closes.

    Returns:
        {
            "win_rate":     float in [0, 1],
            "total_trades": int,
            "avg_pnl":      float,
            "trades":       list[float],  # per-trade PnL
        }
    """
    if len(closes) != len(signals):
        raise ValueError(
            f"closes ({len(closes)}) and signals ({len(signals)}) length mismatch"
        )

    closes_arr = closes.to_numpy(dtype=float)

    effective_signals = resolve_effective_positions(signals)

    # Run-length encoding: group consecutive identical target positions
    run_id = (effective_signals != effective_signals.shift()).cumsum()

    runs = pd.DataFrame(
        {
            "signal": effective_signals.groupby(run_id).first(),
            "start_iloc": (
                effective_signals.groupby(run_id)
                .apply(lambda g: g.index[0])
                .map(effective_signals.index.get_loc)
            ),
            "end_iloc": (
                effective_signals.groupby(run_id)
                .apply(lambda g: g.index[-1])
                .map(effective_signals.index.get_loc)
            ),
        }
    ).reset_index(drop=True)

    active_runs = runs[runs["signal"].isin(["LONG", "SHORT"])]

    pnls: list[float] = []
    for _, row in active_runs.iterrows():
        entry_iloc = int(row["start_iloc"])
        end_iloc = int(row["end_iloc"])
        exit_iloc = end_iloc + 1 if end_iloc + 1 < len(closes_arr) else end_iloc

        entry_price = closes_arr[entry_iloc]
        exit_price = closes_arr[exit_iloc]
        if entry_price <= 0:
            continue

        if row["signal"] == "LONG":
            pnl = (exit_price - entry_price) / entry_price
        else:
            pnl = (entry_price - exit_price) / entry_price
        pnls.append(pnl)

    total = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "win_rate": wins / total if total > 0 else 0.0,
        "total_trades": total,
        "avg_pnl": float(np.mean(pnls)) if pnls else 0.0,
        "trades": pnls,
    }


def passes_winrate(
    stats: dict,
    min_win_rate: float = 0.50,
    min_trades: int = 30,
) -> bool:
    """True when the strategy clears BOTH the win-rate and trade-count floors."""
    return stats["total_trades"] >= min_trades and stats["win_rate"] >= min_win_rate


def compute_trades(closes: pd.Series, signals: pd.Series) -> pd.DataFrame:
    """Detect LONG/SHORT trades with timestamps, prices, PnL, and win flags.

    Uses the same run-length detection as `compute_winrate` so the outputs
    agree. Exit price is the close of the bar AFTER the run ends (or the
    final bar if the run touches the tail).
    """
    if len(closes) != len(signals):
        raise ValueError(
            f"closes ({len(closes)}) and signals ({len(signals)}) length mismatch"
        )

    closes_arr = closes.to_numpy(dtype=float)
    index = signals.index

    effective_signals = resolve_effective_positions(signals)
    run_id = (effective_signals != effective_signals.shift()).cumsum()
    runs = pd.DataFrame({
        "signal": effective_signals.groupby(run_id).first(),
        "start_iloc": (
            effective_signals.groupby(run_id)
            .apply(lambda g: g.index[0])
            .map(effective_signals.index.get_loc)
        ),
        "end_iloc": (
            effective_signals.groupby(run_id)
            .apply(lambda g: g.index[-1])
            .map(effective_signals.index.get_loc)
        ),
    }).reset_index(drop=True)
    active_runs = runs[runs["signal"].isin(["LONG", "SHORT"])]

    rows = []
    for _, row in active_runs.iterrows():
        entry_iloc = int(row["start_iloc"])
        end_iloc = int(row["end_iloc"])
        exit_iloc = end_iloc + 1 if end_iloc + 1 < len(closes_arr) else end_iloc

        entry_price = closes_arr[entry_iloc]
        exit_price = closes_arr[exit_iloc]
        if entry_price <= 0:
            continue

        if row["signal"] == "LONG":
            pnl = (exit_price - entry_price) / entry_price
        else:
            pnl = (entry_price - exit_price) / entry_price

        rows.append({
            "entry_time": index[entry_iloc],
            "exit_time": index[exit_iloc],
            "side": row["signal"],
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "pnl": float(pnl),
            "win": bool(pnl > 0),
        })

    return pd.DataFrame(rows, columns=[
        "entry_time", "exit_time", "side",
        "entry_price", "exit_price", "pnl", "win",
    ])
