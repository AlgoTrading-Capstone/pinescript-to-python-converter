"""
Unified statistical-gate summary plot.

Single 2×2 figure that summarises everything the gate produced:

    ┌──────────────────────┬──────────────────────┐
    │ Price + signals      │ Equity curve + DD    │
    ├──────────────────────┼──────────────────────┤
    │ Rolling win rate     │ Metrics text table   │
    └──────────────────────┴──────────────────────┘

Plus a colored verdict banner above the figure (PASS / FAIL + reason).

This module owns rendering only. All numeric inputs are computed elsewhere
(`src/evaluation/metrics.py`, `src/evaluation/winrate.py`, etc.) and passed
in as plain pandas/dict objects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

import pandas as pd

# Down-sampling cap for the price panel — keeps the figure crisp even on
# multi-year 15m datasets (~140k bars).
_MAX_PRICE_POINTS = 4000
# Maximum LONG / SHORT markers before we just skip the markers altogether
# (otherwise a high-frequency strategy paints the panel solid green/red).
_MAX_SIGNAL_MARKERS = 1500


def _downsample(series: pd.Series, max_points: int) -> pd.Series:
    if len(series) <= max_points:
        return series
    step = max(1, len(series) // max_points)
    return series.iloc[::step]


def render_gate_summary(
    *,
    strategy_name: str,
    closes: pd.Series,
    signals: pd.Series,
    trades: pd.DataFrame,
    equity: pd.Series,
    metrics: Mapping[str, Any],
    variance: Mapping[str, Any],
    eval_window: Mapping[str, Any],
    lane: Optional[str],
    passed: bool,
    reason: Optional[str],
    output_path: Path,
) -> Path | None:
    """Render a one-image summary of the gate run. Returns the output path."""
    try:
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib.gridspec import GridSpec
    except ImportError:
        return None

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    verdict = "PASS" if passed else "FAIL"
    verdict_color = "#1e8449" if passed else "#c0392b"

    fig = plt.figure(figsize=(15.5, 9.0))
    gs = GridSpec(
        2, 2, figure=fig,
        height_ratios=[1.0, 1.0],
        width_ratios=[1.4, 1.0],
        hspace=0.32, wspace=0.20,
    )
    ax_price   = fig.add_subplot(gs[0, 0])
    ax_equity  = fig.add_subplot(gs[0, 1])
    ax_winrate = fig.add_subplot(gs[1, 0])
    ax_table   = fig.add_subplot(gs[1, 1])

    # Banner (figure title)
    banner = f"{strategy_name}    —    Gate {verdict}"
    if lane and passed:
        banner += f"  ({lane})"
    if not passed and reason:
        banner += f"   ·   {reason}"
    fig.suptitle(banner, fontsize=14, fontweight="bold", color=verdict_color, y=0.995)

    # ---- Top-left : Price + signal markers --------------------------------
    closes_ds = _downsample(closes, _MAX_PRICE_POINTS)
    ax_price.plot(closes_ds.index, closes_ds.values,
                  color="#34495e", linewidth=0.9, alpha=0.9)
    long_idx  = signals[signals == "LONG"].index
    short_idx = signals[signals == "SHORT"].index
    if 0 < len(long_idx) <= _MAX_SIGNAL_MARKERS:
        ax_price.scatter(long_idx, closes.reindex(long_idx).values,
                         color="#27ae60", s=8, marker="^",
                         alpha=0.55, label=f"LONG ({len(long_idx)})", linewidths=0)
    if 0 < len(short_idx) <= _MAX_SIGNAL_MARKERS:
        ax_price.scatter(short_idx, closes.reindex(short_idx).values,
                         color="#c0392b", s=8, marker="v",
                         alpha=0.55, label=f"SHORT ({len(short_idx)})", linewidths=0)
    ax_price.set_title("Price & signals", fontsize=11, fontweight="bold")
    ax_price.set_ylabel("Close", fontsize=9)
    ax_price.grid(True, alpha=0.25)
    if len(long_idx) + len(short_idx) > 0:
        ax_price.legend(loc="upper left", fontsize=8, framealpha=0.85)
    if pd.api.types.is_datetime64_any_dtype(closes.index):
        ax_price.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax_price.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

    # ---- Top-right : Equity curve with drawdown shading -------------------
    if equity is not None and not equity.empty:
        eq_pct = (equity - 1.0) * 100.0
        running_max = eq_pct.cummax()
        ax_equity.fill_between(eq_pct.index, eq_pct.values, running_max.values,
                               where=(eq_pct.values < running_max.values),
                               color="#e74c3c", alpha=0.20, linewidth=0,
                               label="Drawdown")
        eq_color = "#1e8449" if eq_pct.iloc[-1] >= 0 else "#c0392b"
        ax_equity.plot(eq_pct.index, eq_pct.values,
                       color=eq_color, linewidth=1.3, alpha=0.95,
                       label="Equity")
        ax_equity.axhline(0.0, color="#555555", linewidth=0.8, linestyle="--", alpha=0.7)
        ax_equity.set_title("Equity & drawdown", fontsize=11, fontweight="bold")
        ax_equity.set_ylabel("Cumulative return (%)", fontsize=9)
        ax_equity.grid(True, alpha=0.25)
        ax_equity.legend(loc="upper left", fontsize=8, framealpha=0.85)
        if pd.api.types.is_datetime64_any_dtype(eq_pct.index):
            ax_equity.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax_equity.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    else:
        ax_equity.text(0.5, 0.5, "no equity data", transform=ax_equity.transAxes,
                       ha="center", va="center", color="#888888", fontsize=11)
        ax_equity.set_title("Equity & drawdown", fontsize=11, fontweight="bold")
        ax_equity.set_xticks([]); ax_equity.set_yticks([])

    # ---- Bottom-left : Rolling win rate -----------------------------------
    if trades is not None and not trades.empty:
        df = trades.sort_values("exit_time").reset_index(drop=True)
        n = len(df)
        window = max(20, n // 20)
        if n < window:
            window = max(5, n // 4 or 1)
        df["rolling_winrate"] = (
            df["win"].astype(float).rolling(window=window, min_periods=window).mean()
        )
        overall_wr = df["win"].mean()
        ax_winrate.plot(df["exit_time"], df["rolling_winrate"] * 100.0,
                        color="#2c3e50", linewidth=1.3, alpha=0.9,
                        label=f"Rolling ({window} trades)")
        ax_winrate.axhline(50.0, color="#555555", linewidth=0.8, linestyle="--",
                           alpha=0.7, label="50% break-even")
        ax_winrate.axhline(overall_wr * 100.0, color="#e67e22", linewidth=1.0,
                           linestyle=":", alpha=0.9,
                           label=f"Overall {overall_wr:.1%}")
        ax_winrate.set_title("Rolling win rate", fontsize=11, fontweight="bold")
        ax_winrate.set_ylabel("Win rate (%)", fontsize=9)
        ax_winrate.grid(True, alpha=0.25)
        ax_winrate.legend(loc="lower right", fontsize=8, framealpha=0.85)
        wr_pct = (df["rolling_winrate"].dropna() * 100.0)
        if not wr_pct.empty:
            lo = max(0.0, min(wr_pct.min(), 50.0) - 5.0)
            hi = min(100.0, max(wr_pct.max(), 50.0) + 5.0)
            ax_winrate.set_ylim(lo, hi)
        if pd.api.types.is_datetime64_any_dtype(df["exit_time"]):
            ax_winrate.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax_winrate.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    else:
        ax_winrate.text(0.5, 0.5, "no trades detected",
                        transform=ax_winrate.transAxes,
                        ha="center", va="center", color="#888888", fontsize=11)
        ax_winrate.set_title("Rolling win rate", fontsize=11, fontweight="bold")
        ax_winrate.set_xticks([]); ax_winrate.set_yticks([])

    # ---- Bottom-right : Metrics text table --------------------------------
    ax_table.axis("off")
    activity_pct = float(variance.get("signal_activity_pct", 0.0)) * 100.0
    activity_thr = float(variance.get("threshold", 0.0)) * 100.0
    pf       = float(metrics.get("profit_factor", 0.0))
    wr       = float(metrics.get("win_rate", 0.0)) * 100.0
    mdd      = float(metrics.get("max_drawdown", 0.0)) * 100.0
    n_trades = int(metrics.get("total_trades", 0))
    sharpe   = float(metrics.get("sharpe", 0.0))
    sortino  = float(metrics.get("sortino", 0.0))
    expect   = float(metrics.get("expectancy", 0.0)) * 100.0

    window_str = (
        f"{eval_window.get('symbol', '?')} {eval_window.get('timeframe', '?')}  "
        f"{str(eval_window.get('start', '?'))[:10]} → {str(eval_window.get('end', '?'))[:10]}"
    )
    sep = "─" * 40
    rows = [
        ("Strategy",        f"{strategy_name}"),
        ("Verdict",         f"{verdict}" + (f"  (lane: {lane})" if lane and passed else "")),
    ]
    if not passed and reason:
        rows.append(("Reject reason", reason))
    rows.extend([
        ("",                sep),
        ("Profit Factor",   f"{pf:.2f}"),
        ("Win Rate",        f"{wr:.1f}%   ({n_trades} trades)"),
        ("Max Drawdown",    f"{mdd:.1f}%"),
        ("Sharpe",          f"{sharpe:.2f}"),
        ("Sortino",         f"{sortino:.2f}"),
        ("Expectancy",      f"{expect:.2f}%"),
        ("",                sep),
        ("Signal activity", f"{activity_pct:.1f}%   (≥ {activity_thr:.1f}%)"),
        ("Window",          window_str),
    ])

    y = 0.97
    line_h = 0.062
    for label, value in rows:
        if label == "" and value.startswith("─"):
            ax_table.text(0.02, y, value, transform=ax_table.transAxes,
                          fontsize=9, family="monospace", color="#999999")
        else:
            ax_table.text(0.02, y, f"{label:<16}", transform=ax_table.transAxes,
                          fontsize=10, family="monospace", color="#666666")
            color = verdict_color if label == "Verdict" else "#222222"
            weight = "bold" if label in ("Strategy", "Verdict") else "normal"
            ax_table.text(0.42, y, value, transform=ax_table.transAxes,
                          fontsize=10, family="monospace", color=color,
                          fontweight=weight)
        y -= line_h

    fig.tight_layout(rect=(0, 0, 1, 0.965))
    fig.savefig(str(output_path), dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path
