"""
Simple MA Crossover Strategy
=============================
Translated from PineScript v5 source.

Original strategy declaration params (not implemented in Python):
  - initial_capital=10000
  - default_qty_type=strategy.percent_of_equity
  - default_qty_value=10

Logic:
  - Short MA (SMA 9) crossing above Long MA (SMA 21) → LONG signal
  - Short MA (SMA 9) crossing below Long MA (SMA 21) → SHORT signal
  - Neither condition → HOLD
  - longCondition and shortCondition are mutually exclusive (crossover vs crossunder).
"""

from datetime import datetime

import numpy as np
import pandas as pd
import talib

from src.base_strategy import BaseStrategy, StrategyRecommendation, SignalType


class SimpleMACrossoverStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            name="Simple MA Crossover Strategy",
            description=(
                "A simple moving average crossover strategy. "
                "Enters long when the 9-period SMA crosses above the 21-period SMA, "
                "and enters short when the 9-period SMA crosses below the 21-period SMA."
            ),
            timeframe="1h",
            lookback_hours=100,
        )
        self.short_length = 9
        self.long_length = 21

    def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
        # === CALCULATIONS ===
        short_ma = pd.Series(
            talib.SMA(df["close"].values, timeperiod=self.short_length),
            index=df.index,
        )
        long_ma = pd.Series(
            talib.SMA(df["close"].values, timeperiod=self.long_length),
            index=df.index,
        )

        # === CONDITIONS ===
        # ta.crossover(a, b)  → (a > b) & (a.shift(1) <= b.shift(1))
        long_condition = (short_ma > long_ma) & (short_ma.shift(1) <= long_ma.shift(1))

        # ta.crossunder(a, b) → (a < b) & (a.shift(1) >= b.shift(1))
        short_condition = (short_ma < long_ma) & (short_ma.shift(1) >= long_ma.shift(1))

        # === SIGNAL EVALUATION (last complete bar) ===
        is_long = bool(long_condition.iloc[-1])
        is_short = bool(short_condition.iloc[-1])

        if is_long:
            signal = SignalType.LONG
        elif is_short:
            signal = SignalType.SHORT
        else:
            signal = SignalType.HOLD

        return StrategyRecommendation(signal, timestamp)
