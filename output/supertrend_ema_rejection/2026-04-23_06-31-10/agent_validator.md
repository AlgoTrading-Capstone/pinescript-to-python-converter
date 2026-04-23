## Validator Decision Log

### Strategy: Supertrend + EMA Rejection

**Target file:** `src/strategies/supertrend_ema_rejection_strategy.py`
**Source Pine:** `input/Supertrend-EMA-Rejection-Strategy.pine`
**Class:** `SupertrendEmaRejectionStrategy`
**Result:** PASS

### Checks performed

| Check | Result | Notes |
|---|---|---|
| 1. File naming / location (`_strategy.py` suffix) | PASS | File is `src/strategies/supertrend_ema_rejection_strategy.py`. |
| 2. Inherits from `BaseStrategy` | PASS | `from src.base_strategy import BaseStrategy, SignalType` + `class SupertrendEmaRejectionStrategy(BaseStrategy)`. |
| 3. Both abstract methods implemented | PASS | `generate_all_signals(self, df)` and `step(self, candle)` both present. Instantiation succeeded: `SupertrendEmaRejectionStrategy()` returned an object with `MIN_CANDLES_REQUIRED=600`, `timeframe='15m'`. No `TypeError`. |
| 4. Dynamic `MIN_CANDLES_REQUIRED` | PASS | Computed inside `__init__` as `3 * max(self.ema_length, self.atr_len1, self.atr_len2, self.atr_len3, self.atr_sl_length)` → 600 with defaults. Not a class-level constant. |
| 5. No `np.roll` | PASS | Grep returned 0 hits. |
| 6. No `df.resample()` | PASS | Grep returned 0 hits. MTF is explicitly dropped (Pine ships with only `useCur=true`). |
| 7. Lowercase timeframe string | PASS | `timeframe="15m"`. |
| 8. Allowed libs only | PASS | Imports are limited to `collections`, `typing`, `__future__`, `numpy`, `pandas`, `talib`, and `src.base_strategy`. No disallowed third-party libs. |
| 9. TA-Lib type safety | PASS | Only `talib.EMA` and `talib.ATR` are used — neither takes MA type args. No raw int `matype`/`slowk_matype`/etc. `MA_Type` not needed. |
| 10. Vectorized `generate_all_signals` correctness | PASS | Returns `pd.Series` with `df.index`, length matches `len(df)`. Values are strings in `{LONG, SHORT, FLAT}` (subset of the allowed set). Warmup (rows 0..599) is forced to `"FLAT"` via `signals.iloc[:MIN_CANDLES_REQUIRED] = "FLAT"`. Empty DF returns empty Series (verified: `len(out) == 0`). No `shift(-n)`, no `np.roll`. The only loop is the documented O(n) numpy pass inside `_supertrend`, which the rules explicitly permit for recursive indicators. Runtime ~7 ms on 1100 bars (well under 60 s). |
| 11. `step` correctness | PASS | Verified: 600 consecutive `SignalType.FLAT` returns on a cold start (matches `MIN_CANDLES_REQUIRED`). Uses bounded `collections.deque(maxlen=MIN_CANDLES_REQUIRED+20)` per OHLCV channel — not unbounded history. Returns `SignalType` enum (LONG/SHORT/FLAT). |
| 12. Trading-logic fidelity | PASS | Long condition: `valid & is_up & (low <= st) & (close > st) & (dir == -1)` OR'd across the three enabled Supertrends — matches Pine `b1/b2/b3`. Short mirror matches `s1/s2/s3`. `is_up = close > ema` and `is_dn = close < ema` when filter on; both `True` when filter off — matches Pine `not useEmaFilter or close > ema`. `use_st1/2/3` toggles respected. When both long and short fire, output is FLAT (conservative). |
| 13. No fake state | PASS | No emulation of `strategy.position_size`, no cooldown via indicators. Pine's `activeBuySetup/activeSellSetup`, `entryWindow`, `strategy.cancel/exit/close` are all dropped, not proxied. |
| 14. Docstring disclosure of dropped exit logic | PASS | Module docstring explicitly lists the drops: `strategy.entry(stop=...)` pending orders, `entryWindow` cancel window, `activeSL` ATR-buffered stop, BE bump at `be_trigger_rr`, trailing EMA(21) at `trail_start_rr`, pivot-based TP (`recentPh/recentPl`), `initialRisk * 1.5` fallback target, MACD bear/bull early exit, and `strategy.position_size`-gated conditions. |

### Issues found
None.

### Evidence (commands run + output snippets)

1. `grep np.roll / .resample( / shift(-` on the file → all 0 hits.

2. Instantiation smoke test:
   ```
   $ .venv/Scripts/python.exe -c "from src.strategies.supertrend_ema_rejection_strategy import SupertrendEmaRejectionStrategy; s = SupertrendEmaRejectionStrategy(); print('OK', s.MIN_CANDLES_REQUIRED, s.timeframe)"
   OK 600 15m
   ```

3. Empty DataFrame handling:
   ```
   $ .venv/Scripts/python.exe -c "import pandas as pd; from src.strategies.supertrend_ema_rejection_strategy import SupertrendEmaRejectionStrategy; s = SupertrendEmaRejectionStrategy(); out = s.generate_all_signals(pd.DataFrame(columns=['open','high','low','close','volume'])); print('empty ok', len(out))"
   empty ok 0
   ```

4. 1000-bar synthetic smoke test (batch + streaming):
   ```
   length match: True
   index match: True
   values subset: True            # {LONG, FLAT, SHORT} ⊆ {LONG, SHORT, FLAT, HOLD}
   warmup all FLAT: True
   unique: {'LONG', 'FLAT', 'SHORT'}
   active bars past warmup: 23
   step warmup FLAT count (must be 600 ): 600
   ```
   Confirms: length/index alignment, allowed-values contract, warmup contract for BOTH batch and streaming modes, strategy is alive on real movement, and `step` correctly emits FLAT for exactly `MIN_CANDLES_REQUIRED` cold-start ticks.

### Verdict
PASS

VALIDATION_PASS
