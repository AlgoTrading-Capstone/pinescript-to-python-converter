# Transpiler Agent — Rudy Breakout Momentum v2

## Files Written
- `src/strategies/rudy_breakout_momentum_v2.py`

## Indicator Mapping
| Pine Script | Python |
|---|---|
| `ta.highest(high, 126)` | `df['high'].rolling(126).max()` |
| `ta.ema(close, 21)` | `talib.EMA(close.values, timeperiod=21)` |
| `ta.ema(close, 50)` | `talib.EMA(close.values, timeperiod=50)` |
| `ta.rsi(close, 14)` | `talib.RSI(close.values, timeperiod=14)` |
| `high126[1]` | `.rolling(126).max().shift(1)` |
| `close[1]` | `close.iloc[idx - 1]` |

## Key Decisions
- `high126[1]` → `rolling(126).max().shift(1)`: shift(1) ensures current bar's high is excluded from the rolling window when evaluating the breakout condition (anti-lookahead).
- Signal priority: FLAT (trendBroken) evaluated before LONG (breakoutBuy).
- No position tracking in BaseStrategy; exit signal is FLAT.
- MIN_BARS = 160 (covers 126 rolling + EMA50 warmup + RSI warmup + buffer).
- Profit target and stop-loss parameters from Pine are NOT implemented — delegated to execution layer.
