"""
Tests for GapFillingStrategy.

Coverage:
1. HOLD returned during warmup phase (len(df) < MIN_CANDLES_REQUIRED).
2. Strategy runs without exceptions on full sample_ohlcv_data fixture.
3. Returns valid SignalType values across all phases.
4. LONG signal on new-session down-gap.
5. SHORT signal on new-session up-gap.
6. FLAT signal on new session with no gap.
7. HOLD within intra-session bars (no new session).
8. Edge cases: empty DataFrame, 2-row DataFrame.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.base_strategy import SignalType
from src.strategies.gap_filling_strategy import GapFillingStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_flat_df(n: int, price: float = 10_000.0) -> pd.DataFrame:
    """Build an n-row daily DataFrame with no gaps, all on the same calendar day."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    dates = [start + timedelta(hours=i) for i in range(n)]
    return pd.DataFrame({
        "date":   pd.to_datetime(dates, utc=True),
        "open":   price,
        "high":   price + 5.0,
        "low":    price - 5.0,
        "close":  price,
        "volume": 1000.0,
    })


def _make_gap_df(gap_type: str) -> pd.DataFrame:
    """
    Build a minimal 3-row DataFrame where the last bar contains a qualifying gap
    on a new calendar day.

    gap_type:
      "up"   → open > prev_high, body fully above prev body  → SHORT signal
      "down" → open < prev_low,  body fully below prev body  → LONG  signal
      "none" → new session, open inside prev range           → FLAT  signal
    """
    day1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    day2 = datetime(2024, 1, 2, tzinfo=timezone.utc)

    base = 10_000.0

    # Bar 0 and Bar 1 are on day1 (no new session on bar 1)
    rows = [
        {"date": day1, "open": base, "high": base + 50, "low": base - 50, "close": base + 10, "volume": 1000},
        {"date": day1 + timedelta(hours=1), "open": base, "high": base + 50, "low": base - 50, "close": base + 10, "volume": 1000},
    ]

    if gap_type == "up":
        # Bar 2 on day2: open well above prev high, close also above prev body top
        prev_high = base + 50
        gap_open = prev_high + 100   # open > prev high
        gap_close = gap_open + 20    # close > open (body fully above prev body)
        rows.append({
            "date": day2, "open": gap_open,
            "high": gap_close + 10, "low": gap_open - 5,
            "close": gap_close, "volume": 2000,
        })
    elif gap_type == "down":
        # Bar 2 on day2: open well below prev low, close also below prev body bottom
        prev_low = base - 50
        gap_open = prev_low - 100   # open < prev low
        gap_close = gap_open - 20   # close < open (body fully below prev body)
        rows.append({
            "date": day2, "open": gap_open,
            "high": gap_open + 5, "low": gap_close - 10,
            "close": gap_close, "volume": 2000,
        })
    else:  # "none" — new session, no significant gap
        rows.append({
            "date": day2, "open": base + 2,
            "high": base + 55, "low": base - 55,
            "close": base + 5, "volume": 1500,
        })

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df


# ---------------------------------------------------------------------------
# Test: Warmup guard
# ---------------------------------------------------------------------------

class TestWarmupGuard:
    def test_hold_on_empty_df(self):
        strat = GapFillingStrategy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = strat.run(pd.DataFrame(), ts)
        assert result.signal == SignalType.HOLD

    def test_hold_below_min_candles(self):
        strat = GapFillingStrategy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        df = _make_flat_df(strat.MIN_CANDLES_REQUIRED - 1)
        result = strat.run(df, ts)
        assert result.signal == SignalType.HOLD

    def test_runs_at_min_candles(self):
        strat = GapFillingStrategy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        df = _make_flat_df(strat.MIN_CANDLES_REQUIRED)
        result = strat.run(df, ts)
        # No gap in flat data; all bars same day → HOLD (not a new session)
        assert result.signal in {SignalType.HOLD, SignalType.FLAT}


# ---------------------------------------------------------------------------
# Test: Full fixture smoke test
# ---------------------------------------------------------------------------

class TestFixtureSmoke:
    def test_runs_without_exception(self, sample_ohlcv_data):
        strat = GapFillingStrategy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = strat.run(sample_ohlcv_data, ts)
        assert result is not None

    def test_returns_valid_signal(self, sample_ohlcv_data):
        strat = GapFillingStrategy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = strat.run(sample_ohlcv_data, ts)
        assert result.signal in {SignalType.LONG, SignalType.SHORT, SignalType.FLAT, SignalType.HOLD}

    def test_timestamp_preserved(self, sample_ohlcv_data):
        strat = GapFillingStrategy()
        ts = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = strat.run(sample_ohlcv_data, ts)
        assert result.timestamp == ts

    def test_warmup_phase_holds(self, sample_ohlcv_data):
        """Strategy must return HOLD when fewer bars than MIN_CANDLES_REQUIRED are supplied."""
        strat = GapFillingStrategy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        slice_df = sample_ohlcv_data.iloc[: strat.MIN_CANDLES_REQUIRED - 1].copy()
        result = strat.run(slice_df, ts)
        assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# Test: Signal correctness
# ---------------------------------------------------------------------------

class TestSignalCorrectness:
    def test_long_on_down_gap(self):
        """New session + down gap → LONG (default invert=False)."""
        strat = GapFillingStrategy(invert=False)
        ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
        df = _make_gap_df("down")
        result = strat.run(df, ts)
        assert result.signal == SignalType.LONG

    def test_short_on_up_gap(self):
        """New session + up gap → SHORT (default invert=False)."""
        strat = GapFillingStrategy(invert=False)
        ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
        df = _make_gap_df("up")
        result = strat.run(df, ts)
        assert result.signal == SignalType.SHORT

    def test_flat_on_new_session_no_gap(self):
        """New session with no significant gap → FLAT (close any open position)."""
        strat = GapFillingStrategy(invert=False)
        ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
        df = _make_gap_df("none")
        result = strat.run(df, ts)
        assert result.signal == SignalType.FLAT

    def test_invert_long_on_up_gap(self):
        """With invert=True: up gap → LONG."""
        strat = GapFillingStrategy(invert=True)
        ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
        df = _make_gap_df("up")
        result = strat.run(df, ts)
        assert result.signal == SignalType.LONG

    def test_invert_short_on_down_gap(self):
        """With invert=True: down gap → SHORT."""
        strat = GapFillingStrategy(invert=True)
        ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
        df = _make_gap_df("down")
        result = strat.run(df, ts)
        assert result.signal == SignalType.SHORT

    def test_hold_within_session(self):
        """No new session → HOLD regardless of price action."""
        strat = GapFillingStrategy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # All bars same day: ses is always False after the first bar check
        df = _make_flat_df(5)
        result = strat.run(df, ts)
        assert result.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_all_nan_ohlc_does_not_raise(self):
        strat = GapFillingStrategy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        df = pd.DataFrame({
            "date":   pd.to_datetime([start + timedelta(days=i) for i in range(5)], utc=True),
            "open":   np.nan,
            "high":   np.nan,
            "low":    np.nan,
            "close":  np.nan,
            "volume": np.nan,
        })
        # Should not raise; result may be FLAT or HOLD (NaN comparisons → False)
        result = strat.run(df, ts)
        assert result.signal in {SignalType.HOLD, SignalType.FLAT}

    def test_single_row_returns_hold(self):
        strat = GapFillingStrategy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        df = _make_flat_df(1)
        result = strat.run(df, ts)
        assert result.signal == SignalType.HOLD

    def test_idempotent_across_calls(self, sample_ohlcv_data):
        """Calling run twice on the same data must return the same signal."""
        strat = GapFillingStrategy()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        r1 = strat.run(sample_ohlcv_data.copy(), ts)
        r2 = strat.run(sample_ohlcv_data.copy(), ts)
        assert r1.signal == r2.signal
