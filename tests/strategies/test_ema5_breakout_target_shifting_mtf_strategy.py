"""Tests for the EMA5 Breakout with Target Shifting (MTF / Buffer) strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.base_strategy import SignalType
from src.strategies.ema5_breakout_target_shifting_mtf_strategy import (
    Ema5BreakoutTargetShiftingMtfStrategy,
)


VALID_SIGNALS = {"LONG", "SHORT", "FLAT", "HOLD"}


# ---- batch-mode tests --------------------------------------------------------


def test_batch_shape_and_index_match_input(sample_ohlcv_data):
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    sig = s.generate_all_signals(sample_ohlcv_data)
    assert isinstance(sig, pd.Series)
    assert len(sig) == len(sample_ohlcv_data)
    assert sig.index.equals(sample_ohlcv_data.index)


def test_batch_values_are_valid_signal_strings(sample_ohlcv_data):
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    sig = s.generate_all_signals(sample_ohlcv_data)
    assert set(sig.unique()).issubset(VALID_SIGNALS)


def test_batch_warmup_rows_are_all_flat(sample_ohlcv_data):
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    sig = s.generate_all_signals(sample_ohlcv_data)
    warmup = sig.iloc[: s.MIN_CANDLES_REQUIRED]
    assert (warmup == "FLAT").all(), (
        f"non-FLAT inside warmup: {warmup[warmup != 'FLAT'].head()}"
    )


def test_batch_emits_signals_in_volatile_phases(sample_ohlcv_data):
    """Phase 2 (bull, 700-900) + Phase 3 (bear, 900-1100) should fire signals."""
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    sig = s.generate_all_signals(sample_ohlcv_data)
    volatile = sig.iloc[700:1100]
    assert (volatile != "FLAT").any(), "no signal across bull + bear phases"


def test_batch_empty_dataframe_returns_empty_series(sample_ohlcv_data):
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    out = s.generate_all_signals(sample_ohlcv_data.iloc[0:0])
    assert isinstance(out, pd.Series)
    assert len(out) == 0


def test_batch_short_history_is_all_flat(sample_ohlcv_data):
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    truncated = sample_ohlcv_data.iloc[: max(1, s.MIN_CANDLES_REQUIRED - 1)]
    sig = s.generate_all_signals(truncated)
    assert len(sig) == len(truncated)
    assert (sig == "FLAT").all()


def test_batch_handles_all_nan_closes(sample_ohlcv_data):
    broken = sample_ohlcv_data.copy()
    broken["close"] = float("nan")
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    sig = s.generate_all_signals(broken)
    assert isinstance(sig, pd.Series)
    assert len(sig) == len(broken)
    assert set(sig.unique()).issubset(VALID_SIGNALS)


# ---- streaming-mode tests ----------------------------------------------------


def test_step_warmup_returns_flat(sample_ohlcv_data):
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    for i in range(s.MIN_CANDLES_REQUIRED):
        assert s.step(sample_ohlcv_data.iloc[i]) is SignalType.FLAT, (
            f"step returned non-FLAT during warmup at i={i}"
        )


def test_step_returns_signaltype_enum(sample_ohlcv_data):
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    for i in range(s.MIN_CANDLES_REQUIRED + 50):
        out = s.step(sample_ohlcv_data.iloc[i])
        assert isinstance(out, SignalType)


def test_step_emits_signals_after_warmup(sample_ohlcv_data):
    """Across the bull/bear slice, step must emit at least one non-FLAT."""
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    fired = False
    for i in range(len(sample_ohlcv_data)):
        sig = s.step(sample_ohlcv_data.iloc[i])
        if i >= s.MIN_CANDLES_REQUIRED and sig is not SignalType.FLAT:
            fired = True
            break
    assert fired, "step never emitted a non-FLAT signal across the full fixture"


# ---- cross-mode agreement ----------------------------------------------------


def test_batch_and_step_agree_on_warmup(sample_ohlcv_data):
    s_batch = Ema5BreakoutTargetShiftingMtfStrategy()
    batch_sig = s_batch.generate_all_signals(sample_ohlcv_data)
    assert (batch_sig.iloc[: s_batch.MIN_CANDLES_REQUIRED] == "FLAT").all()

    s_stream = Ema5BreakoutTargetShiftingMtfStrategy()
    for i in range(s_stream.MIN_CANDLES_REQUIRED):
        assert s_stream.step(sample_ohlcv_data.iloc[i]) is SignalType.FLAT


def test_batch_and_step_active_counts_both_positive(sample_ohlcv_data):
    s_batch = Ema5BreakoutTargetShiftingMtfStrategy()
    batch_sig = s_batch.generate_all_signals(sample_ohlcv_data)
    batch_active = int((batch_sig != "FLAT").sum())

    s_stream = Ema5BreakoutTargetShiftingMtfStrategy()
    stream_active = 0
    for i in range(len(sample_ohlcv_data)):
        if s_stream.step(sample_ohlcv_data.iloc[i]) is not SignalType.FLAT:
            stream_active += 1

    assert batch_active > 0
    assert stream_active > 0


# ---- construction parameter sanity ------------------------------------------


def test_min_candles_required_is_positive():
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    assert s.MIN_CANDLES_REQUIRED > 0


def test_min_candles_required_scales_with_ema_length():
    default = Ema5BreakoutTargetShiftingMtfStrategy()
    tuned = Ema5BreakoutTargetShiftingMtfStrategy(ema_length=20)
    assert tuned.MIN_CANDLES_REQUIRED > default.MIN_CANDLES_REQUIRED
    assert tuned.MIN_CANDLES_REQUIRED == 3 * 20 * 1


def test_min_candles_required_scales_with_htf_ratio():
    """ema_timeframe='1 hour' on 15m base ⇒ ratio=4 ⇒ 3*5*4=60."""
    s = Ema5BreakoutTargetShiftingMtfStrategy(
        ema_timeframe="1 hour", timeframe="15m"
    )
    assert s.MIN_CANDLES_REQUIRED == 60


def test_percentage_buffer_mode_runs_without_error(sample_ohlcv_data):
    s = Ema5BreakoutTargetShiftingMtfStrategy(
        buffer_type="percentage", buffer_value=0.5
    )
    sig = s.generate_all_signals(sample_ohlcv_data)
    assert set(sig.unique()).issubset(VALID_SIGNALS)


def test_invalid_buffer_type_raises():
    with pytest.raises(ValueError):
        Ema5BreakoutTargetShiftingMtfStrategy(buffer_type="bogus")


def test_invalid_ema_timeframe_raises():
    with pytest.raises(ValueError):
        Ema5BreakoutTargetShiftingMtfStrategy(ema_timeframe="Not A TF")


# ---- causal-behaviour sanity check ------------------------------------------


def test_all_flat_data_produces_no_signals():
    """Constant price cannot satisfy the entire-bar-above/below-EMA setup."""
    n = 300
    dates = pd.date_range(start="2020-01-01", periods=n, freq="15min", tz="UTC")
    flat = pd.DataFrame(
        {
            "date": dates,
            "open": np.full(n, 10_000.0),
            "high": np.full(n, 10_000.0),
            "low": np.full(n, 10_000.0),
            "close": np.full(n, 10_000.0),
            "volume": np.full(n, 1.0),
        }
    )
    s = Ema5BreakoutTargetShiftingMtfStrategy()
    sig = s.generate_all_signals(flat)
    assert (sig == "FLAT").all()
