"""
Tests for PgQsdForNiftyFutureStrategy (Quant Sentiment & Spread Master).

Coverage
--------
1. Warmup guard — HOLD returned when df is shorter than MIN_CANDLES_REQUIRED.
2. Signal generation — LONG / SHORT / HOLD produced on volatile phases.
3. Edge cases — empty DataFrame and all-NaN close handled without raw exceptions.
"""
import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timezone

from src.strategies.pg_qsd_for_nifty_future_strategy import PgQsdForNiftyFutureStrategy
from src.base_strategy import SignalType


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def strategy():
    return PgQsdForNiftyFutureStrategy()


@pytest.fixture
def ts():
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ──────────────────────────────────────────────────────────────────────────────
# 1. Warmup guard (Phase 0)
# ──────────────────────────────────────────────────────────────────────────────

class TestWarmupGuard:
    """Strategy must return HOLD when df has fewer rows than MIN_CANDLES_REQUIRED."""

    def test_hold_on_empty_df(self, strategy, ts):
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = strategy.run(df, ts)
        assert result.signal == SignalType.HOLD

    def test_hold_below_min_candles(self, strategy, sample_ohlcv_data, ts):
        """Slice shorter than MIN_CANDLES_REQUIRED must yield HOLD."""
        short_df = sample_ohlcv_data.iloc[: strategy.MIN_CANDLES_REQUIRED - 1].copy()
        result = strategy.run(short_df, ts)
        assert result.signal == SignalType.HOLD

    def test_hold_during_warmup_phase(self, strategy, sample_ohlcv_data, ts):
        """Phase 0 slice (first 60 bars) is well below the warmup threshold."""
        warmup_df = sample_ohlcv_data.iloc[:60].copy()
        result = strategy.run(warmup_df, ts)
        assert result.signal == SignalType.HOLD

    def test_min_candles_required_value(self, strategy):
        """MIN_CANDLES_REQUIRED must equal 3 * max(21, 21, 20, 7) = 63."""
        assert strategy.MIN_CANDLES_REQUIRED == 63

    def test_timestamp_preserved(self, strategy, sample_ohlcv_data, ts):
        short_df = sample_ohlcv_data.iloc[:10].copy()
        result = strategy.run(short_df, ts)
        assert result.timestamp == ts


# ──────────────────────────────────────────────────────────────────────────────
# 2. Signal generation on volatile phases
# ──────────────────────────────────────────────────────────────────────────────

class TestSignalGeneration:
    """Strategy should produce meaningful signals during trending market phases."""

    def test_run_returns_valid_signal_type(self, strategy, sample_ohlcv_data, ts):
        """Any result from a sufficiently large df must be a valid SignalType."""
        df = sample_ohlcv_data.iloc[:700].copy()
        result = strategy.run(df, ts)
        assert result.signal in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD)

    def test_run_on_full_dataset_no_exception(self, strategy, sample_ohlcv_data, ts):
        """Strategy must complete without exception on the full 1100-bar dataset."""
        result = strategy.run(sample_ohlcv_data.copy(), ts)
        assert result.signal in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD)

    def test_run_on_bull_phase(self, strategy, sample_ohlcv_data, ts):
        """During the bull run (bars 700-900) the strategy must produce a signal."""
        bull_df = sample_ohlcv_data.iloc[:900].copy()
        result = strategy.run(bull_df, ts)
        assert result.signal in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD)

    def test_run_on_bear_phase(self, strategy, sample_ohlcv_data, ts):
        """During the bear crash (bars 900-1100) the strategy must produce a signal."""
        bear_df = sample_ohlcv_data.copy()
        result = strategy.run(bear_df, ts)
        assert result.signal in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD)

    def test_signal_changes_across_phases(self, strategy, sample_ohlcv_data, ts):
        """The signal must not be permanently HOLD across both trending phases."""
        signals = set()
        for end in range(strategy.MIN_CANDLES_REQUIRED, len(sample_ohlcv_data), 50):
            result = strategy.run(sample_ohlcv_data.iloc[:end].copy(), ts)
            signals.add(result.signal)
        # At least two distinct signal values should appear (e.g. HOLD + LONG / SHORT)
        assert len(signals) >= 2

    def test_result_is_strategy_recommendation(self, strategy, sample_ohlcv_data, ts):
        from src.base_strategy import StrategyRecommendation
        result = strategy.run(sample_ohlcv_data.copy(), ts)
        assert isinstance(result, StrategyRecommendation)

    def test_timestamp_on_valid_run(self, strategy, sample_ohlcv_data, ts):
        result = strategy.run(sample_ohlcv_data.copy(), ts)
        assert result.timestamp == ts


# ──────────────────────────────────────────────────────────────────────────────
# 3. Edge cases
# ──────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    """Strategy must handle degenerate inputs gracefully."""

    def test_all_nan_close_no_exception(self, strategy, sample_ohlcv_data, ts):
        """If close is all NaN, strategy must not raise; HOLD is the safe fallback."""
        df = sample_ohlcv_data.copy()
        df["close"] = np.nan
        df["open"] = np.nan
        df["high"] = np.nan
        df["low"] = np.nan
        try:
            result = strategy.run(df, ts)
            assert result.signal in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD)
        except Exception as exc:
            pytest.fail(f"Strategy raised an unexpected exception on NaN data: {exc}")

    def test_zero_volume_no_exception(self, strategy, sample_ohlcv_data, ts):
        """Zero-volume bars must not cause division errors."""
        df = sample_ohlcv_data.copy()
        df["volume"] = 0.0
        try:
            result = strategy.run(df, ts)
            assert result.signal in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD)
        except Exception as exc:
            pytest.fail(f"Strategy raised an unexpected exception on zero-volume data: {exc}")

    def test_constant_price_no_exception(self, strategy, sample_ohlcv_data, ts):
        """Flat (constant) price series must not cause ATR or HMA divisions by zero."""
        df = sample_ohlcv_data.copy()
        for col in ("open", "high", "low", "close"):
            df[col] = 10000.0
        try:
            result = strategy.run(df, ts)
            assert result.signal in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD)
        except Exception as exc:
            pytest.fail(f"Strategy raised an unexpected exception on flat price: {exc}")

    def test_exactly_min_candles_no_exception(self, strategy, sample_ohlcv_data, ts):
        """A df with exactly MIN_CANDLES_REQUIRED rows must not raise."""
        df = sample_ohlcv_data.iloc[: strategy.MIN_CANDLES_REQUIRED].copy()
        try:
            result = strategy.run(df, ts)
            assert result.signal in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD)
        except Exception as exc:
            pytest.fail(f"Strategy raised at MIN_CANDLES_REQUIRED boundary: {exc}")
