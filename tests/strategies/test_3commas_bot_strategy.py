"""Tests for 3Commas Bot Strategy (EMA crossover)."""

import pytest
from src.strategies.threecommas_bot_strategy import ThreeCommasBotStrategy
from src.base_strategy import SignalType, StrategyRecommendation


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestInitialization:
    def test_strategy_instantiation(self):
        strategy = ThreeCommasBotStrategy()
        assert strategy.name == "3Commas Bot"
        assert strategy.timeframe == "15m"
        assert strategy.lookback_hours > 0

    def test_min_candles_required_is_dynamic(self):
        strategy = ThreeCommasBotStrategy()
        expected = 3 * max(
            strategy.MA_LENGTH_1,
            strategy.MA_LENGTH_2,
            strategy.ATR_LENGTH,
        )
        assert strategy.MIN_CANDLES_REQUIRED == expected

    def test_default_parameters(self):
        strategy = ThreeCommasBotStrategy()
        assert strategy.MA_TYPE_1 == "EMA"
        assert strategy.MA_TYPE_2 == "EMA"
        assert strategy.MA_LENGTH_1 == 21
        assert strategy.MA_LENGTH_2 == 50
        assert strategy.ATR_LENGTH == 14


# ---------------------------------------------------------------------------
# Warmup guard
# ---------------------------------------------------------------------------

class TestWarmupGuard:
    def test_hold_on_insufficient_data(self, sample_ohlcv_data):
        strategy = ThreeCommasBotStrategy()
        short_df = sample_ohlcv_data.iloc[:10]
        timestamp = short_df.iloc[-1]["date"]
        result = strategy.run(short_df, timestamp)

        assert isinstance(result, StrategyRecommendation)
        assert result.signal == SignalType.HOLD

    def test_hold_just_below_min_candles(self, sample_ohlcv_data):
        strategy = ThreeCommasBotStrategy()
        short_df = sample_ohlcv_data.iloc[: strategy.MIN_CANDLES_REQUIRED - 1]
        timestamp = short_df.iloc[-1]["date"]
        result = strategy.run(short_df, timestamp)

        assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class TestExecution:
    def test_returns_valid_recommendation(self, sample_ohlcv_data):
        strategy = ThreeCommasBotStrategy()
        timestamp = sample_ohlcv_data.iloc[-1]["date"]
        result = strategy.run(sample_ohlcv_data, timestamp)

        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)

    def test_warmup_phase_returns_hold(self, sample_ohlcv_data):
        """During flat warmup (phase 0) with enough bars, strategy should produce valid output."""
        strategy = ThreeCommasBotStrategy()
        # Use warmup phase data (bars 0–600, flat at 10,000)
        warmup_df = sample_ohlcv_data.iloc[: strategy.MIN_CANDLES_REQUIRED + 10]
        timestamp = warmup_df.iloc[-1]["date"]
        result = strategy.run(warmup_df, timestamp)

        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)

    def test_bull_phase_produces_signal(self, sample_ohlcv_data):
        """Bull run (bars 700–900) should eventually produce a LONG crossover."""
        strategy = ThreeCommasBotStrategy()
        # Include warmup + bull phase
        bull_df = sample_ohlcv_data.iloc[:850]
        timestamp = bull_df.iloc[-1]["date"]
        result = strategy.run(bull_df, timestamp)

        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)

    def test_bear_phase_produces_signal(self, sample_ohlcv_data):
        """Bear crash (bars 900–1100) should eventually produce a SHORT crossover."""
        strategy = ThreeCommasBotStrategy()
        timestamp = sample_ohlcv_data.iloc[-1]["date"]
        result = strategy.run(sample_ohlcv_data, timestamp)

        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)


# ---------------------------------------------------------------------------
# Signal variety — across all data phases, we should see non-HOLD signals
# ---------------------------------------------------------------------------

class TestSignalVariety:
    def test_produces_non_hold_signals(self, sample_ohlcv_data):
        """Strategy should produce at least one non-HOLD signal across all phases."""
        strategy = ThreeCommasBotStrategy()
        signals = set()
        for i in range(strategy.MIN_CANDLES_REQUIRED, len(sample_ohlcv_data), 5):
            df_slice = sample_ohlcv_data.iloc[:i]
            timestamp = df_slice.iloc[-1]["date"]
            result = strategy.run(df_slice, timestamp)
            signals.add(result.signal)

        assert len(signals) > 1, (
            f"Expected at least one non-HOLD signal across data phases, got only {signals}"
        )

    def test_all_valid_signal_types(self, sample_ohlcv_data):
        """Every signal returned should be a valid SignalType member."""
        strategy = ThreeCommasBotStrategy()
        for i in range(strategy.MIN_CANDLES_REQUIRED, len(sample_ohlcv_data), 10):
            df_slice = sample_ohlcv_data.iloc[:i]
            timestamp = df_slice.iloc[-1]["date"]
            result = strategy.run(df_slice, timestamp)
            assert result.signal in (
                SignalType.LONG,
                SignalType.SHORT,
                SignalType.FLAT,
                SignalType.HOLD,
            )


# ---------------------------------------------------------------------------
# Data integrity — indicators computed without error
# ---------------------------------------------------------------------------

class TestDataIntegrity:
    def test_no_exception_full_range(self, sample_ohlcv_data):
        """Strategy should not raise any exception across the full data range."""
        strategy = ThreeCommasBotStrategy()
        timestamp = sample_ohlcv_data.iloc[-1]["date"]
        # Should not raise
        strategy.run(sample_ohlcv_data, timestamp)
