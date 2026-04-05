"""Tests for TrendPullbackMomentumSideAwareStrategy.

Note on HTF bias in tests
--------------------------
The strategy's 2h EMA bias filter (bias_ema_len=200) requires ~1,600 base-TF
candles (200 × 8 bars/2h) to converge, but the shared fixture has only 1,100
candles.  Tests that verify directional signal logic therefore instantiate the
strategy with ``use_htf_bias=False`` (or ``require_pullback=False`` where noted)
to isolate base-TF logic.  A separate smoke test confirms no exception is raised
with the default (HTF bias ON) configuration.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.base_strategy import BaseStrategy, SignalType, StrategyRecommendation
from src.strategies.trend_pullback_momentum_side_aware_strategy import (
    TrendPullbackMomentumSideAwareStrategy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> datetime:
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_ohlcv(close: np.ndarray, spread: float = 50.0) -> pd.DataFrame:
    """Build OHLCV where high = max(open,close)+spread, low = min(open,close)-spread."""
    n = len(close)
    dates = [
        datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * i)
        for i in range(n)
    ]
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    rng = np.random.default_rng(0)
    noise = np.abs(rng.normal(0, spread * 0.2, n))
    return pd.DataFrame({
        "date": pd.to_datetime(dates, utc=True),
        "open": open_,
        "high": np.maximum(open_, close) + noise,
        "low": np.minimum(open_, close) - noise,
        "close": close,
        "volume": np.ones(n) * 500.0,
    })


def _no_htf(**kwargs) -> TrendPullbackMomentumSideAwareStrategy:
    """Return strategy with HTF bias disabled for test isolation."""
    return TrendPullbackMomentumSideAwareStrategy(use_htf_bias=False, **kwargs)


def _no_filter(**kwargs) -> TrendPullbackMomentumSideAwareStrategy:
    """Return strategy with both HTF bias and pullback filter disabled."""
    return TrendPullbackMomentumSideAwareStrategy(
        use_htf_bias=False, require_pullback=False, **kwargs
    )


# ---------------------------------------------------------------------------
# Contract / structural tests
# ---------------------------------------------------------------------------


class TestContract:
    def test_inherits_base_strategy(self):
        assert issubclass(TrendPullbackMomentumSideAwareStrategy, BaseStrategy)

    def test_properties(self):
        s = TrendPullbackMomentumSideAwareStrategy()
        assert s.name == "TrendPullbackMomentumSideAware"
        assert s.timeframe == "15m"
        assert s.lookback_hours == 400
        assert isinstance(s.description, str) and len(s.description) > 0

    def test_min_candles_required_is_dynamic(self):
        s_default = TrendPullbackMomentumSideAwareStrategy()
        assert s_default.MIN_CANDLES_REQUIRED == 3 * 200  # bias_ema_len dominates

        s_longer = TrendPullbackMomentumSideAwareStrategy(bias_ema_len=300)
        assert s_longer.MIN_CANDLES_REQUIRED == 3 * 300

        s_no_htf = TrendPullbackMomentumSideAwareStrategy(
            bias_ema_len=10, trend_ema_len=50
        )
        assert s_no_htf.MIN_CANDLES_REQUIRED == 3 * 50  # trend_ema_len dominates

    def test_run_returns_strategy_recommendation(self, sample_ohlcv_data):
        s = _no_htf()
        result = s.run(sample_ohlcv_data, _ts())
        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)
        assert isinstance(result.timestamp, datetime)

    def test_run_preserves_timestamp(self, sample_ohlcv_data):
        s = _no_htf()
        ts = _ts()
        result = s.run(sample_ohlcv_data, ts)
        assert result.timestamp == ts


# ---------------------------------------------------------------------------
# Default parameters
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_params_match_pine(self):
        s = TrendPullbackMomentumSideAwareStrategy()
        assert s.use_htf_bias is True
        assert s.bias_ema_len == 200
        assert s.trade_side == "Both"
        assert s.trend_ema_len == 21
        assert s.require_pullback is True
        assert s.long_pullback_atr_buffer == 0.35
        assert s.long_reclaim_bars == 3
        assert s.long_rsi_min == 50.0
        assert s.short_pullback_atr_buffer == 0.35
        assert s.short_reclaim_bars == 3
        assert s.short_rsi_max == 50.0
        assert s.rsi_len == 14
        assert s.require_rsi_reclaim is True
        assert s.use_volume_expansion is False
        assert s.volume_len == 20
        assert s.atr_len == 14
        assert s.swing_lookback == 10


# ---------------------------------------------------------------------------
# Warmup guard — Phase 0
# ---------------------------------------------------------------------------


class TestWarmupGuard:
    def test_empty_df_returns_hold(self):
        s = TrendPullbackMomentumSideAwareStrategy()
        empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        assert s.run(empty, _ts()).signal == SignalType.HOLD

    def test_single_bar_returns_hold(self):
        s = TrendPullbackMomentumSideAwareStrategy()
        tiny = _make_ohlcv(np.array([10_000.0]))
        assert s.run(tiny, _ts()).signal == SignalType.HOLD

    def test_below_min_candles_returns_hold(self):
        s = _no_htf()
        short_df = _make_ohlcv(np.full(s.MIN_CANDLES_REQUIRED - 1, 10_000.0))
        assert s.run(short_df, _ts()).signal == SignalType.HOLD

    def test_warmup_slice_from_fixture_returns_hold(self, sample_ohlcv_data):
        """Slice shorter than MIN_CANDLES_REQUIRED (600) must return HOLD."""
        s = _no_htf()
        warmup = sample_ohlcv_data.iloc[: s.MIN_CANDLES_REQUIRED - 1].reset_index(drop=True)
        assert s.run(warmup, _ts()).signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# Signal tests
# ---------------------------------------------------------------------------


class TestSignals:
    def test_no_exception_with_default_params(self, sample_ohlcv_data):
        """Full fixture with default params (HTF bias ON) must not raise."""
        s = TrendPullbackMomentumSideAwareStrategy()
        result = s.run(sample_ohlcv_data, _ts())
        assert result.signal in list(SignalType)

    def test_valid_signal_without_htf_bias(self, sample_ohlcv_data):
        """With HTF bias disabled, strategy returns a valid SignalType."""
        s = _no_htf()
        result = s.run(sample_ohlcv_data, _ts())
        assert result.signal in list(SignalType)

    def test_flat_market_returns_hold(self):
        """Completely flat prices → no trend, no reclaim → HOLD."""
        close = np.full(700, 10_000.0)
        df = _make_ohlcv(close, spread=0.01)
        s = _no_htf()
        assert s.run(df, _ts()).signal == SignalType.HOLD

    def test_non_hold_signal_produced_across_full_run(self, sample_ohlcv_data):
        """At least one non-HOLD signal must appear over the full 1,100-bar run."""
        s = _no_filter()
        signals = set()
        for end in range(s.MIN_CANDLES_REQUIRED, len(sample_ohlcv_data) + 1, 5):
            subset = sample_ohlcv_data.iloc[:end].reset_index(drop=True)
            signals.add(s.run(subset, _ts()).signal)
        assert signals - {SignalType.HOLD}, (
            "Strategy never produced a non-HOLD signal across 1,100 bars "
            "(use_htf_bias=False, require_pullback=False)"
        )

    def test_long_signal_during_bull_phase(self, sample_ohlcv_data):
        """Bull run (bars 700-900) must produce at least one LONG signal."""
        s = _no_filter()
        signals = set()
        for end in range(700, 901):
            subset = sample_ohlcv_data.iloc[:end].reset_index(drop=True)
            signals.add(s.run(subset, _ts()).signal)
            if SignalType.LONG in signals:
                break
        assert SignalType.LONG in signals, (
            "No LONG signal detected during bull run (bars 700-900, "
            "use_htf_bias=False, require_pullback=False)"
        )

    def test_short_signal_during_bear_phase(self, sample_ohlcv_data):
        """Bear crash (bars 900-1100) must produce at least one SHORT signal."""
        s = _no_filter()
        signals = set()
        for end in range(900, 1101):
            subset = sample_ohlcv_data.iloc[:end].reset_index(drop=True)
            signals.add(s.run(subset, _ts()).signal)
            if SignalType.SHORT in signals:
                break
        assert SignalType.SHORT in signals, (
            "No SHORT signal detected during bear crash (bars 900-1100, "
            "use_htf_bias=False, require_pullback=False)"
        )

    def test_exact_min_candles_no_error(self):
        """Strategy at exactly MIN_CANDLES_REQUIRED bars must not raise."""
        s = _no_htf()
        df = _make_ohlcv(np.full(s.MIN_CANDLES_REQUIRED, 10_000.0))
        result = s.run(df, _ts())
        assert result.signal in list(SignalType)

    def test_trade_side_long_only_never_shorts(self, sample_ohlcv_data):
        """trade_side='Long only' must never produce SHORT signals."""
        s = _no_filter(trade_side="Long only")
        for end in range(s.MIN_CANDLES_REQUIRED, len(sample_ohlcv_data) + 1, 20):
            subset = sample_ohlcv_data.iloc[:end].reset_index(drop=True)
            assert s.run(subset, _ts()).signal != SignalType.SHORT, (
                f"Unexpected SHORT at bar {end} with trade_side='Long only'"
            )

    def test_trade_side_short_only_never_longs(self, sample_ohlcv_data):
        """trade_side='Short only' must never produce LONG signals."""
        s = _no_filter(trade_side="Short only")
        for end in range(s.MIN_CANDLES_REQUIRED, len(sample_ohlcv_data) + 1, 20):
            subset = sample_ohlcv_data.iloc[:end].reset_index(drop=True)
            assert s.run(subset, _ts()).signal != SignalType.LONG, (
                f"Unexpected LONG at bar {end} with trade_side='Short only'"
            )
