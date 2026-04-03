"""
Tests for the Gold MTF Dashboard strategy.

Covers:
1. Warmup guard  — strategy returns HOLD for any DataFrame shorter than MIN_CANDLES_REQUIRED.
2. Signal detection — strategy produces valid signals on structured 1m data (bull / bear).
3. Edge cases    — empty DataFrame, all-NaN close, single-row DataFrame.

Note on data:
  The sample_ohlcv_data fixture provides 15m candles.  Because the strategy's
  RL guard fires *before* any resampling, all warmup tests that slice the fixture
  below MIN_CANDLES_REQUIRED (315) work correctly regardless of interval.

  Signal tests require 1m-interval candles so that resample_to_interval("3m") /
  resample_to_interval("5m") are valid higher-timeframe requests.  A dedicated
  _make_1m_df helper builds those frames while retaining the fixture's structural
  intent (clear bull / bear phases).
"""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta, timezone

from src.base_strategy import SignalType, StrategyRecommendation
from src.strategies.gold_mtf_strategy import GoldMtfStrategy


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

VALID_SIGNALS = frozenset(SignalType)
_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_1m_df(n: int = 500, direction: str = "up") -> pd.DataFrame:
    """
    Build a synthetic 1m-interval OHLCV DataFrame with a clear trend and a
    breakout on the final bar.

    Structure
    ---------
    - Bars 0 … n-2 : linear ramp (up or down) so EMA9 > EMA21 (bullish) or
                     EMA9 < EMA21 (bearish) across 1m, 3m, and 5m resampled
                     timeframes.
    - Bar n-1      : exaggerated spike (up +150 / down -150) to ensure the
                     closing price breaks through the rolling S/R level.
    - Volume       : elevated on the final 20 bars (2× normal) so the volume
                     filter passes.

    This mirrors the fixture's bull/bear regime philosophy but at 1m granularity.
    """
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    dates = [start + timedelta(minutes=i) for i in range(n)]

    if direction == "up":
        close = np.linspace(10_000.0, 10_500.0, n)
        close[-1] = close[-2] + 150.0   # break above resistance
    else:
        close = np.linspace(10_500.0, 10_000.0, n)
        close[-1] = close[-2] - 150.0   # break below support

    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = 2.0
    high = close + spread
    low = close - spread
    high[-1] = close[-1] + spread
    low[-1] = close[-1] - spread

    volume = np.full(n, 100.0)
    volume[-20:] = 250.0  # exceeds SMA(vol,20)*1.2 by construction

    df = pd.DataFrame(
        {"date": dates, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df["high"] = df[["open", "close", "high"]].max(axis=1)
    df["low"] = df[["open", "close", "low"]].min(axis=1)
    return df


# ---------------------------------------------------------------------------
# Test 1 — Warmup guard
# ---------------------------------------------------------------------------

class TestWarmupGuard:
    """Strategy must return HOLD whenever len(df) < MIN_CANDLES_REQUIRED (315)."""

    def test_empty_dataframe_returns_hold(self):
        strategy = GoldMtfStrategy()
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_single_row_returns_hold(self, sample_ohlcv_data):
        strategy = GoldMtfStrategy()
        df = sample_ohlcv_data.iloc[:1].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_below_min_candles_returns_hold(self, sample_ohlcv_data):
        """Any slice < MIN_CANDLES_REQUIRED must trigger the RL guard."""
        strategy = GoldMtfStrategy()
        cutoff = strategy.MIN_CANDLES_REQUIRED - 1
        df = sample_ohlcv_data.iloc[:cutoff].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_warmup_phase_slice_returns_hold(self, sample_ohlcv_data):
        """Phase 0 fixture slice (200 rows) is well below the guard — must HOLD."""
        strategy = GoldMtfStrategy()
        df = sample_ohlcv_data.iloc[:200].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_result_timestamp_preserved_on_hold(self, sample_ohlcv_data):
        strategy = GoldMtfStrategy()
        df = sample_ohlcv_data.iloc[:1].copy()
        result = strategy.run(df, _TS)
        assert result.timestamp == _TS


# ---------------------------------------------------------------------------
# Test 2 — Signal detection on structured 1m data
# ---------------------------------------------------------------------------

class TestSignalDetection:
    """Strategy must produce correct signals on crafted 1m bullish / bearish data."""

    def test_bull_trend_produces_long_signal(self):
        """
        Sustained uptrend with final-bar breakout above resistance and elevated
        volume must yield a LONG signal.
        """
        strategy = GoldMtfStrategy()
        df = _make_1m_df(n=500, direction="up")
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.LONG

    def test_bear_trend_produces_short_signal(self):
        """
        Sustained downtrend with final-bar breakdown below support and elevated
        volume must yield a SHORT signal.
        """
        strategy = GoldMtfStrategy()
        df = _make_1m_df(n=500, direction="down")
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.SHORT

    def test_result_is_strategy_recommendation(self):
        strategy = GoldMtfStrategy()
        df = _make_1m_df(n=500, direction="up")
        result = strategy.run(df, _TS)
        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)
        assert isinstance(result.timestamp, datetime)

    def test_result_timestamp_preserved(self):
        strategy = GoldMtfStrategy()
        df = _make_1m_df(n=500, direction="up")
        result = strategy.run(df, _TS)
        assert result.timestamp == _TS

    def test_bull_and_bear_produce_different_signals(self):
        strategy = GoldMtfStrategy()
        long_result = strategy.run(_make_1m_df(500, "up"), _TS)
        short_result = strategy.run(_make_1m_df(500, "down"), _TS)
        assert long_result.signal != short_result.signal

    def test_exactly_min_candles_does_not_raise(self):
        """Exactly MIN_CANDLES_REQUIRED rows of 1m data must not raise."""
        strategy = GoldMtfStrategy()
        df = _make_1m_df(n=strategy.MIN_CANDLES_REQUIRED, direction="up")
        try:
            result = strategy.run(df, _TS)
            assert result.signal in VALID_SIGNALS
        except Exception as exc:
            pytest.fail(f"Strategy raised at MIN_CANDLES_REQUIRED: {exc}")


# ---------------------------------------------------------------------------
# Test 3 — Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Strategy must handle degenerate inputs gracefully (no raw exceptions)."""

    def test_all_nan_close_returns_valid_signal(self, sample_ohlcv_data):
        """All-NaN OHLCV on a short slice must not raise — RL guard fires first."""
        strategy = GoldMtfStrategy()
        df = sample_ohlcv_data.iloc[:10].copy()
        df["close"] = np.nan
        df["open"] = np.nan
        df["high"] = np.nan
        df["low"] = np.nan
        try:
            result = strategy.run(df, _TS)
            assert result.signal in VALID_SIGNALS
        except Exception as exc:
            pytest.fail(f"Strategy raised on all-NaN input: {exc}")

    def test_constant_price_on_1m_does_not_raise(self):
        """Flat constant prices must not cause division-by-zero or ATR errors."""
        strategy = GoldMtfStrategy()
        df = _make_1m_df(n=500, direction="up")
        df["open"] = 10_000.0
        df["high"] = 10_001.0
        df["low"] = 9_999.0
        df["close"] = 10_000.0
        df["volume"] = 100.0
        try:
            result = strategy.run(df, _TS)
            assert result.signal in VALID_SIGNALS
        except Exception as exc:
            pytest.fail(f"Strategy raised on constant prices: {exc}")

    def test_min_candles_required_is_positive_integer(self):
        strategy = GoldMtfStrategy()
        assert isinstance(strategy.MIN_CANDLES_REQUIRED, int)
        assert strategy.MIN_CANDLES_REQUIRED > 0

    def test_min_candles_required_value(self):
        """MIN_CANDLES_REQUIRED must equal 3 * max(ema_slow, lookback, atr, vol_sma) * tf3."""
        s = GoldMtfStrategy()
        expected = 3 * max(s.ema_slow_period, s.lookback, s.atr_period, s.vol_sma_period) * s.tf3_minutes
        assert s.MIN_CANDLES_REQUIRED == expected

    def test_strategy_name(self):
        assert GoldMtfStrategy().name == "GoldMtfStrategy"

    def test_timeframe_is_lowercase(self):
        s = GoldMtfStrategy()
        assert s.timeframe == s.timeframe.lower()

    def test_timeframe_is_1m(self):
        assert GoldMtfStrategy().timeframe == "1m"

    def test_lookback_hours_positive(self):
        assert GoldMtfStrategy().lookback_hours > 0
