from collections import deque

import numpy as np
import pandas as pd
import talib
from talib import MA_Type

from src.base_strategy import BaseStrategy, SignalType


class BBRSIRentable4hStrategy(BaseStrategy):
    """Bollinger Bands + RSI mean-reversion strategy (4h).

    LONG  when close < lower band AND RSI < oversold threshold.
    SHORT when close > upper band AND RSI > overbought threshold.

    Risk management (SL/TP, position sizing) from the original PineScript is
    handled by the external execution engine and is intentionally omitted.
    """

    def __init__(self):
        super().__init__(
            name="BBRSIRentable4hStrategy",
            description="BB + RSI mean-reversion, 4h, long below lower band / short above upper band.",
            timeframe="4h",
            lookback_hours=20 * 4,
        )

        self.bb_length = 20
        self.bb_mult = 2.0
        self.rsi_length = 14
        self.rsi_ob = 70.0
        self.rsi_os = 30.0

        self.MIN_CANDLES_REQUIRED = 3 * max(self.bb_length, self.rsi_length)

        self._observed = 0
        self._closes: deque = deque(maxlen=self.bb_length)
        self._prev_close: float | None = None
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._rsi_seed_gains: list[float] = []
        self._rsi_seed_losses: list[float] = []

    def generate_all_signals(self, df: pd.DataFrame) -> pd.Series:
        n = len(df)
        signals = pd.Series(["FLAT"] * n, index=df.index, dtype=object)
        if n < self.MIN_CANDLES_REQUIRED:
            return signals

        close = df["close"].astype(float).to_numpy()

        upper, _middle, lower = talib.BBANDS(
            close,
            timeperiod=self.bb_length,
            nbdevup=self.bb_mult,
            nbdevdn=self.bb_mult,
            matype=MA_Type.SMA,
        )
        rsi = talib.RSI(close, timeperiod=self.rsi_length)

        upper_s = pd.Series(upper, index=df.index)
        lower_s = pd.Series(lower, index=df.index)
        rsi_s = pd.Series(rsi, index=df.index)
        close_s = df["close"].astype(float)

        long_cond = (close_s < lower_s) & (rsi_s < self.rsi_os)
        short_cond = (close_s > upper_s) & (rsi_s > self.rsi_ob)

        signals = pd.Series(
            np.where(long_cond, "LONG", np.where(short_cond, "SHORT", "FLAT")),
            index=df.index,
            dtype=object,
        )
        signals.iloc[: self.MIN_CANDLES_REQUIRED] = "FLAT"
        return signals

    def step(self, candle: pd.Series) -> SignalType:
        close = float(candle["close"])
        self._observed += 1

        self._closes.append(close)
        self._update_rsi(close)
        self._prev_close = close

        if self._observed < self.MIN_CANDLES_REQUIRED:
            return SignalType.FLAT
        if len(self._closes) < self.bb_length:
            return SignalType.FLAT
        rsi = self._current_rsi()
        if rsi is None:
            return SignalType.FLAT

        arr = np.fromiter(self._closes, dtype=float, count=self.bb_length)
        mean = arr.mean()
        std = arr.std(ddof=0)
        upper = mean + self.bb_mult * std
        lower = mean - self.bb_mult * std

        if close < lower and rsi < self.rsi_os:
            return SignalType.LONG
        if close > upper and rsi > self.rsi_ob:
            return SignalType.SHORT
        return SignalType.FLAT

    def _update_rsi(self, close: float) -> None:
        if self._prev_close is None:
            return
        change = close - self._prev_close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if self._avg_gain is None:
            self._rsi_seed_gains.append(gain)
            self._rsi_seed_losses.append(loss)
            if len(self._rsi_seed_gains) == self.rsi_length:
                self._avg_gain = sum(self._rsi_seed_gains) / self.rsi_length
                self._avg_loss = sum(self._rsi_seed_losses) / self.rsi_length
            return

        n = self.rsi_length
        self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
        self._avg_loss = (self._avg_loss * (n - 1) + loss) / n

    def _current_rsi(self) -> float | None:
        if self._avg_gain is None or self._avg_loss is None:
            return None
        if self._avg_loss == 0.0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
