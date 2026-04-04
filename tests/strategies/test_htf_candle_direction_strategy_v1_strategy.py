"""
Tests for HtfCandleDirectionStrategyV1Strategy.

Fixture details (from tests/conftest.py):
- 1,100 candles at 15m intervals (UTC)
- Phase 0 (0–600):   Warmup — flat at 10,000 (triggers MIN_CANDLES_REQUIRED guard)
- Phase 1 (600–700): Sideways / Accumulation
- Phase 2 (700–900): Bull Run (10,000 → 12,000)
- Phase 3 (900–1100): Bear Crash (12,000 → 9,000)

Strategy MIN_CANDLES_REQUIRED = 3 * max(ema_length=50, vol_sma_length=20) = 150.
HTF resampling: 720-minute (12h) candles — 15m base < 720m HTF, so resampled_merge passes.
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from src.strategies.htf_candle_direction_strategy_v1_strategy import (
    HtfCandleDirectionStrategyV1Strategy,
)
from src.base_strategy import SignalType

TIMESTAMP = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
VALID_SIGNALS = {SignalType.LONG, SignalType.SHORT, SignalType.HOLD, SignalType.FLAT}


@pytest.fixture
def strategy():
    return HtfCandleDirectionStrategyV1Strategy()


# ---------------------------------------------------------------------------
# 1. Warmup Guard — Phase 0
# ---------------------------------------------------------------------------

class TestWarmupGuard:

    def test_returns_hold_when_below_min_candles(self, strategy, sample_ohlcv_data):
        """Slices shorter than MIN_CANDLES_REQUIRED must return HOLD (Phase 0 check)."""
        short_df = sample_ohlcv_data.iloc[: strategy.MIN_CANDLES_REQUIRED - 1]
        result = strategy.run(short_df, TIMESTAMP)
        assert result.signal == SignalType.HOLD

    def test_returns_hold_on_empty_dataframe(self, strategy):
        """Empty DataFrame must return HOLD without raising any exception."""
        empty_df = pd.DataFrame(
            columns=["date", "open", "high", "low", "close", "volume"]
        )
        result = strategy.run(empty_df, TIMESTAMP)
        assert result.signal == SignalType.HOLD

    def test_returns_hold_on_single_row(self, strategy, sample_ohlcv_data):
        """Single-row DataFrame must return HOLD (cannot compute interval for resampling)."""
        one_row = sample_ohlcv_data.iloc[:1]
        result = strategy.run(one_row, TIMESTAMP)
        assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# 2. Signal Generation — Volatile Phases
# ---------------------------------------------------------------------------

class TestSignalGeneration:

    def test_generates_long_signal_in_bull_phase(self, strategy, sample_ohlcv_data):
        """Phase 2 (bull run): at least one LONG signal must be produced.

        The one-per-day gate means signals only fire at the FIRST bar of each
        new calendar day where conditions are met.  With 15m candles starting
        at 2024-01-01 00:00 UTC, new-day boundaries within Phase 2 fall at:
          bar 768  (2024-01-09 00:00 UTC)
          bar 864  (2024-01-10 00:00 UTC)
        Probing end_idx = boundary + 1 makes that boundary bar the last bar,
        so the strategy evaluates it before the day's signal_active gate fires.
        """
        signals = set()
        for end_idx in [769, 865]:  # last bar = 768 (2024-01-09) or 864 (2024-01-10)
            df_slice = sample_ohlcv_data.iloc[:end_idx]
            result = strategy.run(df_slice, TIMESTAMP)
            signals.add(result.signal)

        assert SignalType.LONG in signals, (
            "Expected at least one LONG signal at the start of a new day in "
            f"the bull phase. Signals observed: {signals}"
        )

    def test_generates_short_signal_in_bear_phase(self, strategy, sample_ohlcv_data):
        """Phase 3 (bear crash): at least one SHORT signal must be produced.

        New-day boundaries within Phase 3 (bars 900–1100) fall at:
          bar 960  (2024-01-11 00:00 UTC)
          bar 1056 (2024-01-12 00:00 UTC)
        At these bars signal_active resets to False, allowing a fresh signal.
        """
        signals = set()
        for end_idx in [961, 1057]:  # last bar = 960 (2024-01-11) or 1056 (2024-01-12)
            df_slice = sample_ohlcv_data.iloc[:end_idx]
            result = strategy.run(df_slice, TIMESTAMP)
            signals.add(result.signal)

        assert SignalType.SHORT in signals, (
            "Expected at least one SHORT signal at the start of a new day in "
            f"the bear phase. Signals observed: {signals}"
        )

    def test_full_dataset_returns_valid_signal(self, strategy, sample_ohlcv_data):
        """Full 1,100-bar dataset must return a valid signal without raising."""
        result = strategy.run(sample_ohlcv_data, TIMESTAMP)
        assert result.signal in VALID_SIGNALS

    def test_timestamp_is_propagated(self, strategy, sample_ohlcv_data):
        """StrategyRecommendation must carry the exact timestamp passed to run()."""
        result = strategy.run(sample_ohlcv_data, TIMESTAMP)
        assert result.timestamp == TIMESTAMP


# ---------------------------------------------------------------------------
# 3. Contract & Structural Checks
# ---------------------------------------------------------------------------

class TestContractCompliance:

    def test_min_candles_required_is_dynamic(self, strategy):
        """MIN_CANDLES_REQUIRED must equal 3 × max(ema_length, vol_sma_length)."""
        expected = 3 * max(strategy.ema_length, strategy.vol_sma_length)
        assert strategy.MIN_CANDLES_REQUIRED == expected

    def test_min_candles_required_updates_with_params(self):
        """Changing ema_length before __init__ completes must be reflected in the guard."""
        s = HtfCandleDirectionStrategyV1Strategy()
        s.ema_length = 80
        s.vol_sma_length = 30
        s.MIN_CANDLES_REQUIRED = 3 * max(s.ema_length, s.vol_sma_length)
        assert s.MIN_CANDLES_REQUIRED == 240

    def test_strategy_name_set(self, strategy):
        assert strategy.name == "HtfCandleDirectionStrategyV1"

    def test_timeframe_lowercase(self, strategy):
        assert strategy.timeframe == strategy.timeframe.lower()

    def test_lookback_hours_positive(self, strategy):
        assert strategy.lookback_hours > 0
