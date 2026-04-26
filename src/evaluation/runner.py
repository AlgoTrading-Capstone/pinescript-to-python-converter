"""
Strategy Runner — Single Vectorized Call + Contract Validation

Invokes `BaseStrategy.generate_all_signals` exactly once on a full OHLCV
DataFrame, validates the output, and enforces the strategy contract:

  - Output must be a pd.Series with length == len(df) and matching index.
  - All values must be in {'LONG', 'SHORT', 'FLAT', 'HOLD'}.
  - Rows 0..MIN_CANDLES_REQUIRED-1 must all be 'FLAT' (warmup promise).
  - Total execution time must be below MAX_EXECUTION_SECONDS, otherwise the
    strategy has violated the vectorization requirement.

Each violation raises a distinct exception so the statistical gate can record
a precise rejection reason.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable

import pandas as pd

from src.base_strategy import BaseStrategy


logger = logging.getLogger("runner.evaluation")


VALID_SIGNALS = frozenset({"LONG", "SHORT", "FLAT", "HOLD"})
MAX_EXECUTION_SECONDS = 60.0


class StrategyContractError(RuntimeError):
    """Base class for all runner-level contract violations."""


class SignalShapeError(StrategyContractError):
    """Series length or index does not match the input DataFrame."""


class SignalValueError(StrategyContractError):
    """Series contains values outside {'LONG','SHORT','FLAT','HOLD'}."""


class LookbackUnderstatedError(StrategyContractError):
    """A real signal appeared before MIN_CANDLES_REQUIRED — lookback was a lie."""


class PerformanceContractViolation(StrategyContractError):
    """generate_all_signals took longer than MAX_EXECUTION_SECONDS."""


def generate_signals_for_strategy(
    strategy: BaseStrategy,
    ohlcv_df: pd.DataFrame,
) -> pd.Series:
    """
    Run a strategy's batch-mode signal generation with full contract enforcement.

    Args:
        strategy: an instantiated BaseStrategy subclass.
        ohlcv_df: DataFrame of candles (must contain the OHLCV columns the
                  strategy expects; index is typically a UTC DatetimeIndex).

    Returns:
        pd.Series of signal strings aligned to ohlcv_df.index.

    Raises:
        SignalShapeError, SignalValueError, LookbackUnderstatedError,
        PerformanceContractViolation — each describes a distinct contract break.
    """
    logger.info(
        f"Running generate_all_signals for '{strategy.name}' "
        f"on {len(ohlcv_df):,} candles"
    )
    start = time.perf_counter()
    signals = strategy.generate_all_signals(ohlcv_df)
    elapsed = time.perf_counter() - start
    logger.info(f"'{strategy.name}' produced {len(signals):,} signals in {elapsed:.2f}s")

    if elapsed > MAX_EXECUTION_SECONDS:
        raise PerformanceContractViolation(
            f"generate_all_signals took {elapsed:.1f}s "
            f"(limit {MAX_EXECUTION_SECONDS:.0f}s) — strategy is not properly "
            f"vectorized. Check for Python-level loops over df rows."
        )

    _validate_shape(signals, ohlcv_df)
    _validate_values(signals)
    _validate_lookback(signals, strategy.MIN_CANDLES_REQUIRED)

    return signals


def _validate_shape(signals: pd.Series, ohlcv_df: pd.DataFrame) -> None:
    if not isinstance(signals, pd.Series):
        raise SignalShapeError(
            f"generate_all_signals returned {type(signals).__name__}, expected pd.Series"
        )
    if len(signals) != len(ohlcv_df):
        raise SignalShapeError(
            f"signal length {len(signals):,} does not match df length {len(ohlcv_df):,}"
        )
    if not signals.index.equals(ohlcv_df.index):
        raise SignalShapeError(
            "signal index does not match ohlcv_df index — strategies must return a "
            "Series aligned to the input DataFrame's index."
        )


def _validate_values(signals: pd.Series) -> None:
    unique = set(signals.dropna().unique())
    invalid = unique - VALID_SIGNALS
    if invalid:
        raise SignalValueError(
            f"signal Series contains invalid values: {sorted(invalid)}. "
            f"Allowed: {sorted(VALID_SIGNALS)}."
        )


def _validate_lookback(signals: pd.Series, min_candles_required: int) -> None:
    if min_candles_required <= 0:
        return
    warmup_slice = signals.iloc[:min_candles_required]
    non_flat = warmup_slice[warmup_slice != "FLAT"]
    if len(non_flat) > 0:
        first_offender = non_flat.iloc[0]
        first_idx = non_flat.index[0]
        raise LookbackUnderstatedError(
            f"strategy emitted '{first_offender}' at index {first_idx} before "
            f"MIN_CANDLES_REQUIRED={min_candles_required:,}. Rows 0..N-1 must be "
            f"'FLAT' — otherwise lookback_hours in the registry will be wrong."
        )


def count_by_signal(signals: pd.Series) -> dict[str, int]:
    """Helper for the statistical gate's logging/metadata."""
    counts = signals.value_counts().to_dict()
    return {k: int(counts.get(k, 0)) for k in ("LONG", "SHORT", "FLAT", "HOLD")}


def signals_to_dataframe(
    signals_by_name: dict[str, pd.Series],
    index: Iterable,
) -> pd.DataFrame:
    """
    Combine per-strategy signal Series into a single DataFrame suitable for
    handing to `src.evaluation.plots.heatmap.render_heatmap`.
    """
    return pd.DataFrame(signals_by_name, index=list(index))
