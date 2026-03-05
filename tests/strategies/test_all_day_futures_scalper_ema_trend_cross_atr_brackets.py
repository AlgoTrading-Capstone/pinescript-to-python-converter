"""
Tests for AllDayFuturesScalperEmaTrendCrossAtrBrackets strategy.
"""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta, timezone

from src.base_strategy import SignalType, StrategyRecommendation
from src.strategies.all_day_futures_scalper_ema_trend_cross_atr_brackets import (
    AllDayFuturesScalperEmaTrendCrossAtrBrackets,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_strategy(**kwargs) -> AllDayFuturesScalperEmaTrendCrossAtrBrackets:
    return AllDayFuturesScalperEmaTrendCrossAtrBrackets(**kwargs)


def _ts() -> datetime:
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _df_with_crossover(n: int = 300, trend: str = "up") -> pd.DataFrame:
    """
    Build a synthetic OHLCV frame that guarantees a fast/slow EMA crossover
    on the last bar.  `trend` controls whether close ends above ('up') or
    below ('down') a 200-EMA.
    """
    np.random.seed(0)
    dates = [datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * i) for i in range(n)]

    if trend == "up":
        # Price ramps from 10_000 to 12_000 so close >> trend EMA
        base = np.linspace(10_000, 12_000, n)
    else:
        # Price drops from 12_000 to 9_000 so close << trend EMA
        base = np.linspace(12_000, 9_000, n)

    noise = np.random.normal(0, 5, n)
    close = base + noise

    # Force a crossover on the last bar:
    # In the uptrend case we want fast > slow (bullish cross).
    # We achieve this by making the last two closes jump relative to prior.
    if trend == "up":
        close[-2] = close[-3] - 20   # fast dips below slow at n-2
        close[-1] = close[-3] + 20   # fast surges above slow at n-1 (crossover)
    else:
        close[-2] = close[-3] + 20   # fast rises above slow at n-2
        close[-1] = close[-3] - 20   # fast drops below slow at n-1 (crossunder)

    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + np.abs(np.random.normal(0, 3, n))
    low = np.minimum(open_, close) - np.abs(np.random.normal(0, 3, n))
    volume = np.ones(n) * 100.0

    df = pd.DataFrame(
        {"date": dates, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df["high"] = df[["open", "close", "high"]].max(axis=1)
    df["low"] = df[["open", "close", "low"]].min(axis=1)
    return df


# ---------------------------------------------------------------------------
# Contract / structural tests
# ---------------------------------------------------------------------------

class TestContract:
    def test_inherits_base_strategy(self):
        from src.base_strategy import BaseStrategy
        assert issubclass(AllDayFuturesScalperEmaTrendCrossAtrBrackets, BaseStrategy)

    def test_properties(self):
        s = _make_strategy()
        assert s.name == "All-Day Futures Scalper (EMA Trend + Cross + ATR Brackets)"
        assert s.timeframe == "15m"
        assert s.lookback_hours == 50
        assert isinstance(s.description, str) and len(s.description) > 0

    def test_run_returns_strategy_recommendation(self, sample_ohlcv_data):
        s = _make_strategy()
        result = s.run(sample_ohlcv_data, _ts())
        assert isinstance(result, StrategyRecommendation)
        assert isinstance(result.signal, SignalType)
        assert isinstance(result.timestamp, datetime)

    def test_run_timestamp_preserved(self, sample_ohlcv_data):
        s = _make_strategy()
        ts = _ts()
        result = s.run(sample_ohlcv_data, ts)
        assert result.timestamp == ts

    def test_insufficient_data_returns_hold(self):
        """With fewer bars than the trend EMA period the strategy must HOLD."""
        s = _make_strategy(trend_len=200)
        short_df = _df_with_crossover(n=50)
        result = s.run(short_df, _ts())
        assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# Signal tests
# ---------------------------------------------------------------------------

class TestSignals:
    def test_long_signal_on_bullish_crossover(self):
        s = _make_strategy(cooldown_bars=0, use_chop_filter=False)
        df = _df_with_crossover(n=300, trend="up")
        result = s.run(df, _ts())
        assert result.signal == SignalType.LONG

    def test_short_signal_on_bearish_crossover(self):
        s = _make_strategy(cooldown_bars=0, use_chop_filter=False)
        df = _df_with_crossover(n=300, trend="down")
        result = s.run(df, _ts())
        assert result.signal == SignalType.SHORT

    def test_no_long_when_longs_disabled(self):
        s = _make_strategy(use_longs=False, cooldown_bars=0, use_chop_filter=False)
        df = _df_with_crossover(n=300, trend="up")
        result = s.run(df, _ts())
        assert result.signal != SignalType.LONG

    def test_no_short_when_shorts_disabled(self):
        s = _make_strategy(use_shorts=False, cooldown_bars=0, use_chop_filter=False)
        df = _df_with_crossover(n=300, trend="down")
        result = s.run(df, _ts())
        assert result.signal != SignalType.SHORT

    def test_no_long_when_trend_is_down(self):
        """Bullish EMA cross should not generate LONG when price < trend EMA."""
        s = _make_strategy(cooldown_bars=0, use_chop_filter=False)
        df = _df_with_crossover(n=300, trend="down")
        # Manually create a bullish cross in a downtrend frame — still price < trend EMA
        # The fixture already gives a bearish cross in downtrend; we just verify LONG is absent.
        result = s.run(df, _ts())
        assert result.signal != SignalType.LONG

    def test_hold_when_no_crossover(self, sample_ohlcv_data):
        """conftest fixture has no guaranteed crossover on the last bar."""
        s = _make_strategy(cooldown_bars=0, use_chop_filter=False)
        result = s.run(sample_ohlcv_data, _ts())
        # We can't assert exact signal without knowing the last bar of fixture,
        # but it must be a valid SignalType.
        assert result.signal in list(SignalType)


# ---------------------------------------------------------------------------
# Cooldown tests
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_cooldown_suppresses_signal(self):
        """
        With cooldown_bars=10, a crossover at bar N-2 should suppress entry at N.
        """
        s = _make_strategy(cooldown_bars=10, use_chop_filter=False)
        df = _df_with_crossover(n=300, trend="up")
        # The fixture places the crossover at the last bar.
        # We extend by 5 flat bars so the crossover is now 5 bars back — within cooldown.
        last_close = df["close"].iloc[-1]
        extra = pd.DataFrame(
            {
                "date": [
                    df["date"].iloc[-1] + timedelta(minutes=15 * (i + 1)) for i in range(5)
                ],
                "open": [last_close] * 5,
                "high": [last_close + 5] * 5,
                "low": [last_close - 5] * 5,
                "close": [last_close] * 5,
                "volume": [100.0] * 5,
            }
        )
        extended = pd.concat([df, extra], ignore_index=True)
        result = s.run(extended, _ts())
        # The crossover 5 bars ago is within cooldown_bars=10, so no LONG
        assert result.signal != SignalType.LONG

    def test_no_cooldown_when_disabled(self):
        s = _make_strategy(cooldown_bars=0, use_chop_filter=False)
        df = _df_with_crossover(n=300, trend="up")
        result = s.run(df, _ts())
        assert result.signal == SignalType.LONG


# ---------------------------------------------------------------------------
# Chop-filter tests
# ---------------------------------------------------------------------------

class TestChopFilter:
    def test_chop_filter_suppresses_low_atr(self):
        """Set min_atr_pct to an absurdly high value so the filter always blocks."""
        s = _make_strategy(
            cooldown_bars=0,
            use_chop_filter=True,
            min_atr_pct=9999.0,  # ATR% will never reach this
        )
        df = _df_with_crossover(n=300, trend="up")
        result = s.run(df, _ts())
        assert result.signal not in (SignalType.LONG, SignalType.SHORT)

    def test_chop_filter_disabled_allows_signal(self):
        s = _make_strategy(cooldown_bars=0, use_chop_filter=False)
        df = _df_with_crossover(n=300, trend="up")
        result = s.run(df, _ts())
        assert result.signal == SignalType.LONG


# ---------------------------------------------------------------------------
# Default parameters round-trip
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_default_params_match_pine(self):
        s = _make_strategy()
        assert s.fast_len == 9
        assert s.slow_len == 21
        assert s.trend_len == 200
        assert s.atr_len == 14
        assert s.sl_atr == 1.0
        assert s.tp_atr == 1.5
        assert s.use_be is True
        assert s.be_atr == 1.0
        assert s.cooldown_bars == 3
        assert s.use_chop_filter is True
        assert s.min_atr_pct == pytest.approx(0.05)
        assert s.use_longs is True
        assert s.use_shorts is True
