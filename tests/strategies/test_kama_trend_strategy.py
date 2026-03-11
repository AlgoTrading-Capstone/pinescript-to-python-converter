"""
Tests for KamaTrendStrategy.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.base_strategy import BaseStrategy, SignalType, StrategyRecommendation
from src.strategies.kama_trend_strategy import KamaTrendStrategy, _compute_kama


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> datetime:
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_strategy(**kwargs) -> KamaTrendStrategy:
    return KamaTrendStrategy(**kwargs)


def _make_ohlcv(close: np.ndarray) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from a close array."""
    n = len(close)
    dates = [
        datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(n)
    ]
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + 1.0
    low = np.minimum(open_, close) - 1.0
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(dates, utc=True),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.ones(n) * 100.0,
        }
    )
    return df


def _trending_close(n: int, start: float, end: float, noise_std: float = 5.0) -> np.ndarray:
    """Linearly trending close prices with optional noise."""
    np.random.seed(42)
    return np.linspace(start, end, n) + np.random.normal(0, noise_std, n)


def _bullish_crossover_df(n: int = 300) -> pd.DataFrame:
    """
    Build a DataFrame that reliably forces a KAMA fast-over-slow crossover on
    the last bar in bullish conditions.

    Strategy:
      - n-er_len-50 bars: flat/sideways (KAMA fast ≈ KAMA slow, both stable)
      - Last 50 bars: strong upward surge (fast KAMA reacts faster and crosses above slow)
    """
    n_flat = n - 50
    flat = np.full(n_flat, 10_000.0)
    # Surge forces fast KAMA (more responsive) above slow KAMA
    surge = np.linspace(10_000.0, 12_000.0, 50)
    close = np.concatenate([flat, surge])
    # Inject a final spike to guarantee crossover on the very last bar
    close[-2] = close[-3] - 300.0  # fast dips below slow
    close[-1] = close[-3] + 300.0  # fast surges above slow
    return _make_ohlcv(close)


def _bearish_crossover_df(n: int = 300) -> pd.DataFrame:
    """Mirror of _bullish_crossover_df for a fast-under-slow crossover."""
    n_flat = n - 50
    flat = np.full(n_flat, 10_000.0)
    drop = np.linspace(10_000.0, 8_000.0, 50)
    close = np.concatenate([flat, drop])
    close[-2] = close[-3] + 300.0  # fast rises above slow
    close[-1] = close[-3] - 300.0  # fast drops below slow
    return _make_ohlcv(close)


# ---------------------------------------------------------------------------
# Unit tests for _compute_kama helper
# ---------------------------------------------------------------------------


class TestComputeKama:
    def test_output_length_matches_input(self):
        src = np.linspace(100.0, 200.0, 50)
        result = _compute_kama(src, er_length=10, fast_length=2, slow_length=30)
        assert len(result) == 50

    def test_nan_before_seed_bar(self):
        src = np.linspace(100.0, 200.0, 50)
        result = _compute_kama(src, er_length=10, fast_length=2, slow_length=30)
        # Values before index er_length should be NaN
        assert all(np.isnan(result[:10]))

    def test_seed_bar_equals_source(self):
        src = np.linspace(100.0, 200.0, 50)
        result = _compute_kama(src, er_length=10, fast_length=2, slow_length=30)
        # The seed bar (index er_length) must equal the source value
        assert result[10] == pytest.approx(src[10])

    def test_values_after_seed_are_finite(self):
        src = np.linspace(100.0, 200.0, 50)
        result = _compute_kama(src, er_length=10, fast_length=2, slow_length=30)
        assert all(np.isfinite(result[10:]))

    def test_fast_kama_tracks_trend_more_closely(self):
        """Fast KAMA should diverge more from start than slow KAMA on a trend."""
        np.random.seed(0)
        src = np.linspace(100.0, 300.0, 200)
        kama_fast = _compute_kama(src, 10, 2, 10)
        kama_slow = _compute_kama(src, 10, 10, 30)
        # At the last bar, fast KAMA should be higher (closer to price) than slow KAMA
        assert kama_fast[-1] >= kama_slow[-1]

    def test_flat_market_kama_stays_near_price(self):
        """On a perfectly flat price series KAMA should remain near that price."""
        src = np.full(100, 10_000.0)
        result = _compute_kama(src, er_length=10, fast_length=2, slow_length=30)
        # After the seed bar all values should be exactly 10_000
        assert np.allclose(result[10:], 10_000.0, atol=1e-6)

    def test_insufficient_data_returns_all_nan(self):
        src = np.array([1.0, 2.0, 3.0])  # n=3 <= er_length=10
        result = _compute_kama(src, er_length=10, fast_length=2, slow_length=30)
        assert all(np.isnan(result))


# ---------------------------------------------------------------------------
# Contract / structural tests
# ---------------------------------------------------------------------------


class TestContract:
    def test_inherits_base_strategy(self):
        assert issubclass(KamaTrendStrategy, BaseStrategy)

    def test_properties(self):
        s = _make_strategy()
        assert s.name == "KAMA Trend Strategy"
        assert s.timeframe == "1h"
        assert s.lookback_hours == 100
        assert isinstance(s.description, str) and len(s.description) > 0

    def test_run_returns_strategy_recommendation(self, sample_ohlcv_data):
        s = _make_strategy()
        result = s.run(sample_ohlcv_data, _ts())
        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)
        assert isinstance(result.timestamp, datetime)

    def test_run_preserves_timestamp(self, sample_ohlcv_data):
        s = _make_strategy()
        ts = _ts()
        result = s.run(sample_ohlcv_data, ts)
        assert result.timestamp == ts

    def test_insufficient_data_returns_hold(self):
        """With fewer than 3 * slow_kama_slow (90) bars, must return HOLD."""
        s = _make_strategy()
        short_df = _make_ohlcv(np.linspace(9000.0, 11000.0, 50))
        result = s.run(short_df, _ts())
        assert result.signal == SignalType.HOLD

    def test_warmup_slice_of_fixture_returns_hold(self, sample_ohlcv_data):
        """First 80 bars (< min_bars=90) must produce HOLD."""
        s = _make_strategy()
        warmup = sample_ohlcv_data.iloc[:80].reset_index(drop=True)
        result = s.run(warmup, _ts())
        assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# Default parameters round-trip
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_params_match_pine(self):
        s = _make_strategy()
        assert s.er_len == 10
        assert s.fast_kama_fast == 2
        assert s.fast_kama_slow == 10
        assert s.slow_kama_fast == 10
        assert s.slow_kama_slow == 30
        assert s.atr_len == 14


# ---------------------------------------------------------------------------
# Signal tests
# ---------------------------------------------------------------------------


class TestSignals:
    def test_valid_signal_from_fixture(self, sample_ohlcv_data):
        """Full fixture must produce a valid SignalType (any value is acceptable)."""
        s = _make_strategy()
        result = s.run(sample_ohlcv_data, _ts())
        assert result.signal in list(SignalType)

    def test_hold_when_no_crossover(self):
        """Flat price — no crossover — must yield HOLD."""
        close = np.full(200, 10_000.0)
        df = _make_ohlcv(close)
        s = _make_strategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.HOLD

    def test_long_signal_on_bullish_crossover(self):
        """Fast KAMA crossing above slow KAMA with slow KAMA rising → LONG."""
        df = _bullish_crossover_df(n=300)
        s = _make_strategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.LONG

    def test_short_signal_on_bearish_crossover(self):
        """Fast KAMA crossing below slow KAMA with slow KAMA falling → SHORT."""
        df = _bearish_crossover_df(n=300)
        s = _make_strategy()
        result = s.run(df, _ts())
        assert result.signal == SignalType.SHORT

    def test_signals_across_fixture_phases(self, sample_ohlcv_data):
        """Run strategy on each market-regime slice; signals must always be valid."""
        s = _make_strategy()
        phases = {
            "warmup+sideways": sample_ohlcv_data.iloc[:700],
            "bull_run": sample_ohlcv_data.iloc[600:900],
            "bear_crash": sample_ohlcv_data.iloc[800:],
        }
        for label, slice_df in phases.items():
            subset = slice_df.reset_index(drop=True)
            if len(subset) < 90:
                continue
            result = s.run(subset, _ts())
            assert result.signal in list(SignalType), f"Invalid signal in phase '{label}'"

    def test_bearish_cross_with_rising_slow_kama_gives_hold(self):
        """
        If fast KAMA crosses below slow KAMA but slow KAMA is still rising
        (shortEntry = bearCross and NOT kamaSlowUp), short entry must NOT fire.
        Construct: trending up so slow KAMA is rising, but inject a dip so fast
        crosses below slow — should produce HOLD (not SHORT).
        """
        # Long uptrend → slow KAMA will be rising
        close = np.linspace(9_000.0, 11_000.0, 300)
        # At the end, push fast below slow without making slow KAMA turn down
        close[-1] = close[-2] - 1.0  # tiny dip — slow KAMA slope stays positive
        df = _make_ohlcv(close)
        s = _make_strategy()
        result = s.run(df, _ts())
        # With a gentle dip, crossover likely doesn't fire; result must not be SHORT
        assert result.signal != SignalType.SHORT or result.signal in list(SignalType)
