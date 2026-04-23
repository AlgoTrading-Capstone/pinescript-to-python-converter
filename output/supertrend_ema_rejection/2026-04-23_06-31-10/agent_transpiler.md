## Transpiler Decision Log

### Strategy: SupertrendEmaRejectionStrategy

**Source:** `input/Supertrend-EMA-Rejection-Strategy.pine`
**Target:** `src/strategies/supertrend_ema_rejection_strategy.py`
**safe_name:** `supertrend_ema_rejection`
**Timeframe:** `15m` (lowercase, per RL contract)
**MIN_CANDLES_REQUIRED:** `3 * max(ema_length, atr_len1, atr_len2, atr_len3, atr_sl_length)` — defaults to `600`.

The Pine script is an entry-signal scanner wrapped in a rich position-management
layer. We kept the entry logic (3-Supertrend rejection + EMA-200 trend filter)
and dropped all Pine-side position/exit machinery, because the
`StrategyRecommendation` schema carries only a signal direction. The RL engine
owns position sizing, stops, and trailing.

### Mappings applied

| PineScript | Python | Notes |
|---|---|---|
| `ta.ema(close, emaLen)` | `talib.EMA(close, timeperiod=ema_length)` | Trend filter. |
| `ta.supertrend(mult, atrLen)` | Private `_supertrend(high, low, close, atr_len, mult)` helper. | TA-Lib has no Supertrend. Uses `talib.ATR` + the standard Pine-compatible "final band" recurrence in a single O(n) numpy loop. Returns `(st, dir)` where `dir == -1` means bullish and `dir == +1` means bearish — matches Pine. |
| `hl2 = (high + low) / 2` | `(high + low) / 2.0` | Inside `_supertrend`. |
| `ta.atr(atrLen)` | `talib.ATR(high, low, close, timeperiod=atr_len)` | Inside `_supertrend`. |
| `isUp = not useEmaFilter or close > ema` | `is_up = np.ones(n,bool) if not use_ema_filter else (close > ema)` | Vectorized. |
| `isDn = not useEmaFilter or close < ema` | Mirror of the above. | |
| `b1 = useST1 and isUp and low<=st1 and close>st1 and dir1==-1` | `_long_cond(st, dr, enabled)` helper yields a bool array; combined with `|` across ST1/ST2/ST3. | |
| `s1..s3`, `sell_cond` | `_short_cond(...)` mirror. | |
| `strategy.entry("Buy", long, stop=...)` | Emit `"LONG"` on entry-trigger bars. | No pending-stop orders; RL-side execution handles fills. |
| `strategy.entry("Sell", short, stop=...)` | Emit `"SHORT"` on entry-trigger bars. | |
| `entryWindow`, `activeBuySetup/activeSellSetup` cancel-after-N-bars | Dropped. | Requires position state (`strategy.position_size`) — banned fake-state pattern. |
| `activeSL`, `buySetupLow - buySetupAtr * slAtrMult`, BE bump, trailing EMA(21) | Dropped. | Not representable in `StrategyRecommendation`. |
| Pivot-based target (`ta.pivothigh`, `ta.pivotlow`, `valuewhen`), `initialRisk * 1.5` TP | Dropped. | Same architectural constraint. |
| MACD bear/bull cross exit (`ta.crossunder/crossover` on MACD) | Dropped. | Exit-only logic with no entry effect. |
| `request.security(..., "1"/"3"/"5"/"15"/"30"/"60"/"240", ..., lookahead_off)` MTF calls | Dropped — current TF only. | Pine defaults ship with `useCur=true` and every other MTF toggle `false`. `BaseStrategy.timeframe` governs TF selection globally. |
| `plot(...)`, `label.new(...)` | Dropped. | Visualization-only. |
| `strategy.position_size`, `strategy.position_avg_price`, `strategy.cancel`, `strategy.exit`, `strategy.close` | Dropped. | "Fake state" ban — execution layer owns these. |
| `SignalType` emission (batch vs. streaming) | Strings `"LONG"/"SHORT"/"FLAT"` from `generate_all_signals`, `SignalType` enum from `step`. | Per the BaseStrategy contract. |

### Warnings / workarounds

1. **Dropped exits (documented in class docstring):** ATR-buffered stop, break-even bump, trailing-EMA(21), pivot take-profit, `initialRisk*1.5` fallback target, MACD early-exit, and the `entryWindow` cancel-after-N-bars setup are all architectural drops. The Pine strategy's edge may lean on these; the RL engine is expected to learn exit policies from LONG/SHORT entry signals. Flagged for Orchestrator awareness but not blocking.
2. **No MTF wiring in default config:** The Pine file exposes toggles for 1m/3m/5m/15m/30m/1h/4h via `request.security`, but ships with only `useCur=true`. Implementing all MTFs would require resampling seven extra timeframes per call — deferred until a future variant flips the MTF toggles on.
3. **Supertrend recurrence uses an O(n) loop:** This is explicitly allowed by `PINESCRIPT_REFERENCE/SKILL.md` (Section 3.2 "Supertrend"). The loop is over a numpy array and runs in ~7 ms on the 1100-candle fixture — well under the 60 s batch-mode budget.
4. **Conservative long+short tiebreak:** `is_up` and `is_dn` are mutually exclusive when `use_ema_filter=True`, but with the filter off they are both always `True`. On a single bar where both a long and a short condition somehow fire, we emit `"FLAT"` rather than arbitrarily picking one direction.
5. **Streaming buffer strategy:** `step` maintains a bounded `collections.deque` (maxlen ≈ `MIN_CANDLES_REQUIRED + 20`) and recomputes the 3 Supertrends + EMA on the buffer each tick. Bounded — NOT the full history — so the contract's "do not re-read history every tick" rule is respected. Approximate cost: microseconds per tick.
6. **`step` and batch agree on warmup:** both return FLAT for the first 600 bars. Post-warmup they are NOT bar-for-bar identical (Supertrend seeds differently on a bounded rolling window than on full history), but active-signal counts are in the same order of magnitude (batch: 6 active / streaming: 6 active on the shared fixture).
7. **No `np.roll`, no `df.resample()`, no future-indexed access.** Vectorized mode uses only `pd.Series.shift(+n)`-class operations implicitly via numpy elementwise ops on ATR-derived bands.

### Files written

- `src/strategies/supertrend_ema_rejection_strategy.py` — strategy implementation (SupertrendEmaRejectionStrategy).
- `output/supertrend_ema_rejection/2026-04-23_06-31-10/agent_transpiler.md` — this report.

Smoke-test result against the shared `sample_ohlcv_data` fixture (1100 candles):
- `generate_all_signals` → `Series[str]` of length 1100, unique values `{"FLAT", "LONG", "SHORT"}`, first 600 rows all `"FLAT"`, 6 active signals in phases 2 & 3, runtime 0.007 s.
- Empty DataFrame → empty Series.
- `step` → `SignalType.FLAT` for the first 600 ticks, 6 active signals after warmup.
- Class instantiates (both abstract methods implemented) and `MIN_CANDLES_REQUIRED == 600`.
