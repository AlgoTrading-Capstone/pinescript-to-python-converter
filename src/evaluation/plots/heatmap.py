"""
Strategy Signal Heatmap for Converter Evaluation Artifacts

Renders a two-panel plot (price + categorical signal heatmap) used as PR-body
evidence when a strategy passes the statistical gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Patch


WARMUP_LABEL = "Warm-up/No Signal"
PLOT_SIGNAL_MAP = {
    WARMUP_LABEL: 0,
    "FLAT": 1,
    "HOLD": 2,
    "LONG": 3,
    "SHORT": 4,
}
SIGNAL_COLORS = ["#000000", "#f0f4ff", "#bbbbbb", "#2ecc71", "#e74c3c"]
SIGNAL_LABELS = [WARMUP_LABEL, "FLAT", "HOLD", "LONG", "SHORT"]

MAX_PLOT_POINTS = 2000


def _normalize_signal(value) -> str:
    if pd.isna(value):
        return WARMUP_LABEL
    if isinstance(value, str):
        label = value.strip()
        if not label:
            return WARMUP_LABEL
        return label if label in PLOT_SIGNAL_MAP else WARMUP_LABEL
    return value if value in PLOT_SIGNAL_MAP else WARMUP_LABEL


def _trim_leading_warmup(
    signals_df: pd.DataFrame, strategy_cols: Sequence[str]
) -> pd.DataFrame:
    if signals_df.empty or not strategy_cols:
        return signals_df
    normalized = signals_df[list(strategy_cols)].apply(
        lambda col: col.map(_normalize_signal)
    )
    all_warmup = normalized.eq(WARMUP_LABEL).all(axis=1).to_numpy()
    if not all_warmup.any():
        return signals_df
    keep_from = int(np.argmax(~all_warmup)) if (~all_warmup).any() else len(signals_df)
    if keep_from <= 0:
        return signals_df
    return signals_df.iloc[keep_from:].reset_index(drop=True)


def _downsample(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if len(df) <= MAX_PLOT_POINTS:
        return df, len(df)
    original_n = len(df)
    bin_size = len(df) / MAX_PLOT_POINTS
    sampled = df.copy()
    sampled["_bin"] = (np.arange(len(sampled)) / bin_size).astype(int)
    sampled = sampled.groupby("_bin").tail(1).reset_index(drop=True)
    sampled.drop(columns=["_bin"], inplace=True)
    return sampled, original_n


def _build_signal_matrix(
    df: pd.DataFrame, strategy_cols: Sequence[str]
) -> np.ndarray:
    matrix = np.zeros((len(strategy_cols), len(df)), dtype=int)
    for row_idx, col in enumerate(strategy_cols):
        normalized = df[col].map(_normalize_signal)
        matrix[row_idx, :] = normalized.map(PLOT_SIGNAL_MAP).astype(int).values
    return matrix


def render_heatmap(
    signals_df: pd.DataFrame,
    strategy_cols: Sequence[str],
    closes: pd.Series | None,
    timestamps: pd.Series | None,
    output_path: Path,
) -> Path | None:
    """
    Render a price + signal heatmap for the given signal DataFrame.

    Args:
        signals_df:     DataFrame whose columns are strategy signals (strings).
        strategy_cols:  Which columns of signals_df to plot.
        closes:         Optional close-price series (same index) for the top panel.
        timestamps:     Optional timestamp series for x-axis labels.
        output_path:    Destination PNG path.
    """
    if not strategy_cols:
        return None

    df = signals_df.copy()
    if closes is not None:
        df["close"] = closes.values
    if timestamps is not None:
        df["timestamp"] = pd.to_datetime(timestamps.values, errors="coerce")

    df = _trim_leading_warmup(df, strategy_cols)
    if df.empty:
        return None

    df, original_n = _downsample(df)
    n_steps = len(df)
    n_strats = len(strategy_cols)
    matrix = _build_signal_matrix(df, list(strategy_cols))

    if "timestamp" in df.columns and df["timestamp"].notna().any():
        x_labels = df["timestamp"]
        x_label = "Time"
    else:
        x_labels = pd.Series(np.arange(n_steps))
        x_label = "Step"
    x_positions = np.arange(n_steps)

    fig, (ax_price, ax_heat) = plt.subplots(
        2, 1,
        figsize=(14, 4 + n_strats * 0.4),
        sharex=True,
        gridspec_kw={"height_ratios": [2, max(1, n_strats * 0.3)]},
    )

    if "close" in df.columns:
        ax_price.plot(x_positions, df["close"].values, color="#2c3e50", linewidth=0.8)
        ax_price.set_ylabel("Close Price", fontsize=9)
        ax_price.set_title("Price & Strategy Signals", fontsize=11, fontweight="bold")
        ax_price.grid(True, alpha=0.3)
    else:
        ax_price.set_visible(False)

    cmap = ListedColormap(SIGNAL_COLORS)
    norm = BoundaryNorm(np.arange(-0.5, len(SIGNAL_COLORS) + 0.5, 1), cmap.N)
    ax_heat.imshow(
        matrix, aspect="auto", cmap=cmap, norm=norm, interpolation="nearest",
        extent=[-0.5, n_steps - 0.5, n_strats, 0],
    )
    ax_heat.set_yticks(np.arange(n_strats) + 0.5)
    ax_heat.set_yticklabels(list(strategy_cols), fontsize=7)
    ax_heat.set_xlabel(x_label, fontsize=9)

    n_ticks = min(10, n_steps)
    tick_indices = np.linspace(0, n_steps - 1, n_ticks, dtype=int)
    ax_heat.set_xticks(tick_indices)
    if x_label == "Time":
        tick_labels = [pd.Timestamp(x_labels.iloc[i]).strftime("%Y-%m-%d %H:%M") for i in tick_indices]
        ax_heat.set_xticklabels(tick_labels, fontsize=7, rotation=30, ha="right")
    else:
        ax_heat.set_xticklabels([str(x_labels.iloc[i]) for i in tick_indices], fontsize=7)

    legend_patches = [
        Patch(facecolor=SIGNAL_COLORS[0], edgecolor="#ffffff", hatch="////", label=SIGNAL_LABELS[0]),
        Patch(facecolor=SIGNAL_COLORS[1], label=SIGNAL_LABELS[1]),
        Patch(facecolor=SIGNAL_COLORS[2], label=SIGNAL_LABELS[2]),
        Patch(facecolor=SIGNAL_COLORS[3], label=SIGNAL_LABELS[3]),
        Patch(facecolor=SIGNAL_COLORS[4], label=SIGNAL_LABELS[4]),
    ]
    ax_heat.legend(handles=legend_patches, loc="upper right", fontsize=7, ncol=3, framealpha=0.85)

    plt.tight_layout()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_path), dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Render strategy signal heatmap from CSV.")
    parser.add_argument("csv_path", type=str)
    parser.add_argument("--output", "-o", type=str, default=None)
    args = parser.parse_args()
    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        print(f"File not found: {csv_path}", file=sys.stderr)
        sys.exit(1)
    output_path = Path(args.output) if args.output else csv_path.parent / "signal_heatmap.png"

    df = pd.read_csv(csv_path)
    ts = df["timestamp"] if "timestamp" in df.columns else None
    closes = df["close"] if "close" in df.columns else None
    non_strategy = {"timestamp", "date", "close", "step"}
    strategy_cols = [c for c in df.columns if c not in non_strategy]
    render_heatmap(df[strategy_cols], strategy_cols, closes, ts, output_path)


if __name__ == "__main__":
    main()