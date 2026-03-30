---
name: validation-checklist
description: The ultimate gatekeeper checklist for the Validator Agent. Automatically load this skill WHENEVER reviewing, validating, or approving a generated Python strategy. Enforces strict CI/CD constraints (file naming), RL safety (dynamic MIN_CANDLES_REQUIRED, no np.roll, strictly causal vectorization), and BaseStrategy contract compliance before allowing the code to proceed to the Test Generation phase.
---

# Validation Checklist

The Validator Agent MUST verify the generated Python strategy against this exact checklist. If ANY check fails, the validation process is considered FAILED.

## 1. Syntax & Imports Validation
- [ ] Code is valid Python 3.11+.
- [ ] No syntax errors or unresolved references.
- [ ] All necessary imports are present (e.g., `pandas`, `datetime`, `talib`).
- [ ] **Type Safety:** If `talib` moving averages are used, `from talib import MA_Type` MUST be imported and utilized (e.g., `matype=MA_Type.SMA`), never raw integers like `0`.

## 2. Contract Compliance
- [ ] The class inherits correctly from `BaseStrategy`.
- [ ] The `__init__` method explicitly calls `super().__init__(...)` with `name`, `description`, `timeframe` (STRICTLY lowercase, e.g., "15m"), and `lookback_hours`.
- [ ] **Dynamic RL Warmup:** The `__init__` method MUST dynamically compute `self.MIN_CANDLES_REQUIRED` based on the strategy's parameters (e.g., `3 * max(param1, param2)`). Static class-level constants are a FAIL.
- [ ] The `run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation` method is implemented with the exact signature.
- [ ] The `run` method guards execution with: `if len(df) < self.MIN_CANDLES_REQUIRED:` returning a `HOLD` signal if true.
- [ ] The return value of `run` is strictly a `StrategyRecommendation` object using a valid `SignalType` enum (`LONG`, `SHORT`, `FLAT`, or `HOLD`).

## 3. Semantic & Trading Logic Validation
- [ ] **No Lookahead Bias:** The strategy must NEVER access future data. When processing the current row `i` or timestamp `t`, the strategy must only use data from index `<= i`. Shifts or rolling windows must be strictly backward-looking.
- [ ] Logic precisely matches the original PineScript intent (e.g., condition thresholds, crossover directions) but uses **vectorized Operations** (Pandas/TA-Lib) instead of iterative loops wherever mathematically possible.
- [ ] **Multi-Timeframe Handling:** If the strategy fetches data from a higher timeframe (like `request.security` in PineScript), it MUST import and use `resample_to_interval` and `resampled_merge` from `src.utils.resampling`. Custom or manual pandas resampling logic (`df.resample()`) inside the strategy class is strictly FORBIDDEN to prevent lookahead bias.
- [ ] **No Fake State:** If the original Pine Script used cooldown or position-size-based conditions, the Python code must NOT use indicator proxies (e.g., "crossover N bars ago") to simulate them. The condition must be REMOVED entirely, with a docstring note. Our RL engine handles dynamic risk allocation.
- [ ] **Exit logic disclosure:** If the original Pine Script had significant exit management (dynamic SL, ATR TP, breakeven stops), the Python docstring MUST explicitly state that exit logic was not converted and must be configured in the execution layer. A silent omission without a docstring warning is a FAIL.
- [ ] **No `np.roll` shifts:** The strategy must NOT use `np.roll()` to shift time-series data. Use `pd.Series.diff()` or `pd.Series.shift()` instead. `np.roll` wraps the last element to index 0, causing a silent lookahead artifact on bar 0.
- [ ] **CI/CD File Naming (Strategy):** The strategy source file MUST be named `{safe_name}_strategy.py` (e.g., `rsi_divergence_strategy.py`). A generic name or a missing `_strategy` suffix is a FAIL — reject and ask the Orchestrator to re-invoke the Transpiler to rename the file.
- [ ] **CI/CD File Naming (Test) — PROSPECTIVE INSTRUCTION FOR TEST GENERATOR:** The Validator cannot check the test file at this stage because it does not exist yet. This check is a forward-looking instruction: when the Test Generator Agent creates the file, it MUST be named `test_{safe_name}_strategy.py` (e.g., `test_rsi_divergence_strategy.py`). The `test_` prefix AND `_strategy` suffix are both mandatory. The suffix-only format `{safe_name}_strategy_test.py` is a FAIL — pytest discovery will miss it. **Validator: skip this check; Test Generator: enforce it.**