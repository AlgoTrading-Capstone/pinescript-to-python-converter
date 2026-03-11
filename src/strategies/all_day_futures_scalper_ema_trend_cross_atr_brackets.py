"""
All-Day Futures Scalper (EMA Trend + Cross + ATR Brackets)

Pine Script source: input/All-Day-Futures-Scalper-EMA-Trend-Cross-ATR-Brackets.pine
Timeframe: 15m  |  Lookback: 200 bars (50 h)

Entry logic:
  Long  — fast EMA (9) crosses above slow EMA (21) while close > trend EMA (200),
           and chop filter passes
  Short — fast EMA (9) crosses below slow EMA (21) while close < trend EMA (200),
           and chop filter passes

Exit management (stop / TP / breakeven) is handled by the execution layer.
This strategy only emits entry signals (LONG / SHORT) or HOLD.

Cooldown note: the original Pine strategy detects when `strategy.position_size`
transitions to zero and suppresses re-entries for `cooldown_bars` bars afterward.
That position state is unavailable in vectorized signal generation. The
`cooldown_bars` parameter is retained for API compatibility but has no effect.
Configure post-exit cooldown at the execution layer if required.
"""

from datetime import datetime

import numpy as np
import pandas as pd
import talib

from src.base_strategy import BaseStrategy, StrategyRecommendation, SignalType


class AllDayFuturesScalperEmaTrendCrossAtrBrackets(BaseStrategy):
    def __init__(
        self,
        fast_len: int = 9,
        slow_len: int = 21,
        trend_len: int = 200,
        atr_len: int = 14,
        sl_atr: float = 1.0,
        tp_atr: float = 1.5,
        use_be: bool = True,
        be_atr: float = 1.0,
        cooldown_bars: int = 3,
        use_chop_filter: bool = True,
        min_atr_pct: float = 0.05,
        use_longs: bool = True,
        use_shorts: bool = True,
    ):
        super().__init__(
            name="All-Day Futures Scalper (EMA Trend + Cross + ATR Brackets)",
            description=(
                "EMA crossover scalper with 200-EMA trend filter, ATR-based bracket "
                "exits, optional breakeven stop, post-exit cooldown, and chop filter."
            ),
            timeframe="15m",
            lookback_hours=50,  # 200 bars * 15 min = 3 000 min ≈ 50 h
        )
        self.fast_len = fast_len
        self.slow_len = slow_len
        self.trend_len = trend_len
        self.atr_len = atr_len
        self.sl_atr = sl_atr
        self.tp_atr = tp_atr
        self.use_be = use_be
        self.be_atr = be_atr
        self.cooldown_bars = cooldown_bars
        self.use_chop_filter = use_chop_filter
        self.min_atr_pct = min_atr_pct
        self.use_longs = use_longs
        self.use_shorts = use_shorts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _crossover(a: np.ndarray, b: np.ndarray, idx: int) -> bool:
        """True when series `a` crosses above series `b` at position `idx`."""
        return bool(a[idx - 1] <= b[idx - 1] and a[idx] > b[idx])

    @staticmethod
    def _crossunder(a: np.ndarray, b: np.ndarray, idx: int) -> bool:
        """True when series `a` crosses below series `b` at position `idx`."""
        return bool(a[idx - 1] >= b[idx - 1] and a[idx] < b[idx])

    # ------------------------------------------------------------------
    # Public contract
    # ------------------------------------------------------------------

    def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
        close = df["close"].to_numpy(dtype=float)
        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)

        min_bars = 3 * max(self.trend_len, self.slow_len, self.atr_len)
        if len(close) < min_bars:
            return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)

        # ---- Indicators ------------------------------------------------
        fast_ema = talib.EMA(close, timeperiod=self.fast_len)
        slow_ema = talib.EMA(close, timeperiod=self.slow_len)
        trend_ema = talib.EMA(close, timeperiod=self.trend_len)
        atr = talib.ATR(high, low, close, timeperiod=self.atr_len)

        idx = len(close) - 1  # current (last) bar

        # Guard: need valid values at current and previous bar for all indicators
        # (crossover helpers access both idx and idx-1 for fast/slow EMA)
        if (
            np.isnan(fast_ema[idx]) or np.isnan(fast_ema[idx - 1])
            or np.isnan(slow_ema[idx]) or np.isnan(slow_ema[idx - 1])
        ):
            return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)
        if np.isnan(trend_ema[idx]) or np.isnan(atr[idx]):
            return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)

        # ---- Chop filter -----------------------------------------------
        atr_pct = (atr[idx] / close[idx]) * 100.0
        chop_ok = (not self.use_chop_filter) or (atr_pct >= self.min_atr_pct)

        can_trade = chop_ok

        # ---- Crossover signals at current bar --------------------------
        long_trigger = self._crossover(fast_ema, slow_ema, idx)
        short_trigger = self._crossunder(fast_ema, slow_ema, idx)

        # ---- Trend filter ----------------------------------------------
        trend_up = close[idx] > trend_ema[idx]
        trend_down = close[idx] < trend_ema[idx]

        # ---- Entry conditions ------------------------------------------
        long_cond = can_trade and self.use_longs and trend_up and long_trigger
        short_cond = can_trade and self.use_shorts and trend_down and short_trigger

        if long_cond:
            return StrategyRecommendation(signal=SignalType.LONG, timestamp=timestamp)
        if short_cond:
            return StrategyRecommendation(signal=SignalType.SHORT, timestamp=timestamp)
        return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)
