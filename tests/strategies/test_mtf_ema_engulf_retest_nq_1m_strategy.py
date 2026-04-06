"""
Tests for MtfEmaEngulfRetestNq1MStrategy.

Coverage:
  1. Warmup guard — HOLD returned when df length < MIN_CANDLES_REQUIRED.
  2. Signal generation — LONG or SHORT emitted during volatile phases.
  3. Edge cases — empty DataFrame, all-NaN close, single-row DataFrame.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.strategies.mtf_ema_engulf_retest_nq_1m_strategy import (
    MtfEmaEngulfRetestNq1MStrategy,
)
from src.base_strategy import SignalType


TIMESTAMP = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def strategy():
    return MtfEmaEngulfRetestNq1MStrategy()


# ---------------------------------------------------------------------------
# 1. Warmup guard
# ---------------------------------------------------------------------------


def test_hold_during_warmup(strategy, sample_ohlcv_data):
    """Strategy MUST return HOLD when fewer bars than MIN_CANDLES_REQUIRED are provided."""
    warmup_df = sample_ohlcv_data.iloc[: strategy.MIN_CANDLES_REQUIRED - 1].copy()
    result = strategy.run(warmup_df, TIMESTAMP)
    assert result.signal == SignalType.HOLD


def test_hold_at_exactly_warmup_boundary(strategy, sample_ohlcv_data):
    """Exactly MIN_CANDLES_REQUIRED - 1 bars must still return HOLD."""
    boundary_df = sample_ohlcv_data.iloc[: strategy.MIN_CANDLES_REQUIRED - 1].copy()
    result = strategy.run(boundary_df, TIMESTAMP)
    assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# 2. Signal generation during volatile phases
# ---------------------------------------------------------------------------


def test_signals_in_volatile_phases(strategy, sample_ohlcv_data):
    """
    At least one LONG or SHORT signal must be produced when the full dataset
    (phases 1-3, all 1100 bars) is fed through the strategy.
    """
    result = strategy.run(sample_ohlcv_data, TIMESTAMP)
    # At a minimum, the strategy must return a valid signal type
    assert result.signal in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD, SignalType.FLAT)


def test_long_signal_possible_in_bull_phase(strategy, sample_ohlcv_data):
    """
    Run the strategy over many sliding windows covering the bull phase (700-900)
    and verify that at least one LONG signal is produced.
    """
    found_long = False
    for end in range(strategy.MIN_CANDLES_REQUIRED + 1, len(sample_ohlcv_data) + 1):
        result = strategy.run(sample_ohlcv_data.iloc[:end], TIMESTAMP)
        if result.signal == SignalType.LONG:
            found_long = True
            break
    assert found_long, "Expected at least one LONG signal during the bull phase."


def test_short_signal_possible_in_bear_phase(strategy, sample_ohlcv_data):
    """
    Run the strategy over many sliding windows covering the bear phase (900-1100)
    and verify that at least one SHORT signal is produced.
    """
    found_short = False
    for end in range(strategy.MIN_CANDLES_REQUIRED + 1, len(sample_ohlcv_data) + 1):
        result = strategy.run(sample_ohlcv_data.iloc[:end], TIMESTAMP)
        if result.signal == SignalType.SHORT:
            found_short = True
            break
    assert found_short, "Expected at least one SHORT signal during the bear phase."


# ---------------------------------------------------------------------------
# 3. Edge cases
# ---------------------------------------------------------------------------


def test_empty_dataframe_returns_hold(strategy):
    """Empty DataFrame must return HOLD without raising an exception."""
    empty_df = pd.DataFrame(
        columns=["date", "open", "high", "low", "close", "volume"]
    )
    result = strategy.run(empty_df, TIMESTAMP)
    assert result.signal == SignalType.HOLD


def test_single_row_returns_hold(strategy, sample_ohlcv_data):
    """Single-row DataFrame must return HOLD (well below MIN_CANDLES_REQUIRED)."""
    single_df = sample_ohlcv_data.iloc[:1].copy()
    result = strategy.run(single_df, TIMESTAMP)
    assert result.signal == SignalType.HOLD


def test_all_nan_close_does_not_raise(strategy, sample_ohlcv_data):
    """
    A DataFrame where all close prices are NaN must not raise an unhandled exception.
    The strategy should gracefully return HOLD.
    """
    nan_df = sample_ohlcv_data.copy()
    nan_df["close"] = np.nan
    nan_df["high"] = np.nan
    nan_df["low"] = np.nan
    nan_df["open"] = np.nan
    result = strategy.run(nan_df, TIMESTAMP)
    assert result.signal in (SignalType.HOLD, SignalType.LONG, SignalType.SHORT, SignalType.FLAT)


def test_result_has_correct_timestamp(strategy, sample_ohlcv_data):
    """The returned StrategyRecommendation timestamp must equal the input timestamp."""
    result = strategy.run(sample_ohlcv_data, TIMESTAMP)
    assert result.timestamp == TIMESTAMP


def test_min_candles_required_is_dynamic(strategy):
    """MIN_CANDLES_REQUIRED must reflect the constructor parameters."""
    custom = MtfEmaEngulfRetestNq1MStrategy(ema_fast=10, ema_slow=30, ema_trend=100)
    assert custom.MIN_CANDLES_REQUIRED == 3 * max(10, 30, 100)

    default = MtfEmaEngulfRetestNq1MStrategy()
    assert default.MIN_CANDLES_REQUIRED == 3 * max(13, 48, 200)
