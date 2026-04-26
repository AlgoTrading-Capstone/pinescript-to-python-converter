"""
Strategy Evaluation Package

Statistical gate for converted strategies. A strategy must pass variance and
win-rate checks on a representative BTC dataset before it is promoted to the
rl-training project.
"""

from src.evaluation.variance import (
    ACTIVE_SIGNALS,
    evaluate_strategies,
    passes_variance,
    signal_activity_pct,
)
from src.evaluation.winrate import (
    compute_winrate,
    passes_winrate,
)
from src.evaluation.plots.heatmap import render_heatmap
from src.evaluation.plots.winrate_curve import create_winrate_barchart

__all__ = [
    "ACTIVE_SIGNALS",
    "evaluate_strategies",
    "passes_variance",
    "signal_activity_pct",
    "compute_winrate",
    "create_winrate_barchart",
    "passes_winrate",
    "render_heatmap",
]