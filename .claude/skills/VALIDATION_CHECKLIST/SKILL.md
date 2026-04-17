---
name: validation-checklist
description: The ultimate gatekeeper checklist for the Validator Agent. Automatically load this skill WHENEVER reviewing, validating, or approving a generated Python strategy. Enforces strict CI/CD constraints (file naming), RL safety (dynamic MIN_CANDLES_REQUIRED, no np.roll, strictly causal vectorization), BOTH abstract methods (generate_all_signals + step), and the statistical-gate signal contract before allowing the code to proceed to the Test Generation phase.
---

# Validation Checklist

The Validator Agent MUST verify the generated Python strategy against this
exact checklist. If ANY check fails, validation is FAILED.

## 1. Syntax & Imports
- [ ] Code is valid Python 3.11+.
- [ ] No syntax errors or unresolved references.
- [ ] All necessary imports present (`numpy`, `pandas`, `talib` if used,
      `from src.base_strategy import BaseStrategy, SignalType`).
- [ ] **Type Safety:** if `talib` moving averages are used,
      `from talib import MA_Type` MUST be imported and used
      (e.g., `matype=MA_Type.SMA`), never raw integers like `0`.

## 2. Contract Compliance — BOTH Abstract Methods
The class CANNOT be instantiated unless both are implemented. The statistical
gate's loader (`src/evaluation/loader.py`) will reject the strategy with
`TypeError: Can't instantiate abstract class ...` if either is missing.

- [ ] Class inherits from `BaseStrategy`.
- [ ] `__init__` calls `super().__init__(name, description, timeframe, lookback_hours)`
      with `timeframe` STRICTLY lowercase (`"15m"`, `"1h"`, `"4h"`, `"1d"`).
- [ ] `__init__` sets `self.MIN_CANDLES_REQUIRED` DYNAMICALLY from the indicator
      periods (e.g., `3 * max(self.fast_period, self.slow_period)`). Static
      class-level constants are a FAIL.
- [ ] `__init__` initialises any rolling/streaming state used by `step`
      (e.g., `self._observed = 0`, `self._fast_ema = None`).

### 2a. `generate_all_signals(self, df: pd.DataFrame) -> pd.Series`
- [ ] Method exists with this exact signature.
- [ ] Returns a `pd.Series` whose length AND index match the input `df`.
- [ ] Series values are STRINGS in `{"LONG", "SHORT", "FLAT", "HOLD"}` —
      NOT `SignalType` enum members.
- [ ] First `self.MIN_CANDLES_REQUIRED` rows are ALL `"FLAT"` (warmup contract).
- [ ] Implementation is fully vectorized — NO Python `for` loop over `df` rows.
      The runner enforces a 60s wall-clock limit; row loops will trip it.

### 2b. `step(self, candle: pd.Series) -> SignalType`
- [ ] Method exists with this exact signature.
- [ ] Returns a `SignalType` enum member (`SignalType.LONG`,
      `SignalType.SHORT`, `SignalType.FLAT`, `SignalType.HOLD`) —
      NOT a raw string.
- [ ] Cold-start safe: returns `SignalType.FLAT` while
      `self._observed < self.MIN_CANDLES_REQUIRED`.
- [ ] Updates internal accumulators incrementally — never recomputes
      indicators by reading prior history from `candle`.

## 3. Semantic & Trading Logic
- [ ] **No Lookahead Bias:** every operation is strictly backward-looking.
      `shift(+n)`, rolling/expanding/ewm windows, `cummax`/`cummin` allowed.
      `shift(-n)`, future-indexed slices, `np.roll` are FAILs.
- [ ] **No `np.roll`:** banned outright — it wraps the array tail to index 0,
      which silently leaks future data on bar 0. Use `pd.Series.shift(+n)`.
- [ ] **MTF data:** if the source uses `request.security`, the strategy MUST
      import and use `resample_to_interval` and `resampled_merge` from
      `src.utils.resampling`. Custom or raw `df.resample(...)` is FORBIDDEN.
- [ ] Trading logic matches the original PineScript intent (thresholds,
      crossover directions, etc.) but rewritten as vectorized Pandas/TA-Lib.
- [ ] **No fake state:** if Pine used cooldown / position-size conditions,
      the Python code must NOT fake them with indicator proxies. Remove the
      condition entirely and add a docstring note. Risk allocation is the
      RL engine's job.
- [ ] **Exit-logic disclosure:** if Pine had non-trivial exit management
      (dynamic SL, ATR TP, breakeven), the docstring MUST state that exit
      logic was not converted and is delegated to the execution layer.
      A silent omission is a FAIL.

## 4. CI/CD & Naming
- [ ] **Strategy file:** `src/strategies/{safe_name}_strategy.py` —
      missing `_strategy` suffix is a FAIL; the Orchestrator must re-invoke
      the Transpiler to rename.
- [ ] **Test file (forward-looking instruction for Test Generator):**
      `tests/strategies/test_{safe_name}_strategy.py`. The `test_` prefix AND
      `_strategy` suffix are both mandatory. The suffix-only form
      `{safe_name}_strategy_test.py` is a FAIL — pytest discovery will miss it.
      The Validator skips this check; the Test Generator enforces it.