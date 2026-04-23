"""
Supertrend + EMA Rejection Strategy

Transpiled from TradingView Pine Script v5 (see
``input/Supertrend-EMA-Rejection-Strategy.pine``).

Original Pine idea
------------------
A pullback / rejection entry built on top of three Supertrend lines and a
long-period trend EMA (200). On the current timeframe, a candle that:

* has its low wick INTO a bullish Supertrend band (``low <= st``) yet still
  closes above it (``close > st``) while the Supertrend is flipped bullish
  (``dir == -1``) and price is above the trend EMA,

triggers a LONG "setup". The mirror rule fires a SHORT.

What we keep
------------
* The 3-Supertrend entry trigger on the *current* timeframe. The Pine script
  exposes MTF toggles for 1/3/5/15/30/60/240, but the default ships with only
  ``useCur = true``, so we emit signals from the current TF alone. The RL
  engine drives timeframe selection globally via ``BaseStrategy.timeframe``.
* The EMA-200 trend filter (``use_ema_filter=True`` by default).
* The warmup contract: all rows before ``MIN_CANDLES_REQUIRED`` are FLAT.

What we intentionally DROP (architecture constraint, not a bug)
---------------------------------------------------------------
The Pine strategy is an entry-signal scanner wrapped in a rich execution
layer. Our ``StrategyRecommendation`` schema carries only a signal direction
(LONG / SHORT / FLAT / HOLD) — it does NOT carry SL/TP prices, trailing
distances, or pending stop orders. The following Pine-side logic therefore
has no representation here and is left to the downstream execution engine:

* ``strategy.entry(..., stop=trigger)`` pending-stop orders and the
  ``entryWindow`` cancel-after-N-bars setup.
* ``activeSL`` ATR-buffered stop, break-even bump at ``be_trigger_rr``,
  trailing-EMA(21) stop activation at ``trail_start_rr``.
* Pivot-based take-profit (``recentPh`` / ``recentPl``) and the
  ``initialRisk * 1.5`` fallback target.
* MACD bear/bull cross early exit.
* Any condition gated on ``strategy.position_size`` (banned "fake state").

We emit ``LONG`` / ``SHORT`` on the entry-trigger bar and ``FLAT`` otherwise.
No cooldown, no position memory. ``FLAT`` in the RL contract does NOT mean
"close the position" — exposure is inferred from recommendation changes.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Tuple

import numpy as np
import pandas as pd
import talib

from src.base_strategy import BaseStrategy, SignalType


class SupertrendEmaRejectionStrategy(BaseStrategy):
    """Three-Supertrend + EMA-200 rejection entry, current-timeframe only."""

    def __init__(
        self,
        use_ema_filter: bool = True,
        ema_length: int = 200,
        st1: Tuple[int, float] = (10, 2.0),
        st2: Tuple[int, float] = (10, 3.0),
        st3: Tuple[int, float] = (10, 5.0),
        use_st1: bool = True,
        use_st2: bool = True,
        use_st3: bool = True,
        atr_sl_length: int = 14,
    ) -> None:
        super().__init__(
            name="SupertrendEmaRejectionStrategy",
            description=(
                "Entry-only port of the Pine 'Supertrend + EMA Rejection' "
                "strategy. Three Supertrend bands (10/2, 10/3, 10/5) plus a "
                "200-EMA trend filter; emits LONG/SHORT when a candle wicks "
                "into a bullish/bearish Supertrend band and closes through "
                "it in the trend direction. Pine-side SL/TP, pivot targets, "
                "trailing EMA and MACD exits are dropped — the RL engine owns "
                "position management."
            ),
            timeframe="15m",
            lookback_hours=48,
        )

        # ----- parameters (mirror Pine inputs) -----
        self.use_ema_filter = bool(use_ema_filter)
        self.ema_length = int(ema_length)

        self.atr_len1, self.mult1 = int(st1[0]), float(st1[1])
        self.atr_len2, self.mult2 = int(st2[0]), float(st2[1])
        self.atr_len3, self.mult3 = int(st3[0]), float(st3[1])

        self.use_st1 = bool(use_st1)
        self.use_st2 = bool(use_st2)
        self.use_st3 = bool(use_st3)

        self.atr_sl_length = int(atr_sl_length)

        # ----- dynamic warmup (RL-safety contract) -----
        # 3x the slowest indicator period. With ema_length=200 this yields 600,
        # which is also the start of the first volatile phase in the shared
        # test fixture, matching the contract docs.
        self.MIN_CANDLES_REQUIRED = 3 * max(
            self.ema_length,
            self.atr_len1,
            self.atr_len2,
            self.atr_len3,
            self.atr_sl_length,
        )

        # ----- streaming state (consumed by step) -----
        self._observed: int = 0
        # A bounded rolling buffer is sufficient: the longest indicator needs
        # MIN_CANDLES_REQUIRED bars to warm up, so a slightly larger window
        # lets us recompute the 3 Supertrends + EMA deterministically each
        # tick without ever re-scanning the full history.
        self._buffer_size: int = self.MIN_CANDLES_REQUIRED + 20
        self._open_buf: Deque[float] = deque(maxlen=self._buffer_size)
        self._high_buf: Deque[float] = deque(maxlen=self._buffer_size)
        self._low_buf: Deque[float] = deque(maxlen=self._buffer_size)
        self._close_buf: Deque[float] = deque(maxlen=self._buffer_size)
        self._volume_buf: Deque[float] = deque(maxlen=self._buffer_size)

    # ------------------------------------------------------------------
    # Supertrend helper (vectorized via a single O(n) numpy loop)
    # ------------------------------------------------------------------
    @staticmethod
    def _supertrend(
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        atr_len: int,
        mult: float,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Compute Supertrend line + direction array.

        Returns
        -------
        st : np.ndarray
            The Supertrend line value per bar (NaN before warmup).
        direction : np.ndarray
            Per-bar direction:
              ``-1`` means bullish (close above the line),
              ``+1`` means bearish (close below the line).
            This matches Pine's ``ta.supertrend`` convention.
        """
        n = len(close)
        st = np.full(n, np.nan, dtype=float)
        direction = np.zeros(n, dtype=np.int8)
        if n == 0:
            return st, direction

        atr = talib.ATR(high, low, close, timeperiod=atr_len)
        hl2 = (high + low) / 2.0
        upper_base = hl2 + mult * atr
        lower_base = hl2 - mult * atr

        final_upper = np.full(n, np.nan, dtype=float)
        final_lower = np.full(n, np.nan, dtype=float)

        # Iterate once over the array. This is O(n) — permitted for recursive
        # indicators by the strategy-contract rules.
        for i in range(n):
            if np.isnan(atr[i]):
                continue

            if i == 0 or np.isnan(final_upper[i - 1]):
                final_upper[i] = upper_base[i]
                final_lower[i] = lower_base[i]
                # Seed direction: bullish (-1) if close above the midpoint,
                # bearish (+1) otherwise. Pine's SuperTrend seeds similarly.
                direction[i] = -1 if close[i] > hl2[i] else 1
                st[i] = final_lower[i] if direction[i] == -1 else final_upper[i]
                continue

            prev_upper = final_upper[i - 1]
            prev_lower = final_lower[i - 1]

            # Standard Pine-compatible "final band" recurrence.
            final_upper[i] = (
                upper_base[i]
                if (upper_base[i] < prev_upper or close[i - 1] > prev_upper)
                else prev_upper
            )
            final_lower[i] = (
                lower_base[i]
                if (lower_base[i] > prev_lower or close[i - 1] < prev_lower)
                else prev_lower
            )

            prev_dir = direction[i - 1]
            if prev_dir == -1:  # was bullish, close above line
                direction[i] = 1 if close[i] < final_lower[i] else -1
            else:  # was bearish, close below line
                direction[i] = -1 if close[i] > final_upper[i] else 1

            st[i] = final_lower[i] if direction[i] == -1 else final_upper[i]

        return st, direction

    # ------------------------------------------------------------------
    # BATCH MODE — called once per gate evaluation
    # ------------------------------------------------------------------
    def generate_all_signals(self, df: pd.DataFrame) -> pd.Series:
        n = len(df)
        signals = pd.Series(["FLAT"] * n, index=df.index, dtype=object)
        if n == 0:
            return signals
        if n < self.MIN_CANDLES_REQUIRED:
            return signals  # warmup: all FLAT (gate contract)

        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        close = df["close"].to_numpy(dtype=float)

        # Trend EMA (200 by default)
        ema = talib.EMA(close, timeperiod=self.ema_length)

        # Three Supertrends
        st1, dir1 = self._supertrend(high, low, close, self.atr_len1, self.mult1)
        st2, dir2 = self._supertrend(high, low, close, self.atr_len2, self.mult2)
        st3, dir3 = self._supertrend(high, low, close, self.atr_len3, self.mult3)

        # Trend filter masks. ``is_up`` / ``is_dn`` are the Pine variables.
        if self.use_ema_filter:
            is_up = close > ema
            is_dn = close < ema
        else:
            is_up = np.ones(n, dtype=bool)
            is_dn = np.ones(n, dtype=bool)

        # Per-Supertrend rejection conditions (wick into the band + close through
        # it + Supertrend flipped in our favor).
        def _long_cond(st: np.ndarray, dr: np.ndarray, enabled: bool) -> np.ndarray:
            if not enabled:
                return np.zeros(n, dtype=bool)
            valid = ~np.isnan(st)
            return valid & is_up & (low <= st) & (close > st) & (dr == -1)

        def _short_cond(st: np.ndarray, dr: np.ndarray, enabled: bool) -> np.ndarray:
            if not enabled:
                return np.zeros(n, dtype=bool)
            valid = ~np.isnan(st)
            return valid & is_dn & (high >= st) & (close < st) & (dr == 1)

        long_any = (
            _long_cond(st1, dir1, self.use_st1)
            | _long_cond(st2, dir2, self.use_st2)
            | _long_cond(st3, dir3, self.use_st3)
        )
        short_any = (
            _short_cond(st1, dir1, self.use_st1)
            | _short_cond(st2, dir2, self.use_st2)
            | _short_cond(st3, dir3, self.use_st3)
        )

        # Conservative tiebreak: if both fire on the same bar (should be
        # unreachable with is_up/is_dn as defined, but possible when the EMA
        # filter is off), prefer FLAT.
        both = long_any & short_any
        long_only = long_any & ~both
        short_only = short_any & ~both

        out = np.where(
            long_only, "LONG",
            np.where(short_only, "SHORT", "FLAT"),
        )
        signals = pd.Series(out, index=df.index, dtype=object)

        # Re-assert the warmup contract after the vectorized pass.
        signals.iloc[: self.MIN_CANDLES_REQUIRED] = "FLAT"
        return signals

    # ------------------------------------------------------------------
    # STREAMING MODE — one candle at a time
    # ------------------------------------------------------------------
    def step(self, candle: pd.Series) -> SignalType:
        # Append the new candle to the rolling buffers (bounded by maxlen).
        self._open_buf.append(float(candle["open"]))
        self._high_buf.append(float(candle["high"]))
        self._low_buf.append(float(candle["low"]))
        self._close_buf.append(float(candle["close"]))
        if "volume" in candle:
            self._volume_buf.append(float(candle["volume"]))
        else:
            self._volume_buf.append(0.0)

        self._observed += 1
        if self._observed < self.MIN_CANDLES_REQUIRED:
            return SignalType.FLAT

        high = np.fromiter(self._high_buf, dtype=float)
        low = np.fromiter(self._low_buf, dtype=float)
        close = np.fromiter(self._close_buf, dtype=float)
        n = len(close)

        # Recompute indicators on the bounded buffer (contract-allowed because
        # the buffer size is fixed, not unbounded history).
        ema = talib.EMA(close, timeperiod=self.ema_length)
        st1, dir1 = self._supertrend(high, low, close, self.atr_len1, self.mult1)
        st2, dir2 = self._supertrend(high, low, close, self.atr_len2, self.mult2)
        st3, dir3 = self._supertrend(high, low, close, self.atr_len3, self.mult3)

        i = n - 1  # evaluate the just-appended bar
        last_close = close[i]
        last_high = high[i]
        last_low = low[i]
        last_ema = ema[i]

        if self.use_ema_filter:
            if np.isnan(last_ema):
                return SignalType.FLAT
            is_up = last_close > last_ema
            is_dn = last_close < last_ema
        else:
            is_up = True
            is_dn = True

        def _check_long(st: np.ndarray, dr: np.ndarray, enabled: bool) -> bool:
            if not enabled:
                return False
            v = st[i]
            if np.isnan(v):
                return False
            return is_up and (last_low <= v) and (last_close > v) and (dr[i] == -1)

        def _check_short(st: np.ndarray, dr: np.ndarray, enabled: bool) -> bool:
            if not enabled:
                return False
            v = st[i]
            if np.isnan(v):
                return False
            return is_dn and (last_high >= v) and (last_close < v) and (dr[i] == 1)

        long_trigger = (
            _check_long(st1, dir1, self.use_st1)
            or _check_long(st2, dir2, self.use_st2)
            or _check_long(st3, dir3, self.use_st3)
        )
        short_trigger = (
            _check_short(st1, dir1, self.use_st1)
            or _check_short(st2, dir2, self.use_st2)
            or _check_short(st3, dir3, self.use_st3)
        )

        if long_trigger and not short_trigger:
            return SignalType.LONG
        if short_trigger and not long_trigger:
            return SignalType.SHORT
        return SignalType.FLAT

    # ------------------------------------------------------------------
    # Optional lifecycle hook — cheap to seed the rolling buffers from df.
    # ------------------------------------------------------------------
    def warmup(self, df: pd.DataFrame) -> None:
        if df is None or len(df) == 0:
            return
        tail = df.tail(self._buffer_size)
        for col, buf in (
            ("open", self._open_buf),
            ("high", self._high_buf),
            ("low", self._low_buf),
            ("close", self._close_buf),
        ):
            buf.clear()
            buf.extend(tail[col].astype(float).tolist())
        self._volume_buf.clear()
        if "volume" in tail.columns:
            self._volume_buf.extend(tail["volume"].astype(float).tolist())
        else:
            self._volume_buf.extend([0.0] * len(tail))
        self._observed = len(df)

    def reset(self) -> None:
        self._observed = 0
        for buf in (
            self._open_buf,
            self._high_buf,
            self._low_buf,
            self._close_buf,
            self._volume_buf,
        ):
            buf.clear()
