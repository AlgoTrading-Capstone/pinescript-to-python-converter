import pandas as pd
import pytest

from src.base_strategy import SignalType
from src.strategies.bb_rsi_1_2_rentable_4h_strategy import BBRSIRentable4hStrategy


ALLOWED_SIGNALS = {"LONG", "SHORT", "FLAT", "HOLD"}


@pytest.fixture
def strategy():
    return BBRSIRentable4hStrategy()


def test_min_candles_required_is_dynamic(strategy):
    assert strategy.MIN_CANDLES_REQUIRED == 3 * max(strategy.bb_length, strategy.rsi_length)


def test_batch_output_shape_and_index(strategy, sample_ohlcv_data):
    sig = strategy.generate_all_signals(sample_ohlcv_data)
    assert isinstance(sig, pd.Series)
    assert len(sig) == len(sample_ohlcv_data)
    assert (sig.index == sample_ohlcv_data.index).all()


def test_batch_values_are_valid_strings(strategy, sample_ohlcv_data):
    sig = strategy.generate_all_signals(sample_ohlcv_data)
    assert set(sig.unique()).issubset(ALLOWED_SIGNALS)


def test_batch_warmup_is_flat(strategy, sample_ohlcv_data):
    sig = strategy.generate_all_signals(sample_ohlcv_data)
    assert (sig.iloc[: strategy.MIN_CANDLES_REQUIRED] == "FLAT").all()


def test_batch_produces_signals_in_volatile_phases(strategy, sample_ohlcv_data):
    sig = strategy.generate_all_signals(sample_ohlcv_data)
    volatile = sig.iloc[700:1100]
    assert (volatile != "FLAT").any(), "strategy produced no signals during bull/bear phases"


def test_batch_empty_dataframe_is_safe(strategy, sample_ohlcv_data):
    out = strategy.generate_all_signals(sample_ohlcv_data.iloc[0:0])
    assert isinstance(out, pd.Series)
    assert len(out) == 0


def test_batch_all_nan_closes_is_safe(strategy, sample_ohlcv_data):
    df = sample_ohlcv_data.copy()
    df["close"] = float("nan")
    sig = strategy.generate_all_signals(df)
    assert len(sig) == len(df)
    assert set(sig.unique()).issubset(ALLOWED_SIGNALS)


def test_step_warmup_returns_flat(strategy, sample_ohlcv_data):
    for i in range(strategy.MIN_CANDLES_REQUIRED):
        assert strategy.step(sample_ohlcv_data.iloc[i]) is SignalType.FLAT


def test_step_returns_signal_type_enum(strategy, sample_ohlcv_data):
    for i in range(len(sample_ohlcv_data)):
        out = strategy.step(sample_ohlcv_data.iloc[i])
        assert isinstance(out, SignalType)


def test_step_emits_non_flat_during_volatile_phases(strategy, sample_ohlcv_data):
    emitted = []
    for i in range(len(sample_ohlcv_data)):
        sig = strategy.step(sample_ohlcv_data.iloc[i])
        if i >= 700:
            emitted.append(sig)
    assert any(s is not SignalType.FLAT for s in emitted), (
        "step produced no non-FLAT signal across bull/bear phases"
    )


def test_step_warmup_agrees_with_batch_warmup(strategy, sample_ohlcv_data):
    batch_sig = strategy.generate_all_signals(sample_ohlcv_data)
    streamed = [
        strategy.step(sample_ohlcv_data.iloc[i])
        for i in range(strategy.MIN_CANDLES_REQUIRED)
    ]
    assert all(s is SignalType.FLAT for s in streamed)
    assert (batch_sig.iloc[: strategy.MIN_CANDLES_REQUIRED] == "FLAT").all()
