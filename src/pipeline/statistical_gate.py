"""
Statistical Gate — Final pipeline phase before Integration.

After the Transpiler/Validator/TestGenerator agents produce a converted
strategy, this gate answers one question: "Does this strategy perform well
enough on real multi-year BTC data to be worth RL training?"

The gate runs four checks in order — any failure short-circuits:
  1. Load OHLCV from cache (downloaded once via src/evaluation/ohlcv.py).
  2. Execute the strategy's vectorized generate_all_signals.
  3. Variance: LONG+SHORT signals must cover ≥MIN_SIGNAL_ACTIVITY_PCT of bars.
  4. Strict-compliance lane assignment: a 7-dimension `GateMetrics` record
     (Profit Factor, Win Rate, Max Drawdown, Trade Count, Sharpe, Sortino,
     Expectancy) is built from per-trade PnL + bar-level returns and routed
     through the pure `assign_lane()` decision function. Any single overfit
     cap or hard floor failure rejects (`lane=None`); meeting all strict
     bars yields `lane="strict"`; passing the floors but missing one or
     more strict bars yields `lane="research"`.

On PASS: write signal_heatmap.png + stats_report.json into <output_dir>/eval/,
populate the `evaluation` block on the registry entry, and return a GateResult
with passed=True. The caller (main.py) then proceeds to Integration.

On FAIL: write stats_report.json with the failure reason (still useful as PR
body evidence or for post-mortem), set the registry status to
'statistically_rejected' (terminal — does NOT consume a conversion attempt),
and return a GateResult with passed=False.

Public API
----------
run_statistical_gate(strategy, output_dir, *, ohlcv_df=None) -> GateResult
GateMetrics, assign_lane(metrics) -> (lane, reason)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from src.base_strategy import BaseStrategy
from src.evaluation.heatmap import render_heatmap
from src.evaluation.metrics import (
    compute_bar_returns,
    compute_equity_curve,
    compute_expectancy,
    compute_max_drawdown,
    compute_profit_factor,
    compute_sharpe,
    compute_sortino,
)
from src.evaluation.ohlcv import fetch_range
from src.evaluation.runner import (
    StrategyContractError,
    count_by_signal,
    generate_signals_for_strategy,
)
from src.evaluation.variance import signal_activity_pct
from src.evaluation.winrate import (
    compute_trades,
    compute_winrate,
    render_winrate_curve,
)
from src.pipeline import (
    EVAL_END,
    EVAL_EXCHANGE,
    EVAL_START,
    EVAL_SYMBOL,
    EVAL_TIMEFRAME,
    MIN_SIGNAL_ACTIVITY_PCT,
)
from src.utils.timeframes import timeframe_to_minutes


logger = logging.getLogger("runner.gate")


# --- Strict-Compliance Thresholds (academic baseline) -----------------------
# Overfit / cheater caps — REJECT if a metric exceeds the cap.
PF_OVERFIT_CAP        = 2.5
WIN_RATE_OVERFIT_CAP  = 0.70
SHARPE_OVERFIT_CAP    = 2.0
SORTINO_OVERFIT_CAP   = 2.5

# Hard floors — REJECT if a metric falls below the floor (or above, for MDD).
PF_FLOOR              = 1.2
WIN_RATE_FLOOR        = 0.35
MDD_CEILING_FLOOR     = 0.30   # MDD strictly greater than 0.30 → reject
TRADE_COUNT_FLOOR     = 150
SHARPE_FLOOR          = 0.5
SORTINO_FLOOR         = 0.7
EXPECTANCY_FLOOR      = 0.0    # expectancy must be strictly > 0

# Strict-lane bars — assign `lane="strict"` only when ALL are met.
PF_STRICT             = 1.3
WIN_RATE_STRICT       = 0.40
MDD_CEILING_STRICT    = 0.25
TRADE_COUNT_STRICT    = 200
SHARPE_STRICT         = 0.7
SORTINO_STRICT        = 0.9


@dataclass(frozen=True)
class GateMetrics:
    """The 7 metrics that drive lane assignment."""
    profit_factor: float
    win_rate: float
    max_drawdown: float
    total_trades: int
    sharpe: float
    sortino: float
    expectancy: float


def assign_lane(m: GateMetrics) -> tuple[Optional[str], Optional[str]]:
    """Pure decision function returning (lane, reason).

    Order matters:
      1. Overfit caps first — a "too good" metric is more diagnostic than
         a "too bad" one (it usually means data leakage / lookahead).
      2. Hard floors second — anything missing a floor is unviable.
      3. Lane assignment last — passing all strict bars yields "strict",
         otherwise "research".
    """
    # 1. Overfit / cheater caps
    if m.profit_factor > PF_OVERFIT_CAP:
        return None, f"overfit_profit_factor: {m.profit_factor:.3f} > {PF_OVERFIT_CAP}"
    if m.win_rate > WIN_RATE_OVERFIT_CAP:
        return None, f"overfit_win_rate: {m.win_rate:.3f} > {WIN_RATE_OVERFIT_CAP}"
    if m.sharpe > SHARPE_OVERFIT_CAP:
        return None, f"overfit_sharpe: {m.sharpe:.3f} > {SHARPE_OVERFIT_CAP}"
    if m.sortino > SORTINO_OVERFIT_CAP:
        return None, f"overfit_sortino: {m.sortino:.3f} > {SORTINO_OVERFIT_CAP}"

    # 2. Hard floors
    if m.profit_factor < PF_FLOOR:
        return None, f"profit_factor_below_floor: {m.profit_factor:.3f} < {PF_FLOOR}"
    if m.win_rate < WIN_RATE_FLOOR:
        return None, f"win_rate_below_floor: {m.win_rate:.3f} < {WIN_RATE_FLOOR}"
    if m.max_drawdown > MDD_CEILING_FLOOR:
        return None, f"max_drawdown_above_ceiling: {m.max_drawdown:.3f} > {MDD_CEILING_FLOOR}"
    if m.total_trades < TRADE_COUNT_FLOOR:
        return None, f"trade_count_below_floor: {m.total_trades} < {TRADE_COUNT_FLOOR}"
    if m.sharpe < SHARPE_FLOOR:
        return None, f"sharpe_below_floor: {m.sharpe:.3f} < {SHARPE_FLOOR}"
    if m.sortino < SORTINO_FLOOR:
        return None, f"sortino_below_floor: {m.sortino:.3f} < {SORTINO_FLOOR}"
    if m.expectancy <= EXPECTANCY_FLOOR:
        return None, f"expectancy_non_positive: {m.expectancy:.6f} <= 0"

    # 3. Lane assignment
    strict = (
        m.profit_factor   >= PF_STRICT
        and m.win_rate    >= WIN_RATE_STRICT
        and m.max_drawdown<= MDD_CEILING_STRICT
        and m.total_trades>= TRADE_COUNT_STRICT
        and m.sharpe      >= SHARPE_STRICT
        and m.sortino     >= SORTINO_STRICT
        and m.expectancy   > 0
    )
    return ("strict" if strict else "research"), None


@dataclass
class GateResult:
    """Verdict + stats payload written to stats_report.json and returned to main."""
    passed: bool
    reason: Optional[str]
    strategy_name: str
    evaluated_at: str
    eval_window: dict[str, Any]
    lookback: dict[str, Any]
    lane: Optional[str] = None
    variance: dict[str, Any] = field(default_factory=dict)
    winrate: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    signal_counts: dict[str, int] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_registry_block(self) -> dict[str, Any]:
        """Compact form written into strategies_registry.json under `evaluation`."""
        return {
            "passed": self.passed,
            "reason": self.reason,
            "lane": self.lane,
            "signal_activity_pct": self.variance.get("signal_activity_pct", 0.0),
            "win_rate": self.winrate.get("win_rate", 0.0),
            "trade_count": self.winrate.get("total_trades", 0),
            "avg_pnl": self.winrate.get("avg_pnl", 0.0),
            "profit_factor": self.metrics.get("profit_factor", 0.0),
            "max_drawdown": self.metrics.get("max_drawdown", 0.0),
            "sharpe": self.metrics.get("sharpe", 0.0),
            "sortino": self.metrics.get("sortino", 0.0),
            "expectancy": self.metrics.get("expectancy", 0.0),
            "evaluated_at": self.evaluated_at,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_lookback(strategy: BaseStrategy) -> dict[str, Any]:
    min_candles = int(getattr(strategy, "MIN_CANDLES_REQUIRED", 0) or 0)
    tf_minutes = timeframe_to_minutes(strategy.timeframe)
    lookback_hours = (min_candles * tf_minutes) / 60.0
    return {
        "min_candles_required": min_candles,
        "timeframe": strategy.timeframe,
        "lookback_hours": lookback_hours,
    }


def _write_artifacts(
    eval_dir: Path,
    result: GateResult,
    signals: Optional[pd.Series],
    ohlcv_df: Optional[pd.DataFrame],
    strategy_name: str,
    trades: Optional[pd.DataFrame] = None,
) -> None:
    eval_dir.mkdir(parents=True, exist_ok=True)

    if signals is not None and ohlcv_df is not None:
        heatmap_df = pd.DataFrame({strategy_name: signals.values}, index=ohlcv_df.index)
        heatmap_path = eval_dir / "signal_heatmap.png"
        render_heatmap(
            signals_df=heatmap_df,
            strategy_cols=[strategy_name],
            closes=ohlcv_df["close"],
            timestamps=pd.Series(ohlcv_df.index, index=ohlcv_df.index),
            output_path=heatmap_path,
        )
        if heatmap_path.exists():
            result.artifacts["heatmap"] = str(heatmap_path.relative_to(eval_dir.parent))

    if trades is not None and not trades.empty:
        curve_path = eval_dir / "winrate_curve.png"
        verdict = (
            f"PASS {result.lane.upper()}"
            if result.passed and result.lane
            else f"REJECT ({result.reason or 'unknown'})"
        )
        rendered = render_winrate_curve(
            trades=trades,
            output_path=curve_path,
            title=f"{strategy_name} [{verdict}]",
        )
        if rendered is not None and curve_path.exists():
            result.artifacts["winrate_curve"] = str(
                curve_path.relative_to(eval_dir.parent)
            )

    stats_path = eval_dir / "stats_report.json"
    result.artifacts["stats_report"] = str(stats_path.relative_to(eval_dir.parent))
    stats_path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")


def run_statistical_gate(
    strategy: BaseStrategy,
    output_dir: Path,
    *,
    ohlcv_df: Optional[pd.DataFrame] = None,
) -> GateResult:
    """
    Run the full statistical gate for one converted strategy.

    Args:
        strategy:   An instantiated BaseStrategy subclass (already imported by caller).
        output_dir: <repo>/output/<StrategyName>/<timestamp>/.
                    Artifacts will be written under output_dir/eval/.
        ohlcv_df:   Optional pre-fetched candles (for tests). If None, the gate
                    calls fetch_range with the pipeline's EVAL_* constants.

    Returns:
        GateResult with passed=True|False, stats, and artifact paths.
    """
    output_dir = Path(output_dir)
    eval_dir = output_dir / "eval"
    evaluated_at = _now_iso()

    logger.info(f"[GATE] Starting statistical gate for '{strategy.name}'")

    # Step 1 — load candles
    if ohlcv_df is None:
        ohlcv_df = fetch_range(
            exchange_name=EVAL_EXCHANGE,
            symbol=EVAL_SYMBOL,
            timeframe=EVAL_TIMEFRAME,
            start=EVAL_START,
            end=EVAL_END,
        )

    eval_window = {
        "exchange": EVAL_EXCHANGE,
        "symbol": EVAL_SYMBOL,
        "timeframe": EVAL_TIMEFRAME,
        "start": EVAL_START,
        "end": EVAL_END,
        "candle_count": len(ohlcv_df),
    }
    lookback = _compute_lookback(strategy)

    base_result = GateResult(
        passed=False,
        reason=None,
        strategy_name=strategy.name,
        evaluated_at=evaluated_at,
        eval_window=eval_window,
        lookback=lookback,
    )

    # Step 2 — run the strategy (raises on any contract violation)
    try:
        signals = generate_signals_for_strategy(strategy, ohlcv_df)
    except StrategyContractError as e:
        base_result.reason = f"contract_violation: {type(e).__name__}: {e}"
        logger.error(f"[GATE] {strategy.name} — {base_result.reason}")
        _write_artifacts(eval_dir, base_result, None, None, strategy.name)
        return base_result

    base_result.signal_counts = count_by_signal(signals)
    winrate_stats = compute_winrate(ohlcv_df["close"], signals)
    trades_df = compute_trades(ohlcv_df["close"], signals)

    # Step 3 — variance check
    activity_pct = signal_activity_pct(signals)
    variance_passed = activity_pct >= MIN_SIGNAL_ACTIVITY_PCT
    base_result.variance = {
        "signal_activity_pct": activity_pct,
        "threshold": MIN_SIGNAL_ACTIVITY_PCT,
        "passed": variance_passed,
    }
    base_result.winrate = {
        "win_rate": float(winrate_stats["win_rate"]),
        "total_trades": int(winrate_stats["total_trades"]),
        "avg_pnl": float(winrate_stats["avg_pnl"]),
    }

    if not variance_passed:
        base_result.reason = (
            f"variance_below_threshold: {activity_pct:.2%} < {MIN_SIGNAL_ACTIVITY_PCT:.0%}"
        )
        logger.info(f"[GATE] {strategy.name} — {base_result.reason}")
        base_result.winrate["informational_only"] = True
        _write_artifacts(
            eval_dir, base_result, signals, ohlcv_df, strategy.name, trades_df,
        )
        return base_result

    # Step 4 — strict-compliance lane assignment over the 7 metrics.
    bar_returns = compute_bar_returns(ohlcv_df["close"], signals)
    equity = compute_equity_curve(bar_returns)
    trade_pnl = (
        trades_df["pnl"]
        if not trades_df.empty
        else pd.Series(dtype=float)
    )
    metrics = GateMetrics(
        profit_factor = compute_profit_factor(trade_pnl),
        win_rate      = float(winrate_stats["win_rate"]),
        max_drawdown  = compute_max_drawdown(equity),
        total_trades  = int(winrate_stats["total_trades"]),
        sharpe        = compute_sharpe(bar_returns),
        sortino       = compute_sortino(bar_returns),
        expectancy    = compute_expectancy(trade_pnl),
    )
    base_result.metrics = asdict(metrics)

    lane, reason = assign_lane(metrics)
    base_result.lane = lane
    base_result.reason = reason
    base_result.passed = lane is not None

    if lane is None:
        logger.info(f"[GATE] {strategy.name} — {reason}")
    else:
        logger.info(
            f"[GATE] {strategy.name} PASSED ({lane}) — "
            f"PF={metrics.profit_factor:.2f}, WR={metrics.win_rate:.1%}, "
            f"MDD={metrics.max_drawdown:.1%}, trades={metrics.total_trades}, "
            f"Sharpe={metrics.sharpe:.2f}, Sortino={metrics.sortino:.2f}, "
            f"Exp={metrics.expectancy:.4%}"
        )

    _write_artifacts(
        eval_dir, base_result, signals, ohlcv_df, strategy.name, trades_df,
    )
    return base_result
