"""
HTF Candle Direction Strategy V1
Converted from PineScript v5.

Trades based on Higher Timeframe (HTF) candle direction:
- LONG  when HTF close > HTF open (bullish 12h candle) and EMA filter passes
- SHORT when HTF close < HTF open (bearish 12h candle) and EMA filter passes
- One signal per calendar day (Pine's `var bool signalActive` / `isNewDay` gate)
- Optional EMA(50) filter (default ON) and Volume filter (default OFF)

Note: The original Pine script exposes a repainting lookahead option. This
conversion always uses `lookahead_off` semantics — `resampled_merge` shifts
HTF data by one base-timeframe candle so no future information leaks.
"""

from datetime import datetime

import pandas as pd
import talib

from src.base_strategy import BaseStrategy, StrategyRecommendation, SignalType
from src.utils.resampling import resample_to_interval, resampled_merge


class HtfCandleDirectionStrategyV1Strategy(BaseStrategy):

    def __init__(self):
        super().__init__(
            name="HtfCandleDirectionStrategyV1",
            description=(
                "Trades in the direction of the 12h (720m) Higher Timeframe candle. "
                "Long when HTF close > open; short when HTF close < open. "
                "Optional EMA-50 and volume filters. One trade per calendar day."
            ),
            timeframe="15m",
            lookback_hours=600,  # 50 × 12h HTF bars
        )
        self.ema_length = 50
        self.vol_sma_length = 20
        self.vol_multiplier = 1.0
        self.use_ema_filter = True
        self.use_vol_filter = False
        # Dynamic RL warmup: 3× the longest indicator period on the base timeframe
        self.MIN_CANDLES_REQUIRED = 3 * max(self.ema_length, self.vol_sma_length)

    # ------------------------------------------------------------------
    def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
        # --- CRITICAL RL GUARD ---
        if len(df) < self.MIN_CANDLES_REQUIRED:
            return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)

        df = df.copy()

        # ----------------------------------------------------------------
        # HTF Resampling — request.security(syminfo.tickerid, "720", close/open)
        # 720 integer minutes = 12 h; resampled_merge prevents lookahead bias.
        # ----------------------------------------------------------------
        resampled_df = resample_to_interval(df, 720)
        df = resampled_merge(original=df, resampled=resampled_df, fill_na=True)

        # HTF candle direction (longCondition / shortCondition in Pine)
        htf_close = df["resample_720_close"]
        htf_open = df["resample_720_open"]
        df["_htf_long"] = (htf_close > htf_open).fillna(False)
        df["_htf_short"] = (htf_close < htf_open).fillna(False)

        # ----------------------------------------------------------------
        # EMA Filter — ta.ema(close, emaLength)
        # ----------------------------------------------------------------
        ema = pd.Series(
            talib.EMA(df["close"].values, timeperiod=self.ema_length),
            index=df.index,
        )
        df["_ema_long"] = (df["close"] > ema).fillna(False)
        df["_ema_short"] = (df["close"] < ema).fillna(False)

        # ----------------------------------------------------------------
        # Volume Filter — volume > ta.sma(volume, 20) * volMultiplier
        # ----------------------------------------------------------------
        vol_avg = pd.Series(
            talib.SMA(df["volume"].values, timeperiod=self.vol_sma_length),
            index=df.index,
        )
        df["_vol_ok"] = (df["volume"] > vol_avg * self.vol_multiplier).fillna(False)

        # ----------------------------------------------------------------
        # Combined filter (mirrors Pine's filterLong / filterShort)
        # ----------------------------------------------------------------
        if self.use_ema_filter and self.use_vol_filter:
            filter_long = df["_ema_long"] & df["_vol_ok"]
            filter_short = df["_ema_short"] & df["_vol_ok"]
        elif self.use_ema_filter:
            filter_long = df["_ema_long"]
            filter_short = df["_ema_short"]
        elif self.use_vol_filter:
            filter_long = df["_vol_ok"]
            filter_short = df["_vol_ok"]
        else:
            filter_long = pd.Series(True, index=df.index)
            filter_short = pd.Series(True, index=df.index)

        # ----------------------------------------------------------------
        # Raw signals (before one-per-day gate)
        # ----------------------------------------------------------------
        df["_raw_long"] = df["_htf_long"] & filter_long
        df["_raw_short"] = df["_htf_short"] & filter_short

        # ----------------------------------------------------------------
        # One-signal-per-day gate
        # Mirrors Pine: var bool signalActive / reset on ta.change(time("D"))
        # ----------------------------------------------------------------
        df["_day"] = pd.to_datetime(df["date"]).dt.normalize()
        df["_is_new_day"] = df["_day"] != df["_day"].shift(1)
        df["_day_group"] = df["_is_new_day"].cumsum()

        df["_any_signal"] = df["_raw_long"] | df["_raw_short"]
        # Cumulative count of signals that already fired earlier in the same day
        df["_prev_signal_count"] = df.groupby("_day_group")["_any_signal"].transform(
            lambda x: x.shift(1).fillna(0).cumsum()
        )
        df["_signal_active"] = df["_prev_signal_count"] > 0

        df["_enter_long"] = df["_raw_long"] & ~df["_signal_active"]
        df["_enter_short"] = df["_raw_short"] & ~df["_signal_active"]

        # ----------------------------------------------------------------
        # Evaluate on the last confirmed bar
        # ----------------------------------------------------------------
        last = df.iloc[-1]

        # Guard against a fully-NaN last bar (e.g. EMA not yet converged)
        if pd.isna(ema.iloc[-1]) or pd.isna(htf_close.iloc[-1]):
            return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)

        if last["_enter_long"]:
            return StrategyRecommendation(signal=SignalType.LONG, timestamp=timestamp)
        if last["_enter_short"]:
            return StrategyRecommendation(signal=SignalType.SHORT, timestamp=timestamp)

        return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)
