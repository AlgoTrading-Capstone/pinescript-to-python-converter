# Validation Checklist

The Validator Agent MUST verify the generated Python strategy against this exact checklist. If ANY check fails, the validation process is considered FAILED.

## 1. Syntax & Imports Validation
- [ ] Code is valid Python 3.11.
- [ ] No syntax errors or unresolved references.
- [ ] All necessary imports are present (e.g., `pandas`, `datetime`).
- [ ] Third-party technical indicator libraries (e.g., `ta-lib`) are imported correctly if used.

## 2. Contract Compliance
- [ ] The class inherits correctly from `BaseStrategy`.
- [ ] The `__init__` method explicitly calls `super().__init__(...)` with `name`, `description`, `timeframe`, and `lookback_hours`.
- [ ] The `run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation` method is implemented with the exact signature.
- [ ] The return value of `run` is strictly a `StrategyRecommendation` object using a valid `SignalType` enum.

## 3. Semantic & Trading Logic Validation
- [ ] **No Lookahead Bias:** The strategy must NEVER access future data. When processing the current row `i` or timestamp `t`, the strategy must only use data from index `<= i`. Shifts or rolling windows must be strictly backward-looking.
- [ ] Logic precisely matches the original PineScript intent (e.g., condition thresholds, crossover directions).
- [ ] **Multi-Timeframe Handling:** If the strategy fetches data from a higher timeframe (like `request.security` in PineScript), it MUST import and use `resample_to_interval` and `resampled_merge` from `src.utils.resample`. Custom or manual pandas resampling logic (`df.resample()`) inside the strategy class is strictly FORBIDDEN to prevent lookahead bias.
- [ ] **No Fake State:** If the original Pine Script used cooldown or position-size-based conditions, the Python code must NOT use indicator proxies (e.g., "crossover N bars ago") to simulate them. The condition must be REMOVED entirely, with a docstring note.
- [ ] **Exit logic disclosure:** If the original Pine Script had significant exit management (dynamic SL, ATR TP, breakeven stops), the Python docstring MUST explicitly state that exit logic was not converted and must be configured in the execution layer. A silent omission without a docstring warning is a FAIL.
- [ ] **Indicator warmup:** The `min_bars` guard in `run()` must use `3 * max_indicator_period` for any recursive indicator (EMA, ATR, RSI, MACD, Bollinger, etc.). Using `max_period + 1` is a FAIL — it causes unstable indicator values and false signals.