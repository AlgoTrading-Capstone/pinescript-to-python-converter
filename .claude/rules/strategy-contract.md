# Strategy Code Contract & Anti-Lookahead Rules

This rule applies ANY TIME you are writing, editing, or reviewing a Python trading strategy.

## 1. Class Structure & Naming
- **File Naming:** MUST be `src/strategies/<safe_name>_strategy.py`.
- **Inheritance:** MUST inherit from `BaseStrategy`.
- **Dynamic Warmup (RL Constraint):** `self.MIN_CANDLES_REQUIRED` MUST be computed dynamically in `__init__` based on max indicator lengths (e.g., `3 * max(p1, p2)`). Static limits crash the RL hyperparameter tuning.

## 2. Anti-Lookahead Bias (CRITICAL)
- **Forbidden:** `np.roll()` is STRICTLY BANNED. It wraps arrays and leaks future data. Use `pd.Series.shift()` (positive integers only).
- **Multi-Timeframe (MTF):** You MUST NOT use `df.resample()` directly. You MUST use:
  ```python
  from src.utils.resampling import resample_to_interval, resampled_merge
  ```
All timeframe strings must be strictly lowercase (e.g., "15m", "4h").

## 3. Allowed Libraries & Type Safety
Allowed: pandas, numpy, talib, and src.*. No other third-party libs.

TA-Lib Type Safety: NEVER use raw integers for moving average types. ALWAYS import and use from talib import MA_Type.

Missing TA-Lib Indicators: Implement in pure Pandas.

RMA: df.ewm(alpha=1/length, adjust=False).mean()

4. Run Method Signature
```Python
def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
    # Must guard execution:
    if len(df) < self.MIN_CANDLES_REQUIRED:
        return StrategyRecommendation(signal=SignalType.HOLD, timestamp=timestamp)
    # ... logic ...
```