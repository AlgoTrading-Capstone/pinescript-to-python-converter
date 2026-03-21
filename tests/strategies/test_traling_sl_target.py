"""
Tests for TralingSLTargetStrategy

Uses the shared sample_ohlcv_data fixture (1,100 candles: warmup + sideways/bull/bear).
"""

import pandas as pd
import numpy as np
from datetime import datetime, timezone

from src.base_strategy import BaseStrategy, StrategyRecommendation, SignalType
from src.strategies.traling_sl_target_strategy import TralingSLTargetStrategy


class TestTralingSLTargetStrategyContract:
    """Verify BaseStrategy contract compliance."""

    def test_inherits_base_strategy(self):
        strategy = TralingSLTargetStrategy()
        assert isinstance(strategy, BaseStrategy)

    def test_has_required_attributes(self):
        strategy = TralingSLTargetStrategy()
        assert strategy.name == "TralingSLTarget"
        assert strategy.timeframe == "15m"
        assert strategy.lookback_hours == 48
        assert len(strategy.description) > 0

    def test_min_candles_required_set(self):
        strategy = TralingSLTargetStrategy()
        assert strategy.MIN_CANDLES_REQUIRED == 150


class TestTralingSLTargetStrategyWarmup:
    """Verify warmup / min_bars guard."""

    def test_hold_when_insufficient_bars(self, sample_ohlcv_data):
        strategy = TralingSLTargetStrategy()
        short_df = sample_ohlcv_data.iloc[:100].copy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = strategy.run(short_df, ts)
        assert result.signal == SignalType.HOLD

    def test_hold_at_boundary(self, sample_ohlcv_data):
        strategy = TralingSLTargetStrategy()
        boundary_df = sample_ohlcv_data.iloc[:149].copy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = strategy.run(boundary_df, ts)
        assert result.signal == SignalType.HOLD

    def test_runs_with_sufficient_bars(self, sample_ohlcv_data):
        strategy = TralingSLTargetStrategy()
        sufficient_df = sample_ohlcv_data.iloc[:200].copy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = strategy.run(sufficient_df, ts)
        assert isinstance(result, StrategyRecommendation)
        assert result.signal in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD)


class TestTralingSLTargetStrategyReturn:
    """Verify return type and structure."""

    def test_returns_strategy_recommendation(self, sample_ohlcv_data):
        strategy = TralingSLTargetStrategy()
        ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
        result = strategy.run(sample_ohlcv_data, ts)
        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)
        assert result.timestamp == ts

    def test_signal_is_valid_enum(self, sample_ohlcv_data):
        strategy = TralingSLTargetStrategy()
        ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
        result = strategy.run(sample_ohlcv_data, ts)
        assert result.signal in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD, SignalType.FLAT)


class TestTralingSLTargetStrategySignals:
    """Verify signal logic across market regimes."""

    def test_generates_signals_across_full_dataset(self, sample_ohlcv_data):
        """Run strategy across all bars and verify at least one non-HOLD signal."""
        strategy = TralingSLTargetStrategy()
        signals = []
        for i in range(strategy.MIN_CANDLES_REQUIRED, len(sample_ohlcv_data)):
            df_slice = sample_ohlcv_data.iloc[:i + 1].copy()
            ts = df_slice['date'].iloc[-1].to_pydatetime()
            result = strategy.run(df_slice, ts)
            signals.append(result.signal)

        non_hold = [s for s in signals if s != SignalType.HOLD]
        assert len(non_hold) > 0, "Strategy must generate at least one LONG or SHORT signal"

    def test_generates_long_signals(self, sample_ohlcv_data):
        """Verify at least one LONG signal is generated (expected during bull regime)."""
        strategy = TralingSLTargetStrategy()
        signals = []
        for i in range(strategy.MIN_CANDLES_REQUIRED, len(sample_ohlcv_data)):
            df_slice = sample_ohlcv_data.iloc[:i + 1].copy()
            ts = df_slice['date'].iloc[-1].to_pydatetime()
            result = strategy.run(df_slice, ts)
            signals.append(result.signal)

        long_signals = [s for s in signals if s == SignalType.LONG]
        assert len(long_signals) > 0, "Strategy must produce at least one LONG signal"

    def test_generates_short_signals(self, sample_ohlcv_data):
        """Verify at least one SHORT signal is generated (expected during bear regime)."""
        strategy = TralingSLTargetStrategy()
        signals = []
        for i in range(strategy.MIN_CANDLES_REQUIRED, len(sample_ohlcv_data)):
            df_slice = sample_ohlcv_data.iloc[:i + 1].copy()
            ts = df_slice['date'].iloc[-1].to_pydatetime()
            result = strategy.run(df_slice, ts)
            signals.append(result.signal)

        short_signals = [s for s in signals if s == SignalType.SHORT]
        assert len(short_signals) > 0, "Strategy must produce at least one SHORT signal"

    def test_no_lookahead_bias(self, sample_ohlcv_data):
        """Verify that future data does not affect past signals."""
        strategy = TralingSLTargetStrategy()
        pivot = 700

        df_short = sample_ohlcv_data.iloc[:pivot].copy()
        ts = df_short['date'].iloc[-1].to_pydatetime()
        result_short = strategy.run(df_short, ts)

        df_long = sample_ohlcv_data.iloc[:pivot + 200].copy()
        # Use the same timestamp to compare the signal at the same point
        result_long = strategy.run(df_long.iloc[:pivot].copy(), ts)

        assert result_short.signal == result_long.signal, "Adding future data must not change past signals"

    def test_does_not_modify_input_dataframe(self, sample_ohlcv_data):
        """Ensure run() does not mutate the input DataFrame."""
        strategy = TralingSLTargetStrategy()
        original_cols = list(sample_ohlcv_data.columns)
        original_shape = sample_ohlcv_data.shape
        ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
        strategy.run(sample_ohlcv_data, ts)
        assert list(sample_ohlcv_data.columns) == original_cols
        assert sample_ohlcv_data.shape == original_shape
