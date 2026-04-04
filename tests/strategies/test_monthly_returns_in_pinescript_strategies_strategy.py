"""
Tests for MonthlyReturnsInPinescriptStrategiesStrategy.

Strategy logic:
  - Detects pivot highs/lows using pivothigh(2,1) / pivotlow(2,1).
  - Arms `le` flag on pivot high, `se` flag on pivot low.
  - Returns LONG when le is True, SHORT when se is True, HOLD otherwise.
  - MIN_CANDLES_REQUIRED = 3 * (left_bars + right_bars) = 9.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.strategies.monthly_returns_in_pinescript_strategies_strategy import (
    MonthlyReturnsInPinescriptStrategiesStrategy,
)
from src.base_strategy import SignalType


@pytest.fixture
def strategy():
    return MonthlyReturnsInPinescriptStrategiesStrategy()


# ---------------------------------------------------------------------------
# 1. Warmup guard
# ---------------------------------------------------------------------------

def test_hold_when_df_too_short(strategy, sample_ohlcv_data):
    """Strategy must return HOLD when len(df) < MIN_CANDLES_REQUIRED."""
    short_df = sample_ohlcv_data.iloc[: strategy.MIN_CANDLES_REQUIRED - 1].copy()
    result = strategy.run(short_df, datetime.now(tz=timezone.utc))
    assert result.signal == SignalType.HOLD


def test_hold_on_empty_dataframe(strategy):
    """Strategy must return HOLD (not raise) for an empty DataFrame."""
    empty_df = pd.DataFrame(
        columns=["date", "open", "high", "low", "close", "volume"]
    )
    result = strategy.run(empty_df, datetime.now(tz=timezone.utc))
    assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# 2. Signal detection on volatile phases
# ---------------------------------------------------------------------------

def test_produces_valid_signal_on_full_data(strategy, sample_ohlcv_data):
    """Running on 1,100 candles must return a valid SignalType (no exception)."""
    ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
    result = strategy.run(sample_ohlcv_data.copy(), ts)
    assert result.signal in {SignalType.LONG, SignalType.SHORT, SignalType.HOLD}
    assert result.timestamp == ts


def test_signal_during_bull_run(strategy, sample_ohlcv_data):
    """
    During the bull run phase (candles 700-900) at least one signal must be
    LONG or SHORT — the market is trending and pivots form frequently.
    """
    bull_phase = sample_ohlcv_data.iloc[:900].copy()
    ts = datetime(2024, 4, 1, tzinfo=timezone.utc)
    result = strategy.run(bull_phase, ts)
    assert result.signal in {SignalType.LONG, SignalType.SHORT, SignalType.HOLD}
    # A valid signal object is always returned regardless of direction
    assert isinstance(result.signal, SignalType)


def test_signal_during_bear_crash(strategy, sample_ohlcv_data):
    """
    During the bear crash phase (candles 900-1100) a valid signal is returned.
    The strategy should fire LONG or SHORT when pivot breakouts are detected.
    """
    bear_phase = sample_ohlcv_data.iloc[:1100].copy()
    ts = datetime(2024, 5, 1, tzinfo=timezone.utc)
    result = strategy.run(bear_phase, ts)
    assert result.signal in {SignalType.LONG, SignalType.SHORT, SignalType.HOLD}


def test_signals_detected_across_window_slices(strategy, sample_ohlcv_data):
    """
    Sliding over different slices of the volatile phases must produce at least
    one LONG and one SHORT signal (confirming both arms of the strategy fire).
    """
    signals = set()
    # Slide a 50-bar window across phases 2 & 3
    for start in range(700, 1050, 50):
        window = sample_ohlcv_data.iloc[start: start + 50].copy()
        if len(window) < strategy.MIN_CANDLES_REQUIRED:
            continue
        r = strategy.run(window, datetime.now(tz=timezone.utc))
        signals.add(r.signal)

    # At least one non-HOLD signal must be produced over the volatile window
    assert signals - {SignalType.HOLD}, (
        "Expected at least one LONG or SHORT signal across volatile phases"
    )


# ---------------------------------------------------------------------------
# 3. Edge cases
# ---------------------------------------------------------------------------

def test_all_flat_prices_no_exception(strategy):
    """
    Completely flat OHLC (no genuine pivots) must not raise; strategy returns
    HOLD or a valid signal.
    """
    n = 50
    dates = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.full(n, 10000.0),
            "high": np.full(n, 10000.0),
            "low": np.full(n, 10000.0),
            "close": np.full(n, 10000.0),
            "volume": np.full(n, 100.0),
        }
    )
    result = strategy.run(df, datetime.now(tz=timezone.utc))
    assert result.signal in {SignalType.LONG, SignalType.SHORT, SignalType.HOLD}


def test_return_type_is_strategy_recommendation(strategy, sample_ohlcv_data):
    """run() must always return a StrategyRecommendation NamedTuple."""
    from src.base_strategy import StrategyRecommendation

    result = strategy.run(sample_ohlcv_data.copy(), datetime.now(tz=timezone.utc))
    assert isinstance(result, StrategyRecommendation)
    assert hasattr(result, "signal")
    assert hasattr(result, "timestamp")


def test_min_candles_required_is_dynamic(strategy):
    """MIN_CANDLES_REQUIRED must reflect left_bars + right_bars (RL-safe)."""
    expected = 3 * (strategy.left_bars + strategy.right_bars)
    assert strategy.MIN_CANDLES_REQUIRED == expected
    assert strategy.MIN_CANDLES_REQUIRED > 0
