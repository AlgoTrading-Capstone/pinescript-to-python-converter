"""
Tests for SpecialKStrategy (Special K Strategy).

Covers:
- Module import and class instantiation
- Return type contract (StrategyRecommendation)
- min_bars guard: HOLD on data shorter than MIN_BARS (1160 bars)
- HOLD on the full 1100-bar 15m fixture (not enough bars for 1D indicator)
- Filters bypass: disabling zero-line and slope filters expands signal space
- Timestamp passthrough
- Determinism / no-lookahead consistency
- Partial data scanning: no exception raised across the full dataset range

Design note
-----------
The strategy is tuned for 1D candles with MIN_BARS=1160. The `sample_ohlcv_data`
fixture provides only 1100 bars at 15m resolution, which is insufficient for the
indicator to converge. Therefore, `run()` will return HOLD for the full fixture.

Tests that exercise the signal-generation code paths do so by:
  a) Constructing synthetic daily-scale data (1200+ rows) inline, or
  b) Disabling the min_bars guard implicitly via a subclass monkey-patch, or
  c) Calling `_compute_special_k()` directly to verify the mathematical output.

Tests are kept deterministic (no random state beyond what conftest supplies).
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

from src.strategies.special_k_strategy import SpecialKStrategy, _compute_special_k
from src.base_strategy import StrategyRecommendation, SignalType


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXED_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_at(strategy: SpecialKStrategy, df: pd.DataFrame, row_idx: int) -> StrategyRecommendation:
    """Run strategy on df[:row_idx] using a fixed timestamp."""
    return strategy.run(df.iloc[:row_idx].copy(), _FIXED_TS)


def _make_daily_ohlcv(n: int, start_price: float = 10000.0, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic daily OHLCV DataFrame with `n` rows.

    Prices follow a mild random walk so Special K can converge.
    """
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0, 0.005, n)   # ~0.5% daily vol
    close = start_price * np.exp(np.cumsum(returns))

    open_price = np.roll(close, 1)
    open_price[0] = close[0]

    high = np.maximum(open_price, close) * (1 + np.abs(rng.normal(0, 0.002, n)))
    low  = np.minimum(open_price, close) * (1 - np.abs(rng.normal(0, 0.002, n)))
    volume = np.abs(rng.normal(100, 30, n))

    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    dates = [start + timedelta(days=i) for i in range(n)]

    df = pd.DataFrame({
        "date":   dates,
        "open":   open_price,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": volume,
    })
    df["high"] = df[["open", "close", "high"]].max(axis=1)
    df["low"]  = df[["open", "close", "low"]].min(axis=1)
    return df


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSpecialKStrategyImportAndInstantiation:
    """Verify that the module and class are importable and constructable."""

    def test_import(self):
        """Module must be importable without errors."""
        from src.strategies.special_k_strategy import SpecialKStrategy  # noqa: F401

    def test_default_instantiation(self):
        """Default constructor must succeed and expose expected properties."""
        strategy = SpecialKStrategy()
        assert strategy.name == "Special K Strategy"
        assert strategy.timeframe == "1D"
        assert strategy.lookback_hours == 19776
        assert strategy.MIN_BARS == 1160

    def test_custom_instantiation(self):
        """Custom parameters must be stored correctly."""
        strategy = SpecialKStrategy(
            len1=50,
            use_zero_filter=False,
            use_slope_filter=False,
            slope_len=5,
        )
        assert strategy.len1 == 50
        assert strategy.use_zero_filter is False
        assert strategy.use_slope_filter is False
        assert strategy.slope_len == 5


class TestReturnTypeContract:
    """Strategy must always return StrategyRecommendation with a valid SignalType."""

    def test_returns_strategy_recommendation_on_full_fixture(self, sample_ohlcv_data):
        """Full 1100-bar fixture: must return StrategyRecommendation (HOLD expected)."""
        strategy = SpecialKStrategy()
        result = strategy.run(sample_ohlcv_data, _FIXED_TS)

        assert isinstance(result, StrategyRecommendation), (
            f"Expected StrategyRecommendation, got {type(result)}"
        )
        assert isinstance(result.signal, SignalType), (
            f"Expected SignalType member, got {type(result.signal)}"
        )

    def test_returns_strategy_recommendation_on_tiny_data(self, sample_ohlcv_data):
        """Tiny dataset must also return StrategyRecommendation."""
        strategy = SpecialKStrategy()
        result = strategy.run(sample_ohlcv_data.iloc[:5].copy(), _FIXED_TS)

        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)


class TestMinBarsGuard:
    """Strategy must return HOLD for any dataset shorter than MIN_BARS."""

    def test_hold_on_empty_dataframe(self):
        """Empty DataFrame must return HOLD."""
        strategy = SpecialKStrategy()
        empty_df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = strategy.run(empty_df, _FIXED_TS)
        assert result.signal == SignalType.HOLD

    def test_hold_on_10_rows(self, sample_ohlcv_data):
        """10 rows must return HOLD."""
        strategy = SpecialKStrategy()
        result = _run_at(strategy, sample_ohlcv_data, 10)
        assert result.signal == SignalType.HOLD, (
            f"Expected HOLD with only 10 rows, got {result.signal}"
        )

    def test_hold_on_min_bars_minus_one(self, sample_ohlcv_data):
        """MIN_BARS - 1 rows must still return HOLD."""
        strategy = SpecialKStrategy()
        # sample_ohlcv_data only has 1100 rows, which is already less than MIN_BARS (1160)
        # so the full fixture (1100 rows) must return HOLD
        result = strategy.run(sample_ohlcv_data, _FIXED_TS)
        assert result.signal == SignalType.HOLD, (
            f"Expected HOLD for 1100 rows (< MIN_BARS=1160), got {result.signal}"
        )

    def test_hold_on_full_15m_fixture(self, sample_ohlcv_data):
        """Full 1100-bar 15m fixture is insufficient for a 1D strategy — must return HOLD."""
        strategy = SpecialKStrategy()
        result = strategy.run(sample_ohlcv_data, _FIXED_TS)
        assert result.signal == SignalType.HOLD, (
            f"1100 15m bars << MIN_BARS=1160 daily bars; expected HOLD, got {result.signal}"
        )


class TestTimestampPassthrough:
    """Returned timestamp must exactly match the input timestamp."""

    def test_timestamp_matches_on_short_data(self):
        """Even when HOLD is returned the timestamp must be forwarded."""
        strategy = SpecialKStrategy()
        ts = datetime(2025, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
        df = pd.DataFrame({"date": [], "open": [], "high": [], "low": [], "close": [], "volume": []})
        result = strategy.run(df, ts)
        assert result.timestamp == ts

    def test_timestamp_matches_on_fixture(self, sample_ohlcv_data):
        """Timestamp must match the one passed in regardless of signal type."""
        strategy = SpecialKStrategy()
        ts = datetime(2025, 3, 14, 8, 30, 0, tzinfo=timezone.utc)
        result = strategy.run(sample_ohlcv_data, ts)
        assert result.timestamp == ts, (
            f"Expected timestamp {ts}, got {result.timestamp}"
        )


class TestDeterminism:
    """Running identical inputs twice must produce identical outputs."""

    def test_no_lookahead_signal_consistency(self, sample_ohlcv_data):
        """Two runs on identical data must return identical results."""
        strategy = SpecialKStrategy()
        result1 = strategy.run(sample_ohlcv_data.copy(), _FIXED_TS)
        result2 = strategy.run(sample_ohlcv_data.copy(), _FIXED_TS)

        assert result1.signal == result2.signal
        assert result1.timestamp == result2.timestamp

    def test_determinism_on_synthetic_daily_data(self):
        """Two runs on the same synthetic 1200-bar daily dataset must agree."""
        strategy = SpecialKStrategy()
        df = _make_daily_ohlcv(1200)
        result1 = strategy.run(df.copy(), _FIXED_TS)
        result2 = strategy.run(df.copy(), _FIXED_TS)

        assert result1.signal == result2.signal
        assert result1.timestamp == result2.timestamp


class TestNoExceptionAcrossDataRange:
    """Strategy must not raise for any slice of the fixture dataset."""

    def test_no_exception_full_range(self, sample_ohlcv_data):
        """Calling run() for row counts 0, 50, 100, ..., 1100 must not raise."""
        strategy = SpecialKStrategy()
        for end_idx in range(0, len(sample_ohlcv_data) + 1, 50):
            try:
                result = strategy.run(sample_ohlcv_data.iloc[:end_idx].copy(), _FIXED_TS)
                assert isinstance(result, StrategyRecommendation), (
                    f"Unexpected return type at end_idx={end_idx}"
                )
            except Exception as exc:
                pytest.fail(f"run() raised {type(exc).__name__} at end_idx={end_idx}: {exc}")


class TestComputeSpecialKDirectly:
    """Unit tests for the `_compute_special_k()` helper function."""

    def test_returns_two_series(self):
        """Must return a tuple of two pandas Series."""
        close = pd.Series(np.linspace(10000, 12000, 200))
        sk, sig = _compute_special_k(close, len1=10)
        assert isinstance(sk, pd.Series)
        assert isinstance(sig, pd.Series)
        assert len(sk) == len(close)
        assert len(sig) == len(close)

    def test_early_values_are_nan(self):
        """First ~(530+530) values of special_k should be NaN or zero due to min_periods."""
        close = pd.Series(np.linspace(10000, 12000, 600))
        sk, sig = _compute_special_k(close, len1=10)
        # The longest component (ROC530 + SMA530) needs 1060 bars — with only 600
        # bars available, special_k will be non-zero but based on partial components
        # (shorter components converge first). Check that signal is NaN before len1.
        assert pd.isna(sig.iloc[9]), "Signal should be NaN before len1=10 bars of special_k"

    def test_values_converge_with_enough_data(self):
        """With 1200 bars, the last value of special_k and signal must be finite."""
        close = pd.Series(np.linspace(10000, 12000, 1200))
        sk, sig = _compute_special_k(close, len1=100)
        assert np.isfinite(sk.iloc[-1]), "Special K last value must be finite"
        assert np.isfinite(sig.iloc[-1]), "Signal last value must be finite"

    def test_special_k_positive_in_uptrend(self):
        """In a consistent uptrend, Special K should be positive at the end."""
        close = pd.Series(np.linspace(5000, 15000, 1300))  # strong monotonic uptrend
        sk, _ = _compute_special_k(close, len1=100)
        last_valid = sk.dropna().iloc[-1]
        assert last_valid > 0.0, (
            f"Expected positive Special K in strong uptrend, got {last_valid:.4f}"
        )

    def test_special_k_negative_in_downtrend(self):
        """In a consistent downtrend, Special K should be negative at the end."""
        close = pd.Series(np.linspace(15000, 5000, 1300))  # strong monotonic downtrend
        sk, _ = _compute_special_k(close, len1=100)
        last_valid = sk.dropna().iloc[-1]
        assert last_valid < 0.0, (
            f"Expected negative Special K in strong downtrend, got {last_valid:.4f}"
        )


class TestSignalGenerationOnSyntheticDailyData:
    """Test signal generation on synthetic 1200-bar daily data (sufficient for MIN_BARS=1160)."""

    def test_strategy_produces_non_hold_signal_with_enough_data(self):
        """With 1200 synthetic daily bars, strategy must produce at least one non-HOLD signal
        when scanning across the tail end of the dataset.
        """
        strategy = SpecialKStrategy(use_zero_filter=False, use_slope_filter=False)
        df = _make_daily_ohlcv(1300)

        non_hold_count = 0
        # Scan the final 100 bars to check signal variety
        for end_idx in range(1160, 1300):
            result = strategy.run(df.iloc[:end_idx].copy(), _FIXED_TS)
            if result.signal != SignalType.HOLD:
                non_hold_count += 1

        assert non_hold_count >= 1, (
            "Expected at least one non-HOLD signal with 1300 daily bars and filters disabled, "
            f"but got {non_hold_count}"
        )

    def test_all_four_signals_are_possible_signal_types(self):
        """All returned signals must be valid SignalType members."""
        strategy = SpecialKStrategy()
        df = _make_daily_ohlcv(1300)

        for end_idx in range(1160, 1300, 10):
            result = strategy.run(df.iloc[:end_idx].copy(), _FIXED_TS)
            assert result.signal in (
                SignalType.LONG,
                SignalType.SHORT,
                SignalType.FLAT,
                SignalType.HOLD,
            ), f"Unexpected signal type: {result.signal}"

    def test_filters_disabled_produce_more_signals(self):
        """Disabling filters should produce >= as many non-HOLD signals as enabled."""
        df = _make_daily_ohlcv(1300)

        strategy_filtered   = SpecialKStrategy(use_zero_filter=True,  use_slope_filter=True)
        strategy_unfiltered = SpecialKStrategy(use_zero_filter=False, use_slope_filter=False)

        filtered_non_hold   = 0
        unfiltered_non_hold = 0

        for end_idx in range(1160, 1300):
            r_f = strategy_filtered.run(df.iloc[:end_idx].copy(), _FIXED_TS)
            r_u = strategy_unfiltered.run(df.iloc[:end_idx].copy(), _FIXED_TS)
            if r_f.signal not in (SignalType.HOLD, SignalType.FLAT):
                filtered_non_hold += 1
            if r_u.signal not in (SignalType.HOLD, SignalType.FLAT):
                unfiltered_non_hold += 1

        assert unfiltered_non_hold >= filtered_non_hold, (
            f"Expected unfiltered ({unfiltered_non_hold}) >= filtered ({filtered_non_hold}) signals"
        )
