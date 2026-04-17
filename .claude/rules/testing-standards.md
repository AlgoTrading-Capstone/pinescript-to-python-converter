# Testing Standards & Fixtures

This rule applies when generating or modifying pytest files for strategies.

## 1. Naming & Execution
- **File Naming (CI/CD Constraint):** Test files MUST be named
  `tests/strategies/test_<safe_name>_strategy.py` (e.g.,
  `test_kama_trend_strategy.py`). Do NOT use the suffix-only format
  `<safe_name>_strategy_test.py` — the `test_` prefix is mandatory for
  pytest discovery.
- **Running tests:** `pytest tests/strategies/test_<safe_name>_strategy.py -v`

## 2. The `sample_ohlcv_data` Fixture
You MUST use the shared fixture defined in `tests/conftest.py`. DO NOT mock
your own OHLCV data. The fixture contains 1,100 candles at 15m intervals
with 4 distinct phases:
- **Phase 0 (0–600):** Warmup. Flat at 10,000 for indicator convergence
  (tests the `MIN_CANDLES_REQUIRED` guard).
- **Phase 1 (600–700):** Sideways / accumulation (low volatility).
- **Phase 2 (700–900):** Bull run (10,000 → 12,000).
- **Phase 3 (900–1,100):** Bear crash (12,000 → 9,000).

## 3. Test Coverage Requirements
Tests MUST cover BOTH execution modes — the same checks the statistical gate
and the live runner each rely on.

### 3a. `generate_all_signals` (batch mode)
1. Returns a `pd.Series` whose length AND index match `sample_ohlcv_data`.
2. All values are in `{"LONG", "SHORT", "FLAT", "HOLD"}` (strings).
3. The first `MIN_CANDLES_REQUIRED` rows are ALL `"FLAT"` (warmup contract).
4. The volatile phases (2 & 3) produce at least one non-`"FLAT"` signal —
   confirms the strategy is alive on real movement.
5. Edge cases handled gracefully (empty DataFrame, all-NaN closes) — no raw
   exceptions; an empty input returns an empty Series.

### 3b. `step` (streaming mode)
6. A fresh instance returns `SignalType.FLAT` for the first
   `MIN_CANDLES_REQUIRED` candles fed via `step`.
7. After warmup, feeding the Phase 2 / Phase 3 slice candle-by-candle
   produces at least one non-`SignalType.FLAT` signal.
8. The streaming and batch outputs agree on the warmup region (both all
   `FLAT`). They are NOT required to agree bar-for-bar afterwards
   (incremental indicators may seed differently), but the active-signal
   counts should be in the same order of magnitude.

### Example skeleton
```python
import pandas as pd
from src.base_strategy import SignalType
from src.strategies.<safe_name>_strategy import <ClassName>


def test_batch_warmup_is_flat(sample_ohlcv_data):
    s = <ClassName>()
    sig = s.generate_all_signals(sample_ohlcv_data)
    assert len(sig) == len(sample_ohlcv_data)
    assert (sig.iloc[: s.MIN_CANDLES_REQUIRED] == "FLAT").all()
    assert set(sig.unique()).issubset({"LONG", "SHORT", "FLAT", "HOLD"})


def test_batch_signals_during_volatile_phases(sample_ohlcv_data):
    s = <ClassName>()
    sig = s.generate_all_signals(sample_ohlcv_data)
    volatile = sig.iloc[700:1100]
    assert (volatile != "FLAT").any(), "no signals during bull/bear phases"


def test_step_warmup_returns_flat(sample_ohlcv_data):
    s = <ClassName>()
    for i in range(s.MIN_CANDLES_REQUIRED):
        assert s.step(sample_ohlcv_data.iloc[i]) is SignalType.FLAT


def test_empty_dataframe_is_safe():
    s = <ClassName>()
    out = s.generate_all_signals(sample_ohlcv_data.iloc[0:0])
    assert len(out) == 0
```