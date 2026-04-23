"""
Statistical Gate — Final pipeline phase before Integration.

After the Transpiler/Validator/TestGenerator agents produce a converted
strategy, this gate answers one question: "Does this strategy perform well
enough on real multi-year BTC data to be worth RL training?"

The gate runs four checks in order — any failure short-circuits:
  1. Load OHLCV from cache (downloaded once via src/evaluation/ohlcv.py).
  2. Execute the strategy's vectorized generate_all_signals.
  3. Variance: LONG+SHORT signals must cover ≥MIN_SIGNAL_ACTIVITY_PCT of bars.
  4. Win rate: ≥MIN_WIN_RATE over ≥MIN_TRADE_COUNT trades.

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
    passes_winrate,
    render_winrate_curve,
)
from src.pipeline import (
    EVAL_END,
    EVAL_EXCHANGE,
    EVAL_START,
    EVAL_SYMBOL,
    EVAL_TIMEFRAME,
    MIN_SIGNAL_ACTIVITY_PCT,
    MIN_TRADE_COUNT,
    MIN_WIN_RATE,
)
from src.utils.timeframes import timeframe_to_minutes


logger = logging.getLogger("runner.gate")


@dataclass
class GateResult:
    """Verdict + stats payload written to stats_report.json and returned to main."""
    passed: bool
    reason: Optional[str]
    strategy_name: str
    evaluated_at: str
    eval_window: dict[str, Any]
    lookback: dict[str, Any]
    variance: dict[str, Any] = field(default_factory=dict)
    winrate: dict[str, Any] = field(default_factory=dict)
    signal_counts: dict[str, int] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_registry_block(self) -> dict[str, Any]:
        """Compact form written into strategies_registry.json under `evaluation`."""
        return {
            "passed": self.passed,
            "reason": self.reason,
            "signal_activity_pct": self.variance.get("signal_activity_pct", 0.0),
            "win_rate": self.winrate.get("win_rate", 0.0),
            "trade_count": self.winrate.get("total_trades", 0),
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
        verdict = "PASS" if result.passed else f"REJECT ({result.reason or 'unknown'})"
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
    stats_path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    result.artifacts["stats_report"] = str(stats_path.relative_to(eval_dir.parent))


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

    # Step 3 — variance check
    activity_pct = signal_activity_pct(signals)
    variance_passed = activity_pct >= MIN_SIGNAL_ACTIVITY_PCT
    base_result.variance = {
        "signal_activity_pct": activity_pct,
        "threshold": MIN_SIGNAL_ACTIVITY_PCT,
        "passed": variance_passed,
    }
    if not variance_passed:
        base_result.reason = (
            f"variance_below_threshold: {activity_pct:.2%} < {MIN_SIGNAL_ACTIVITY_PCT:.0%}"
        )
        logger.info(f"[GATE] {strategy.name} — {base_result.reason}")
        _write_artifacts(eval_dir, base_result, signals, ohlcv_df, strategy.name)
        return base_result

    # Step 4 — win-rate check
    winrate_stats = compute_winrate(ohlcv_df["close"], signals)
    trades_df = compute_trades(ohlcv_df["close"], signals)
    winrate_passed = passes_winrate(
        winrate_stats,
        min_win_rate=MIN_WIN_RATE,
        min_trades=MIN_TRADE_COUNT,
    )
    base_result.winrate = {
        "win_rate": winrate_stats["win_rate"],
        "total_trades": winrate_stats["total_trades"],
        "avg_pnl": winrate_stats["avg_pnl"],
        "min_trades_threshold": MIN_TRADE_COUNT,
        "min_winrate_threshold": MIN_WIN_RATE,
        "passed": winrate_passed,
    }
    if not winrate_passed:
        if winrate_stats["total_trades"] < MIN_TRADE_COUNT:
            base_result.reason = (
                f"too_few_trades: {winrate_stats['total_trades']} < {MIN_TRADE_COUNT}"
            )
        else:
            base_result.reason = (
                f"winrate_below_threshold: "
                f"{winrate_stats['win_rate']:.1%} < {MIN_WIN_RATE:.0%}"
            )
        logger.info(f"[GATE] {strategy.name} — {base_result.reason}")
        _write_artifacts(
            eval_dir, base_result, signals, ohlcv_df, strategy.name, trades_df,
        )
        return base_result

    # All checks passed
    base_result.passed = True
    logger.info(
        f"[GATE] {strategy.name} PASSED — "
        f"activity={activity_pct:.1%}, "
        f"winrate={winrate_stats['win_rate']:.1%} over {winrate_stats['total_trades']} trades"
    )
    _write_artifacts(
        eval_dir, base_result, signals, ohlcv_df, strategy.name, trades_df,
    )
    return base_result