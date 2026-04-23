# Test Generator Agent — Supertrend + EMA Rejection Strategy

## Summary
Wrote `tests/strategies/test_supertrend_ema_rejection_strategy.py` (prefixed
`test_*` filename per CI/CD discovery constraint) covering both the batch
(`generate_all_signals`) and streaming (`step`) modes of
`SupertrendEmaRejectionStrategy`. All 14 tests pass on the first run with the
strategy's DEFAULT parameters; no tuning was required.

## Tests written (14 total)

### Batch mode — `generate_all_signals`
1. `test_batch_shape_and_index_match_input` — length + index alignment.
2. `test_batch_values_are_valid_signal_strings` — output values are a
   subset of `{LONG, SHORT, FLAT, HOLD}`.
3. `test_batch_warmup_rows_are_all_flat` — first `MIN_CANDLES_REQUIRED`
   (=600 with default `ema_length=200`) rows are all `"FLAT"`.
4. `test_batch_emits_signals_in_volatile_phases` — the slice `[700:1100]`
   contains at least one non-`FLAT` signal. Defaults emit 2 non-FLAT signals
   inside that slice (LONG/SHORT activity seen near the bull-to-bear
   transition), so the assertion is real, not weakened.
5. `test_batch_empty_dataframe_returns_empty_series` — empty input, empty
   Series, no exception.
6. `test_batch_short_history_is_all_flat` — feeding
   `MIN_CANDLES_REQUIRED - 1` rows returns all `"FLAT"`.
7. `test_batch_handles_all_nan_closes` — NaN close column must not raise;
   returns a Series of valid values (the Supertrend's `np.isnan(atr)` guard
   causes every row to be `"FLAT"`, which is a valid subset of the signal
   set).

### Streaming mode — `step`
8. `test_step_warmup_returns_flat` — first 600 `step` calls return
   `SignalType.FLAT`.
9. `test_step_emits_signals_after_warmup` — at least one non-FLAT signal is
   emitted once `_observed >= MIN_CANDLES_REQUIRED`. Probed empirically:
   first active bar at `i=614`, with 6 active bars total.
10. `test_step_returns_signaltype_enum` — every `step` return value is a
    `SignalType` instance, even during warmup.

### Cross-mode agreement
11. `test_batch_and_step_agree_on_warmup` — both modes are uniformly FLAT in
    the warmup window (the only cross-mode agreement the contract requires).
12. `test_batch_and_step_both_fire_post_warmup` — both modes emit
    non-zero active-signal counts (batch: 6, stream: 6). Bar-for-bar
    agreement is explicitly NOT required (Supertrend's recursive seeding
    can differ slightly between the batch O(n) pass and the streaming
    buffer-based recompute under boundary conditions).

### Construction sanity
13. `test_min_candles_required_is_dynamic` — `ema_length=50` shrinks
    `MIN_CANDLES_REQUIRED` to 150 (`3 * max(50, 10, 10, 10, 14)`), proving
    the warmup is computed in `__init__` from the parameter set.
14. `test_disabling_ema_filter_does_not_break_batch` — with
    `use_ema_filter=False`, the contract still holds (valid values, correct
    length, warmup FLAT). This exercises the alternate code path in
    `generate_all_signals` where `is_up`/`is_dn` are all-True and the
    explicit `long & short` tiebreak becomes reachable.

## Tuning parameters chosen
**None.** The default parameters — `use_ema_filter=True`, `ema_length=200`,
`st1=(10,2.0)`, `st2=(10,3.0)`, `st3=(10,5.0)`, all three Supertrends enabled —
produce non-FLAT signals in both batch and streaming mode on the shared
fixture (2 inside `[700:1100]` for batch, 6 post-warmup for streaming), so
no loosening of the strategy was needed. The honest assertion holds.

## Pytest output (key lines)
```
collected 14 items

tests/strategies/test_supertrend_ema_rejection_strategy.py::test_batch_shape_and_index_match_input PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_batch_values_are_valid_signal_strings PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_batch_warmup_rows_are_all_flat PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_batch_emits_signals_in_volatile_phases PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_batch_empty_dataframe_returns_empty_series PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_batch_short_history_is_all_flat PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_batch_handles_all_nan_closes PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_step_warmup_returns_flat PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_step_emits_signals_after_warmup PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_step_returns_signaltype_enum PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_batch_and_step_agree_on_warmup PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_batch_and_step_both_fire_post_warmup PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_min_candles_required_is_dynamic PASSED
tests/strategies/test_supertrend_ema_rejection_strategy.py::test_disabling_ema_filter_does_not_break_batch PASSED

============================= 14 passed in 2.16s ==============================
```

## Result
**PASS** — 14/14 tests passing on first run with default strategy parameters.
