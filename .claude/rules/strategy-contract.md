# Strategy Code Contract & Anti-Lookahead Rules

This rule applies ANY TIME you are writing, editing, or reviewing a Python trading strategy.

## 1. Class Structure & Naming
- **File Naming:** MUST be `src/strategies/<safe_name>_strategy.py`.
- **Inheritance:** MUST inherit from `BaseStrategy`.
- **Two abstract methods, both required:** every subclass MUST implement BOTH
  `generate_all_signals` (batch / vectorized) AND `step` (live / streaming).
  The class CANNOT be instantiated otherwise — the statistical gate's loader
  will raise `TypeError: Can't instantiate abstract class ...`.
- **Dynamic Warmup (RL Constraint):** `self.MIN_CANDLES_REQUIRED` MUST be
  computed dynamically in `__init__` from the indicator periods
  (e.g., `3 * max(p1, p2)`). Static class-level constants crash RL
  hyperparameter tuning.

## 2. Anti-Lookahead Bias (CRITICAL)
- **Forbidden:** `np.roll()` is STRICTLY BANNED — it wraps arrays and leaks
  future data. Use `pd.Series.shift()` (positive integers only).
- **Vectorized mode is causal:** every operation in `generate_all_signals`
  must be strictly backward-looking. Rolling windows, `shift(+n)`, `cummax`,
  `expanding`, `ewm` are allowed; `shift(-n)`, `np.roll`, future-indexed
  slices are forbidden.
- **Multi-Timeframe (MTF):** You MUST NOT use `df.resample()` directly.
  Use:
  ```python
  from src.utils.resampling import resample_to_interval, resampled_merge
  ```
  All timeframe strings must be strictly lowercase (e.g., `"15m"`, `"4h"`).

## 3. Allowed Libraries & Type Safety
Allowed: `pandas`, `numpy`, `talib`, and `src.*`. No other third-party libs.

- **TA-Lib type safety:** NEVER use raw integers for moving average types.
  Always import: `from talib import MA_Type`.
- **Missing TA-Lib indicators:** Implement in pure Pandas.
  - RMA: `df.ewm(alpha=1/length, adjust=False).mean()`

## 4. Required Method Signatures

### 4.1 `generate_all_signals` — batch / vectorized mode
The statistical gate calls this exactly once on a multi-year DataFrame.

```python
def generate_all_signals(self, df: pd.DataFrame) -> pd.Series:
    """Return one signal string per row, aligned to df.index."""
    n = len(df)
    signals = pd.Series(["FLAT"] * n, index=df.index, dtype=object)

    if n < self.MIN_CANDLES_REQUIRED:
        return signals  # all FLAT during warmup — required by the gate

    # ... fully vectorized indicator + condition computation ...
    # signals.iloc[self.MIN_CANDLES_REQUIRED:] = np.where(long_cond, "LONG",
    #                                            np.where(short_cond, "SHORT", "FLAT"))
    return signals
```

Contract enforced by `src/evaluation/runner.py`:
- Returns a `pd.Series` of length `len(df)` with `df.index`.
- Values strictly in `{"LONG", "SHORT", "FLAT", "HOLD"}`.
- Rows `0 .. MIN_CANDLES_REQUIRED - 1` MUST all be `"FLAT"`.
- Total runtime under 60s — Python-level row loops are a contract failure.

### 4.2 `step` — live / streaming mode
Consumes a single new candle and returns the signal for that bar. Maintains
the strategy's own internal rolling state (do NOT rebuild indicators every call).

```python
def step(self, candle: pd.Series) -> SignalType:
    """Update internal state with one new candle and return its signal."""
    self._observed += 1
    if self._observed < self.MIN_CANDLES_REQUIRED:
        return SignalType.FLAT
    # ... incremental update of self._fast_ema, self._slow_ema, etc. ...
    return SignalType.LONG  # or SHORT / FLAT / HOLD
```

The `__init__` must seed any rolling accumulators (`self._observed = 0`,
`self._fast_ema = None`, etc.) so `step` works from a cold start.

### 4.3 Optional: `warmup(df)`
Default is a no-op. Override only if `step` needs historical state seeded
before the first live tick.