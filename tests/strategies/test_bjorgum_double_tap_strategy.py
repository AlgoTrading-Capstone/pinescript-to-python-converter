"""Tests for BjorgumDoubleTapStrategy.

Uses the shared `sample_ohlcv_data` fixture (1,100 candles, 15-minute bars,
seed=42) from tests/conftest.py.

Test coverage:
  1. Import smoke test
  2. min_bars guard — HOLD when insufficient data
  3. Signal type correctness — all returned values are valid SignalType members
  4. Full run without exceptions across all 1,100 candles
  5. Signal coverage — verify LONG, SHORT or FLAT/HOLD signals are produced
  6. Pattern detection — synthetic double top MUST produce strict SHORT signal
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from src.base_strategy import SignalType, StrategyRecommendation
from src.strategies.bjorgum_double_tap_strategy import BjorgumDoubleTapStrategy


# ---------------------------------------------------------------------------
# 1. Import smoke test
# ---------------------------------------------------------------------------

def test_import():
    """Strategy can be imported and instantiated without errors."""
    strategy = BjorgumDoubleTapStrategy()
    assert strategy is not None
    assert strategy.name == "Bjorgum Double Tap"
    assert strategy.timeframe == "4h"
    assert strategy.lookback_hours == 1200


# ---------------------------------------------------------------------------
# 2. min_bars guard
# ---------------------------------------------------------------------------

def test_min_bars_guard_returns_hold():
    """Returns HOLD when fewer than MIN_BARS candles are provided."""
    strategy = BjorgumDoubleTapStrategy()
    min_bars = strategy.MIN_BARS

    # Create a minimal OHLCV DataFrame with one row fewer than MIN_BARS
    n = min_bars - 1
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    dates = pd.date_range(start=start, periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame({
        "date": dates,
        "open":   np.full(n, 100.0),
        "high":   np.full(n, 101.0),
        "low":    np.full(n, 99.0),
        "close":  np.full(n, 100.5),
        "volume": np.full(n, 1000.0),
    })

    rec = strategy.run(df, dates[-1].to_pydatetime())
    assert rec.signal == SignalType.HOLD, (
        f"Expected HOLD for df with {n} rows (< MIN_BARS={min_bars}), got {rec.signal}"
    )


def test_single_row_returns_hold():
    """Returns HOLD for a single-row DataFrame."""
    strategy = BjorgumDoubleTapStrategy()
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    df = pd.DataFrame({
        "date": [ts],
        "open": [100.0], "high": [101.0], "low": [99.0],
        "close": [100.5], "volume": [1000.0],
    })
    rec = strategy.run(df, ts)
    assert rec.signal == SignalType.HOLD


# ---------------------------------------------------------------------------
# 3. Return type correctness
# ---------------------------------------------------------------------------

VALID_SIGNALS = {SignalType.LONG, SignalType.SHORT, SignalType.FLAT, SignalType.HOLD}


def test_signal_type_is_valid(sample_ohlcv_data):
    """Every signal returned across all 1,100 candles is a valid SignalType."""
    strategy = BjorgumDoubleTapStrategy()
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
    strategy = BjorgumDoubleTapStrategy()
    df = sample_ohlcv_data

    signals = []
    for i in range(1, len(df) + 1):
        slice_df = df.iloc[:i].copy()
        ts = slice_df["date"].iloc[-1].to_pydatetime()
        rec = strategy.run(slice_df, ts)
        signals.append(rec.signal)

    assert len(signals) == len(df)


# ---------------------------------------------------------------------------
# 5. Signal coverage — bulk run
# ---------------------------------------------------------------------------

def test_bulk_run_produces_signals(sample_ohlcv_data):
    """At least HOLD signals are produced; strategy completes without error.

    The conftest fixture uses 15m candles while the strategy targets 4h, so the
    synthetic price movements may or may not trigger pattern-match conditions.
    We verify that the strategy runs correctly and returns valid signals.
    A mix of HOLD and non-HOLD signals is ideal but we accept all-HOLD if the
    pattern does not arise in this specific dataset.
    """
    strategy = BjorgumDoubleTapStrategy()
    df = sample_ohlcv_data

    # Run only for the full dataset (faster — no rolling slice)
    all_signals = []
    for i in range(strategy.MIN_BARS, len(df) + 1):
        slice_df = df.iloc[:i].copy()
        ts = slice_df["date"].iloc[-1].to_pydatetime()
        rec = strategy.run(slice_df, ts)
        all_signals.append(rec.signal)

    assert len(all_signals) > 0, "Expected at least one evaluation after MIN_BARS"

    valid = set(all_signals) <= VALID_SIGNALS
    assert valid, f"Invalid signals found: {set(all_signals) - VALID_SIGNALS}"

    # Log signal distribution (useful during debugging)
    from collections import Counter
    counts = Counter(all_signals)
    print(f"\nSignal distribution (bars {strategy.MIN_BARS}–{len(df)}): {dict(counts)}")


# ---------------------------------------------------------------------------
# 6. Pattern detection — strict SHORT on verified synthetic double top
# ---------------------------------------------------------------------------

def _make_double_top_df() -> pd.DataFrame:
    """Craft an OHLCV DataFrame that triggers a verified SHORT (double top) signal.

    Uses len_=5 (short pivot window) to minimise bars required.
    Each bar is engineered so that exactly the following pivot sequence fires
    inside BjorgumDoubleTapStrategy._build_pivot_list:

        idx 120 → p1 (low,  direction=0)  lbar=0, piv_low=400
        idx 125 → p2 (high, direction=1)  hbar=0, piv_high=600
        idx 130 → p3 (low,  direction=0)  lbar=0, piv_low=515  ← neckline y3
        idx 135 → p4 (high, direction=1)  hbar=0, piv_high=595  ← second top
        idx 137 → p5 (low,  direction=0)  lbar=0 (updated at 138 to piv_low=511)

    At idx 138 (final bar):
        close_prev = 516 >= y3=515   ✓
        close_curr = 513 <  y3=515   ✓  → neckline cross-down → SHORT
        |y4 - y2| = |595-600| = 5,  height = 82.5,  tol_band = 12.375  ✓
    """
    N = 139  # bars 0-138

    close = np.empty(N)
    high  = np.empty(N)
    low   = np.empty(N)

    # ---- Warmup (bars 0-119): flat — no hbar/lbar ever fires ----
    close[:120] = 500.0
    high[:120]  = 505.0
    low[:120]   = 495.0

    # ---- Bar 120: p1 (lbar=0 with len_=5, window=[495,495,495,495,400]) ----
    close[120] = 410.0;  high[120] = 415.0;  low[120] = 400.0

    # ---- Bars 121-124: quiet descent — bar 120's high (415) stays window max,
    #      so hbar never fires; bar 120's low (400) stays window min, lbar never fires ----
    for k, i in enumerate(range(121, 125)):
        close[i] = 412.0 - k * 2   # 412, 410, 408, 406
        high[i]  = 414.0 - k * 2   # 414, 412, 410, 408  (all < 415)
        low[i]   = 410.0 - k * 2   # 410, 408, 406, 404  (all > 400)

    # ---- Bar 125: p2 (hbar=0, window high=[414,412,410,408,600]=600)
    #      low=400 matches p1 low — shields bars 126-129 from spurious lbar ----
    close[125] = 590.0;  high[125] = 600.0;  low[125] = 400.0

    # ---- Bars 126-129: pullback from first top.
    #      bar 125's low=400 keeps window min=400, so no lbar fires here ----
    close[126] = 570.0;  high[126] = 572.0;  low[126] = 568.0
    close[127] = 555.0;  high[127] = 557.0;  low[127] = 553.0
    close[128] = 540.0;  high[128] = 542.0;  low[128] = 538.0
    close[129] = 525.0;  high[129] = 527.0;  low[129] = 523.0

    # ---- Bar 130: p3 neckline (lbar=0, window low=[568,553,538,523,515]=515) ----
    close[130] = 520.0;  high[130] = 525.0;  low[130] = 515.0   # y3 = 515

    # ---- Bars 131-134: quiet rise. bar 130's low (515) stays window min
    #      while in window, preventing spurious lbar; highs stay below window max ----
    close[131] = 520.0;  high[131] = 522.0;  low[131] = 518.0
    close[132] = 520.0;  high[132] = 522.0;  low[132] = 520.0
    close[133] = 520.0;  high[133] = 522.0;  low[133] = 522.0
    close[134] = 520.0;  high[134] = 522.0;  low[134] = 524.0

    # ---- Bar 135: p4 second top (hbar=0, window high=[522,522,522,522,595]=595)
    #      |piv_high[135]-piv_high[125]| = |595-600| = 5 < tol_band=12.375 ✓ ----
    close[135] = 590.0;  high[135] = 595.0;  low[135] = 585.0

    # ---- Bar 136: descent, no pivot ----
    close[136] = 560.0;  high[136] = 562.0;  low[136] = 558.0

    # ---- Bar 137: lbar=0 (window low=[522,524,585,558,514]=514) → p5 appended.
    #      close=516 >= neckline 515 (this is close_prev for the signal bar) ----
    close[137] = 516.0;  high[137] = 519.0;  low[137] = 514.0

    # ---- Bar 138: neckline cross-down.
    #      lbar=0 again (window low=[524,585,558,514,511]=511) → p5 updated (still dir=0).
    #      close_curr=513 < y3=515  AND  close_prev=516 >= y3=515  → SHORT ----
    close[138] = 513.0;  high[138] = 516.0;  low[138] = 511.0

    open_ = np.roll(close, 1)
    open_[0] = close[0]

    dates = pd.date_range(
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        periods=N,
        freq="4h",
        tz="UTC",
    )

    return pd.DataFrame({
        "date":   dates,
        "open":   open_,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": np.full(N, 1000.0),
    })


def test_double_top_produces_short():
    """Synthetic double top MUST produce a strict SHORT signal.

    The fixture data is engineered bar-by-bar to satisfy the exact mathematical
    conditions of BjorgumDoubleTapStrategy (len_=5, tol=15%).  The assertion is
    strict — HOLD is not acceptable.  If this test fails, the pivot detection or
    neckline crossdown logic is broken.
    """
    strategy = BjorgumDoubleTapStrategy(len_=5)
    df = _make_double_top_df()
    ts = df["date"].iloc[-1].to_pydatetime()

    rec = strategy.run(df, ts)

    assert rec.signal == SignalType.SHORT, (
        f"Expected SignalType.SHORT for verified double top, got {rec.signal}"
    )


def test_returns_recommendation_with_correct_timestamp(sample_ohlcv_data):
    """The returned StrategyRecommendation.timestamp matches the input timestamp."""
    strategy = BjorgumDoubleTapStrategy()
    df = sample_ohlcv_data.iloc[: strategy.MIN_BARS + 50].copy()
    ts = df["date"].iloc[-1].to_pydatetime()
    rec = strategy.run(df, ts)
    assert rec.timestamp == ts