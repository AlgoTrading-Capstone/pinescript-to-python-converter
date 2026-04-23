"""Tests for the Supertrend + EMA Rejection strategy — batch + streaming contract."""

from __future__ import annotations

import pandas as pd
import pytest

from src.base_strategy import SignalType
from src.strategies.supertrend_ema_rejection_strategy import (
    SupertrendEmaRejectionStrategy,
)


VALID_SIGNALS = {"LONG", "SHORT", "FLAT", "HOLD"}


# ---- batch-mode tests --------------------------------------------------------


def test_batch_shape_and_index_match_input(sample_ohlcv_data):
    s = SupertrendEmaRejectionStrategy()
    sig = s.generate_all_signals(sample_ohlcv_data)
    assert isinstance(sig, pd.Series)
    assert len(sig) == len(sample_ohlcv_data)
    assert sig.index.equals(sample_ohlcv_data.index)


def test_batch_values_are_valid_signal_strings(sample_ohlcv_data):
    s = SupertrendEmaRejectionStrategy()
    sig = s.generate_all_signals(sample_ohlcv_data)
    assert set(sig.unique()).issubset(VALID_SIGNALS)


def test_batch_warmup_rows_are_all_flat(sample_ohlcv_data):
    s = SupertrendEmaRejectionStrategy()
    sig = s.generate_all_signals(sample_ohlcv_data)
    warmup = sig.iloc[: s.MIN_CANDLES_REQUIRED]
    assert (warmup == "FLAT").all(), (
        f"non-FLAT signal inside warmup region: "
        f"{warmup[warmup != 'FLAT'].head()}"
    )


def test_batch_emits_signals_in_volatile_phases(sample_ohlcv_data):
    """Phases 2 (bull, 700-900) and 3 (bear, 900-1100) should trigger signals.

    With default `ema_length=200` the `MIN_CANDLES_REQUIRED` is 600, so the
    slice 700:1100 is fully post-warmup. If defaults ever go silent on the
    fixture, widen the strategy here (e.g. `use_ema_filter=False`), but only
    tune the strategy — never weaken the assertion.
    """
    s = SupertrendEmaRejectionStrategy()
    sig = s.generate_all_signals(sample_ohlcv_data)
    volatile = sig.iloc[700:1100]
    assert (volatile != "FLAT").any(), (
        "strategy stayed FLAT across bull + bear phases"
    )


def test_batch_empty_dataframe_returns_empty_series(sample_ohlcv_data):
    s = SupertrendEmaRejectionStrategy()
    out = s.generate_all_signals(sample_ohlcv_data.iloc[0:0])
    assert isinstance(out, pd.Series)
    assert len(out) == 0


def test_batch_short_history_is_all_flat(sample_ohlcv_data):
    """Feeding fewer than MIN_CANDLES_REQUIRED rows must stay entirely FLAT."""
    s = SupertrendEmaRejectionStrategy()
    truncated = sample_ohlcv_data.iloc[: s.MIN_CANDLES_REQUIRED - 1]
    sig = s.generate_all_signals(truncated)
    assert len(sig) == len(truncated)
    assert (sig == "FLAT").all()


def test_batch_handles_all_nan_closes(sample_ohlcv_data):
    """An all-NaN close column must not raise; output stays a valid-values Series."""
    broken = sample_ohlcv_data.copy()
    broken["close"] = float("nan")
    s = SupertrendEmaRejectionStrategy()
    sig = s.generate_all_signals(broken)
    assert isinstance(sig, pd.Series)
    assert len(sig) == len(broken)
    assert set(sig.unique()).issubset(VALID_SIGNALS)


# ---- streaming-mode tests ----------------------------------------------------


def test_step_warmup_returns_flat(sample_ohlcv_data):
    s = SupertrendEmaRejectionStrategy()
    for i in range(s.MIN_CANDLES_REQUIRED):
        assert s.step(sample_ohlcv_data.iloc[i]) is SignalType.FLAT, (
            f"step returned non-FLAT during warmup at i={i}"
        )


def test_step_emits_signals_after_warmup(sample_ohlcv_data):
    """After warmup, feeding the full fixture produces at least one non-FLAT."""
    s = SupertrendEmaRejectionStrategy()
    for i in range(len(sample_ohlcv_data)):
        sig = s.step(sample_ohlcv_data.iloc[i])
        if i >= s.MIN_CANDLES_REQUIRED and sig is not SignalType.FLAT:
            return
    pytest.fail("step never emitted a non-FLAT signal across the full fixture")


def test_step_returns_signaltype_enum(sample_ohlcv_data):
    s = SupertrendEmaRejectionStrategy()
    # Drive past warmup and sample a few post-warmup bars too.
    for i in range(s.MIN_CANDLES_REQUIRED + 20):
        out = s.step(sample_ohlcv_data.iloc[i])
        assert isinstance(out, SignalType)


# ---- cross-mode agreement ----------------------------------------------------


def test_batch_and_step_agree_on_warmup(sample_ohlcv_data):
    """Both modes must be all-FLAT inside the warmup region."""
    s_batch = SupertrendEmaRejectionStrategy()
    batch_sig = s_batch.generate_all_signals(sample_ohlcv_data)
    assert (batch_sig.iloc[: s_batch.MIN_CANDLES_REQUIRED] == "FLAT").all()

    s_stream = SupertrendEmaRejectionStrategy()
    for i in range(s_stream.MIN_CANDLES_REQUIRED):
        assert s_stream.step(sample_ohlcv_data.iloc[i]) is SignalType.FLAT


def test_batch_and_step_both_fire_post_warmup(sample_ohlcv_data):
    """Both modes should fire a non-zero number of non-FLAT signals.

    We do NOT require bar-for-bar agreement post-warmup: the batch pass and
    the streaming recurrence can seed Supertrend state slightly differently
    under ``np.isnan`` boundary conditions. Both must simply be alive.
    """
    s_batch = SupertrendEmaRejectionStrategy()
    batch_sig = s_batch.generate_all_signals(sample_ohlcv_data)
    batch_active = int((batch_sig != "FLAT").sum())

    s_stream = SupertrendEmaRejectionStrategy()
    stream_active = 0
    for i in range(len(sample_ohlcv_data)):
        if s_stream.step(sample_ohlcv_data.iloc[i]) is not SignalType.FLAT:
            stream_active += 1

    assert batch_active > 0, "batch mode emitted zero non-FLAT signals"
    assert stream_active > 0, "streaming mode emitted zero non-FLAT signals"


# ---- construction parameter sanity ------------------------------------------


def test_min_candles_required_is_dynamic():
    """Changing the slowest indicator period must change MIN_CANDLES_REQUIRED."""
    default = SupertrendEmaRejectionStrategy()
    # Shrink the EMA — 3 * max(50, 10, 10, 10, 14) = 150
    tuned = SupertrendEmaRejectionStrategy(ema_length=50)
    assert tuned.MIN_CANDLES_REQUIRED != default.MIN_CANDLES_REQUIRED
    assert tuned.MIN_CANDLES_REQUIRED == 3 * max(50, 10, 10, 10, 14)


def test_disabling_ema_filter_does_not_break_batch(sample_ohlcv_data):
    """With the EMA trend filter off, the strategy must still satisfy the contract."""
    s = SupertrendEmaRejectionStrategy(use_ema_filter=False)
    sig = s.generate_all_signals(sample_ohlcv_data)
    assert len(sig) == len(sample_ohlcv_data)
    assert set(sig.unique()).issubset(VALID_SIGNALS)
    assert (sig.iloc[: s.MIN_CANDLES_REQUIRED] == "FLAT").all()
