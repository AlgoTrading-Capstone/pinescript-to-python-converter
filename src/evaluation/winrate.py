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

create_winrate_barchart(all_stats: dict[str, dict], out_path: Path, title: str = "")
    -> Path | None
    Ported from rl-training visualization/plot_winrates.py for PR-body evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd


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

    # Run-length encoding: group consecutive identical signals
    run_id = (signals != signals.shift()).cumsum()

    runs = pd.DataFrame(
        {
            "signal": signals.groupby(run_id).first(),
            "start_iloc": (
                signals.groupby(run_id)
                .apply(lambda g: g.index[0])
                .map(signals.index.get_loc)
            ),
            "end_iloc": (
                signals.groupby(run_id)
                .apply(lambda g: g.index[-1])
                .map(signals.index.get_loc)
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


def create_winrate_barchart(
    all_stats: Dict[str, dict],
    out_path: Path,
    title: str = "",
) -> Path | None:
    """Render a horizontal bar chart of win rates. Used for PR-body evidence."""
    try:
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mtick
    except ImportError:
        return None

    if not all_stats:
        return None

    sorted_items = sorted(all_stats.items(), key=lambda x: x[1]["win_rate"], reverse=True)
    labels = [item[0] for item in sorted_items]
    rates = [item[1]["win_rate"] * 100 for item in sorted_items]
    counts = [item[1]["total_trades"] for item in sorted_items]
    avg_pnls = [item[1]["avg_pnl"] * 100 for item in sorted_items]

    bar_colors = ["#2ecc71" if r > 50 else "#e74c3c" for r in rates]
    fig_height = max(4, len(labels) * 0.8 + 1.5)
    fig, ax = plt.subplots(figsize=(10, fig_height))
    bars = ax.barh(labels, rates, color=bar_colors, edgecolor="white", height=0.6)
    ax.axvline(x=50, color="#555555", linestyle="--", linewidth=1.2, alpha=0.7, label="50% break-even")

    for bar, rate, count, apnl in zip(bars, rates, counts, avg_pnls):
        ax.text(
            bar.get_width() + 0.5,
            bar.get_y() + bar.get_height() / 2,
            f"{rate:.1f}%  ({count:,} trades, avg {apnl:+.2f}%)",
            va="center", ha="left", fontsize=9, color="#222222",
        )

    ax.set_xlabel("Win Rate (%)", fontsize=11)
    ax.set_xlim(0, max(rates + [50]) + 25)
    ax.xaxis.set_major_formatter(mtick.PercentFormatter())
    ax.invert_yaxis()

    full_title = "Strategy Win Rate"
    if title:
        full_title += f"\n{title}"
    ax.set_title(full_title, fontsize=13, fontweight="bold", pad=12)

    ax.legend(loc="lower right", fontsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path