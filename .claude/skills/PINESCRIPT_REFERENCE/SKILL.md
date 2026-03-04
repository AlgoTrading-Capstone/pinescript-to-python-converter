# Pine Script v5 to Python Translation Reference

This document is the authoritative translation dictionary for converting Pine Script v5 strategies to Python.
The target Python stack is **Pandas + TA-Lib + NumPy**. All generated strategies must conform to the
`BaseStrategy` contract defined in `src/base_strategy.py`.

**Scope:** Only logic and math constructs are mapped. All UI/visual functions (`plot`, `bgcolor`, `fill`,
`line.new`, `label.new`, `alert`, `input`) are **excluded** — we execute strategy logic, not visualize it.

---

## 1. Core Data Structures

Pine Script operates on implicit bar-by-bar series. In Python, these are columns of a Pandas DataFrame `df`.

### 1.1 Built-in Price Series

| Pine Script | Python Equivalent |
|---|---|
| `open` | `df['open']` |
| `high` | `df['high']` |
| `low` | `df['low']` |
| `close` | `df['close']` |
| `volume` | `df['volume']` |
| `time` | `df['date']` |
| `bar_index` | `df.index` (integer positional) |

### 1.2 Composite Price Sources

| Pine Script | Python Equivalent |
|---|---|
| `hl2` | `(df['high'] + df['low']) / 2` |
| `hlc3` | `(df['high'] + df['low'] + df['close']) / 3` |
| `ohlc4` | `(df['open'] + df['high'] + df['low'] + df['close']) / 4` |
| `hlcc4` | `(df['high'] + df['low'] + df['close'] + df['close']) / 4` |

### 1.3 NA Handling

| Pine Script | Python Equivalent | Notes |
|---|---|---|
| `na` | `np.nan` | Missing / unavailable value |
| `na(x)` | `pd.isna(x)` or `x.isna()` | Test if value is NA |
| `nz(x)` | `x.fillna(0)` | Replace NA with 0 (default) |
| `nz(x, y)` | `x.fillna(y)` | Replace NA with custom value `y` |
| `fixnan(x)` | `x.ffill()` | Forward-fill: replace NA with last non-NA value |

---

## 2. Historical Referencing (`[]` Operator)

Pine's `[]` operator accesses past bar values. In Python, use `.shift()`.

### 2.1 Basic Mapping

| Pine Script | Python Equivalent |
|---|---|
| `close[0]` | `df['close']` (current bar, no shift needed) |
| `close[1]` | `df['close'].shift(1)` |
| `close[n]` | `df['close'].shift(n)` |
| `ta.sma(close, 14)[1]` | Compute SMA into a column first, then `.shift(1)` on that column |
| `high - low[2]` | `df['high'] - df['low'].shift(2)` |

### 2.2 The `var` Keyword (State Preservation)

Pine's `var` declares a variable that preserves its value across bars (initialized once, then carried forward).

**Simple state (counters / accumulators) — use vectorized Pandas:**

```pine
// Pine: count green bars
var int count = 0
if close > open
    count := count + 1
```

```python
# Python: vectorized equivalent
df['count'] = (df['close'] > df['open']).astype(int).cumsum()
```

**Rolling state with shift:**

```pine
// Pine: track previous signal value
var float prevSignal = na
prevSignal := signal
```

```python
# Python equivalent
df['prev_signal'] = df['signal'].shift(1)
```

**Complex iterative state (cannot be vectorized):**

When logic depends on its own previous output in a way that cannot be expressed with `.cumsum()`, `.shift()`,
or rolling windows (e.g., adaptive trailing stops, stateful flip-flop logic), use a Python `for` loop:

```python
results = [np.nan] * len(df)
for i in range(1, len(df)):
    if some_condition(df, i):
        results[i] = compute_value(df, i, results[i - 1])
    else:
        results[i] = results[i - 1]
df['result'] = results
```

> **Rule:** Prefer vectorized operations whenever possible. Only fall back to loops when the computation
> at bar `i` depends on its own output at bar `i-1` in a non-trivial way.

---

## 3. Technical Indicators (`ta.*` Namespace)

### 3.1 TA-Lib Supported Indicators

These Pine Script indicators map directly to TA-Lib function calls.

**Required import:**
```python
import talib
```

#### Moving Averages

| Pine Script | Python (TA-Lib) |
|---|---|
| `ta.sma(src, len)` | `talib.SMA(src, timeperiod=len)` |
| `ta.ema(src, len)` | `talib.EMA(src, timeperiod=len)` |
| `ta.wma(src, len)` | `talib.WMA(src, timeperiod=len)` |
| `ta.hma(src, len)` | No direct TA-Lib call — see Section 3.2 |
| `ta.alma(src, len, offset, sigma)` | No direct TA-Lib call — see Section 3.2 |

#### Oscillators

| Pine Script | Python (TA-Lib) |
|---|---|
| `ta.rsi(src, len)` | `talib.RSI(src, timeperiod=len)` |
| `ta.cci(src, len)` | `talib.CCI(high, low, close, timeperiod=len)` |
| `ta.mfi(src, len)` | `talib.MFI(high, low, close, volume, timeperiod=len)` |
| `ta.roc(src, len)` | `talib.ROC(src, timeperiod=len)` |
| `ta.mom(src, len)` | `talib.MOM(src, timeperiod=len)` |
| `ta.cmo(src, len)` | `talib.CMO(src, timeperiod=len)` |
| `ta.willr(high, low, close, len)` | `talib.WILLR(high, low, close, timeperiod=len)` |

#### MACD (Tuple Return)

```pine
// Pine Script
[macdLine, signalLine, histLine] = ta.macd(close, 12, 26, 9)
```

```python
# Python — TA-Lib returns three arrays
macd_line, signal_line, hist_line = talib.MACD(
    df['close'], fastperiod=12, slowperiod=26, signalperiod=9
)
df['macd']        = macd_line
df['macd_signal'] = signal_line
df['macd_hist']   = hist_line
```

#### Stochastic (Tuple Return)

```pine
// Pine Script
k = ta.stoch(close, high, low, 14, 3, 3)
```

```python
# Python — TA-Lib Stochastic
slowk, slowd = talib.STOCH(
    df['high'], df['low'], df['close'],
    fastk_period=14, slowk_period=3, slowd_period=3
)
df['stoch_k'] = slowk
df['stoch_d'] = slowd
```

#### Bollinger Bands (Tuple Return)

```pine
// Pine Script
[middle, upper, lower] = ta.bb(close, 20, 2)
```

```python
# Python — TA-Lib Bollinger Bands
upper, middle, lower = talib.BBANDS(
    df['close'], timeperiod=20, nbdevup=2, nbdevdn=2
)
df['bb_upper']  = upper
df['bb_middle'] = middle
df['bb_lower']  = lower
```

#### Volatility

| Pine Script | Python (TA-Lib) |
|---|---|
| `ta.atr(len)` | `talib.ATR(df['high'], df['low'], df['close'], timeperiod=len)` |
| `ta.tr` | `talib.TRANGE(df['high'], df['low'], df['close'])` |

#### Trend

| Pine Script | Python (TA-Lib) |
|---|---|
| `ta.adx(len)` | `talib.ADX(df['high'], df['low'], df['close'], timeperiod=len)` |
| `ta.sar(start, inc, max)` | `talib.SAR(df['high'], df['low'], acceleration=start, maximum=max)` |
| `ta.linreg(src, len, offset)` | `talib.LINEARREG(src, timeperiod=len)` (offset applied separately via `.shift()`) |

#### Volume

| Pine Script | Python (TA-Lib) |
|---|---|
| `ta.obv` | `talib.OBV(df['close'], df['volume'])` |
| `ta.ad` | `talib.AD(df['high'], df['low'], df['close'], df['volume'])` |

#### Statistical / Range

| Pine Script | Python (TA-Lib or Pandas) |
|---|---|
| `ta.highest(src, len)` | `src.rolling(len).max()` |
| `ta.lowest(src, len)` | `src.rolling(len).min()` |
| `ta.highestbars(src, len)` | `src.rolling(len).apply(lambda x: len(x) - 1 - x.argmax(), raw=True).astype(int)` |
| `ta.lowestbars(src, len)` | `src.rolling(len).apply(lambda x: len(x) - 1 - x.argmin(), raw=True).astype(int)` |
| `ta.stdev(src, len)` | `src.rolling(len).std()` |
| `ta.variance(src, len)` | `src.rolling(len).var()` |
| `ta.percentrank(src, len)` | `src.rolling(len).apply(lambda x: stats.percentileofscore(x, x.iloc[-1]), raw=False)` |
| `ta.correlation(s1, s2, len)` | `s1.rolling(len).corr(s2)` |
| `ta.dev(src, len)` | `src.rolling(len).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)` |
| `ta.median(src, len)` | `src.rolling(len).median()` |
| `ta.mode(src, len)` | `src.rolling(len).apply(lambda x: pd.Series(x).mode().iloc[0], raw=False)` |

#### Cross Detection

| Pine Script | Python Equivalent (Vectorized) |
|---|---|
| `ta.crossover(a, b)` | `(a > b) & (a.shift(1) <= b.shift(1))` |
| `ta.crossunder(a, b)` | `(a < b) & (a.shift(1) >= b.shift(1))` |
| `ta.cross(a, b)` | `((a > b) & (a.shift(1) <= b.shift(1))) \| ((a < b) & (a.shift(1) >= b.shift(1)))` |

#### Change / Trend Detection

| Pine Script | Python Equivalent |
|---|---|
| `ta.change(src)` | `src.diff()` |
| `ta.change(src, len)` | `src.diff(len)` |
| `ta.rising(src, len)` | All of the last `len` bars rising: see note below |
| `ta.falling(src, len)` | All of the last `len` bars falling: see note below |

`ta.rising(close, 3)` means `close[0] > close[1] and close[1] > close[2] and close[2] > close[3]`:

```python
def rising(series, length):
    result = pd.Series(True, index=series.index)
    for i in range(length):
        result = result & (series.shift(i) > series.shift(i + 1))
    return result
```

#### Misc

| Pine Script | Python Equivalent |
|---|---|
| `ta.cum(src)` | `src.cumsum()` |
| `ta.barssince(cond)` | See helper below |
| `ta.valuewhen(cond, src, n)` | See helper below |

**`ta.barssince` helper:**
```python
def barssince(condition: pd.Series) -> pd.Series:
    groups = (~condition).cumsum()
    return condition.groupby(groups).cumcount()
```

**`ta.valuewhen` helper:**
```python
def valuewhen(condition: pd.Series, source: pd.Series, occurrence: int = 0) -> pd.Series:
    masked = source.where(condition)
    filled = masked.ffill()
    if occurrence > 0:
        for _ in range(occurrence):
            masked = masked.where(masked != filled).ffill()
            filled = masked
    return filled
```

#### Pivot Points

| Pine Script | Python Equivalent |
|---|---|
| `ta.pivothigh(src, leftbars, rightbars)` | Requires lookahead — see note |
| `ta.pivotlow(src, leftbars, rightbars)` | Requires lookahead — see note |

> **Warning:** `ta.pivothigh` and `ta.pivotlow` inherently require future bars (`rightbars`) to confirm a
> pivot. In a live strategy context, the pivot is only confirmed `rightbars` bars after the actual high/low.
> Shift the result by `rightbars` to avoid lookahead bias:
> `df['pivot_high'] = df['high'].shift(rightbars).where(is_pivot_condition)`

### 3.2 Manual Implementation Required (No TA-Lib Equivalent)

The following Pine indicators have **NO TA-Lib function**. Use the Pandas/NumPy implementations below.

#### RMA (Running Moving Average / Wilder's Smoothing)

```pine
// Pine Script
rmaVal = ta.rma(close, 14)
```

```python
# Python — RMA is EWM with alpha = 1/length
df['rma'] = df['close'].ewm(alpha=1/14, adjust=False).mean()
```

#### VWMA (Volume Weighted Moving Average)

```pine
// Pine Script
vwmaVal = ta.vwma(close, 20)
```

```python
# Python — VWMA manual calculation
df['vwma'] = (df['close'] * df['volume']).rolling(20).sum() / df['volume'].rolling(20).sum()
```

#### VWAP (Volume Weighted Average Price)

```pine
// Pine Script
vwapVal = ta.vwap
```

```python
# Python — cumulative VWAP (resets are session-dependent; this is the basic form)
typical_price = (df['high'] + df['low'] + df['close']) / 3
df['vwap'] = (df['volume'] * typical_price).cumsum() / df['volume'].cumsum()
```

#### HMA (Hull Moving Average)

```pine
// Pine Script
hmaVal = ta.hma(close, 9)
```

```python
# Python — HMA = WMA(2 * WMA(n/2) - WMA(n), sqrt(n))
import math
half_len = length // 2
sqrt_len = int(math.sqrt(length))
wma_half = talib.WMA(src, timeperiod=half_len)
wma_full = talib.WMA(src, timeperiod=length)
df['hma'] = talib.WMA(2 * wma_half - wma_full, timeperiod=sqrt_len)
```

#### SWMA (Symmetrically Weighted Moving Average)

```pine
// Pine Script
swmaVal = ta.swma(close)
```

```python
# Python — SWMA is a fixed 4-bar weighted average with weights [1/6, 2/6, 2/6, 1/6]
df['swma'] = (
    df['close'].shift(3) * (1/6) +
    df['close'].shift(2) * (2/6) +
    df['close'].shift(1) * (2/6) +
    df['close']          * (1/6)
)
```

#### Supertrend

```pine
// Pine Script
[supertrend, direction] = ta.supertrend(factor, atrPeriod)
```

```python
# Python — Supertrend helper function
def compute_supertrend(df: pd.DataFrame, factor: float, atr_period: int):
    hl2 = (df['high'] + df['low']) / 2
    atr = talib.ATR(df['high'].values, df['low'].values, df['close'].values, timeperiod=atr_period)
    atr = pd.Series(atr, index=df.index)

    upper_band = hl2 + factor * atr
    lower_band = hl2 - factor * atr

    supertrend = pd.Series(np.nan, index=df.index)
    direction  = pd.Series(1, index=df.index)  # 1 = up (bullish), -1 = down (bearish)

    for i in range(1, len(df)):
        if pd.isna(atr.iloc[i]):
            continue

        prev_upper = upper_band.iloc[i - 1] if not pd.isna(supertrend.iloc[i - 1]) else upper_band.iloc[i]
        prev_lower = lower_band.iloc[i - 1] if not pd.isna(supertrend.iloc[i - 1]) else lower_band.iloc[i]

        idx = df.index[i]
        prev_idx = df.index[i - 1]

        if lower_band.iloc[i] > prev_lower or df['close'].iloc[i - 1] < prev_lower:
            lower_band.at[idx] = lower_band.iloc[i]
        else:
            lower_band.at[idx] = prev_lower

        if upper_band.iloc[i] < prev_upper or df['close'].iloc[i - 1] > prev_upper:
            upper_band.at[idx] = upper_band.iloc[i]
        else:
            upper_band.at[idx] = prev_upper

        if direction.at[prev_idx] == 1:  # was bullish
            if df['close'].iloc[i] < lower_band.at[idx]:
                direction.at[idx] = -1
                supertrend.at[idx] = upper_band.at[idx]
            else:
                direction.at[idx] = 1
                supertrend.at[idx] = lower_band.at[idx]
        else:  # was bearish
            if df['close'].iloc[i] > upper_band.at[idx]:
                direction.at[idx] = 1
                supertrend.at[idx] = lower_band.at[idx]
            else:
                direction.at[idx] = -1
                supertrend.at[idx] = upper_band.at[idx]

    return supertrend, direction
```

#### DMI / DI+, DI-

```pine
// Pine Script
[diplus, diminus, adxVal] = ta.dmi(len, adxSmoothing)
```

```python
# Python — TA-Lib provides these separately
df['plus_di']  = talib.PLUS_DI(df['high'], df['low'], df['close'], timeperiod=length)
df['minus_di'] = talib.MINUS_DI(df['high'], df['low'], df['close'], timeperiod=length)
df['adx']      = talib.ADX(df['high'], df['low'], df['close'], timeperiod=length)
```

#### Keltner Channels

```pine
// Pine Script
[kcMiddle, kcUpper, kcLower] = ta.kc(close, length, mult)
```

```python
# Python — Keltner Channels: EMA +/- mult * ATR
df['kc_middle'] = talib.EMA(df['close'], timeperiod=length)
atr = talib.ATR(df['high'], df['low'], df['close'], timeperiod=length)
df['kc_upper'] = df['kc_middle'] + mult * atr
df['kc_lower'] = df['kc_middle'] - mult * atr
```

#### TSI (True Strength Index)

```pine
// Pine Script
tsiVal = ta.tsi(close, 25, 13)
```

```python
# Python — TSI: double-smoothed momentum
diff = df['close'].diff()
smooth1 = diff.ewm(span=long_len, adjust=False).mean()
smooth2 = smooth1.ewm(span=short_len, adjust=False).mean()
abs_smooth1 = diff.abs().ewm(span=long_len, adjust=False).mean()
abs_smooth2 = abs_smooth1.ewm(span=short_len, adjust=False).mean()
df['tsi'] = 100 * (smooth2 / abs_smooth2)
```

---

## 4. Math & Helper Functions (`math.*` Namespace)

### 4.1 Constants

| Pine Script | Python Equivalent |
|---|---|
| `math.pi` | `np.pi` |
| `math.e` | `np.e` |
| `math.phi` | `1.6180339887498948` |
| `math.rphi` | `0.6180339887498948` |

### 4.2 Basic Math

| Pine Script | Python Equivalent | Notes |
|---|---|---|
| `math.abs(x)` | `np.abs(x)` | Works on Series and scalars |
| `math.sign(x)` | `np.sign(x)` | |
| `math.max(a, b)` | `np.maximum(a, b)` | Element-wise for Series |
| `math.min(a, b)` | `np.minimum(a, b)` | Element-wise for Series |
| `math.avg(a, b, ...)` | `np.mean([a, b, ...], axis=0)` | Average of multiple values |
| `math.pow(base, exp)` | `np.power(base, exp)` | |
| `math.sqrt(x)` | `np.sqrt(x)` | |
| `math.exp(x)` | `np.exp(x)` | |
| `math.log(x)` | `np.log(x)` | Natural logarithm |
| `math.log10(x)` | `np.log10(x)` | Base-10 logarithm |

### 4.3 Rounding

| Pine Script | Python Equivalent |
|---|---|
| `math.round(x)` | `np.round(x)` |
| `math.round(x, precision)` | `np.round(x, precision)` |
| `math.ceil(x)` | `np.ceil(x)` |
| `math.floor(x)` | `np.floor(x)` |

### 4.4 Trigonometry

| Pine Script | Python Equivalent |
|---|---|
| `math.sin(x)` | `np.sin(x)` |
| `math.cos(x)` | `np.cos(x)` |
| `math.tan(x)` | `np.tan(x)` |
| `math.asin(x)` | `np.arcsin(x)` |
| `math.acos(x)` | `np.arccos(x)` |
| `math.atan(x)` | `np.arctan(x)` |
| `math.todegrees(x)` | `np.degrees(x)` |
| `math.toradians(x)` | `np.radians(x)` |

### 4.5 Series Aggregation

These Pine functions operate over a rolling window of a series — they are NOT simple scalar math.

| Pine Script | Python Equivalent |
|---|---|
| `math.sum(src, len)` | `src.rolling(len).sum()` |
| `ta.cum(src)` | `src.cumsum()` |
| `math.random(min, max)` | `np.random.uniform(min, max)` |

---

## 5. Strategy Execution (`strategy.*` Namespace)

### 5.1 Core Mapping

In our system, Pine strategy commands translate to returning a `StrategyRecommendation` from the `run()` method.

**Required imports:**
```python
from datetime import datetime
import pandas as pd
from src.base_strategy import BaseStrategy, StrategyRecommendation, SignalType
```

| Pine Script | Python Equivalent |
|---|---|
| `strategy.entry("Long", strategy.long)` | `return StrategyRecommendation(SignalType.LONG, timestamp)` |
| `strategy.entry("Short", strategy.short)` | `return StrategyRecommendation(SignalType.SHORT, timestamp)` |
| `strategy.close("Long")` | `return StrategyRecommendation(SignalType.FLAT, timestamp)` |
| `strategy.close("Short")` | `return StrategyRecommendation(SignalType.FLAT, timestamp)` |
| `strategy.close_all()` | `return StrategyRecommendation(SignalType.FLAT, timestamp)` |
| No entry/exit condition met | `return StrategyRecommendation(SignalType.HOLD, timestamp)` |

### 5.2 Signal Priority

When a Pine strategy has both entry and exit conditions on the same bar, follow this priority:

1. If an **exit** condition is met, return `SignalType.FLAT`.
2. If a **long entry** condition is met, return `SignalType.LONG`.
3. If a **short entry** condition is met, return `SignalType.SHORT`.
4. Otherwise, return `SignalType.HOLD`.

### 5.3 Items to Ignore

The following Pine strategy constructs are handled by the external execution engine, **NOT** by the strategy class:

| Pine Script | Action |
|---|---|
| `strategy.exit()` with `stop`, `limit`, `trail_*` | **Ignore.** Stop-loss and take-profit are managed externally. |
| `strategy.position_size` | **Ignore.** Position sizing is managed externally. |
| `strategy.equity` | **Ignore.** |
| `strategy.risk.*` | **Ignore.** Risk management is external. |
| `strategy.order()` | Convert to `strategy.entry()` logic. |
| `strategy()` declaration params (pyramiding, default_qty, etc.) | Note in strategy description but do not implement. |

### 5.4 Multi-Timeframe Data (`request.security`)

**CRITICAL RULE:** NEVER use `df.resample()` directly inside a strategy class.
Always use the project's utility functions to prevent lookahead bias.

```pine
// Pine Script
htfClose = request.security(syminfo.tickerid, "240", close)
htfSma   = request.security(syminfo.tickerid, "240", ta.sma(close, 14))
```

```python
# Python — MANDATORY pattern
from src.utils.resampling import resample_to_interval, resampled_merge

# Step 1: Resample base df to higher timeframe
resampled_df = resample_to_interval(df, "4h")

# Step 2: Compute indicators on the resampled dataframe
resampled_df['sma_14'] = talib.SMA(resampled_df['close'].values, timeperiod=14)

# Step 3: Merge back without lookahead bias
df = resampled_merge(original=df, resampled=resampled_df, fill_na=True)

# Access via prefixed columns: df['resample_240_close'], df['resample_240_sma_14']
```

**Timeframe string mapping:**

| Pine `request.security` timeframe | `resample_to_interval` argument |
|---|---|
| `"5"` | `"5m"` |
| `"15"` | `"15m"` |
| `"60"` | `"1h"` |
| `"240"` | `"4h"` |
| `"D"` or `"1D"` | `"1d"` |
| `"W"` or `"1W"` | `"1w"` |

### 5.5 Full Strategy Template

```python
from datetime import datetime
import numpy as np
import pandas as pd
import talib
from src.base_strategy import BaseStrategy, StrategyRecommendation, SignalType


class MyConvertedStrategy(BaseStrategy):

    def __init__(self):
        super().__init__(
            name="MyConvertedStrategy",
            description="Converted from Pine Script: ...",
            timeframe="15m",
            lookback_hours=48,
        )

    def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
        # --- Indicator calculations ---
        df['sma_fast'] = talib.SMA(df['close'].values, timeperiod=10)
        df['sma_slow'] = talib.SMA(df['close'].values, timeperiod=30)

        # --- Get the last complete bar ---
        last = df.iloc[-1]
        prev = df.iloc[-2]

        # --- Entry / exit conditions ---
        long_entry  = last['sma_fast'] > last['sma_slow'] and prev['sma_fast'] <= prev['sma_slow']
        short_entry = last['sma_fast'] < last['sma_slow'] and prev['sma_fast'] >= prev['sma_slow']

        if long_entry:
            return StrategyRecommendation(SignalType.LONG, timestamp)
        elif short_entry:
            return StrategyRecommendation(SignalType.SHORT, timestamp)

        return StrategyRecommendation(SignalType.HOLD, timestamp)
```

---

## 6. Control Flow

### 6.1 Conditional Assignments

**Pine ternary on series -> `np.where`:**

```pine
// Pine Script
color = close > open ? color.green : color.red
value = condition ? high : low
```

```python
# Python — vectorized
df['value'] = np.where(df['close'] > df['open'], df['high'], df['low'])
```

**Pine `if/else` on series -> `np.where` or `np.select`:**

```pine
// Pine Script
signal = 0
if rsi > 70
    signal := -1
else if rsi < 30
    signal := 1
```

```python
# Python — vectorized with np.select for multiple conditions
conditions = [
    df['rsi'] > 70,
    df['rsi'] < 30,
]
choices = [-1, 1]
df['signal'] = np.select(conditions, choices, default=0)
```

### 6.2 Scalar Conditionals (Last Bar Only)

When conditions apply only to the latest bar (inside `run()` for signal generation), standard Python `if/elif/else` is correct:

```python
last = df.iloc[-1]
if last['rsi'] > 70:
    return StrategyRecommendation(SignalType.SHORT, timestamp)
elif last['rsi'] < 30:
    return StrategyRecommendation(SignalType.LONG, timestamp)
return StrategyRecommendation(SignalType.HOLD, timestamp)
```

### 6.3 Loops

**Avoid Pine-style `for` loops when possible.** Most loops over bars can be replaced with vectorized Pandas operations:

| Pine Pattern | Python Replacement |
|---|---|
| `for i = 0 to len - 1: sum += close[i]` | `df['close'].rolling(len).sum()` |
| `for i = 0 to len - 1: if high[i] > max: max = high[i]` | `df['high'].rolling(len).max()` |
| Loop counting condition occurrences | `condition.rolling(len).sum()` |

When a loop is truly necessary (complex iterative logic), use a standard Python `for` loop over `range(len(df))`.

### 6.4 `switch` Statement

```pine
// Pine Script
result = switch
    condition1 => value1
    condition2 => value2
    => defaultValue
```

```python
# Python — if/elif/else
if condition1:
    result = value1
elif condition2:
    result = value2
else:
    result = default_value

# Or vectorized with np.select:
df['result'] = np.select([condition1, condition2], [value1, value2], default=default_value)
```

---

## Quick Reference Card

| Concept | Pine Script | Python |
|---|---|---|
| Current close | `close` | `df['close']` |
| Previous close | `close[1]` | `df['close'].shift(1)` |
| Missing value | `na` | `np.nan` |
| Replace NA | `nz(x)` | `x.fillna(0)` |
| Forward fill | `fixnan(x)` | `x.ffill()` |
| SMA | `ta.sma(close, 14)` | `talib.SMA(df['close'], 14)` |
| EMA | `ta.ema(close, 14)` | `talib.EMA(df['close'], 14)` |
| RSI | `ta.rsi(close, 14)` | `talib.RSI(df['close'], 14)` |
| RMA | `ta.rma(close, 14)` | `df['close'].ewm(alpha=1/14, adjust=False).mean()` |
| ATR | `ta.atr(14)` | `talib.ATR(high, low, close, 14)` |
| Crossover | `ta.crossover(a, b)` | `(a > b) & (a.shift(1) <= b.shift(1))` |
| Go Long | `strategy.entry("L", strategy.long)` | `StrategyRecommendation(SignalType.LONG, ts)` |
| Go Short | `strategy.entry("S", strategy.short)` | `StrategyRecommendation(SignalType.SHORT, ts)` |
| Close position | `strategy.close_all()` | `StrategyRecommendation(SignalType.FLAT, ts)` |
| Higher TF data | `request.security(sym, "240", close)` | `resample_to_interval(df, "4h")` + `resampled_merge(...)` |
| Conditional (series) | `x > y ? a : b` | `np.where(x > y, a, b)` |
