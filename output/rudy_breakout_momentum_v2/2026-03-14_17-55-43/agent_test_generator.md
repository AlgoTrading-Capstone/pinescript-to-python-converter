# Test Generator Agent — Rudy Breakout Momentum v2

## Files Written
- `tests/strategies/test_rudy_breakout_momentum_v2.py`

## Test Count: 17 tests — all PASS (0.18s)

## Test Design Decisions
- Synthetic `_make_trending_df()` helper with sine oscillation on top of linear trend to keep RSI in 40-80 window while ensuring EMA21 > EMA50 uptrend.
- Pure monotone trend saturates RSI at 100 — sine overlay prevents this.
- FLAT signal test: steady uptrend then sharp last-bar drop below EMA21 to force crossunder.
- FLAT priority test: combines new high + crossunder on same bar.
- No-lookahead test: two sequential runs on identical slices produce identical results.

## Tests Written
| Test | Coverage |
|---|---|
| test_instantiation | name, timeframe, lookback_hours, MIN_BARS |
| test_custom_parameters | constructor kwargs |
| test_min_bars_guard | HOLD for 10-row input |
| test_hold_on_insufficient_data | HOLD for 159 rows (MIN_BARS-1) |
| test_exactly_min_bars_does_not_hold_unconditionally | no exception at boundary |
| test_returns_valid_signal_type | valid StrategyRecommendation on full fixture |
| test_timestamp_passthrough | returned timestamp matches input |
| test_long_signal | LONG on calibrated synthetic uptrend |
| test_long_signal_not_emitted_in_downtrend | no LONG during downtrend |
| test_flat_signal | FLAT on close crossunder EMA21 |
| test_flat_priority_over_long | FLAT wins over LONG |
| test_no_lookahead_bias | identical results on identical inputs |
| test_determinism_on_identical_input | deterministic across runs |
| test_signal_across_phases | no crash on full 1,100-row fixture |
| test_no_long_during_warmup | rows 1-159 all HOLD |
| test_signals_emitted_in_bull_phase | at least one signal in rows 700-900 |
| test_no_invalid_signals_ever_emitted | only LONG/FLAT/HOLD ever returned |
