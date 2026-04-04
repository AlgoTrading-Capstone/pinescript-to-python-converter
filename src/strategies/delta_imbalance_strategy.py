"""
Delta Imbalance Strategy
Converted from Pine Script: Delta Imbalance Strategy
Source: https://www.tradingview.com/script/tpgANCaq-Delta-Imbalance-Strategy/

Order-flow based scalping strategy that maintains a bull/bear imbalance ledger.
Each candle's delta (close - open) * volume is accumulated into separate bull/bear
pools. Imbalance pools decay each bar and are resolved by opposing delta.
Signals fire when an extreme imbalance (above threshold * avg_delta) starts
resolving via an opposing candle.

Designed for low-timeframe scalping (1m – 5m). Do NOT use above 5m.

EXIT LOGIC NOT CONVERTED:
The original Pine Script managed exits via strategy.exit() with ATR-based parameters:
  - Long exit:  stop = close - ATR(14)*2,  limit = close + ATR(14)*3
  - Short exit: stop = close + ATR(14)*2,  limit = close - ATR(14)*3
These stop-loss and take-profit levels are NOT implemented here. They must be
configured in the external execution / RL engine layer.
"""

from datetime import datetime

import numpy as np
import pandas as pd
import talib

from src.base_strategy import BaseStrategy, StrategyRecommendation, SignalType


class DeltaImbalanceStrategy(BaseStrategy):

    def __init__(self):
        super().__init__(
            name="DeltaImbalanceStrategy",
            description=(
                "Converted from Pine Script: Delta Imbalance Strategy. "
                "Tracks cumulative bull/bear order-flow imbalance pools with decay, "
                "normalised by a 50-bar SMA of |delta|. Enters SHORT when bull imbalance "
                "exceeds threshold and the current candle is bearish; enters LONG when bear "
                "imbalance exceeds threshold and the current candle is bullish. "
                "Optimised for 1m\u20135m scalping."
            ),
            timeframe="1m",
            lookback_hours=6,
        )

        # Indicator parameters
        self.decay = 0.95
        self.threshold = 1.5
        self.sma_period = 50       # ta.sma(|delta|, 50)
        self.atr_period = 14       # ta.atr(14) — stop/limit ignored (external RL engine)

        # CRITICAL RL GUARD: dynamic warmup from max indicator length
        self.MIN_CANDLES_REQUIRED = 3 * max(self.sma_period, self.atr_period)  # 150

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_imbalance(self, delta_vals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """
        Vectorised-impossible stateful loop: each bar's imbalance depends on
        the previous bar's output after decay, so a Python loop is mandatory.

        Returns:
            bull_imbalance, bear_imbalance  — both shape (n,)
        """
        n = len(delta_vals)
        bull_imb = np.zeros(n)
        bear_imb = np.zeros(n)

        decay = self.decay

        for i in range(1, n):
            b = bull_imb[i - 1]
            br = bear_imb[i - 1]
            d = delta_vals[i]

            # Accumulate imbalance
            if d > 0:
                b += d
            else:
                br += -d

            # Resolve imbalance (counter candles)
            if d < 0:
                b -= abs(d)
            if d > 0:
                br -= abs(d)

            # Prevent negative pools
            b = b if b > 0.0 else 0.0
            br = br if br > 0.0 else 0.0

            # Apply decay
            bull_imb[i] = b * decay
            bear_imb[i] = br * decay

        return bull_imb, bear_imb

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
        # --- CRITICAL RL GUARD ---
        if len(df) < self.MIN_CANDLES_REQUIRED:
            return StrategyRecommendation(SignalType.HOLD, timestamp)

        df = df.copy()

        # --- Delta (order flow proxy) ---
        df['delta'] = (df['close'] - df['open']) * df['volume']
        delta_vals = df['delta'].values

        # --- Imbalance ledger (stateful loop) ---
        bull_imb, bear_imb = self._compute_imbalance(delta_vals)
        df['bull_imbalance'] = bull_imb
        df['bear_imbalance'] = bear_imb

        # --- Normalisation: avg_delta = SMA(|delta|, 50) ---
        df['avg_delta'] = talib.SMA(np.abs(delta_vals).astype(float), timeperiod=self.sma_period)

        # Guard against division by zero / NaN avg_delta
        df['bull_strength'] = (df['bull_imbalance'] / df['avg_delta']).fillna(0.0).replace([np.inf, -np.inf], 0.0)
        df['bear_strength'] = (df['bear_imbalance'] / df['avg_delta']).fillna(0.0).replace([np.inf, -np.inf], 0.0)

        # --- Signal logic (evaluate on last complete bar) ---
        last = df.iloc[-1]

        bull_peak = last['bull_strength'] > self.threshold
        bear_peak = last['bear_strength'] > self.threshold

        resolve_short = bull_peak and last['delta'] < 0
        resolve_long = bear_peak and last['delta'] > 0

        # Signal priority: SHORT before LONG (both are entries, exits handled externally)
        if resolve_short:
            return StrategyRecommendation(SignalType.SHORT, timestamp)
        if resolve_long:
            return StrategyRecommendation(SignalType.LONG, timestamp)

        return StrategyRecommendation(SignalType.HOLD, timestamp)
