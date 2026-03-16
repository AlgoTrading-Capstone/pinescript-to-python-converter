"""Tests for EvasiveSuperTrendStrategySourceSelectStrategy.

Uses the shared ``sample_ohlcv_data`` fixture (1,100 candles, 15-minute bars,
seed=42) from tests/conftest.py.

Test coverage:
  1. Import smoke test
  2. min_bars guard — HOLD when insufficient data
  3. Signal type correctness — all returned values are valid SignalType members
  4. Full run without exceptions across all 1,100 candles
  5. Signal coverage — verify valid signals are produced after MIN_BARS
  6. Synthetic LONG — engineered bear→bull flip must produce SignalType.LONG
  7. Synthetic SHORT — engineered bull→bear flip must produce SignalType.SHORT
  8. Timestamp echo — returned timestamp matches input timestamp
"""

import numpy as np
import pandas as pd
import pytest
from collections import Counter
from datetime import datetime, timezone

from src.base_strategy import SignalType, StrategyRecommendation
from src.strategies.evasive_super_trend_strategy_source_select_strategy import (
    EvasiveSuperTrendStrategySourceSelectStrategy,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

VALID_SIGNALS = {SignalType.LONG, SignalType.SHORT, SignalType.FLAT, SignalType.HOLD}


def _make_df(close: np.ndarray, freq: str = "1h") -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from a close array."""
    n = len(close)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + 5.0
    low = np.minimum(open_, close) - 5.0
    volume = np.full(n, 1000.0)
    dates = pd.date_range(
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        periods=n,
        freq=freq,
        tz="UTC",
    )
    return pd.DataFrame(
        {"date": dates, "open": open_, "high": high, "low": low,
         "close": close, "volume": volume}
    )


# ---------------------------------------------------------------------------
# 1. Import smoke test
# ---------------------------------------------------------------------------

def test_import():
    """Strategy can be imported and instantiated without errors."""
    strategy = EvasiveSuperTrendStrategySourceSelectStrategy()
    assert strategy is not None
    assert strategy.name == "Evasive SuperTrend Strategy [Source Select]"
    assert strategy.timeframe == "1h"
    assert strategy.lookback_hours == 50


# ---------------------------------------------------------------------------
# 2. min_bars guard
# ---------------------------------------------------------------------------

def test_min_bars_guard_returns_hold():
    """Returns HOLD when fewer than MIN_BARS candles are provided."""
    strategy = EvasiveSuperTrendStrategySourceSelectStrategy()
    min_bars = strategy.MIN_BARS

    n = min_bars - 1
    close = np.full(n, 10_000.0)
    df = _make_df(close)
    ts = df["date"].iloc[-1].to_pydatetime()

    rec = strategy.run(df, ts)
    assert rec.signal == SignalType.HOLD, (
        f"Expected HOLD for {n} rows (< MIN_BARS={min_bars}), got {rec.signal}"
    )


def test_single_row_returns_hold():
    """Returns HOLD for a single-row DataFrame."""
    strategy = EvasiveSuperTrendStrategySourceSelectStrategy()
    close = np.array([10_000.0])
    df = _make_df(close)
    ts = df["date"].iloc[-1].to_pydatetime()
    rec = strategy.run(df, ts)
    assert rec.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# 3. Return type correctness
# ---------------------------------------------------------------------------

def test_signal_type_is_valid(sample_ohlcv_data):
    """Every signal returned across all 1,100 candles is a valid SignalType."""
    strategy = EvasiveSuperTrendStrategySourceSelectStrategy()
    df = sample_ohlcv_data

    for i in range(1, len(df) + 1):
        slice_df = df.iloc[:i].copy()
        ts = slice_df["date"].iloc[-1].to_pydatetime()
        rec = strategy.run(slice_df, ts)

        assert isinstance(rec, StrategyRecommendation), (
            f"Bar {i}: expected StrategyRecommendation, got {type(rec)}"
        )
        assert rec.signal in VALID_SIGNALS, (
            f"Bar {i}: unexpected signal value {rec.signal!r}"
        )
        assert rec.timestamp == ts, (
            f"Bar {i}: timestamp mismatch — expected {ts}, got {rec.timestamp}"
        )


# ---------------------------------------------------------------------------
# 4. Full run without exceptions
# ---------------------------------------------------------------------------

def test_full_run_no_exception(sample_ohlcv_data):
    """Strategy runs across all 1,100 candles without raising any exception."""
    strategy = EvasiveSuperTrendStrategySourceSelectStrategy()
    df = sample_ohlcv_data

    signals = []
    for i in range(1, len(df) + 1):
        slice_df = df.iloc[:i].copy()
        ts = slice_df["date"].iloc[-1].to_pydatetime()
        rec = strategy.run(slice_df, ts)
        signals.append(rec.signal)

    assert len(signals) == len(df)


# ---------------------------------------------------------------------------
# 5. Signal coverage (bulk run)
# ---------------------------------------------------------------------------

def test_bulk_run_produces_valid_signals(sample_ohlcv_data):
    """All signals produced after MIN_BARS are valid SignalType members.

    The conftest fixture uses 15m candles while the strategy targets 1h.
    The synthetic price regimes (sideways / bull / bear) should exercise
    the noise-avoidance logic and produce trend flips in at least one phase.
    """
    strategy = EvasiveSuperTrendStrategySourceSelectStrategy()
    df = sample_ohlcv_data

    all_signals = []
    for i in range(strategy.MIN_BARS, len(df) + 1):
        slice_df = df.iloc[:i].copy()
        ts = slice_df["date"].iloc[-1].to_pydatetime()
        rec = strategy.run(slice_df, ts)
        all_signals.append(rec.signal)

    assert len(all_signals) > 0, "Expected at least one evaluation after MIN_BARS"

    invalid = set(all_signals) - VALID_SIGNALS
    assert not invalid, f"Invalid signals found: {invalid}"

    counts = Counter(all_signals)
    print(f"\nSignal distribution (bars {strategy.MIN_BARS}–{len(df)}): {dict(counts)}")


# ---------------------------------------------------------------------------
# 6. Synthetic LONG — bear→bull flip
# ---------------------------------------------------------------------------

def test_synthetic_long_signal():
    """Engineered data must produce SignalType.LONG on a bear→bull flip.

    Construction:
    - 60 bars of flat price at 10,000 with ATR period=10 — allows ATR to converge.
    - Force a bearish trend by making close steadily drop below hl2 - 3×ATR band.
    - Then sharply reverse upward so close crosses above the bearish band on the
      last bar → trend flips from -1 to +1 → LONG.

    We use multiplier=3.0 and threshold=0 to disable noise avoidance for clarity.
    """
    strategy = EvasiveSuperTrendStrategySourceSelectStrategy(
        length_input=10,
        multiplier_input=3.0,
        threshold_input=0.0,  # disable noise avoidance
        alpha_input=0.0,
    )

    n = 80
    # Phase 1 (0-49): flat warmup so ATR converges
    close = np.full(n, 10_000.0, dtype=float)
    # Phase 2 (50-69): gradual decline to build bearish trend
    close[50:70] = np.linspace(10_000.0, 9_600.0, 20)
    # Phase 3 (70-79): sharp reversal — should cross above upper band → flip to bullish
    close[70:] = np.linspace(9_600.0, 11_000.0, 10)

    df = _make_df(close, freq="1h")
    ts = df["date"].iloc[-1].to_pydatetime()

    # Run the full dataset
    rec = strategy.run(df, ts)

    # We expect a non-HOLD signal; specifically LONG or a prior LONG carries through.
    # The final bar should see the bullish flip.
    assert rec.signal in {SignalType.LONG, SignalType.HOLD}, (
        f"Expected LONG (or HOLD if flip didn't occur at last bar), got {rec.signal}"
    )

    # Scan for a LONG signal in the reversal phase
    long_found = False
    for i in range(strategy.MIN_BARS, len(df) + 1):
        slice_df = df.iloc[:i].copy()
        ts_i = slice_df["date"].iloc[-1].to_pydatetime()
        rec_i = strategy.run(slice_df, ts_i)
        if rec_i.signal == SignalType.LONG:
            long_found = True
            break

    assert long_found, (
        "Expected at least one LONG signal during the engineered bull reversal"
    )


# ---------------------------------------------------------------------------
# 7. Synthetic SHORT — bull→bear flip
# ---------------------------------------------------------------------------

def test_synthetic_short_signal():
    """Engineered data must produce SignalType.SHORT on a bull→bear flip.

    Construction:
    - 60 bars of flat price — ATR converges; strategy initialises in bullish trend.
    - Then a sharp decline so close drops below the bullish lower band → flip to -1.
    """
    strategy = EvasiveSuperTrendStrategySourceSelectStrategy(
        length_input=10,
        multiplier_input=3.0,
        threshold_input=0.0,  # disable noise avoidance
        alpha_input=0.0,
    )

    n = 80
    close = np.full(n, 10_000.0, dtype=float)
    # Sharp drop after warmup — close falls well below lower_base (hl2 - 3×ATR)
    close[60:] = np.linspace(10_000.0, 8_500.0, 20)

    df = _make_df(close, freq="1h")

    short_found = False
    for i in range(strategy.MIN_BARS, len(df) + 1):
        slice_df = df.iloc[:i].copy()
        ts_i = slice_df["date"].iloc[-1].to_pydatetime()
        rec_i = strategy.run(slice_df, ts_i)
        if rec_i.signal == SignalType.SHORT:
            short_found = True
            break

    assert short_found, (
        "Expected at least one SHORT signal during the engineered bear decline"
    )


# ---------------------------------------------------------------------------
# 8. Timestamp echo
# ---------------------------------------------------------------------------

def test_returns_recommendation_with_correct_timestamp(sample_ohlcv_data):
    """The returned StrategyRecommendation.timestamp matches the input timestamp."""
    strategy = EvasiveSuperTrendStrategySourceSelectStrategy()
    df = sample_ohlcv_data.iloc[: strategy.MIN_BARS + 50].copy()
    ts = df["date"].iloc[-1].to_pydatetime()
    rec = strategy.run(df, ts)
    assert rec.timestamp == ts
