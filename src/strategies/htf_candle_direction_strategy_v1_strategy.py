"""
HTF Candle Direction Strategy V1
Converted from PineScript v5.

Trades based on Higher Timeframe (12h) candle direction:
- LONG when HTF candle is bullish (HTF close > HTF open) and EMA filter passes
- SHORT when HTF candle is bearish (HTF close < HTF open) and EMA filter passes
- One signal per calendar day (signalActive gate)
- Optional EMA(50) filter (default ON) and Volume filter (default OFF)

Note: lookahead_on is disabled in this conversion to prevent lookahead bias;
      resampled_merge provides the safe alignment equivalent of lookahead_off.
"""

from datetime import datetime
import numpy as np
import pandas as pd
import talib

from src.base_strategy import BaseStrategy, StrategyRecommendation, SignalType
from src.utils.resampling import resample_to_interval, resampled_merge


class HtfCandleDirectionStrategyV1Strategy(BaseStrategy):

    def __init__(self):
        super().__init__(
            name="HtfCandleDirectionStrategyV1",
            description=(
                "Trades based on Higher Timeframe (12h) candle direction. "
                "Long when HTF candle is bullish, short when bearish. "
                "EMA(50) filter enabled by default. One trade per calendar day."
            ),
            timeframe="1h",
            lookback_hours=600,  # 50 HTF (12h) candles
        )
        self.ema_length = 50
        self.vol_sma_length = 20
        self.vol_multiplier = 1.0
        self.use_ema_filter = True
        self.use_vol_filter = False
        # Dynamic RL warmup: 3× the longest indicator period on the base timeframe
        self.MIN_CANDLES_REQUIRED = 3 * max(self.ema_length, self.vol_sma_length)

    def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
        # --- CRITICAL RL GUARD ---
        if len(df) < self.MIN_CANDLES_REQUIRED:
            return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)

        df = df.copy()

        # --- HTF resampling: 720 min = 12h (request.security period="720") ---
        # Pass integer minutes directly because "12h" is not in TIMEFRAME_MINUTES_MAP
        resampled_df = resample_to_interval(df, 720)
        df = resampled_merge(original=df, resampled=resampled_df, fill_na=True)

        # HTF candle direction: longCondition / shortCondition
        htf_close = df["resample_720_close"]
        htf_open = df["resample_720_open"]
        df["_long_cond"] = htf_close > htf_open
        df["_short_cond"] = htf_close < htf_open

        # EMA filter on base timeframe close
        df["_ema"] = pd.Series(
            talib.EMA(df["close"].values, timeperiod=self.ema_length),
            index=df.index,
        )
        df["_ema_long"] = df["close"] > df["_ema"]
        df["_ema_short"] = df["close"] < df["_ema"]

        # Volume filter: volume > SMA(volume, 20) * multiplier
        df["_vol_avg"] = pd.Series(
            talib.SMA(df["volume"].values, timeperiod=self.vol_sma_length),
            index=df.index,
        )
        df["_vol_ok"] = df["volume"] > df["_vol_avg"] * self.vol_multiplier

        # Combined filter logic (mirrors Pine's filterLong / filterShort)
        if self.use_ema_filter and self.use_vol_filter:
            df["_filter_long"] = df["_ema_long"] & df["_vol_ok"]
            df["_filter_short"] = df["_ema_short"] & df["_vol_ok"]
        elif self.use_ema_filter:
            df["_filter_long"] = df["_ema_long"]
            df["_filter_short"] = df["_ema_short"]
        elif self.use_vol_filter:
            df["_filter_long"] = df["_vol_ok"]
            df["_filter_short"] = df["_vol_ok"]
        else:
            df["_filter_long"] = pd.Series(True, index=df.index)
            df["_filter_short"] = pd.Series(True, index=df.index)

        # Raw entry conditions (before one-per-day gate)
        df["_raw_long"] = df["_long_cond"] & df["_filter_long"]
        df["_raw_short"] = df["_short_cond"] & df["_filter_short"]

        # One-per-day gate: mirrors Pine's var bool signalActive + isNewDay reset
        # Detect calendar-day boundaries
        df["_day"] = pd.to_datetime(df["date"]).dt.normalize()
        df["_is_new_day"] = df["_day"] != df["_day"].shift(1)
        df["_day_group"] = df["_is_new_day"].cumsum()

        # Within each day, track whether a signal has already fired on a prior bar
        df["_any_signal"] = df["_raw_long"] | df["_raw_short"]
        df["_prev_signal_count"] = df.groupby("_day_group")["_any_signal"].transform(
            lambda x: x.shift(1).fillna(0).cumsum()
        )
        df["_signal_active"] = df["_prev_signal_count"] > 0

        # Final entry conditions: only first signal of the day passes
        df["_enter_long"] = df["_raw_long"] & ~df["_signal_active"]
        df["_enter_short"] = df["_raw_short"] & ~df["_signal_active"]

        # --- Evaluate on the last confirmed bar ---
        last = df.iloc[-1]

        if pd.isna(last["_ema"]) or pd.isna(last.get("resample_720_close", np.nan)):
            return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)

        if last["_enter_long"]:
            return StrategyRecommendation(signal=SignalType.LONG, timestamp=timestamp)
        if last["_enter_short"]:
            return StrategyRecommendation(signal=SignalType.SHORT, timestamp=timestamp)

        return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)
