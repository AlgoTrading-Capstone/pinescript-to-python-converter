"""
HTF Candle Direction Strategy V1
Converted from Pine Script by Transpiler Agent.

Trades in the direction of the Higher Timeframe (12h / 720 min) candle bias:
  - HTF Bullish (HTF Close > HTF Open) → BUY signal
  - HTF Bearish (HTF Close < HTF Open) → SELL signal

Optional filters:
  - EMA(50): long only above EMA, short only below EMA
  - Volume filter: trade only when volume exceeds 20-bar SMA * multiplier

One trade per day maximum (mirrors Pine's `var bool signalActive` logic, vectorized).

NOTE: The original Pine Script exposes a `useLookahead` toggle. This conversion
permanently uses lookahead=OFF (the safe, non-repainting mode) via `resampled_merge`.
"""

from datetime import datetime

import pandas as pd
import talib

from src.base_strategy import BaseStrategy, StrategyRecommendation, SignalType
from src.utils.resampling import resample_to_interval, resampled_merge


class HtfCandleDirectionStrategyV1Strategy(BaseStrategy):

    def __init__(self):
        super().__init__(
            name="HtfCandleDirectionStrategyV1Strategy",
            description=(
                "Converted from Pine Script: HTF Candle Direction Strategy V1. "
                "Trades aligned with the 12h Higher Timeframe candle direction. "
                "Optional EMA(50) and Volume filters. One trade per calendar day."
            ),
            timeframe="15m",
            lookback_hours=600,  # 50 HTF bars × 12 h each
        )

        # --- Indicator parameters (mirrors Pine inputs) ---
        self.ema_length = 50
        self.vol_sma_length = 20
        self.vol_multiplier = 1.0

        # Filters enabled by default (mirrors Pine defaults)
        self.use_ema_filter = True
        self.use_vol_filter = False  # disabled by default in original

        # HTF timeframe: Pine "720" → 720 minutes (12 h)
        self.htf_minutes = 720

        # --- Dynamic RL warmup guard ---
        # 3× the longest base-timeframe indicator period
        self.MIN_CANDLES_REQUIRED = 3 * max(self.ema_length, self.vol_sma_length)

    # ------------------------------------------------------------------
    def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
        # --- RL warmup guard ---
        if len(df) < self.MIN_CANDLES_REQUIRED:
            return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)

        # ------------------------------------------------------------------
        # Phase 1: Multi-Timeframe – resample to 720 min (12 h)
        # ------------------------------------------------------------------
        resampled_df = resample_to_interval(df, self.htf_minutes)
        if len(resampled_df) < 2:
            return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)
        merged_df = resampled_merge(original=df, resampled=resampled_df, fill_na=True)

        prefix = f"resample_{self.htf_minutes}"
        htf_close = merged_df[f"{prefix}_close"]
        htf_open = merged_df[f"{prefix}_open"]

        # ------------------------------------------------------------------
        # Phase 2: HTF candle direction bias
        # ------------------------------------------------------------------
        long_condition = htf_close > htf_open    # bullish HTF candle
        short_condition = htf_close < htf_open   # bearish HTF candle

        # ------------------------------------------------------------------
        # Phase 3: Base-timeframe indicators
        # ------------------------------------------------------------------
        close_vals = merged_df["close"].values
        volume_vals = merged_df["volume"].values

        ema_arr = talib.EMA(close_vals, timeperiod=self.ema_length)
        ema_series = pd.Series(ema_arr, index=merged_df.index)

        vol_sma_arr = talib.SMA(volume_vals, timeperiod=self.vol_sma_length)
        vol_avg = pd.Series(vol_sma_arr, index=merged_df.index)

        ema_long = merged_df["close"] > ema_series
        ema_short = merged_df["close"] < ema_series
        vol_ok = merged_df["volume"] > vol_avg * self.vol_multiplier

        # ------------------------------------------------------------------
        # Phase 4: Combine filters (vectorized scalar-flag expansion)
        # ------------------------------------------------------------------
        if self.use_ema_filter:
            filter_long = ema_long
            filter_short = ema_short
        else:
            filter_long = pd.Series(True, index=merged_df.index)
            filter_short = pd.Series(True, index=merged_df.index)

        if self.use_vol_filter:
            filter_long = filter_long & vol_ok
            filter_short = filter_short & vol_ok

        # ------------------------------------------------------------------
        # Phase 5: Raw entry conditions
        # ------------------------------------------------------------------
        raw_long = long_condition & filter_long
        raw_short = short_condition & filter_short

        # ------------------------------------------------------------------
        # Phase 6: One trade per day (vectorized `var bool signalActive`)
        #
        # In Pine: signalActive resets to False on each new day and becomes
        # True the moment a trade fires, blocking further entries that day.
        #
        # Vectorized equivalent:
        #   - Compute cumulative signal count within each calendar day.
        #   - "prior signals today" = cumsum − current bar's contribution.
        #   - Block the bar if prior_signals_today > 0.
        # ------------------------------------------------------------------
        date_only = pd.to_datetime(merged_df["date"]).dt.date
        any_signal = raw_long | raw_short
        cum_per_day = any_signal.groupby(date_only).cumsum()
        prior_signals_today = cum_per_day - any_signal.astype(int)
        signal_blocked = prior_signals_today > 0

        enter_long = (raw_long & ~signal_blocked).fillna(False)
        enter_short = (raw_short & ~signal_blocked).fillna(False)

        # ------------------------------------------------------------------
        # Phase 7: Emit recommendation for last confirmed bar
        # ------------------------------------------------------------------
        if bool(enter_long.iloc[-1]):
            return StrategyRecommendation(signal=SignalType.LONG, timestamp=timestamp)
        elif bool(enter_short.iloc[-1]):
            return StrategyRecommendation(signal=SignalType.SHORT, timestamp=timestamp)

        return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)
