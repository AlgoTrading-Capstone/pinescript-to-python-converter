"""
Paginated OHLCV Fetcher with Parquet Cache

Downloads historical OHLCV candles over a multi-year window via ccxt,
paginating to work around exchange per-call limits (Binance: 1500 bars max).
Results are cached to parquet so a given (exchange, symbol, timeframe, range)
tuple is downloaded exactly once across all converter runs.

Public API
----------
fetch_range(exchange_name, symbol, timeframe, start, end, cache_dir=None,
            force_refresh=False) -> pd.DataFrame
    Returns a DataFrame indexed by UTC timestamps with columns:
    open, high, low, close, volume.

The fetch raises `OHLCVCoverageError` if the returned candle count is below
`OHLCV_MIN_COVERAGE` of the expected count for the requested range — this
prevents the statistical gate from silently approving a strategy on a
gap-riddled dataset.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import ccxt
import pandas as pd

from src.pipeline import OHLCV_CACHE_DIR, OHLCV_MIN_COVERAGE
from src.utils.timeframes import (
    datetime_to_timestamp_ms,
    timeframe_to_minutes,
)


logger = logging.getLogger("runner.ohlcv")


class OHLCVCoverageError(RuntimeError):
    """Raised when the downloaded candle count is below OHLCV_MIN_COVERAGE."""


def _parse_iso_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    # ccxt ISO strings may end in 'Z' which datetime.fromisoformat handles in 3.11+
    text = value.rstrip("Z")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cache_path(
    cache_dir: Path,
    exchange_name: str,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> Path:
    safe_symbol = symbol.replace("/", "").replace(":", "_")
    name = (
        f"{exchange_name}_{safe_symbol}_{timeframe}_"
        f"{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.parquet"
    )
    return cache_dir / name


_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
_TIMESTAMP_COLUMN_CANDIDATES = ("date", "timestamp", "time", "datetime", "open_time")


def _normalize_ohlcv_df(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a user-supplied OHLCV DataFrame to the canonical schema.

    Canonical schema: tz-aware UTC DatetimeIndex, columns exactly
    ``[open, high, low, close, volume]``, sorted, de-duplicated.
    Accepts FinRL-style inputs (RangeIndex + ``date`` column + extra
    columns like ``tic``) alongside the already-canonical form.
    """
    working = df.copy()

    if not isinstance(working.index, pd.DatetimeIndex):
        ts_col = next(
            (c for c in _TIMESTAMP_COLUMN_CANDIDATES if c in working.columns),
            None,
        )
        if ts_col is None:
            raise ValueError(
                "user-supplied OHLCV parquet has no DatetimeIndex and no "
                f"timestamp column in {_TIMESTAMP_COLUMN_CANDIDATES}"
            )
        working[ts_col] = pd.to_datetime(working[ts_col], utc=True)
        working = working.set_index(ts_col)

    if working.index.tz is None:
        working.index = working.index.tz_localize("UTC")
    else:
        working.index = working.index.tz_convert("UTC")

    missing = [c for c in _OHLCV_COLUMNS if c not in working.columns]
    if missing:
        raise ValueError(
            f"user-supplied OHLCV parquet missing required columns: {missing}"
        )

    working = working[list(_OHLCV_COLUMNS)]
    working = working[~working.index.duplicated(keep="first")].sort_index()
    working.index.name = "timestamp"
    return working


def _candidate_cache_files(
    cache_dir: Path,
    exchange_name: str,
    symbol: str,
    timeframe: str,
) -> list[Path]:
    """Return cache files that could plausibly hold this (exchange, symbol, tf)."""
    slash_variants = {
        symbol.replace("/", "").replace(":", "_"),
        symbol.replace("/", "_").replace(":", "_"),
    }
    patterns: list[str] = []
    for sym in slash_variants:
        patterns.append(f"{exchange_name}_{sym}_{timeframe}_*.parquet")
        patterns.append(f"{exchange_name}_{sym}_{timeframe}.parquet")
    seen: set[Path] = set()
    results: list[Path] = []
    for pat in patterns:
        for path in cache_dir.glob(pat):
            if path not in seen:
                seen.add(path)
                results.append(path)
    return results


def _scan_compatible_cache(
    cache_dir: Path,
    exchange_name: str,
    symbol: str,
    timeframe: str,
    start_dt: datetime,
    end_dt: datetime,
) -> Optional[pd.DataFrame]:
    """Locate a user-placed raw parquet whose coverage spans [start, end).

    Returns the normalized + sliced DataFrame if one is found, else None.
    """
    for path in _candidate_cache_files(cache_dir, exchange_name, symbol, timeframe):
        try:
            raw = pd.read_parquet(path)
            df = _normalize_ohlcv_df(raw)
        except Exception as exc:
            logger.warning(f"Skipping incompatible cache file {path.name}: {exc}")
            continue

        sliced = df[(df.index >= start_dt) & (df.index < end_dt)]
        try:
            _assert_coverage(sliced, start_dt, end_dt, timeframe)
        except OHLCVCoverageError as exc:
            logger.info(f"Cache {path.name} below coverage for requested window: {exc}")
            continue

        logger.info(
            f"Loaded user-supplied cache {path.name}: "
            f"{len(sliced):,} candles in requested window"
        )
        return sliced
    return None


def _expected_candle_count(start: datetime, end: datetime, timeframe: str) -> int:
    total_minutes = (end - start).total_seconds() / 60.0
    return max(1, int(total_minutes // timeframe_to_minutes(timeframe)))


def _paginate_download(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    page_limit: int = 1500,
) -> list[list]:
    """
    Walk `since` forward in page_limit-sized chunks until end_ms is reached.
    Returns the raw list of [timestamp_ms, open, high, low, close, volume] rows.
    """
    tf_ms = timeframe_to_minutes(timeframe) * 60 * 1000
    all_rows: list[list] = []
    since = start_ms
    rate_sleep = (exchange.rateLimit or 0) / 1000.0

    while since < end_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=page_limit)
        if not batch:
            logger.warning(f"Empty batch at since={since}; stopping pagination.")
            break

        all_rows.extend(batch)
        last_ts = batch[-1][0]
        # Advance past the last candle we got; if the exchange returned fewer
        # than page_limit rows, we're at the tail and can stop.
        next_since = last_ts + tf_ms
        if next_since <= since or len(batch) < page_limit:
            if next_since >= end_ms or len(batch) < page_limit:
                break
        since = next_since

        if rate_sleep > 0:
            time.sleep(rate_sleep)

    return [row for row in all_rows if start_ms <= row[0] < end_ms]


def _rows_to_df(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(
        rows, columns=["timestamp_ms", "open", "high", "low", "close", "volume"]
    )
    df = df.drop_duplicates(subset="timestamp_ms").sort_values("timestamp_ms")
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df = df.set_index("timestamp").drop(columns="timestamp_ms")
    return df


def fetch_range(
    exchange_name: str,
    symbol: str,
    timeframe: str,
    start: str | datetime,
    end: str | datetime,
    cache_dir: Optional[Path] = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Fetch OHLCV candles for [start, end) and cache the result.

    Args:
        exchange_name: ccxt exchange id (e.g. 'binance').
        symbol:        Trading pair in ccxt format (e.g. 'BTC/USDT').
        timeframe:     Candle size (e.g. '15m', '1h', '4h').
        start, end:    ISO-8601 strings or UTC datetimes defining the range.
        cache_dir:     Override for the cache directory (defaults to OHLCV_CACHE_DIR).
        force_refresh: If True, ignore the cached parquet and re-download.

    Returns:
        pd.DataFrame indexed by UTC timestamps with columns
        open, high, low, close, volume.
    """
    start_dt = _parse_iso_utc(start)
    end_dt = _parse_iso_utc(end)
    if end_dt <= start_dt:
        raise ValueError(f"end ({end_dt}) must be after start ({start_dt})")

    cache_dir = Path(cache_dir) if cache_dir else OHLCV_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_path(cache_dir, exchange_name, symbol, timeframe, start_dt, end_dt)

    if cache_file.exists() and not force_refresh:
        logger.info(f"Loading OHLCV from cache: {cache_file.name}")
        df = pd.read_parquet(cache_file)
        df = _normalize_ohlcv_df(df)
        _assert_coverage(df, start_dt, end_dt, timeframe)
        return df

    if not force_refresh:
        compatible = _scan_compatible_cache(
            cache_dir, exchange_name, symbol, timeframe, start_dt, end_dt
        )
        if compatible is not None:
            compatible.to_parquet(cache_file)
            logger.info(
                f"Persisted canonical copy of user cache to {cache_file.name}"
            )
            return compatible

    logger.info(
        f"Downloading OHLCV {exchange_name} {symbol} {timeframe} "
        f"{start_dt.isoformat()} → {end_dt.isoformat()}"
    )
    exchange_cls = getattr(ccxt, exchange_name)
    exchange = exchange_cls({"enableRateLimit": True})

    start_ms = datetime_to_timestamp_ms(start_dt)
    end_ms = datetime_to_timestamp_ms(end_dt)

    rows = _paginate_download(exchange, symbol, timeframe, start_ms, end_ms)
    df = _rows_to_df(rows)

    _assert_coverage(df, start_dt, end_dt, timeframe)

    df.to_parquet(cache_file)
    logger.info(f"Cached {len(df):,} candles to {cache_file.name}")
    return df


def _assert_coverage(
    df: pd.DataFrame,
    start_dt: datetime,
    end_dt: datetime,
    timeframe: str,
) -> None:
    expected = _expected_candle_count(start_dt, end_dt, timeframe)
    actual = len(df)
    coverage = actual / expected if expected > 0 else 0.0
    if coverage < OHLCV_MIN_COVERAGE:
        raise OHLCVCoverageError(
            f"OHLCV coverage {coverage:.1%} below threshold {OHLCV_MIN_COVERAGE:.0%} "
            f"({actual:,}/{expected:,} candles). The exchange likely has gaps in this "
            f"range — delete the cache file and re-run to retry, or pick a shorter range."
        )
    logger.info(f"OHLCV coverage OK: {actual:,}/{expected:,} candles ({coverage:.1%})")