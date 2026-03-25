"""
Built-in Kelly Ratio for Dynamic Position Sizing Strategy

Converted from PineScript v4 to Python.
Original: https://www.tradingview.com/script/bFXf4IXh-Built-in-Kelly-ratio-for-dynamic-position-sizing/

Description:
    Keltner Channel strategy with Kelly ratio for dynamic position sizing
    based on trade performance. Uses an Exponential/Simple MA band with
    ATR/TR/Range envelopes to detect breakout entries via stop orders.
    Kelly ratio is computed from cumulative trade history (win rate, avg
    win/loss) and used to size positions dynamically.
"""

from datetime import datetime

import numpy as np
import pandas as pd
import talib
from talib import MA_Type

from src.base_strategy import BaseStrategy, SignalType, StrategyRecommendation


class BuiltInKellyRatioForDynamicPositionSizingStrategy(BaseStrategy):
    """
    Keltner Channel breakout strategy with Kelly ratio position sizing.

    Parameters
    ----------
    length : int
        MA / band lookback (default 20).
    mult : float
        Band multiplier (default 1.0).
    use_exp : bool
        Use EMA instead of SMA for the mid-band (default True).
    bands_style : str
        One of "Average True Range", "True Range", "Range" (default "Average True Range").
    atr_length : int
        ATR period used when bands_style == "Average True Range" (default 10).
    """

    def __init__(
        self,
        length: int = 20,
        mult: float = 1.0,
        use_exp: bool = True,
        bands_style: str = "Average True Range",
        atr_length: int = 10,
    ):
        super().__init__(
            name="Built-in Kelly Ratio for Dynamic Position Sizing",
            description=(
                "Keltner Channel breakout strategy with Kelly ratio for dynamic "
                "position sizing based on cumulative trade performance."
            ),
            timeframe="15m",
            lookback_hours=int(max(length, atr_length) * 3 * 15 / 60) + 1,
        )
        self.length = length
        self.mult = mult
        self.use_exp = use_exp
        self.bands_style = bands_style
        self.atr_length = atr_length

        # Dynamic warmup: need enough bars for the longest indicator to stabilise.
        # 3x the longest period ensures convergence.
        self.MIN_CANDLES_REQUIRED = 3 * max(self.length, self.atr_length)

        # Internal trade-history state for Kelly ratio computation
        self._closed_trades: int = 0
        self._win_trades: int = 0
        self._gross_profit: float = 0.0
        self._gross_loss: float = 0.0
        self._open_position: str | None = None   # "long" | "short" | None
        self._entry_price: float = 0.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _kelly_ratio(self) -> float:
        """Compute the fractional Kelly ratio from accumulated trade history."""
        if self._closed_trades == 0 or self._win_trades == 0:
            return 0.0
        loss_trades = self._closed_trades - self._win_trades
        if loss_trades == 0:
            return 1.0  # All winners – cap exposure at 100 %
        krp = self._win_trades / self._closed_trades
        avg_win = self._gross_profit / self._win_trades
        avg_loss = self._gross_loss / loss_trades
        if avg_loss == 0.0:
            return 1.0
        kr = krp - (1 - krp) / (avg_win / avg_loss)
        return max(0.0, kr)  # Kelly < 0 → no trade

    def _record_close(self, exit_price: float) -> None:
        """Update trade history when a position is closed."""
        if self._open_position is None:
            return
        if self._open_position == "long":
            pnl = exit_price - self._entry_price
        else:
            pnl = self._entry_price - exit_price

        self._closed_trades += 1
        if pnl > 0:
            self._win_trades += 1
            self._gross_profit += pnl
        else:
            self._gross_loss += abs(pnl)

        self._open_position = None
        self._entry_price = 0.0

    # ------------------------------------------------------------------
    # Core run method
    # ------------------------------------------------------------------

    def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
        """
        Execute strategy logic on the provided OHLCV DataFrame.

        Parameters
        ----------
        df : pd.DataFrame
            Columns: open, high, low, close, volume.
        timestamp : datetime
            Current evaluation timestamp (UTC).

        Returns
        -------
        StrategyRecommendation
        """
        if len(df) < self.MIN_CANDLES_REQUIRED:
            return StrategyRecommendation(SignalType.HOLD, timestamp)

        close = df["close"].values.astype(np.float64)
        high = df["high"].values.astype(np.float64)
        low = df["low"].values.astype(np.float64)

        # ---- Mid-band (EMA or SMA) ----
        if self.use_exp:
            ma = talib.EMA(close, timeperiod=self.length)
        else:
            ma = talib.SMA(close, timeperiod=self.length)

        # ---- Range band ----
        if self.bands_style == "True Range":
            tr = talib.TRANGE(high, low, close)
            # RMA = EWM with alpha=1/length (equivalent to PineScript's rma())
            rangema = (
                pd.Series(tr)
                .ewm(alpha=1.0 / self.length, adjust=False)
                .mean()
                .values
            )
        elif self.bands_style == "Average True Range":
            rangema = talib.ATR(high, low, close, timeperiod=self.atr_length)
        else:  # "Range"
            rng = high - low
            rangema = (
                pd.Series(rng)
                .ewm(alpha=1.0 / self.length, adjust=False)
                .mean()
                .values
            )

        upper = ma + rangema * self.mult
        lower = ma - rangema * self.mult

        # Convert to Series for shift-based comparisons (no np.roll)
        src = pd.Series(close)
        upper_s = pd.Series(upper)
        lower_s = pd.Series(lower)

        # crossover(src, upper)  → src crosses above upper
        cross_upper = (src > upper_s) & (src.shift(1) <= upper_s.shift(1))
        # crossunder(src, lower) → src crosses below lower
        cross_lower = (src < lower_s) & (src.shift(1) >= lower_s.shift(1))

        # bprice: last high+tick when cross_upper triggered
        # sprice: last low-tick when cross_lower triggered
        tick = src.iloc[-1] * 1e-5  # approximate syminfo.mintick

        bprice = pd.Series(np.nan, index=src.index)
        sprice = pd.Series(np.nan, index=src.index)
        for i in range(len(src)):
            if cross_upper.iloc[i]:
                bprice.iloc[i] = high[i] + tick
            elif i > 0 and not np.isnan(bprice.iloc[i - 1]):
                bprice.iloc[i] = bprice.iloc[i - 1]

            if cross_lower.iloc[i]:
                sprice.iloc[i] = low[i] - tick
            elif i > 0 and not np.isnan(sprice.iloc[i - 1]):
                sprice.iloc[i] = sprice.iloc[i - 1]

        ma_s = pd.Series(ma)

        # crossBcond: True once crossUpper fires, stays True until cancelled
        cross_bcond = pd.Series(False, index=src.index)
        cross_scond = pd.Series(False, index=src.index)
        for i in range(len(src)):
            if cross_upper.iloc[i]:
                cross_bcond.iloc[i] = True
            elif i > 0:
                cross_bcond.iloc[i] = cross_bcond.iloc[i - 1]
            if cross_lower.iloc[i]:
                cross_scond.iloc[i] = True
            elif i > 0:
                cross_scond.iloc[i] = cross_scond.iloc[i - 1]

        # cancelBcond = crossBcond and (src < ma or high >= bprice)
        cancel_bcond = cross_bcond & (
            (src < ma_s) | (pd.Series(high) >= bprice)
        )
        # cancelScond = crossScond and (src > ma or low <= sprice)
        cancel_scond = cross_scond & (
            (src > ma_s) | (pd.Series(low) <= sprice)
        )

        # ---- Evaluate current bar (last row) ----
        idx = len(df) - 1
        c = close[idx]

        do_cross_upper = bool(cross_upper.iloc[idx])
        do_cross_lower = bool(cross_lower.iloc[idx])
        do_cancel_b = bool(cancel_bcond.iloc[idx])
        do_cancel_s = bool(cancel_scond.iloc[idx])

        # Simulate closing any open position when a reversal signal fires
        if self._open_position == "long" and (do_cross_lower or do_cancel_b):
            self._record_close(c)
        elif self._open_position == "short" and (do_cross_upper or do_cancel_s):
            self._record_close(c)

        # ---- Determine signal ----
        signal = SignalType.HOLD

        # In PineScript the stop orders (bprice / sprice) are *placed* on the
        # crossover bar and would fill on a subsequent bar once price trades
        # through them.  In a vectorised feature-extractor we treat the
        # crossover bar itself as the entry signal (conservative approximation).
        if do_cancel_b and not do_cross_upper:
            # Long entry cancelled
            signal = SignalType.HOLD
        elif do_cross_upper:
            # Crossover detected → long entry signal
            kr = self._kelly_ratio()
            if kr > 0 or self._closed_trades == 0:
                self._open_position = "long"
                self._entry_price = c
                signal = SignalType.LONG
        elif do_cancel_s and not do_cross_lower:
            signal = SignalType.HOLD
        elif do_cross_lower:
            # Crossunder detected → short entry signal
            kr = self._kelly_ratio()
            if kr > 0 or self._closed_trades == 0:
                self._open_position = "short"
                self._entry_price = c
                signal = SignalType.SHORT

        # If we have an open position and no new signal, hold current direction
        if signal == SignalType.HOLD and self._open_position == "long":
            signal = SignalType.LONG
        elif signal == SignalType.HOLD and self._open_position == "short":
            signal = SignalType.SHORT

        return StrategyRecommendation(signal, timestamp)
