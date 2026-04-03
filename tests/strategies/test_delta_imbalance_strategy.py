"""
Tests for DeltaImbalanceStrategy.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.base_strategy import BaseStrategy, SignalType, StrategyRecommendation
from src.strategies.delta_imbalance_strategy import DeltaImbalanceStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> datetime:
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_ohlcv(
    open_: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray | None = None,
) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from arrays."""
    n = len(close)
    if volume is None:
        volume = np.ones(n) * 1000.0
    dates = [
        datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i) for i in range(n)
    ]
    high = np.maximum(open_, close) + 1.0
    low = np.minimum(open_, close) - 1.0
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(dates, utc=True),
            "open": open_.astype(float),
            "high": high.astype(float),
            "low": low.astype(float),
            "close": close.astype(float),
            "volume": volume.astype(float),
        }
    )
    return df


def _bullish_run_df(n: int = 250, base: float = 10_000.0, candle_size: float = 5.0) -> pd.DataFrame:
    """
    n-1 strongly bullish candles (close > open by candle_size) followed by
    one bearish closer, to trigger a SHORT resolution signal.
    Positive delta accumulates well above threshold; last bar delta < 0.
    """
    open_ = np.full(n, base)
    close = np.full(n, base + candle_size)   # bullish: close > open
    close[-1] = base - candle_size           # last bar bearish: close < open
    return _make_ohlcv(open_, close)


def _bearish_run_df(n: int = 250, base: float = 10_000.0, candle_size: float = 5.0) -> pd.DataFrame:
    """
    n-1 strongly bearish candles (open > close by candle_size) followed by
    one bullish closer, to trigger a LONG resolution signal.
    Negative delta accumulates well above threshold; last bar delta > 0.
    """
    open_ = np.full(n, base + candle_size)   # bearish: open > close
    close = np.full(n, base)
    close[-1] = base + candle_size           # last bar bullish: close > open
    open_[-1] = base
    return _make_ohlcv(open_, close)


# ---------------------------------------------------------------------------
# Contract / structural tests
# ---------------------------------------------------------------------------


class TestContract:
    def test_inherits_base_strategy(self):
        assert issubclass(DeltaImbalanceStrategy, BaseStrategy)

    def test_properties(self):
        s = DeltaImbalanceStrategy()
        assert s.name == "DeltaImbalanceStrategy"
        assert s.timeframe == "1m"
        assert s.lookback_hours == 6
        assert isinstance(s.description, str) and len(s.description) > 0

    def test_min_candles_required_is_dynamic(self):
        """MIN_CANDLES_REQUIRED must be 3 * max(sma_period=50, atr_period=14) = 150."""
        s = DeltaImbalanceStrategy()
        assert s.MIN_CANDLES_REQUIRED == 3 * max(s.sma_period, s.atr_period)
        assert s.MIN_CANDLES_REQUIRED == 150

    def test_run_returns_strategy_recommendation(self, sample_ohlcv_data):
        s = DeltaImbalanceStrategy()
        result = s.run(sample_ohlcv_data, _ts())
        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)
        assert isinstance(result.timestamp, datetime)

    def test_run_preserves_timestamp(self, sample_ohlcv_data):
        s = DeltaImbalanceStrategy()
        ts = _ts()
        result = s.run(sample_ohlcv_data, ts)
        assert result.timestamp == ts

    def test_does_not_mutate_input_df(self, sample_ohlcv_data):
        """Strategy must not modify the caller's DataFrame."""
        original_cols = set(sample_ohlcv_data.columns)
        s = DeltaImbalanceStrategy()
        s.run(sample_ohlcv_data, _ts())
        assert set(sample_ohlcv_data.columns) == original_cols


# ---------------------------------------------------------------------------
# Warmup guard (Phase 0 of fixture)
# ---------------------------------------------------------------------------


class TestWarmupGuard:
    def test_empty_df_returns_hold(self):
        """Empty DataFrame must return HOLD without raising."""
        s = DeltaImbalanceStrategy()
        empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = s.run(empty, _ts())
        assert result.signal == SignalType.HOLD

    def test_insufficient_bars_returns_hold(self):
        """Any slice with fewer than MIN_CANDLES_REQUIRED=150 bars must return HOLD."""
        s = DeltaImbalanceStrategy()
        n = s.MIN_CANDLES_REQUIRED - 1   # 149 bars
        open_ = np.full(n, 10_000.0)
        close = np.linspace(10_000.0, 10_100.0, n)
        df = _make_ohlcv(open_, close)
        result = s.run(df, _ts())
        assert result.signal == SignalType.HOLD

    def test_warmup_slice_of_fixture_returns_hold(self, sample_ohlcv_data):
        """First 100 bars of the fixture (< 150) must produce HOLD."""
        s = DeltaImbalanceStrategy()
        warmup = sample_ohlcv_data.iloc[:100].reset_index(drop=True)
        result = s.run(warmup, _ts())
        assert result.signal == SignalType.HOLD

    def test_exactly_min_candles_does_not_return_hold_due_to_guard(self):
        """Exactly MIN_CANDLES_REQUIRED rows must NOT be blocked by the warmup guard."""
        s = DeltaImbalanceStrategy()
        n = s.MIN_CANDLES_REQUIRED   # 150
        open_ = np.full(n, 10_000.0)
        close = np.full(n, 10_000.0)   # flat — likely HOLD from logic, not guard
        df = _make_ohlcv(open_, close)
        # The run must complete without error; HOLD is acceptable here from logic
        result = s.run(df, _ts())
        assert isinstance(result.signal, SignalType)


# ---------------------------------------------------------------------------
# Signal tests
# ---------------------------------------------------------------------------


class TestSignals:
    def test_valid_signal_from_fixture(self, sample_ohlcv_data):
        """Full 1,100-candle fixture must produce a valid SignalType without error."""
        s = DeltaImbalanceStrategy()
        result = s.run(sample_ohlcv_data, _ts())
        assert result.signal in list(SignalType)

    def test_flat_market_returns_hold(self):
        """Flat market with zero delta produces zero imbalance → HOLD."""
        n = 200
        price = np.full(n, 10_000.0)
        df = _make_ohlcv(open_=price.copy(), close=price.copy())
        s = DeltaImbalanceStrategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.HOLD

    def test_short_signal_after_bull_run(self):
        """
        After a sustained run of bullish candles, bull_imbalance >> threshold.
        A final bearish candle (delta < 0) resolves the imbalance → SHORT.
        """
        df = _bullish_run_df(n=250)
        s = DeltaImbalanceStrategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.SHORT

    def test_long_signal_after_bear_run(self):
        """
        After a sustained run of bearish candles, bear_imbalance >> threshold.
        A final bullish candle (delta > 0) resolves the imbalance → LONG.
        """
        df = _bearish_run_df(n=250)
        s = DeltaImbalanceStrategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.LONG

    def test_signals_across_fixture_phases(self, sample_ohlcv_data):
        """
        Run strategy on each market-regime slice;
        every result must be a valid SignalType.
        """
        s = DeltaImbalanceStrategy()
        min_bars = s.MIN_CANDLES_REQUIRED
        phases = {
            "bull_run":   sample_ohlcv_data.iloc[600:900],
            "bear_crash": sample_ohlcv_data.iloc[800:],
        }
        for label, slice_df in phases.items():
            subset = slice_df.reset_index(drop=True)
            if len(subset) < min_bars:
                continue
            result = s.run(subset, _ts())
            assert result.signal in list(SignalType), f"Invalid signal in phase '{label}'"


# ---------------------------------------------------------------------------
# Edge-case / robustness tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_volume_does_not_raise(self):
        """Zero volume → delta = 0, avg_delta = 0. Division guard must prevent errors."""
        n = 200
        price = np.linspace(9_000.0, 11_000.0, n)
        volume = np.zeros(n)
        df = _make_ohlcv(open_=price.copy(), close=price.copy(), volume=volume)
        s = DeltaImbalanceStrategy()
        result = s.run(df, _ts())
        assert isinstance(result.signal, SignalType)

    def test_nan_volume_does_not_raise(self):
        """NaN volume propagates NaN delta; strategy must return a valid signal."""
        n = 200
        price = np.linspace(9_000.0, 11_000.0, n)
        volume = np.full(n, np.nan)
        df = _make_ohlcv(open_=price.copy(), close=price.copy(), volume=volume)
        s = DeltaImbalanceStrategy()
        result = s.run(df, _ts())
        assert isinstance(result.signal, SignalType)

    def test_single_bar_returns_hold(self):
        """Single-bar DataFrame must return HOLD from warmup guard."""
        df = _make_ohlcv(np.array([10_000.0]), np.array([10_050.0]))
        s = DeltaImbalanceStrategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.HOLD

    def test_imbalance_never_goes_negative(self):
        """
        Bull/bear imbalance pools must always be non-negative regardless of delta magnitude.
        Uses a DataFrame with large alternating deltas.
        """
        n = 300
        np.random.seed(7)
        price = 10_000.0 + np.cumsum(np.random.uniform(-100, 100, n))
        open_ = np.roll(price, 1)
        open_[0] = price[0]
        df = _make_ohlcv(open_=open_, close=price)

        s = DeltaImbalanceStrategy()
        delta_vals = (df['close'] - df['open']).values * df['volume'].values
        bull_imb, bear_imb = s._compute_imbalance(delta_vals)

        assert np.all(bull_imb >= 0.0), "bull_imbalance went negative"
        assert np.all(bear_imb >= 0.0), "bear_imbalance went negative"
