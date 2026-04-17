"""
Base Strategy Interface

Every trading strategy subclass must support TWO distinct execution modes:

1. BATCH / VECTORIZED MODE — `generate_all_signals(df) -> pd.Series`
   Compute signals for every row of a historical DataFrame in a single vectorized
   pass. Used by the converter's statistical gate, the RL DataManager cache
   build, and any backtesting pipeline. Must NOT be used in live trading.

2. STREAMING / LIVE MODE — `step(candle) -> SignalType`
   Consume a single new candle (typically a pd.Series with OHLCV fields and a
   timestamp), update the strategy's own internal stateful indicators (rolling
   EMA trackers, ATR accumulators, position state, etc.), and return the signal
   for this bar. Used ONLY in live production trading.

These modes are intentionally separate. A live strategy cannot afford to
recompute 200k rows of history on every tick, and a batch evaluator cannot
afford the overhead of iterative Python calls. Neither method may emulate the
other — each is implemented from scratch against the same strategy logic.

Optional lifecycle helpers (concrete, default to no-op):
    - warmup(df):  hydrate internal state from historical data before step()
                   is called in a live session.
    - reset():     clear internal state (e.g., when restarting a live session).
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import NamedTuple
from datetime import datetime
import pandas as pd


class SignalType(Enum):
    """
    Enumeration of possible trading signals.
    """
    LONG = "LONG"   # Target position is long
    SHORT = "SHORT" # Target position is short
    FLAT = "FLAT"   # Target position is zero (no exposure)
    HOLD = "HOLD"   # No recommendation / keep current exposure


class StrategyRecommendation(NamedTuple):
    """Standard output when a caller wants signal + timestamp bundled."""
    signal: SignalType
    timestamp: datetime


class BaseStrategy(ABC):
    """
    Universal contract for all trading strategies.

    Required attributes:
        - name, description, timeframe, lookback_hours (via __init__)
        - MIN_CANDLES_REQUIRED (set by subclass; minimum history needed for a
          non-FLAT signal)

    Required methods (both abstract — subclasses MUST implement both):
        - generate_all_signals(df) -> pd.Series        (batch mode)
        - step(candle)             -> SignalType       (live/streaming mode)

    Optional methods (concrete no-ops by default):
        - warmup(df) -> None       (seed live state from history)
        - reset()    -> None       (clear live state)
    """

    def __init__(self, name: str, description: str, timeframe: str, lookback_hours: int):
        self._name = name
        self._description = description
        self._timeframe = timeframe
        self._lookback_hours = lookback_hours
        self.MIN_CANDLES_REQUIRED: int = 0

    # ---- getters (read-only) ----

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def timeframe(self) -> str:
        return self._timeframe

    @property
    def lookback_hours(self) -> int:
        return self._lookback_hours

    # ---- required: batch mode ----

    @abstractmethod
    def generate_all_signals(self, df: pd.DataFrame) -> pd.Series:
        """
        Compute the signal for every row of `df` in a single vectorized pass.

        :param df: pandas DataFrame of candles ordered by time. Must contain the
                   OHLCV columns the strategy depends on.
        :return:   pd.Series with the same index as `df` whose values are strings
                   in {'LONG', 'SHORT', 'FLAT', 'HOLD'}. Rows before
                   MIN_CANDLES_REQUIRED must be 'FLAT'.

        Implementation guidance: use pandas/numpy/TA-Lib vectorized operations.
        Do NOT loop `step` internally — that defeats the purpose of this mode.
        """
        pass

    # ---- required: live/streaming mode ----

    @abstractmethod
    def step(self, candle: pd.Series) -> SignalType:
        """
        Process a single new candle and return this bar's signal.

        :param candle: pd.Series with fields ('open', 'high', 'low', 'close',
                       'volume') and a name/index that carries the candle
                       timestamp (or a 'timestamp' field).
        :return:       SignalType for this bar.

        Implementation guidance: the strategy MUST maintain its own internal
        rolling state (e.g., partial EMA values, ATR accumulators, current
        position flag) and update it incrementally with this single candle.
        Do NOT rebuild indicators from scratch on every call.

        Strategies should handle the cold-start case: if fewer than
        MIN_CANDLES_REQUIRED candles have been observed (either via prior
        `step` calls or a preceding `warmup`), return SignalType.FLAT.
        """
        pass

    # ---- optional lifecycle hooks ----

    def warmup(self, df: pd.DataFrame) -> None:
        """
        Seed live-mode state from a historical DataFrame before streaming begins.

        Default implementation is a no-op. Strategies with stateful indicators
        should override this to initialize their rolling accumulators so the
        first live `step` call produces a valid signal without waiting
        MIN_CANDLES_REQUIRED ticks in production.
        """
        return None

 