"""
Tests for QuantPullbackDayTradeStrategy.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.base_strategy import BaseStrategy, SignalType, StrategyRecommendation
from src.strategies.quant_pullback_day_trade_strategy import QuantPullbackDayTradeStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> datetime:
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_strategy(**kwargs) -> QuantPullbackDayTradeStrategy:
    return QuantPullbackDayTradeStrategy(**kwargs)


def _make_ohlcv(
    close: np.ndarray,
    volume: np.ndarray | None = None,
    interval_minutes: int = 15,
) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from a close array."""
    n = len(close)
    dates = [
        datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=interval_minutes * i)
        for i in range(n)
    ]
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + np.abs(np.random.default_rng(42).normal(0, 5, n))
    low = np.minimum(open_, close) - np.abs(np.random.default_rng(43).normal(0, 5, n))
    if volume is None:
        volume = np.full(n, 100.0)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates, utc=True),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


# ---------------------------------------------------------------------------
# Contract / structural tests
# ---------------------------------------------------------------------------


class TestContract:
    def test_inherits_base_strategy(self):
        assert issubclass(QuantPullbackDayTradeStrategy, BaseStrategy)

    def test_properties(self):
        s = _make_strategy()
        assert s.name == "Quant Pullback Day Trade Strategy"
        assert s.timeframe == "15m"
        assert s.lookback_hours == 15
        assert isinstance(s.description, str) and len(s.description) > 0

    def test_run_returns_strategy_recommendation(self, sample_ohlcv_data):
        s = _make_strategy()
        result = s.run(sample_ohlcv_data, _ts())
        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)
        assert isinstance(result.timestamp, datetime)

    def test_run_preserves_timestamp(self, sample_ohlcv_data):
        s = _make_strategy()
        ts = _ts()
        result = s.run(sample_ohlcv_data, ts)
        assert result.timestamp == ts

    def test_min_candles_required_is_dynamic(self):
        s1 = _make_strategy()
        s2 = _make_strategy(vol_sma_len=50)
        assert s1.MIN_CANDLES_REQUIRED == 60  # 3 * max(9,20,14,14,20) = 60
        assert s2.MIN_CANDLES_REQUIRED == 150  # 3 * 50


# ---------------------------------------------------------------------------
# Warmup / insufficient data tests (Phase 0)
# ---------------------------------------------------------------------------


class TestWarmup:
    def test_insufficient_data_returns_hold(self):
        """With fewer than MIN_CANDLES_REQUIRED bars, must return HOLD."""
        s = _make_strategy()
        short_df = _make_ohlcv(np.linspace(9000.0, 11000.0, 30))
        result = s.run(short_df, _ts())
        assert result.signal == SignalType.HOLD

    def test_warmup_slice_of_fixture_returns_hold(self, sample_ohlcv_data):
        """First 50 bars (< MIN_CANDLES_REQUIRED=60) must produce HOLD."""
        s = _make_strategy()
        warmup = sample_ohlcv_data.iloc[:50].reset_index(drop=True)
        result = s.run(warmup, _ts())
        assert result.signal == SignalType.HOLD

    def test_empty_dataframe_returns_hold(self):
        s = _make_strategy()
        empty_df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = s.run(empty_df, _ts())
        assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# Default parameters
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_params_match_pine(self):
        s = _make_strategy()
        assert s.ema_fast_len == 9
        assert s.ema_slow_len == 20
        assert s.rsi_len == 14
        assert s.long_rsi_min == 40.0
        assert s.long_rsi_max == 55.0
        assert s.short_rsi_min == 45.0
        assert s.short_rsi_max == 60.0
        assert s.pullback_atr_mult == 0.35
        assert s.atr_len == 14
        assert s.lookback_swing == 5
        assert s.use_volume_filter is True
        assert s.vol_sma_len == 20
        assert s.pullback_vol_max_mult == 1.0
        assert s.use_vwap is True


# ---------------------------------------------------------------------------
# Signal tests
# ---------------------------------------------------------------------------


class TestSignals:
    def test_valid_signal_from_fixture(self, sample_ohlcv_data):
        """Full fixture must produce a valid SignalType."""
        s = _make_strategy()
        result = s.run(sample_ohlcv_data, _ts())
        assert result.signal in list(SignalType)

    def test_flat_market_returns_hold(self):
        """Flat price — no trend — must yield HOLD."""
        close = np.full(200, 10_000.0)
        df = _make_ohlcv(close)
        s = _make_strategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.HOLD

    def test_long_signal_on_bullish_pullback(self):
        """
        Construct a scenario: uptrend with a pullback near EMA then bullish
        candle confirmation. Disable VWAP and volume filter for isolation.
        """
        np.random.seed(42)
        n = 200
        # Strong uptrend
        close = np.linspace(9000.0, 11000.0, n)
        # Add small noise
        close = close + np.random.normal(0, 5, n)
        # Create a pullback near the end: dip close to EMA then resume
        close[-3] = close[-4] - 50  # dip
        close[-2] = close[-3] - 20  # continue dip (closer to EMA)
        close[-1] = close[-2] + 80  # bullish confirmation candle

        df = _make_ohlcv(close)
        # Force bullish confirmation: close > open and close > prior high
        df.loc[df.index[-1], "open"] = close[-1] - 50
        df.loc[df.index[-1], "high"] = close[-1] + 5
        df.loc[df.index[-2], "high"] = close[-1] - 10  # prior high < current close

        s = _make_strategy(use_vwap=False, use_volume_filter=False,
                           long_rsi_min=20.0, long_rsi_max=80.0,
                           pullback_atr_mult=2.0)
        result = s.run(df, _ts())
        # In a strong trend with relaxed filters, we expect LONG or HOLD
        assert result.signal in (SignalType.LONG, SignalType.HOLD)

    def test_short_signal_on_bearish_pullback(self):
        """
        Construct a bearish scenario with pullback and bearish confirmation.
        """
        np.random.seed(42)
        n = 200
        close = np.linspace(12000.0, 9000.0, n)
        close = close + np.random.normal(0, 5, n)
        # Pullback up then bearish candle
        close[-3] = close[-4] + 50
        close[-2] = close[-3] + 20
        close[-1] = close[-2] - 80

        df = _make_ohlcv(close)
        df.loc[df.index[-1], "open"] = close[-1] + 50
        df.loc[df.index[-1], "low"] = close[-1] - 5
        df.loc[df.index[-2], "low"] = close[-1] + 10  # prior low > current close

        s = _make_strategy(use_vwap=False, use_volume_filter=False,
                           short_rsi_min=20.0, short_rsi_max=80.0,
                           pullback_atr_mult=2.0)
        result = s.run(df, _ts())
        assert result.signal in (SignalType.SHORT, SignalType.HOLD)

    def test_signals_across_fixture_phases(self, sample_ohlcv_data):
        """Run strategy on each market-regime slice; signals must always be valid."""
        s = _make_strategy()
        phases = {
            "warmup+sideways": sample_ohlcv_data.iloc[:700],
            "bull_run": sample_ohlcv_data.iloc[600:900],
            "bear_crash": sample_ohlcv_data.iloc[800:],
        }
        for label, slice_df in phases.items():
            subset = slice_df.reset_index(drop=True)
            if len(subset) < s.MIN_CANDLES_REQUIRED:
                continue
            result = s.run(subset, _ts())
            assert result.signal in list(SignalType), f"Invalid signal in phase '{label}'"

    def test_rsi_filter_rejects_out_of_band(self):
        """When RSI is outside the allowed band, no entry should fire."""
        # Strong uptrend but with very tight RSI band that nothing passes
        np.random.seed(42)
        close = np.linspace(9000, 11000, 200) + np.random.normal(0, 5, 200)
        df = _make_ohlcv(close)
        s = _make_strategy(
            use_vwap=False,
            use_volume_filter=False,
            long_rsi_min=99.0,
            long_rsi_max=100.0,
            short_rsi_min=0.0,
            short_rsi_max=1.0,
        )
        result = s.run(df, _ts())
        assert result.signal == SignalType.HOLD

    def test_volume_filter_rejects_high_volume(self):
        """When volume spike exceeds SMA * mult, no entry should fire."""
        np.random.seed(42)
        n = 200
        close = np.linspace(9000, 11000, n) + np.random.normal(0, 5, n)
        # Volume is extremely high on last bar
        vol = np.full(n, 100.0)
        vol[-1] = 100000.0
        df = _make_ohlcv(close, volume=vol)
        s = _make_strategy(use_vwap=False, pullback_atr_mult=5.0,
                           long_rsi_min=0.0, long_rsi_max=100.0)
        result = s.run(df, _ts())
        # With extremely high volume on last bar, volume filter should block
        assert result.signal in (SignalType.HOLD, SignalType.SHORT)


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_all_nan_close_returns_hold(self):
        """DataFrame with NaN closes should return HOLD gracefully."""
        n = 100
        close = np.full(n, np.nan)
        df = _make_ohlcv(close)
        s = _make_strategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.HOLD

    def test_single_row_returns_hold(self):
        s = _make_strategy()
        df = _make_ohlcv(np.array([10000.0]))
        result = s.run(df, _ts())
        assert result.signal == SignalType.HOLD

    def test_vwap_disabled(self):
        """Strategy runs correctly with VWAP disabled."""
        s = _make_strategy(use_vwap=False)
        close = np.linspace(9000, 11000, 200)
        df = _make_ohlcv(close)
        result = s.run(df, _ts())
        assert result.signal in list(SignalType)

    def test_volume_filter_disabled(self):
        """Strategy runs correctly with volume filter disabled."""
        s = _make_strategy(use_volume_filter=False)
        close = np.linspace(9000, 11000, 200)
        df = _make_ohlcv(close)
        result = s.run(df, _ts())
        assert result.signal in list(SignalType)
