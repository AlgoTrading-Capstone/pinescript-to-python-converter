# CLAUDE.md

This file provides high-level guidance to Claude Code when working in this repository. 
**CRITICAL:** Detailed architectural constraints are located in `.claude/rules/`. Claude will automatically load them based on context.

## What This Project Does
An AI-driven pipeline that converts TradingView Pine Script v5 strategies into vectorized Python feature-extractors for a Reinforcement Learning (RL) Engine. The pipeline uses a multi-agent architecture (Orchestrator, Transpiler, Validator, TestGenerator, Integration) to transpile, strictly validate against lookahead bias, test, and submit a GitHub PR.

## Architectural Rules (Read Before Coding)
For specific coding tasks, refer to the domain rules loaded from `.claude/rules/`:
- **`strategy-contract.md`**: Strict constraints on vectorization, RL dynamic warmup (`MIN_CANDLES_REQUIRED`), and banning `np.roll`.
- **`agent-protocols.md`**: Multi-agent state transition logging and Output contracts.
- **`testing-standards.md`**: Naming conventions and usage of the `sample_ohlcv_data` fixture.
- **`pipeline-flow.md`**: The state machine and registry logic.

## Commands

```bash
# Run the full pipeline (requires Claude CLI in PATH)
python main.py

# Run tests (Linux/macOS)
pytest tests/strategies/ -v

# Run tests (Windows — use the venv interpreter)
.venv/Scripts/python.exe -m pytest tests/strategies/ -v

# Run a single test file (Note the mandatory test_*_strategy.py naming format)
pytest tests/strategies/test_<safe_name>_strategy.py -v

# Run integration smoke tests (importability)
pytest tests/integrations/ -v
```

**Dependencies:** `TA-Lib` requires the C library to be installed separately. `ccxt` is required for candle boundary alignment.

## Pipeline Flow (High Level)
`main.py` is the single entry point orchestrating these phases:
1. **Scrape:** Auto-downloads public strategies via Selenium if `input/` is nearly empty.
2. **Evaluate:** `strategy_selector` agent scores strategies (BTC & Project scores).
3. **Select:** Highest-scoring strategy is chosen; others skipped or archived.
4. **Convert:** Orchestrator delegates to sub-agents:
   - **Transpiler:** Writes `src/strategies/<safe_name>_strategy.py`
   - **Validator:** Enforces static analysis, anti-lookahead rules, and contract compliance.
   - **Test Generator:** Writes `tests/strategies/test_<safe_name>_strategy.py` and runs pytest.
   - **Integration:** Pushes branch and opens GitHub PR via MCP.
5. **Archive:** Low-scoring or frequently skipped strategies are archived.

## Registry State Machine
Tracked in `data/strategies_registry.json`. Each strategy progresses through:
```
new → evaluated → selected → completed
                           → failed → archived (recyclable, up to MAX_CONVERSION_ATTEMPTS)
                           → failed (3x) → rejected (TERMINAL, never recycled)
new/evaluated (low score or skipped 2x) → archived
archived (score >= 4, recycle_eligible) → evaluated (recycled)
PR closed without merge → rejected (TERMINAL)
```

**Terminal statuses** (`completed`, `rejected`) are permanent — strategies in these states are never re-evaluated, re-selected, or recycled. The `conversion_attempts` counter tracks how many times conversion has failed; after `MAX_CONVERSION_ATTEMPTS` (3) the strategy is promoted to `rejected`.

**Key constants** (in `src/pipeline/__init__.py`):
- `ARCHIVE_SCORE_THRESHOLD = 4` — btc + proj score below this → archive
- `MAX_SKIP_COUNT = 2` — archive after being skipped this many times
- `MAX_CONVERSION_ATTEMPTS = 3` — reject after this many failed conversions
- `TARGET_STRATEGY_COUNT = 6` — minimum .pine files to maintain in `input/`

## Key Files & Directories

| Path | Purpose |
|---|---|
| `main.py` | Pipeline entry point and main orchestrator trigger. |
| `src/pipeline/` | Pipeline core modules (`registry.py`, `evaluator.py`, `orchestrator.py`, etc.). |
| `data/strategies_registry.json` | State tracker (new → evaluated → selected → completed / failed → archived / rejected). |
| `src/base_strategy.py` | **Immutable** abstract base class for all generated strategies. |
| `src/utils/resampling.py` | Multi-Timeframe (MTF) utilities required for all `request.security` conversions. |
| `tests/conftest.py` | Shared `sample_ohlcv_data` fixture (1,100 candles with varying market phases). |
| `output/<safe_name>/<timestamp>/` | Per-run snapshot: generated code, tests, and individual agent decision logs. |

## `/convert` Slash Command
To bypass scraping and evaluation for a specific file, drop a `.pine` file in `input/` and run:
```bash
/convert input/MyStrategy.pine
```
