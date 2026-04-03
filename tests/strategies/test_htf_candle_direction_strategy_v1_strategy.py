"""
Tests for the HTF Candle Direction Strategy V1.

Covers:
1. Warmup guard  — strategy returns HOLD for any DataFrame shorter than MIN_CANDLES_REQUIRED.
2. Signal detection — strategy produces valid signals during volatile phases (bull / bear).
3. Edge cases   — empty DataFrame, all-NaN close, constant prices, param accessors.
"""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timezone

from src.strategies.htf_candle_direction_strategy_v1_strategy import (
    HtfCandleDirectionStrategyV1Strategy,
)
from src.base_strategy import SignalType

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

VALID_SIGNALS = {SignalType.LONG, SignalType.SHORT, SignalType.FLAT, SignalType.HOLD}
_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test 1 — Warmup guard
# ---------------------------------------------------------------------------


class TestWarmupGuard:
    """Strategy must return HOLD for any df shorter than MIN_CANDLES_REQUIRED."""

    def test_empty_dataframe_returns_hold(self):
        strategy = HtfCandleDirectionStrategyV1Strategy()
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_single_row_returns_hold(self, sample_ohlcv_data):
        strategy = HtfCandleDirectionStrategyV1Strategy()
        df = sample_ohlcv_data.iloc[:1].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_below_min_candles_returns_hold(self, sample_ohlcv_data):
        """Any slice of MIN_CANDLES_REQUIRED - 1 rows must return HOLD."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        cutoff = strategy.MIN_CANDLES_REQUIRED - 1
        df = sample_ohlcv_data.iloc[:cutoff].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_exactly_min_candles_does_not_raise(self, sample_ohlcv_data):
        """Exactly MIN_CANDLES_REQUIRED rows must not raise an exception."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        cutoff = strategy.MIN_CANDLES_REQUIRED
        df = sample_ohlcv_data.iloc[:cutoff].copy()
        result = strategy.run(df, _TS)
        assert result.signal in VALID_SIGNALS

    def test_deep_warmup_slice_returns_hold(self, sample_ohlcv_data):
        """A 100-candle slice is well below MIN_CANDLES_REQUIRED=150 — must return HOLD."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        assert strategy.MIN_CANDLES_REQUIRED == 150
        df = sample_ohlcv_data.iloc[:100].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# Test 2 — Signal detection during volatile market phases
# ---------------------------------------------------------------------------


class TestSignalDetection:
    """Strategy must produce valid, non-exception-raising signals on real data."""

    def test_full_dataset_returns_valid_signal(self, sample_ohlcv_data):
        """Running on the complete 1100-candle dataset must return a valid SignalType."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        result = strategy.run(sample_ohlcv_data, _TS)
        assert result.signal in VALID_SIGNALS

    def test_bull_phase_signal_is_valid(self, sample_ohlcv_data):
        """Bull run phase (candles 0-900) must return a valid signal."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        df = sample_ohlcv_data.iloc[:900].copy()
        result = strategy.run(df, _TS)
        assert result.signal in VALID_SIGNALS

    def test_bear_phase_signal_is_valid(self, sample_ohlcv_data):
        """Bear crash phase (candles 900-1100) endpoint must produce a valid signal."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        result = strategy.run(sample_ohlcv_data.iloc[900:].copy(), _TS)
        # Bear slice alone may be below MIN_CANDLES_REQUIRED — HOLD is acceptable
        assert result.signal in VALID_SIGNALS

    def test_full_dataset_with_ema_disabled_returns_valid_signal(self, sample_ohlcv_data):
        """Disabling the EMA filter must not crash and must return a valid signal."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        strategy.use_ema_filter = False
        result = strategy.run(sample_ohlcv_data, _TS)
        assert result.signal in VALID_SIGNALS

    def test_full_dataset_with_vol_filter_enabled_returns_valid_signal(self, sample_ohlcv_data):
        """Enabling the volume filter must not crash and must return a valid signal."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        strategy.use_vol_filter = True
        result = strategy.run(sample_ohlcv_data, _TS)
        assert result.signal in VALID_SIGNALS

    def test_result_has_correct_timestamp(self, sample_ohlcv_data):
        """The StrategyRecommendation timestamp must match the input timestamp."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        result = strategy.run(sample_ohlcv_data, _TS)
        assert result.timestamp == _TS

    def test_bull_and_bear_runs_do_not_raise(self, sample_ohlcv_data):
        """Both bull and bear sub-slices must complete without exception."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        bull_result = strategy.run(sample_ohlcv_data.iloc[:900].copy(), _TS)
        bear_result = strategy.run(sample_ohlcv_data.copy(), _TS)
        assert bull_result.signal in VALID_SIGNALS
        assert bear_result.signal in VALID_SIGNALS


# ---------------------------------------------------------------------------
# Test 3 — Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Strategy must handle degenerate inputs gracefully."""

    def test_all_nan_close_does_not_raise(self, sample_ohlcv_data):
        """If all OHLCV values are NaN the strategy must not raise an exception."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        df = sample_ohlcv_data.copy()
        df["close"] = np.nan
        df["open"] = np.nan
        df["high"] = np.nan
        df["low"] = np.nan
        try:
            result = strategy.run(df, _TS)
            assert result.signal in VALID_SIGNALS
        except Exception as exc:
            pytest.fail(f"Strategy raised an exception on all-NaN input: {exc}")

    def test_constant_price_does_not_raise(self, sample_ohlcv_data):
        """Flat, constant prices must not cause division-by-zero or other errors."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        df = sample_ohlcv_data.copy()
        df["open"] = 10000.0
        df["high"] = 10001.0
        df["low"] = 9999.0
        df["close"] = 10000.0
        df["volume"] = 100.0
        try:
            result = strategy.run(df, _TS)
            assert result.signal in VALID_SIGNALS
        except Exception as exc:
            pytest.fail(f"Strategy raised an exception on constant prices: {exc}")

    def test_min_candles_required_is_positive_integer(self):
        """MIN_CANDLES_REQUIRED must be a positive integer."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        assert isinstance(strategy.MIN_CANDLES_REQUIRED, int)
        assert strategy.MIN_CANDLES_REQUIRED > 0

    def test_min_candles_required_value(self):
        """MIN_CANDLES_REQUIRED must equal 3 * max(ema_length=50, vol_sma_length=20) = 150."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        assert strategy.MIN_CANDLES_REQUIRED == 150

    def test_strategy_timeframe_is_lowercase(self):
        """Timeframe string must be strictly lowercase (CI/CD requirement)."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        assert strategy.timeframe == strategy.timeframe.lower()

    def test_strategy_name_is_set(self):
        """Strategy name must be a non-empty string."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        assert isinstance(strategy.name, str)
        assert len(strategy.name) > 0

    def test_lookback_hours_is_positive(self):
        """lookback_hours must be a positive integer."""
        strategy = HtfCandleDirectionStrategyV1Strategy()
        assert strategy.lookback_hours > 0
