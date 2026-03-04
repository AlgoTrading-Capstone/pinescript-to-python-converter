"""
Tests for SimpleMACrossoverStrategy
=====================================
Covers:
  1. Initialization — verify strategy metadata attributes.
  2. Smoke / execution test — run() on the shared fixture returns a valid StrategyRecommendation.
  3. Data integrity — run() does not mutate the input DataFrame.
  4. Signal logic (crossover) — synthetic prices that force SMA-9 to cross above SMA-21 → LONG.
  5. Signal logic (crossunder) — synthetic prices that force SMA-9 to cross below SMA-21 → SHORT.
"""

import pandas as pd
import numpy as np
import pytest
from datetime import datetime, timezone

from src.strategies.simple_ma_crossover import SimpleMACrossoverStrategy
from src.base_strategy import StrategyRecommendation, SignalType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(close_prices) -> pd.DataFrame:
    """
    Build a minimal OHLCV DataFrame from a list/array of close prices.
    Uses a fixed UTC start date with 1-hour spacing.
    """
    n = len(close_prices)
    dates = pd.date_range(start="2024-01-01 00:00:00", periods=n, freq="1h", tz="UTC")
    close = np.array(close_prices, dtype=float)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + 1.0
    low = np.minimum(open_, close) - 1.0
    volume = np.ones(n) * 1000.0
    return pd.DataFrame(
        {
            "date": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


# ---------------------------------------------------------------------------
# Test 1 — Initialization
# ---------------------------------------------------------------------------

class TestInitialization:
    def test_name(self):
        strategy = SimpleMACrossoverStrategy()
        assert strategy.name == "Simple MA Crossover Strategy"

    def test_timeframe(self):
        strategy = SimpleMACrossoverStrategy()
        assert strategy.timeframe == "1h"

    def test_lookback_hours(self):
        strategy = SimpleMACrossoverStrategy()
        assert strategy.lookback_hours == 100

    def test_short_length(self):
        strategy = SimpleMACrossoverStrategy()
        assert strategy.short_length == 9

    def test_long_length(self):
        strategy = SimpleMACrossoverStrategy()
        assert strategy.long_length == 21


# ---------------------------------------------------------------------------
# Test 2 — Smoke / execution test
# ---------------------------------------------------------------------------

class TestExecution:
    def test_run_returns_strategy_recommendation(self, sample_ohlcv_data):
        strategy = SimpleMACrossoverStrategy()
        timestamp = sample_ohlcv_data.iloc[-1]["date"]
        result = strategy.run(sample_ohlcv_data, timestamp)
        assert isinstance(result, StrategyRecommendation)

    def test_run_returns_valid_signal_type(self, sample_ohlcv_data):
        strategy = SimpleMACrossoverStrategy()
        timestamp = sample_ohlcv_data.iloc[-1]["date"]
        result = strategy.run(sample_ohlcv_data, timestamp)
        assert result.signal in list(SignalType)

    def test_run_returns_correct_timestamp(self, sample_ohlcv_data):
        strategy = SimpleMACrossoverStrategy()
        timestamp = sample_ohlcv_data.iloc[-1]["date"]
        result = strategy.run(sample_ohlcv_data, timestamp)
        assert result.timestamp == timestamp


# ---------------------------------------------------------------------------
# Test 3 — Data integrity
# ---------------------------------------------------------------------------

class TestDataIntegrity:
    EXPECTED_COLUMNS = {"date", "open", "high", "low", "close", "volume"}

    def test_input_dataframe_columns_unchanged(self, sample_ohlcv_data):
        strategy = SimpleMACrossoverStrategy()
        timestamp = sample_ohlcv_data.iloc[-1]["date"]
        strategy.run(sample_ohlcv_data, timestamp)
        assert set(sample_ohlcv_data.columns) == self.EXPECTED_COLUMNS

    def test_input_dataframe_row_count_unchanged(self, sample_ohlcv_data):
        strategy = SimpleMACrossoverStrategy()
        timestamp = sample_ohlcv_data.iloc[-1]["date"]
        original_len = len(sample_ohlcv_data)
        strategy.run(sample_ohlcv_data, timestamp)
        assert len(sample_ohlcv_data) == original_len

    def test_input_dataframe_close_values_unchanged(self, sample_ohlcv_data):
        strategy = SimpleMACrossoverStrategy()
        timestamp = sample_ohlcv_data.iloc[-1]["date"]
        close_before = sample_ohlcv_data["close"].copy()
        strategy.run(sample_ohlcv_data, timestamp)
        pd.testing.assert_series_equal(sample_ohlcv_data["close"], close_before)

    def test_no_extra_columns_injected(self, sample_ohlcv_data):
        strategy = SimpleMACrossoverStrategy()
        timestamp = sample_ohlcv_data.iloc[-1]["date"]
        cols_before = list(sample_ohlcv_data.columns)
        strategy.run(sample_ohlcv_data, timestamp)
        assert list(sample_ohlcv_data.columns) == cols_before


# ---------------------------------------------------------------------------
# Test 4 — Signal logic: LONG (crossover — SMA-9 crosses above SMA-21)
# ---------------------------------------------------------------------------

class TestLongSignal:
    def _build_crossover_df(self) -> pd.DataFrame:
        """
        Construct a 30-row synthetic close price series that guarantees
        SMA-9 crosses above SMA-21 on the final bar only.

        Strategy:
          - Rows 0-28: flat at 100 — both SMAs settle to 100.
          - Row 29 (last bar): spikes to 500.
            SMA-9 (8×100 + 1×500)/9 ≈ 144 > SMA-21 (20×100 + 1×500)/21 ≈ 119.
            Previous bar had SMA-9 = SMA-21 = 100, satisfying the <= guard.
        """
        prices = [100.0] * 29 + [500.0]  # total 30 rows; spike only on last bar
        return _make_df(prices)

    def test_long_signal_returned(self):
        strategy = SimpleMACrossoverStrategy()
        df = self._build_crossover_df()
        timestamp = df.iloc[-1]["date"]
        result = strategy.run(df, timestamp)
        assert result.signal == SignalType.LONG

    def test_long_signal_timestamp_matches(self):
        strategy = SimpleMACrossoverStrategy()
        df = self._build_crossover_df()
        timestamp = df.iloc[-1]["date"]
        result = strategy.run(df, timestamp)
        assert result.timestamp == timestamp


# ---------------------------------------------------------------------------
# Test 5 — Signal logic: SHORT (crossunder — SMA-9 crosses below SMA-21)
# ---------------------------------------------------------------------------

class TestShortSignal:
    def _build_crossunder_df(self) -> pd.DataFrame:
        """
        Construct a 30-row synthetic close price series that guarantees
        SMA-9 crosses below SMA-21 on the final bar only.

        Strategy:
          - Rows 0-28: flat at 500 — both SMAs settle to 500.
          - Row 29 (last bar): drops to 100.
            SMA-9 (8×500 + 1×100)/9 ≈ 456 < SMA-21 (20×500 + 1×100)/21 ≈ 481.
            Previous bar had SMA-9 = SMA-21 = 500, satisfying the >= guard.
        """
        prices = [500.0] * 29 + [100.0]  # total 30 rows; drop only on last bar
        return _make_df(prices)

    def test_short_signal_returned(self):
        strategy = SimpleMACrossoverStrategy()
        df = self._build_crossunder_df()
        timestamp = df.iloc[-1]["date"]
        result = strategy.run(df, timestamp)
        assert result.signal == SignalType.SHORT

    def test_short_signal_timestamp_matches(self):
        strategy = SimpleMACrossoverStrategy()
        df = self._build_crossunder_df()
        timestamp = df.iloc[-1]["date"]
        result = strategy.run(df, timestamp)
        assert result.timestamp == timestamp
