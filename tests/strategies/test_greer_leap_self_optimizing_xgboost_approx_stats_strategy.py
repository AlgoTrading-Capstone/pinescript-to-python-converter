"""
Tests for GreerLeapSelfOptimizingXgboostApproxStatsStrategy.

Coverage
--------
1. Warmup guard  — HOLD for any df shorter than MIN_CANDLES_REQUIRED.
2. Signal detection — LONG on uptrend, SHORT on downtrend (regime filter
   disabled so we control the composite-score direction precisely).
3. Edge cases    — empty df, all-NaN, constant prices, contract invariants.

Note on regime filter in signal tests
--------------------------------------
The default strategy has ``use_regime=True``, which requires a daily EMA(50)
to converge (>= 50 daily bars = ~4 800 15m candles).  Signal-detection tests
disable the regime filter (``strategy.use_regime = False``) so we can drive
the composite score with a modest-sized synthetic dataset and assert clear
directional signals without depending on enough daily history.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.base_strategy import SignalType, StrategyRecommendation
from src.strategies.greer_leap_self_optimizing_xgboost_approx_stats_strategy import (
    GreerLeapSelfOptimizingXgboostApproxStatsStrategy,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SIGNALS = frozenset(SignalType)
_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_15m_df(n: int = 500, direction: str = "up") -> pd.DataFrame:
    """
    Build a synthetic 15m-interval OHLCV DataFrame engineered to trigger a
    composite zero-crossover on the final bar.

    Structure
    ---------
    - Phase 1 (first ~85 % of bars): opposite-direction trend so that the
      composite score stabilises on the wrong side of zero.
    - Phase 2 (final ~15 % of bars): strong reversal in ``direction`` that
      drives RSI, ROC, WaveTrend and the rolling-correlation weights to flip
      the composite across zero on the last bar.

    Parameters
    ----------
    n         : total candles (default 500, well above MIN_CANDLES_REQUIRED=63)
    direction : "up"  -> expect LONG signal on last bar
                "down" -> expect SHORT signal on last bar
    """
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    dates = [start + timedelta(minutes=15 * i) for i in range(n)]

    split = int(n * 0.85)           # ~425 bars of counter-trend
    reversal = n - split            # ~75 bars of sharp reversal

    if direction == "up":
        phase1 = np.linspace(13_000.0, 10_000.0, split)      # fall
        phase2 = np.linspace(10_000.0, 13_500.0, reversal)   # sharp rise
    else:
        phase1 = np.linspace(10_000.0, 13_000.0, split)      # rise
        phase2 = np.linspace(13_000.0, 9_500.0, reversal)    # sharp fall

    close = np.concatenate([phase1, phase2])

    np.random.seed(42)
    close = close + np.random.normal(0, 8, n)

    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = 4.0
    high = np.maximum(open_, close) + spread
    low  = np.minimum(open_, close) - spread

    volume = np.abs(np.random.normal(100, 10, n)) + 10.0

    df = pd.DataFrame(
        {
            "date":   dates,
            "open":   open_,
            "high":   high,
            "low":    low,
            "close":  close,
            "volume": volume,
        }
    )
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df["high"] = df[["open", "close", "high"]].max(axis=1)
    df["low"]  = df[["open", "close", "low"]].min(axis=1)
    return df


# ---------------------------------------------------------------------------
# Test 1 -- Warmup guard
# ---------------------------------------------------------------------------

class TestWarmupGuard:
    """Strategy must return HOLD whenever len(df) < MIN_CANDLES_REQUIRED (63)."""

    def test_empty_dataframe_returns_hold(self):
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_single_row_returns_hold(self, sample_ohlcv_data):
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        df = sample_ohlcv_data.iloc[:1].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_below_min_candles_returns_hold(self, sample_ohlcv_data):
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        cutoff = strategy.MIN_CANDLES_REQUIRED - 1
        df = sample_ohlcv_data.iloc[:cutoff].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_warmup_phase_slice_returns_hold(self, sample_ohlcv_data):
        """Phase-0 fixture slice (50 rows) is well below the 63-bar guard."""
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        df = sample_ohlcv_data.iloc[:50].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_result_timestamp_preserved_on_hold(self, sample_ohlcv_data):
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        df = sample_ohlcv_data.iloc[:1].copy()
        result = strategy.run(df, _TS)
        assert result.timestamp == _TS


# ---------------------------------------------------------------------------
# Test 2 -- Signal detection
# ---------------------------------------------------------------------------

def _scan_signals(df: pd.DataFrame, start_bar: int) -> set:
    """
    Run the strategy on each prefix df[:i+1] for i in [start_bar, len(df))
    and return the set of all distinct signals seen over that window.

    The regime filter is disabled so tests are independent of daily-EMA
    convergence.  This mirrors how the RL engine walks the series bar-by-bar.
    """
    strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
    strategy.use_regime = False
    seen = set()
    for i in range(start_bar, len(df)):
        result = strategy.run(df.iloc[: i + 1].copy(), _TS)
        seen.add(result.signal)
    return seen


class TestSignalDetection:
    """
    Strategy must produce correct directional signals on crafted trend data.

    Because the composite-score crossover is a *state-change* signal (only
    fires on the bar the score crosses zero), the tests scan the reversal
    phase bar-by-bar and verify the expected signal appears at least once
    within that window.  The regime filter is disabled to keep tests
    independent of daily-EMA convergence history.
    """

    def test_uptrend_reversal_produces_long_signal(self):
        """
        A dataset that falls then sharply rises must produce at least one
        LONG signal during the rising reversal phase.
        """
        df = _make_15m_df(n=500, direction="up")
        split = int(len(df) * 0.85)   # start of the bull reversal
        signals = _scan_signals(df, split)
        assert SignalType.LONG in signals, (
            f"Expected LONG in reversal window; saw {signals}"
        )

    def test_downtrend_reversal_produces_short_signal(self):
        """
        A dataset that rises then sharply falls must produce at least one
        SHORT signal during the falling reversal phase.
        """
        df = _make_15m_df(n=500, direction="down")
        split = int(len(df) * 0.85)
        signals = _scan_signals(df, split)
        assert SignalType.SHORT in signals, (
            f"Expected SHORT in reversal window; saw {signals}"
        )

    def test_reversal_produces_non_hold_signals(self):
        """
        During both bull and bear reversals the strategy must fire at least
        one active (non-HOLD) signal, confirming the crossover logic works.
        """
        for direction in ("up", "down"):
            df = _make_15m_df(n=500, direction=direction)
            split = int(len(df) * 0.85)
            signals = _scan_signals(df, split)
            active = signals - {SignalType.HOLD}
            assert active, (
                f"Strategy produced only HOLD during '{direction}' reversal; "
                f"expected at least one LONG or SHORT"
            )

    def test_result_is_strategy_recommendation(self):
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        strategy.use_regime = False
        result = strategy.run(_make_15m_df(500, "up"), _TS)
        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)
        assert isinstance(result.timestamp, datetime)

    def test_result_timestamp_preserved(self):
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        strategy.use_regime = False
        result = strategy.run(_make_15m_df(500, "up"), _TS)
        assert result.timestamp == _TS

    def test_exactly_min_candles_does_not_raise(self):
        """Exactly MIN_CANDLES_REQUIRED rows must not raise."""
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        strategy.use_regime = False
        df = _make_15m_df(n=strategy.MIN_CANDLES_REQUIRED, direction="up")
        try:
            result = strategy.run(df, _TS)
            assert result.signal in VALID_SIGNALS
        except Exception as exc:
            pytest.fail(f"Strategy raised at MIN_CANDLES_REQUIRED: {exc}")

    def test_signal_is_valid_enum(self):
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        strategy.use_regime = False
        result = strategy.run(_make_15m_df(200, "up"), _TS)
        assert result.signal in VALID_SIGNALS


# ---------------------------------------------------------------------------
# Test 3 -- Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Strategy must handle degenerate inputs gracefully -- no raw exceptions."""

    def test_all_nan_close_returns_valid_signal(self, sample_ohlcv_data):
        """All-NaN OHLCV on a short slice fires the RL guard before any calc."""
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        df = sample_ohlcv_data.iloc[:10].copy()
        df["close"] = np.nan
        df["open"]  = np.nan
        df["high"]  = np.nan
        df["low"]   = np.nan
        try:
            result = strategy.run(df, _TS)
            assert result.signal in VALID_SIGNALS
        except Exception as exc:
            pytest.fail(f"Strategy raised on all-NaN input: {exc}")

    def test_constant_price_does_not_raise(self):
        """Flat prices can produce zero ATR; strategy must handle gracefully."""
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        strategy.use_regime = False
        df = _make_15m_df(n=200, direction="up")
        df["open"]   = 10_000.0
        df["high"]   = 10_001.0
        df["low"]    = 9_999.0
        df["close"]  = 10_000.0
        df["volume"] = 100.0
        try:
            result = strategy.run(df, _TS)
            assert result.signal in VALID_SIGNALS
        except Exception as exc:
            pytest.fail(f"Strategy raised on constant prices: {exc}")

    def test_min_candles_required_is_positive_integer(self):
        strategy = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        assert isinstance(strategy.MIN_CANDLES_REQUIRED, int)
        assert strategy.MIN_CANDLES_REQUIRED > 0

    def test_min_candles_required_value(self):
        """MIN_CANDLES_REQUIRED == 3 * max of all indicator period parameters."""
        s = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        expected = 3 * max(
            s.rsi_length,
            s.cci_length,
            s.atr_length,
            s.roc_length,
            s.wt_ema_length,
            s.wt_channel_length,
            s.opt_window,
        )
        assert s.MIN_CANDLES_REQUIRED == expected

    def test_strategy_name(self):
        s = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        assert s.name == "GreerLeapSelfOptimizingXgboostApproxStatsStrategy"

    def test_timeframe_is_lowercase(self):
        s = GreerLeapSelfOptimizingXgboostApproxStatsStrategy()
        assert s.timeframe == s.timeframe.lower()

    def test_timeframe_is_15m(self):
        assert GreerLeapSelfOptimizingXgboostApproxStatsStrategy().timeframe == "15m"

    def test_lookback_hours_positive(self):
        assert GreerLeapSelfOptimizingXgboostApproxStatsStrategy().lookback_hours > 0
