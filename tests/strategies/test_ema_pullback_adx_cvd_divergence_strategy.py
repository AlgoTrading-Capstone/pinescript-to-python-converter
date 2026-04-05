"""
Tests for EmaPullbackAdxCvdDivergenceStrategy.

Coverage:
  1. Warmup guard: returns HOLD when df has fewer than MIN_CANDLES_REQUIRED rows.
  2. Signal validity: run() always returns a valid StrategyRecommendation.
  3. Divergence signal tests: crafted minimal DataFrames trigger LONG / SHORT.
     Note: the standard sample_ohlcv_data fixture uses a monotone synthetic
     bull/bear trend where price_ll and price_hh never fire (smooth trends never
     produce new lows in a bull or new highs in a bear). Tests therefore build
     bespoke DataFrames that explicitly create the divergence spike patterns.
  4. Edge cases: empty DataFrame, all-NaN, zero-volume, constant price.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.strategies.ema_pullback_adx_cvd_divergence_strategy import (
    EmaPullbackAdxCvdDivergenceStrategy,
)
from src.base_strategy import SignalType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts() -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def _make_ohlcv(
    close: np.ndarray,
    open_: np.ndarray | None = None,
    high: np.ndarray | None = None,
    low: np.ndarray | None = None,
    volume: np.ndarray | None = None,
    interval_minutes: int = 15,
) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from arrays."""
    n = len(close)
    dates = [
        datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=interval_minutes * i)
        for i in range(n)
    ]
    if open_ is None:
        open_ = np.roll(close, 1)
        open_[0] = close[0]
    if volume is None:
        volume = np.full(n, 100.0)
    rng = np.random.default_rng(42)
    if high is None:
        high = np.maximum(open_, close) + np.abs(rng.normal(0, 5, n))
    if low is None:
        low = np.minimum(open_, close) - np.abs(rng.normal(0, 5, n))
    df = pd.DataFrame({
        "date":   pd.to_datetime(dates, utc=True),
        "open":   open_,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": volume,
    })
    df["high"] = df[["open", "close", "high"]].max(axis=1)
    df["low"]  = df[["open", "close", "low"]].min(axis=1)
    return df


def _make_bull_divergence_df() -> pd.DataFrame:
    """
    Build a DataFrame that triggers a bullish CVD divergence LONG on the last bar.

    Phase layout (150 bars):
      0-29   : flat at 10500 -- EMA convergence
      30-59  : down 10500->9800 -- EMA9 falls below EMA20 (bear_cross)
      60-139 : up   9800->11400 -- EMA9 crosses back above EMA20 (bull_cross),
               ADX builds above 25, wait_buy=True
      140-148: gentle plateau ~11400 -- establishes stable prior-10 low baseline
      149    : hammer candle -- close > open, low spikes 1200 pts below prior lows
               -> price_ll=True, cvd_hl=True, pullback_buy=True, buy_signal=True
    """
    n = 150
    close = np.empty(n)
    close[:30] = 10500.0
    close[30:60] = np.linspace(10500.0, 9800.0, 30)
    close[60:140] = np.linspace(9800.0, 11400.0, 80)
    close[140:149] = 11400.0
    close[149] = 11420.0

    open_ = np.roll(close, 1); open_[0] = close[0]
    open_[149] = 11405.0

    high = np.maximum(open_, close) + 5.0
    low  = np.minimum(open_, close) - 5.0
    low[149] = 10100.0

    volume = np.full(n, 100.0)
    dates = [
        datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * i)
        for i in range(n)
    ]
    df = pd.DataFrame({
        "date":   pd.to_datetime(dates, utc=True),
        "open":   open_,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": volume,
    })
    df["high"] = df[["open", "close", "high"]].max(axis=1)
    df["low"]  = df[["open", "close", "low"]].min(axis=1)
    return df


def _make_bear_divergence_df() -> pd.DataFrame:
    """
    Build a DataFrame that triggers a bearish CVD divergence SHORT on the last bar.

    Phase layout (150 bars):
      0-29   : flat at 10500 -- EMA convergence
      30-59  : up   10500->11200 -- EMA9 rises above EMA20 (bull_cross)
      60-139 : down 11200->9600 -- EMA9 crosses below EMA20 (bear_cross),
               ADX builds above 25, wait_sell=True
      140-148: gentle plateau ~9600 -- establishes stable prior-10 high baseline
      149    : shooting-star -- close < open, high spikes 1200 pts above prior highs
               -> price_hh=True, cvd_lh=True, pullback_sell=True, sell_signal=True
    """
    n = 150
    close = np.empty(n)
    close[:30] = 10500.0
    close[30:60] = np.linspace(10500.0, 11200.0, 30)
    close[60:140] = np.linspace(11200.0, 9600.0, 80)
    close[140:149] = 9600.0
    close[149] = 9580.0

    open_ = np.roll(close, 1); open_[0] = close[0]
    open_[149] = 9595.0

    high = np.maximum(open_, close) + 5.0
    low  = np.minimum(open_, close) - 5.0
    high[149] = 10800.0

    volume = np.full(n, 100.0)
    dates = [
        datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * i)
        for i in range(n)
    ]
    df = pd.DataFrame({
        "date":   pd.to_datetime(dates, utc=True),
        "open":   open_,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": volume,
    })
    df["high"] = df[["open", "close", "high"]].max(axis=1)
    df["low"]  = df[["open", "close", "low"]].min(axis=1)
    return df


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def strategy():
    return EmaPullbackAdxCvdDivergenceStrategy()


# ---------------------------------------------------------------------------
# 1. Warmup guard
# ---------------------------------------------------------------------------

class TestWarmupGuard:

    def test_returns_hold_when_below_min_candles(self, strategy, sample_ohlcv_data):
        """Strategy must return HOLD when fewer than MIN_CANDLES_REQUIRED rows are provided."""
        short_df = sample_ohlcv_data.iloc[:strategy.MIN_CANDLES_REQUIRED - 1]
        rec = strategy.run(short_df, _ts())
        assert rec.signal == SignalType.HOLD

    def test_returns_hold_on_single_row(self, strategy, sample_ohlcv_data):
        single = sample_ohlcv_data.iloc[:1]
        rec = strategy.run(single, _ts())
        assert rec.signal == SignalType.HOLD

    def test_min_candles_required_is_positive(self, strategy):
        assert strategy.MIN_CANDLES_REQUIRED > 0

    def test_min_candles_required_is_dynamic(self, strategy):
        """MIN_CANDLES_REQUIRED must equal 3 * max of relevant period params."""
        expected = 3 * max(
            strategy.ema_slow_period,
            strategy.adx_len,
            strategy.divergence_lookback,
        )
        assert strategy.MIN_CANDLES_REQUIRED == expected


# ---------------------------------------------------------------------------
# 2. Signal validity -- run() always returns a well-formed recommendation
# ---------------------------------------------------------------------------

class TestSignalValidity:

    def test_full_dataset_returns_valid_signal_type(self, strategy, sample_ohlcv_data):
        rec = strategy.run(sample_ohlcv_data, _ts())
        assert isinstance(rec.signal, SignalType)

    def test_run_returns_strategy_recommendation_namedtuple(self, strategy, sample_ohlcv_data):
        rec = strategy.run(sample_ohlcv_data, _ts())
        assert hasattr(rec, "signal")
        assert hasattr(rec, "timestamp")

    def test_timestamp_is_preserved(self, strategy, sample_ohlcv_data):
        ts = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        rec = strategy.run(sample_ohlcv_data, ts)
        assert rec.timestamp == ts

    def test_signal_not_none(self, strategy, sample_ohlcv_data):
        rec = strategy.run(sample_ohlcv_data, _ts())
        assert rec.signal is not None


# ---------------------------------------------------------------------------
# 3. Signal detection -- crafted divergence scenarios
# ---------------------------------------------------------------------------

class TestDivergenceSignals:
    """
    The standard sample_ohlcv_data fixture has a smooth synthetic bull/bear run
    where lows rise monotonically during the uptrend (price_ll never True) and
    highs fall monotonically during the downtrend (price_hh never True).  The
    divergence condition therefore never fires on that fixture.

    These tests instead use minimal bespoke DataFrames that embed a single
    hammer/shooting-star spike candle to explicitly satisfy all entry conditions.
    """

    def test_long_signal_on_bullish_cvd_divergence(self, strategy):
        """LONG must fire on a hammer candle that spikes below the 10-bar low in an uptrend."""
        df = _make_bull_divergence_df()
        rec = strategy.run(df, _ts())
        assert rec.signal == SignalType.LONG, (
            f"Expected LONG on bullish divergence hammer, got {rec.signal}"
        )

    def test_short_signal_on_bearish_cvd_divergence(self, strategy):
        """SHORT must fire on a shooting-star candle that spikes above the 10-bar high in a downtrend."""
        df = _make_bear_divergence_df()
        rec = strategy.run(df, _ts())
        assert rec.signal == SignalType.SHORT, (
            f"Expected SHORT on bearish divergence shooting-star, got {rec.signal}"
        )

    def test_no_signal_in_flat_warmup(self, strategy, sample_ohlcv_data):
        """Phase 0 is flat -- no EMA cross, no trend -> only HOLD expected."""
        non_hold = []
        for bar in range(60, 600, 10):
            window = sample_ohlcv_data.iloc[:bar + 1]
            rec = strategy.run(window, _ts())
            if rec.signal != SignalType.HOLD:
                non_hold.append((bar, rec.signal))
        assert len(non_hold) == 0, (
            f"Expected only HOLD in flat warmup, got unexpected signals: {non_hold}"
        )

    def test_fixture_phases_produce_valid_signals(self, strategy, sample_ohlcv_data):
        """Strategy must never raise and must always return a valid SignalType."""
        phases = {
            "bull_run":   sample_ohlcv_data.iloc[600:900],
            "bear_crash": sample_ohlcv_data.iloc[900:],
        }
        for label, slice_df in phases.items():
            rec = strategy.run(slice_df.reset_index(drop=True), _ts())
            assert rec.signal in list(SignalType), (
                f"Invalid signal in phase '{label}': {rec.signal}"
            )


# ---------------------------------------------------------------------------
# 4. Edge cases -- no exception should be raised
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_empty_dataframe_returns_hold(self, strategy):
        empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        rec = strategy.run(empty, _ts())
        assert rec.signal == SignalType.HOLD

    def test_all_nan_close_does_not_raise(self, strategy, sample_ohlcv_data):
        """All-NaN close should not throw -- strategy must handle gracefully."""
        nan_df = sample_ohlcv_data.copy()
        nan_df["close"] = np.nan
        nan_df["open"] = np.nan
        nan_df["high"] = np.nan
        nan_df["low"] = np.nan
        try:
            rec = strategy.run(nan_df, _ts())
            assert isinstance(rec.signal, SignalType)
        except Exception as exc:
            pytest.fail(f"Strategy raised an exception on all-NaN data: {exc}")

    def test_zero_volume_does_not_raise(self, strategy, sample_ohlcv_data):
        """Zero volume (delta=0 everywhere) should not cause division errors."""
        zero_vol_df = sample_ohlcv_data.copy()
        zero_vol_df["volume"] = 0.0
        try:
            rec = strategy.run(zero_vol_df, _ts())
            assert isinstance(rec.signal, SignalType)
        except Exception as exc:
            pytest.fail(f"Strategy raised an exception on zero-volume data: {exc}")

    def test_constant_price_returns_hold(self, strategy, sample_ohlcv_data):
        """Constant price -> no EMA cross -> no trend state -> should return HOLD."""
        flat_df = sample_ohlcv_data.copy()
        flat_df["close"] = 10000.0
        flat_df["open"] = 10000.0
        flat_df["high"] = 10001.0
        flat_df["low"] = 9999.0
        rec = strategy.run(flat_df, _ts())
        assert rec.signal == SignalType.HOLD
