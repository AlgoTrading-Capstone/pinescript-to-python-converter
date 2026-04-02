from datetime import datetime
import numpy as np
import pandas as pd
from src.base_strategy import BaseStrategy, StrategyRecommendation, SignalType


class GapFillingStrategy(BaseStrategy):
    """
    Gap Filling Strategy — converted from Pine Script v4 by alexgrover.

    Detects significant up-gaps (open > prev high, no overlap with prev body)
    and down-gaps (open < prev low, no overlap with prev body) at new daily
    sessions. Trades the expectation that the gap will be filled:
      - Down gap  → LONG  (price expected to fill up)
      - Up gap    → SHORT (price expected to fill down)
      (reversed when invert=True)

    Positions are closed at the start of a new session with no new gap (FLAT).
    strategy.exit() stop/limit levels are managed externally.

    Original source: https://www.tradingview.com/script/ghocsiv7-Gap-Filling-Strategy/
    """

    def __init__(self, invert: bool = False):
        super().__init__(
            name="GapFillingStrategy",
            description=(
                "Gap Filling Strategy: enters long on significant down-gaps and short "
                "on up-gaps at new daily sessions, expecting the gap to be filled. "
                "Exits on the next new session with no gap."
            ),
            timeframe="1d",
            lookback_hours=48,
        )
        self.invert = invert
        # Only requires shift(1); 3 bars is a safe minimum
        self.MIN_CANDLES_REQUIRED = 3

    def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
        if len(df) < self.MIN_CANDLES_REQUIRED:
            return StrategyRecommendation(SignalType.HOLD, timestamp)

        # --- Session detection: True on every bar where the calendar date changes ---
        dates = df['date'].dt.date
        ses = pd.Series(dates != dates.shift(1), index=df.index)
        ses.iloc[0] = False

        # --- Previous bar references ---
        prev_high = df['high'].shift(1)
        prev_low = df['low'].shift(1)
        prev_close = df['close'].shift(1)
        prev_open = df['open'].shift(1)

        # --- Gap conditions (significant gaps: full body non-overlap + open beyond prev high/low) ---
        # Up gap: current open above previous high AND current body is entirely above previous body
        upgap = (df['open'] > prev_high) & (
            np.minimum(df['close'], df['open']) > np.maximum(prev_close, prev_open)
        )
        # Down gap: current open below previous low AND current body is entirely below previous body
        dngap = (df['open'] < prev_low) & (
            np.minimum(prev_close, prev_open) > np.maximum(df['close'], df['open'])
        )

        # --- Evaluate last bar ---
        last_ses = bool(ses.iloc[-1])
        last_upgap = bool(upgap.iloc[-1])
        last_dngap = bool(dngap.iloc[-1])

        # --- Signal logic (default clw="New Session") ---
        # On new session: enter if gap found, else flatten
        if self.invert:
            long_entry = last_ses and last_upgap
            short_entry = last_ses and last_dngap
        else:
            long_entry = last_ses and last_dngap
            short_entry = last_ses and last_upgap

        if long_entry:
            return StrategyRecommendation(SignalType.LONG, timestamp)
        elif short_entry:
            return StrategyRecommendation(SignalType.SHORT, timestamp)
        elif last_ses:
            # New session but no qualifying gap → close any open position
            return StrategyRecommendation(SignalType.FLAT, timestamp)

        return StrategyRecommendation(SignalType.HOLD, timestamp)
