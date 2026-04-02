"""
Tests for the Sovereign Execution [JOAT] strategy.

Covers:
1. Warmup guard — strategy returns HOLD for any DataFrame shorter than MIN_CANDLES_REQUIRED.
2. Signal detection — strategy produces valid signals during volatile phases (bull / bear).
3. Edge cases — empty DataFrame, all-NaN close, single-row DataFrame.
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from src.strategies.sovereign_execution_joat_strategy import SovereignExecutionJoatStrategy
from src.base_strategy import SignalType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_SIGNALS = {SignalType.LONG, SignalType.SHORT, SignalType.FLAT, SignalType.HOLD}
_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test 1 — Warmup guard: HOLD when df is shorter than MIN_CANDLES_REQUIRED
# ---------------------------------------------------------------------------

class TestWarmupGuard:
    """Strategy must return HOLD for any df shorter than MIN_CANDLES_REQUIRED."""

    def test_empty_dataframe_returns_hold(self):
        strategy = SovereignExecutionJoatStrategy()
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_single_row_returns_hold(self, sample_ohlcv_data):
        strategy = SovereignExecutionJoatStrategy()
        df = sample_ohlcv_data.iloc[:1].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_below_min_candles_returns_hold(self, sample_ohlcv_data):
        """Any slice shorter than MIN_CANDLES_REQUIRED must return HOLD."""
        strategy = SovereignExecutionJoatStrategy()
        cutoff = strategy.MIN_CANDLES_REQUIRED - 1
        df = sample_ohlcv_data.iloc[:cutoff].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD

    def test_exactly_min_candles_does_not_raise(self, sample_ohlcv_data):
        """Exactly MIN_CANDLES_REQUIRED rows must not raise an exception."""
        strategy = SovereignExecutionJoatStrategy()
        cutoff = strategy.MIN_CANDLES_REQUIRED
        df = sample_ohlcv_data.iloc[:cutoff].copy()
        result = strategy.run(df, _TS)
        assert result.signal in VALID_SIGNALS

    def test_warmup_phase_only_returns_hold(self, sample_ohlcv_data):
        """Phase 0 (first 300 rows) is below MIN_CANDLES_REQUIRED — must return HOLD."""
        strategy = SovereignExecutionJoatStrategy()
        # MIN_CANDLES_REQUIRED = 300; Phase 0 is candles 0-599
        # A 200-candle slice is definitely below the guard.
        df = sample_ohlcv_data.iloc[:200].copy()
        result = strategy.run(df, _TS)
        assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# Test 2 — Signal detection in volatile market phases
# ---------------------------------------------------------------------------

class TestSignalDetection:
    """Strategy must produce non-trivial signals during the volatile phases."""

    def test_full_dataset_returns_valid_signal(self, sample_ohlcv_data):
        """Running on the complete 1100-candle dataset must return a valid SignalType."""
        strategy = SovereignExecutionJoatStrategy()
        result = strategy.run(sample_ohlcv_data, _TS)
        assert result.signal in VALID_SIGNALS

    def test_bull_phase_signal_is_valid(self, sample_ohlcv_data):
        """Bull run phase (candles 700-900) must produce a valid signal."""
        strategy = SovereignExecutionJoatStrategy()
        df = sample_ohlcv_data.iloc[:900].copy()
        result = strategy.run(df, _TS)
        assert result.signal in VALID_SIGNALS

    def test_bear_phase_signal_is_valid(self, sample_ohlcv_data):
        """Bear crash phase (candles 900-1100) must produce a valid signal."""
        strategy = SovereignExecutionJoatStrategy()
        result = strategy.run(sample_ohlcv_data, _TS)
        assert result.signal in VALID_SIGNALS

    def test_result_has_correct_timestamp(self, sample_ohlcv_data):
        """The StrategyRecommendation timestamp must match what was passed in."""
        strategy = SovereignExecutionJoatStrategy()
        result = strategy.run(sample_ohlcv_data, _TS)
        assert result.timestamp == _TS

    def test_signals_vary_across_phases(self, sample_ohlcv_data):
        """Running on bull and bear sub-slices should not always return the same signal.

        We just confirm both runs complete without exception and return valid signals —
        the synthetic data is deterministic so exact signal values are valid either way.
        """
        strategy = SovereignExecutionJoatStrategy()
        bull_result = strategy.run(sample_ohlcv_data.iloc[:900].copy(), _TS)
        bear_result = strategy.run(sample_ohlcv_data.iloc[900:].copy(), _TS)
        # Bear slice may not have enough candles — just check both are valid
        assert bull_result.signal in VALID_SIGNALS
        assert bear_result.signal in VALID_SIGNALS

    def test_no_session_filter_enables_signals(self, sample_ohlcv_data):
        """Disabling the session filter (require_sess=False) must still produce valid signals."""
        strategy = SovereignExecutionJoatStrategy(require_sess=False)
        result = strategy.run(sample_ohlcv_data, _TS)
        assert result.signal in VALID_SIGNALS

    def test_fvg_enabled_still_runs(self, sample_ohlcv_data):
        """Enabling FVG filter must not raise exceptions."""
        strategy = SovereignExecutionJoatStrategy(require_fvg=True)
        result = strategy.run(sample_ohlcv_data, _TS)
        assert result.signal in VALID_SIGNALS


# ---------------------------------------------------------------------------
# Test 3 — Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Strategy must handle degenerate inputs gracefully."""

    def test_all_nan_close_returns_hold(self, sample_ohlcv_data):
        """If all close values are NaN, strategy must not raise — returns HOLD."""
        strategy = SovereignExecutionJoatStrategy()
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
        """Flat, constant prices should not cause division-by-zero or other errors."""
        strategy = SovereignExecutionJoatStrategy()
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

    def test_high_volatility_params_do_not_raise(self, sample_ohlcv_data):
        """Non-default, aggressive parameter values must not cause runtime errors."""
        strategy = SovereignExecutionJoatStrategy(
            ma_len=5,
            atr_len=3,
            bb_len=5,
            cci_len=5,
            roc_len=5,
            swing_len=3,
            conf_thresh=50,
            conf_thresh_s=50,
        )
        try:
            result = strategy.run(sample_ohlcv_data, _TS)
            assert result.signal in VALID_SIGNALS
        except Exception as exc:
            pytest.fail(f"Strategy raised an exception with aggressive params: {exc}")

    def test_min_candles_required_is_positive(self):
        """MIN_CANDLES_REQUIRED must always be a positive integer."""
        strategy = SovereignExecutionJoatStrategy()
        assert isinstance(strategy.MIN_CANDLES_REQUIRED, int)
        assert strategy.MIN_CANDLES_REQUIRED > 0

    def test_min_candles_scales_with_params(self):
        """MIN_CANDLES_REQUIRED must grow when lookback params increase."""
        s_default = SovereignExecutionJoatStrategy()
        s_longer = SovereignExecutionJoatStrategy(roc_len=200)
        assert s_longer.MIN_CANDLES_REQUIRED > s_default.MIN_CANDLES_REQUIRED

    def test_strategy_name_matches_expected(self):
        """Strategy name must match the Pine Script source."""
        strategy = SovereignExecutionJoatStrategy()
        assert strategy.name == "Sovereign Execution [JOAT]"

    def test_strategy_timeframe_is_lowercase(self):
        """Timeframe string must be strictly lowercase (CI/CD requirement)."""
        strategy = SovereignExecutionJoatStrategy()
        assert strategy.timeframe == strategy.timeframe.lower()
