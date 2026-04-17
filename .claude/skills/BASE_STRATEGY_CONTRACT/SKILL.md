---
name: base-strategy-contract
description: Enforces the mandatory BaseStrategy Python contract for PineScript transpilation. Automatically use this skill WHENEVER generating, modifying, or validating a strategy class. It ensures the strategy correctly inherits from BaseStrategy, implements BOTH abstract methods (generate_all_signals + step), uses the dynamic MIN_CANDLES_REQUIRED for RL safety, and follows the _strategy.py file naming convention required for CI/CD discovery and the statistical gate's loader.
---

# Base Strategy Contract

All generated Python strategies MUST strictly adhere to the `BaseStrategy`
interface defined in `src/base_strategy.py`. You are strictly forbidden from
modifying `src/base_strategy.py`.

`BaseStrategy` declares **two abstract methods**. A subclass that fails to
implement either one cannot be instantiated, and the statistical gate will
reject the strategy with `TypeError: Can't instantiate abstract class ...`.

## Core Implementation Rules

1. **Inheritance:** Every generated strategy class MUST inherit from `BaseStrategy`.
2. **File naming:** `src/strategies/<safe_name>_strategy.py` — the `_strategy.py`
   suffix is mandatory.
3. **Required imports:**
   ```python
   import numpy as np
   import pandas as pd
   from src.base_strategy import BaseStrategy, SignalType
   ```
   Add `import talib` and `from talib import MA_Type` only if used.

## Initialization (`__init__`)

```python
def __init__(self):
    super().__init__(
        name="StrategyName",
        description="One-line summary of the strategy.",
        timeframe="15m",          # lowercase (e.g., "15m", "1h", "4h", "1d")
        lookback_hours=48,
    )

    # Indicator periods (extracted from the PineScript inputs)
    self.fast_period = 10
    self.slow_period = 30

    # CRITICAL RL GUARD: warmup MUST be derived from indicator periods.
    # Static class-level constants crash RL hyperparameter tuning.
    self.MIN_CANDLES_REQUIRED = 3 * max(self.fast_period, self.slow_period)

    # Streaming-mode state (used by step()). Seed cold-start values here
    # so the first live tick has well-defined buffers.
    self._observed = 0
    self._fast_ema = None
    self._slow_ema = None
    self._prev_fast_above_slow = None
```

## Required Method 1 — `generate_all_signals` (Batch / Vectorized)

The statistical gate calls this exactly once on a multi-year DataFrame.
Vectorize aggressively — no Python loops over `df` rows.

```python
def generate_all_signals(self, df: pd.DataFrame) -> pd.Series:
    n = len(df)
    signals = pd.Series(["FLAT"] * n, index=df.index, dtype=object)
    if n < self.MIN_CANDLES_REQUIRED:
        return signals  # all FLAT during warmup

    fast = df["close"].rolling(self.fast_period).mean()
    slow = df["close"].rolling(self.slow_period).mean()
    fast_above = fast > slow
    long_entry  = fast_above & ~fast_above.shift(1).fillna(False)
    short_entry = ~fast_above & fast_above.shift(1).fillna(False)

    signals = pd.Series(
        np.where(long_entry,  "LONG",
        np.where(short_entry, "SHORT", "FLAT")),
        index=df.index, dtype=object,
    )
    # Re-assert the warmup contract after the vectorized pass
    signals.iloc[: self.MIN_CANDLES_REQUIRED] = "FLAT"
    return signals
```

**Hard requirements (enforced by `src/evaluation/runner.py`):**
- Returns a `pd.Series` whose length and index match the input `df`.
- Values are strictly in `{"LONG", "SHORT", "FLAT", "HOLD"}`.
- Rows `0 .. MIN_CANDLES_REQUIRED - 1` are ALL `"FLAT"`.
- Whole call completes in under 60 seconds. A row-by-row loop will time out.
- `np.roll` is BANNED. Use `pd.Series.shift(+n)` only.

## Required Method 2 — `step` (Live / Streaming)

Receives a single new candle and returns the signal for that bar.
The strategy MUST maintain its own internal rolling state (no rebuilding
indicators from scratch every tick).

```python
def step(self, candle: pd.Series) -> SignalType:
    close = float(candle["close"])
    self._observed += 1

    # Incremental EMA update — replace with the strategy's actual indicators
    alpha_fast = 2 / (self.fast_period + 1)
    alpha_slow = 2 / (self.slow_period + 1)
    self._fast_ema = close if self._fast_ema is None else (
        alpha_fast * close + (1 - alpha_fast) * self._fast_ema
    )
    self._slow_ema = close if self._slow_ema is None else (
        alpha_slow * close + (1 - alpha_slow) * self._slow_ema
    )

    if self._observed < self.MIN_CANDLES_REQUIRED:
        return SignalType.FLAT

    fast_above = self._fast_ema > self._slow_ema
    crossed_up   = self._prev_fast_above_slow is False and fast_above
    crossed_down = self._prev_fast_above_slow is True  and not fast_above
    self._prev_fast_above_slow = fast_above

    if crossed_up:   return SignalType.LONG
    if crossed_down: return SignalType.SHORT
    return SignalType.FLAT
```

**Hard requirements:**
- Returns a `SignalType` enum member, NOT a string.
- Cold-start safe: returns `SignalType.FLAT` while
  `self._observed < self.MIN_CANDLES_REQUIRED`.
- Updates internal accumulators incrementally — never re-reads history.

## Optional: `warmup(df)` and `reset()`

Override `warmup(df)` if `step` needs internal state seeded from history
before the first live tick. Default is a no-op. `reset()` clears state and
defaults to a no-op as well — override if your strategy holds buffers that
must be flushed when a live session restarts.

## Validation

A generated strategy is REJECTED by the Validator if it:
- Implements only one of `generate_all_signals` / `step`.
- Returns a string from `step` (must be `SignalType` enum).
- Returns a `SignalType` enum (instead of strings) from `generate_all_signals`.
- Emits any non-FLAT value within the first `MIN_CANDLES_REQUIRED` rows.
- Uses `np.roll` or any negative-shift / future-indexed access.
- Uses static class-level `MIN_CANDLES_REQUIRED`.