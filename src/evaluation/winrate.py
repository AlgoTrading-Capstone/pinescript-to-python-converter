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


def render_winrate_curve(
    trades: pd.DataFrame,
    output_path: Path,
    *,
    rolling_window: int | None = None,
    title: str = "",
) -> Path | None:
    """Two-panel chart: cumulative return (with drawdown shading) + rolling win rate.

    Args:
        trades:         DataFrame from `compute_trades` (must have `exit_time`,
                        `pnl`, `win` columns).
        output_path:    Destination PNG.
        rolling_window: Trades per rolling-winrate bucket. When None (default),
                        sized dynamically as `max(100, len(trades) // 20)` so
                        high-frequency strategies show macro trend instead of
                        seismograph noise. Clamped down to
                        `max(5, len(trades) // 4)` if the dataset is too small.
        title:          Optional title suffix (strategy name + verdict).

    Returns the output path if rendered, else None when there are no trades.
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        return None

    if trades is None or trades.empty:
        return None

    df = trades.sort_values("exit_time").reset_index(drop=True)
    df["cumulative_return"] = (1.0 + df["pnl"]).cumprod() - 1.0

    n = len(df)
    if rolling_window is None:
        window = max(100, n // 20)
    else:
        window = rolling_window
    if n < window:
        window = max(5, n // 4 or 1)

    df["rolling_winrate"] = (
        df["win"].astype(float).rolling(window=window, min_periods=window).mean()
    )
    overall_wr = df["win"].mean()

    equity_pct = df["cumulative_return"] * 100.0
    final_positive = equity_pct.iloc[-1] >= 0
    color_equity = "#1e8449" if final_positive else "#c0392b"

    fig, (ax_eq, ax_wr) = plt.subplots(
        2, 1, figsize=(12, 6), sharex=True,
        gridspec_kw={"height_ratios": [2, 1]},
    )

    ax_eq.fill_between(
        df["exit_time"], equity_pct, 0.0,
        where=(equity_pct >= 0.0), interpolate=True,
        color="#2ecc71", alpha=0.18, linewidth=0,
        label="In profit",
    )
    ax_eq.fill_between(
        df["exit_time"], equity_pct, 0.0,
        where=(equity_pct < 0.0), interpolate=True,
        color="#e74c3c", alpha=0.22, linewidth=0,
        label="Drawdown",
    )
    ax_eq.plot(df["exit_time"], equity_pct,
               color=color_equity, linewidth=1.4, alpha=0.95)
    ax_eq.axhline(0.0, color="#555555", linewidth=0.9, linestyle="--", alpha=0.7)
    ax_eq.set_ylabel("Cumulative return (%)", fontsize=10)
    eq_title = "Equity curve & rolling win rate"
    if title:
        eq_title += f"  —  {title}"
    ax_eq.set_title(eq_title, fontsize=12, fontweight="bold")
    ax_eq.grid(True, alpha=0.25)
    ax_eq.legend(loc="upper left", fontsize=8, framealpha=0.85)

    ax_wr.plot(df["exit_time"], df["rolling_winrate"] * 100.0,
               color="#2c3e50", linewidth=1.4, alpha=0.9,
               label=f"Rolling win rate ({window} trades)")
    ax_wr.axhline(50.0, color="#555555", linewidth=0.9, linestyle="--", alpha=0.7,
                  label="50% break-even")
    ax_wr.axhline(overall_wr * 100.0, color="#e67e22", linewidth=1.0,
                  linestyle=":", alpha=0.9,
                  label=f"Overall {overall_wr:.1%}")
    ax_wr.set_ylabel("Win rate (%)", fontsize=10)
    wr_series_pct = (df["rolling_winrate"].dropna() * 100.0)
    if not wr_series_pct.empty:
        lo = max(0.0, min(wr_series_pct.min(), 50.0) - 5.0)
        hi = min(100.0, max(wr_series_pct.max(), 50.0) + 5.0)
    else:
        lo, hi = 30.0, 70.0
    ax_wr.set_ylim(lo, hi)
    ax_wr.grid(True, alpha=0.25)
    ax_wr.legend(loc="lower right", fontsize=8, framealpha=0.85)

    if pd.api.types.is_datetime64_any_dtype(df["exit_time"]):
        ax_wr.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax_wr.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        fig.autofmt_xdate()

    plt.tight_layout()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_path), dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


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
