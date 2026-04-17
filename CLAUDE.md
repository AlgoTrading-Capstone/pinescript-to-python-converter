# CLAUDE.md

This file provides high-level guidance to Claude Code when working in this
repository. **CRITICAL:** Detailed architectural constraints live in
`.claude/rules/` and `.claude/skills/`. Claude auto-loads them based on context.

## What This Project Does
An AI-driven pipeline that converts TradingView Pine Script v5 strategies into
vectorized Python feature-extractors for a Reinforcement Learning (RL) Engine.
A multi-agent architecture (Orchestrator → Transpiler → Validator →
TestGenerator → Statistical Gate → Integration) transpiles, validates against
lookahead bias, tests, statistically vets on multi-year BTC data, then opens
a GitHub PR.

## Architectural Rules (Read Before Coding)
For specific coding tasks, refer to the domain rules in `.claude/rules/`:
- **`strategy-contract.md`** — vectorization, dynamic `MIN_CANDLES_REQUIRED`,
  banning `np.roll`, AND the **two abstract methods** (`generate_all_signals`
  + `step`) every strategy must implement.
- **`agent-protocols.md`** — multi-agent state-transition logging and output
  contracts.
- **`testing-standards.md`** — naming conventions and `sample_ohlcv_data`
  coverage for both batch and streaming modes.
- **`pipeline-flow.md`** — state machine and registry logic.

## Strategy Contract — TWO Abstract Methods
`src/base_strategy.py` is **immutable** and declares two abstract methods.
A subclass that implements only one CANNOT be instantiated, and the
statistical gate's loader will reject it with `TypeError: Can't instantiate
abstract class ...`.

| Method | Mode | Returns |
|---|---|---|
| `generate_all_signals(self, df) -> pd.Series` | batch / vectorized | `pd.Series[str]` aligned to `df.index`, values in `{LONG, SHORT, FLAT, HOLD}`, first `MIN_CANDLES_REQUIRED` rows ALL `"FLAT"` |
| `step(self, candle) -> SignalType` | live / streaming | `SignalType` enum, returns `FLAT` until internal counter ≥ `MIN_CANDLES_REQUIRED` |

`generate_all_signals` runs under a 60s wall-clock limit enforced by
`src/evaluation/runner.py` — Python loops over `df` rows are a contract
failure. `step` must maintain its own rolling state (no recomputing
indicators per tick).

## Git Rules
- **Never** include `Co-Authored-By` trailers in commit messages.

## Commands

```bash
# Run the full pipeline (requires Claude CLI in PATH)
python main.py

# Run tests (Linux/macOS)
pytest tests/strategies/ -v

# Run tests (Windows — use the venv interpreter)
.venv/Scripts/python.exe -m pytest tests/strategies/ -v

# Run a single test file (mandatory test_*_strategy.py naming)
pytest tests/strategies/test_<safe_name>_strategy.py -v

# Run integration smoke tests (importability)
pytest tests/integrations/ -v
```

**Dependencies:** `TA-Lib` requires the C library installed separately.
`ccxt` powers candle boundary alignment and the statistical gate's OHLCV
fetch. `matplotlib` and `pyarrow` are required by the gate
(heatmap rendering and parquet OHLCV cache).

## Pipeline Flow (High Level)
`main.py` is the single entry point orchestrating these phases:
1. **Scrape:** Auto-downloads public strategies via Selenium if `input/`
   has fewer than `TARGET_STRATEGY_COUNT` `.pine` files.
2. **Evaluate:** `strategy_selector` agent scores each strategy
   (BTC & Project scores).
3. **Select:** Highest-scoring strategy is chosen; others get
   `skip_count++` and are archived after `MAX_SKIP_COUNT` skips.
4. **Convert:** Orchestrator delegates to sub-agents:
   - **Transpiler** — writes `src/strategies/<safe_name>_strategy.py` with
     BOTH `generate_all_signals` and `step`.
   - **Validator** — static analysis, anti-lookahead, contract compliance
     (incl. both abstract methods).
   - **Test Generator** — writes `tests/strategies/test_<safe_name>_strategy.py`
     and runs pytest. Tests cover both modes.
4b. **Statistical Gate** (`src/pipeline/statistical_gate.py`) — loads the
    just-converted strategy, runs `generate_all_signals` on multi-year
    BTC/USDT 15m candles, then enforces:
    - **Variance:** ≥`MIN_SIGNAL_ACTIVITY_PCT` (5%) of bars are LONG/SHORT.
    - **Win rate:** ≥`MIN_WIN_RATE` (50%) over ≥`MIN_TRADE_COUNT` (30) trades.
    A failure here is **terminal** (`statistically_rejected`) and does NOT
    consume a conversion attempt — the code was correct, the strategy is
    just dead on data. Artifacts written to `output/<safe_name>/<ts>/eval/`.
5. **Integration:** Pushes branch and opens GitHub PR via MCP.
6. **Archive:** Low-scoring or stale strategies move to `archive/`.

## Registry State Machine
Tracked in `data/strategies_registry.json`. Each strategy progresses through:
```
new → evaluated → selected → completed
                           → failed → archived (recyclable, up to MAX_CONVERSION_ATTEMPTS)
                           → failed (3x) → rejected (TERMINAL, never recycled)
                           → statistically_rejected (TERMINAL — gate failure)
new/evaluated (low score or skipped 2x) → archived
archived (score >= 4, recycle_eligible) → evaluated (recycled)
PR closed without merge → rejected (TERMINAL)
```

**Terminal statuses** (`completed`, `rejected`, `statistically_rejected`)
are permanent — strategies in these states are never re-evaluated,
re-selected, or recycled. The `conversion_attempts` counter tracks
conversion failures only; gate failures do NOT increment it.

**Key constants** (in `src/pipeline/__init__.py`):
- `ARCHIVE_SCORE_THRESHOLD = 4` — btc + proj score below this → archive
- `MAX_SKIP_COUNT = 2` — archive after being skipped this many times
- `MAX_CONVERSION_ATTEMPTS = 3` — reject after this many failed conversions
- `TARGET_STRATEGY_COUNT = 6` — minimum `.pine` files to maintain in `input/`
- `MIN_SIGNAL_ACTIVITY_PCT = 0.05`, `MIN_WIN_RATE = 0.50`,
  `MIN_TRADE_COUNT = 30` — statistical-gate thresholds
- `EVAL_EXCHANGE/SYMBOL/TIMEFRAME/START/END` — gate evaluation window
  (binance BTC/USDT 15m, 2018-01-01 → 2023-12-31)

## Key Files & Directories

| Path | Purpose |
|---|---|
| `main.py` | Pipeline entry point and orchestrator trigger. |
| `src/pipeline/` | Pipeline core modules (`registry.py`, `evaluator.py`, `orchestrator.py`, `statistical_gate.py`, ...). |
| `src/evaluation/` | Statistical-gate primitives — `runner.py` (contract enforcement), `loader.py` (dynamic strategy import), `ohlcv.py` (paginated ccxt fetch + parquet cache), `variance.py`, `winrate.py`, `heatmap.py`. |
| `data/strategies_registry.json` | State tracker (`new → evaluated → selected → completed / failed → archived / rejected / statistically_rejected`). |
| `data/ohlcv_cache/` | Parquet cache of historical candles, downloaded once per (exchange, symbol, timeframe, range). |
| `src/base_strategy.py` | **Immutable** abstract base. Both `generate_all_signals` and `step` are required. |
| `src/utils/resampling.py` | MTF utilities — required for all `request.security` conversions. |
| `tests/conftest.py` | Shared `sample_ohlcv_data` fixture (1,100 candles with warmup / sideways / bull / bear phases). |
| `archive/` | Archived `.pine` sources. |
| `archive/old_strategies/` | Pre-statistical-gate strategies and tests, kept for reference (do NOT import from here). |
| `output/<safe_name>/<timestamp>/` | Per-run snapshot: generated code, tests, agent logs, `eval/stats_report.json`, `eval/signal_heatmap.png`. |

## `/convert` Slash Command
To bypass scraping and evaluation for a specific file, drop a `.pine` file in
`input/` and run:
```bash
/convert input/MyStrategy.pine
```

## Current State (post-reset)
`src/strategies/` has been wiped — every previously generated strategy was
written to the old `run()`-based contract and is incompatible with the new
two-method abstract surface. They live under `archive/old_strategies/` for
reference. The pipeline starts fresh from the next conversion.