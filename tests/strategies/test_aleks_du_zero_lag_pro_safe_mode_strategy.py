"""Tests for AleksDuZeroLagProSafeModeStrategy."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.base_strategy import BaseStrategy, SignalType, StrategyRecommendation
from src.strategies.aleks_du_zero_lag_pro_safe_mode_strategy import (
    AleksDuZeroLagProSafeModeStrategy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> datetime:
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_ohlcv(close: np.ndarray) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from a close price array."""
    n = len(close)
    dates = [
        datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=4 * i)
        for i in range(n)
    ]
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + 1.0
    low = np.minimum(open_, close) - 1.0
    return pd.DataFrame({
        "date": pd.to_datetime(dates, utc=True),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.ones(n) * 100.0,
    })


def _make_ohlcv_spread(close: np.ndarray, spread: float) -> pd.DataFrame:
    """Build OHLCV where high/low are always close ± spread.

    Decouples ATR from close-to-close moves so that flat close prices still
    produce a well-defined, predictable ATR (= 2 * spread after convergence).
    """
    n = len(close)
    dates = [
        datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=4 * i)
        for i in range(n)
    ]
    return pd.DataFrame({
        "date": pd.to_datetime(dates, utc=True),
        "open": close.copy(),
        "high": close + spread,
        "low": close - spread,
        "close": close,
        "volume": np.ones(n) * 100.0,
    })


def _long_signal_df(n: int = 200) -> pd.DataFrame:
    """
    n-2 flat bars at 10,000 (HMA converges to 10,000).
    spread=50 → ATR(14) converges to 100, noise_filter = 50.
    upper_band ≈ 10,050; lower_band ≈ 9,950.

    prev bar  : 10,025 — above HMA (≈10,000) so no crossover-up on the last bar,
                         but below upper_band (≈10,051).
    last bar  : 12,000 — well above upper_band → long_cond fires.
    Expected  : LONG (exit_cond does NOT fire because close[prev] > fast_ma[prev]).
    """
    base = 10_000.0
    spread = 50.0
    close = np.concatenate([
        np.full(n - 2, base),
        [base + 25.0, base + 2_000.0],
    ])
    return _make_ohlcv_spread(close, spread)


def _short_signal_df(n: int = 200) -> pd.DataFrame:
    """
    n-2 flat bars at 10,000 (HMA converges to 10,000).
    spread=50 → ATR(14) = 100, noise_filter = 50.
    upper_band ≈ 10,050; lower_band ≈ 9,950.

    prev bar  : 9,975 — below HMA (≈10,000) so no crossunder on the last bar,
                        but above lower_band (≈9,949).
    last bar  :  8,000 — well below lower_band → short_cond fires.
    Expected  : SHORT (exit_cond does NOT fire because close[prev] < fast_ma[prev]).
    """
    base = 10_000.0
    spread = 50.0
    close = np.concatenate([
        np.full(n - 2, base),
        [base - 25.0, base - 2_000.0],
    ])
    return _make_ohlcv_spread(close, spread)


def _exit_signal_df(n: int = 200) -> pd.DataFrame:
    """
    198 bars flat at 10,000 → HMA converges to 10,000.
    prev bar  : 10,300 (above HMA → position established above MA).
    last bar  :  9,500 (crosses below HMA).
    Expected  : FLAT (HMA crossunder fires, highest priority).
    """
    close = np.concatenate([
        np.full(n - 2, 10_000.0),
        [10_300.0, 9_500.0],
    ])
    return _make_ohlcv(close)


# ---------------------------------------------------------------------------
# Contract / structural tests
# ---------------------------------------------------------------------------


class TestContract:
    def test_inherits_base_strategy(self):
        assert issubclass(AleksDuZeroLagProSafeModeStrategy, BaseStrategy)

    def test_properties(self):
        s = AleksDuZeroLagProSafeModeStrategy()
        assert s.name == "[AleksDU AI] Zero-Lag Pro: Safe Mode"
        assert s.timeframe == "4h"
        assert s.lookback_hours == 152
        assert isinstance(s.description, str) and len(s.description) > 0

    def test_min_candles_required_is_dynamic(self):
        s_default = AleksDuZeroLagProSafeModeStrategy()
        s_longer = AleksDuZeroLagProSafeModeStrategy(hma_len=64)
        assert s_default.MIN_CANDLES_REQUIRED == 3 * max(32, 14)   # 96
        assert s_longer.MIN_CANDLES_REQUIRED == 3 * max(64, 14)    # 192

    def test_run_returns_strategy_recommendation(self, sample_ohlcv_data):
        s = AleksDuZeroLagProSafeModeStrategy()
        result = s.run(sample_ohlcv_data, _ts())
        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)
        assert isinstance(result.timestamp, datetime)

    def test_run_preserves_timestamp(self, sample_ohlcv_data):
        s = AleksDuZeroLagProSafeModeStrategy()
        ts = _ts()
        result = s.run(sample_ohlcv_data, ts)
        assert result.timestamp == ts


# ---------------------------------------------------------------------------
# Default parameters
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_params_match_pine(self):
        s = AleksDuZeroLagProSafeModeStrategy()
        assert s.hma_len == 32
        assert s.filter_strength == 0.5
        assert s.atr_period == 14
        assert s.MIN_CANDLES_REQUIRED == 96


# ---------------------------------------------------------------------------
# Warmup guard — Phase 0 of the conftest fixture
# ---------------------------------------------------------------------------


class TestWarmupGuard:
    def test_empty_df_returns_hold(self):
        """Empty DataFrame must not raise and must return HOLD."""
        s = AleksDuZeroLagProSafeModeStrategy()
        empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = s.run(empty, _ts())
        assert result.signal == SignalType.HOLD

    def test_single_bar_returns_hold(self):
        s = AleksDuZeroLagProSafeModeStrategy()
        tiny = _make_ohlcv(np.array([10_000.0]))
        result = s.run(tiny, _ts())
        assert result.signal == SignalType.HOLD

    def test_below_min_candles_returns_hold(self):
        """One bar below MIN_CANDLES_REQUIRED must yield HOLD."""
        s = AleksDuZeroLagProSafeModeStrategy()
        short_df = _make_ohlcv(np.linspace(9_000.0, 11_000.0, s.MIN_CANDLES_REQUIRED - 1))
        result = s.run(short_df, _ts())
        assert result.signal == SignalType.HOLD

    def test_warmup_slice_from_fixture_returns_hold(self, sample_ohlcv_data):
        """First 80 candles (< MIN_CANDLES_REQUIRED=96) must produce HOLD."""
        s = AleksDuZeroLagProSafeModeStrategy()
        warmup = sample_ohlcv_data.iloc[:80].reset_index(drop=True)
        result = s.run(warmup, _ts())
        assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# Signal tests
# ---------------------------------------------------------------------------


class TestSignals:
    def test_flat_market_returns_hold(self):
        """No price movement → no band crossover → HOLD."""
        close = np.full(200, 10_000.0)
        df = _make_ohlcv(close)
        s = AleksDuZeroLagProSafeModeStrategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.HOLD

    def test_long_signal_on_upper_band_breakout(self):
        """Price breaks above upper noise band from between HMA and upper band → LONG."""
        df = _long_signal_df()
        s = AleksDuZeroLagProSafeModeStrategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.LONG

    def test_short_signal_on_lower_band_breakdown(self):
        """Price breaks below lower noise band from between HMA and lower band → SHORT."""
        df = _short_signal_df()
        s = AleksDuZeroLagProSafeModeStrategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.SHORT

    def test_exit_signal_on_hma_crossunder(self):
        """Price was above HMA and crosses below it → FLAT (exit)."""
        df = _exit_signal_df()
        s = AleksDuZeroLagProSafeModeStrategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.FLAT

    def test_valid_signal_on_full_fixture(self, sample_ohlcv_data):
        """Full 1100-bar conftest fixture must produce a valid SignalType."""
        s = AleksDuZeroLagProSafeModeStrategy()
        result = s.run(sample_ohlcv_data, _ts())
        assert result.signal in list(SignalType)

    def test_signals_across_fixture_phases(self, sample_ohlcv_data):
        """Bull and bear phase slices must each produce a valid signal."""
        s = AleksDuZeroLagProSafeModeStrategy()
        phases = {
            "bull_run":   sample_ohlcv_data.iloc[600:900].reset_index(drop=True),
            "bear_crash": sample_ohlcv_data.iloc[900:].reset_index(drop=True),
        }
        for label, subset in phases.items():
            result = s.run(subset, _ts())
            assert result.signal in list(SignalType), f"Invalid signal in phase '{label}'"

    def test_non_hold_signal_produced_across_full_run(self, sample_ohlcv_data):
        """Strategy must emit at least one non-HOLD signal over the full price history."""
        s = AleksDuZeroLagProSafeModeStrategy()
        signals = set()
        step = 10
        for end in range(s.MIN_CANDLES_REQUIRED, len(sample_ohlcv_data) + 1, step):
            subset = sample_ohlcv_data.iloc[:end].reset_index(drop=True)
            result = s.run(subset, _ts())
            signals.add(result.signal)
        assert signals - {SignalType.HOLD}, (
            "Strategy never produced a non-HOLD signal across all 1,100 bars"
        )
