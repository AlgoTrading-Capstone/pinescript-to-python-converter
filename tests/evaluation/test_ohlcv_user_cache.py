"""Regression tests for user-supplied OHLCV parquet ingestion.

The statistical gate must accept a FinRL-style parquet (RangeIndex + `date`
column + extra `tic` column) dropped into `data/ohlcv_cache/` by the user
without renaming or reformatting. See `src/evaluation/ohlcv.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.evaluation.ohlcv import (
    _normalize_ohlcv_df,
    _scan_compatible_cache,
    fetch_range,
)


def _make_finrl_parquet(path: Path, *, start: datetime, periods: int, freq: str = "15min") -> None:
    idx = pd.date_range(start=start, periods=periods, freq=freq, tz="UTC")
    df = pd.DataFrame(
        {
            "date": idx.tz_convert(None),  # naive timestamps, FinRL-style
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
            "tic": "BTC/USDT",
        }
    )
    df.to_parquet(path, index=False)


def test_normalize_finrl_schema_roundtrips_to_canonical_form():
    idx = pd.date_range("2020-01-01", periods=5, freq="15min")
    df = pd.DataFrame({
        "date": idx,
        "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0,
        "tic": "BTC/USDT",
    })

    normalized = _normalize_ohlcv_df(df)

    assert isinstance(normalized.index, pd.DatetimeIndex)
    assert str(normalized.index.tz) == "UTC"
    assert list(normalized.columns) == ["open", "high", "low", "close", "volume"]
    assert len(normalized) == 5


def test_fetch_range_consumes_finrl_parquet_in_cache_dir(tmp_path):
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    # 2 days of 15m candles = 192; request 1 day = 96.
    _make_finrl_parquet(
        tmp_path / "binance_BTC_USDT_15m.parquet",
        start=start,
        periods=192,
    )

    df = fetch_range(
        exchange_name="binance",
        symbol="BTC/USDT",
        timeframe="15m",
        start=start,
        end=datetime(2020, 1, 2, tzinfo=timezone.utc),
        cache_dir=tmp_path,
    )

    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "UTC"
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 96
    assert (tmp_path / "binance_BTCUSDT_15m_20200101_20200102.parquet").exists()


def test_scan_compatible_cache_skips_insufficient_coverage(tmp_path):
    # 10 candles supplied, but requested window expects 96 -> below 95% threshold.
    _make_finrl_parquet(
        tmp_path / "binance_BTC_USDT_15m.parquet",
        start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        periods=10,
    )

    result = _scan_compatible_cache(
        cache_dir=tmp_path,
        exchange_name="binance",
        symbol="BTC/USDT",
        timeframe="15m",
        start_dt=datetime(2020, 1, 1, tzinfo=timezone.utc),
        end_dt=datetime(2020, 1, 2, tzinfo=timezone.utc),
    )
    assert result is None
