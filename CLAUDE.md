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
# Plug-and-play: drops into an interactive Manual / Scrape menu.
python main.py

# Manual mode (interactive): pick #1, drop a .pine into input/manual/, press
# Enter, the system identifies the strategy and asks before running.

# Manual mode (scripted, bypasses the menu):
python main.py --manual input/manual/MyStrategy.pine --timeframe 15m

# Run tests (Linux/macOS)
pytest tests/strategies/ -v

# Run tests (Windows — use the venv interpreter)
.venv/Scripts/python.exe -m pytest tests/strategies/ -v

# Run a single test file (mandatory test_*_strategy.py naming)
pytest tests/strategies/test_<safe_name>_strategy.py -v

# Run integration smoke tests (importability)
pytest tests/integrations/ -v
```

**Non-interactive invocations** (CI, cron, redirected stdin) bypass the menu
and fall through to the existing scrape flow — no flag changes required.

**Dependencies:** `TA-Lib` requires the C library installed separately.
`ccxt` powers candle boundary alignment and the statistical gate's OHLCV
fetch. `matplotlib` and `pyarrow` are required by the gate
(heatmap rendering and parquet OHLCV cache).

## Pipeline Flow (High Level)
`main.py` is the single entry point. On a bare invocation the **interactive
CLI** (`src/cli/interactive_menu.py`) prompts the user to pick **Manual**
(drop a `.pine` into `input/manual/`, the system identifies it and asks to
run) or **Scrape** (the auto-fetch flow below). Both modes print
value-laden phase summaries (`src/cli/phase_reporter.py`) and end with an
"Artifacts" block listing every file written. The legacy phases below are
unchanged:

1. **Scrape:** Auto-downloads public strategies via Selenium if `input/`
   has fewer than `TARGET_STRATEGY_COUNT` `.pine` files. The scraper draws
   from a named `SOURCE_URLS` catalogue in `src/scrapers/tradingview.py`
   (crypto_recent, cryptotrading, popular, editors_pick — crypto sources
   first so cross-source dedup biases the pool toward BTC-suitable picks).
   `_allocate_source_targets` splits `max_results` evenly across the
   catalogue, with any remainder falling on the earlier (crypto) sources.
2. **Evaluate:** `strategy_selector` agent scores each strategy
   (BTC & Project scores). `evaluator.py` applies deterministic
   precheck rejects on top of the LLM output:
   - `profit_factor < 1.0` → rejected.
   - `max_drawdown_pct > MAX_DRAWDOWN_PCT` (50%) → rejected.
3. **Select:** The selector enforces a **conviction floor**
   (`btc_score + project_score >= MIN_SELECTION_SCORE`, currently 6) —
   anything below is never converted. Others get `skip_count++` and are
   archived after `MAX_SKIP_COUNT` skips. If no candidate clears the floor,
   the pipeline fetches a fresh batch and retries.
4. **Convert** (orchestrator subprocess, `run_orchestrator`): delegates to
   - **Transpiler** — writes `src/strategies/<safe_name>_strategy.py` with
     BOTH `generate_all_signals` and `step`.
   - **Validator** — static analysis, anti-lookahead, contract compliance
     (incl. both abstract methods).
   - **Test Generator** — writes `tests/strategies/test_<safe_name>_strategy.py`
     and runs pytest. Tests cover both modes.

   The orchestrator emits `CONVERSION_PASS` when the Test Generator
   returns. Integration is NOT delegated here — it runs in its own
   subprocess after the gate passes. If stdout is buffered by `claude -p`
   and the token never surfaces, `main.py` scans `agent_test_generator.md`
   on disk as a fallback before declaring failure.
4b. **Statistical Gate** (`src/pipeline/statistical_gate.py`) — loads the
    just-converted strategy, runs `generate_all_signals` on multi-year
    BTC/USDT 15m candles, then enforces:
    - **Variance:** ≥`MIN_SIGNAL_ACTIVITY_PCT` (5%) of bars are LONG/SHORT.
    - **Win rate:** ≥`MIN_WIN_RATE` (50%) over ≥`MIN_TRADE_COUNT` (30) trades.
    A failure here is **terminal** (`statistically_rejected`) and does NOT
    consume a conversion attempt — the code was correct, the strategy is
    just dead on data. Artifacts (`signal_heatmap.png`, `winrate_curve.png`,
    `gate_summary.png`, `stats_report.json`) are written to
    `output/<safe_name>/<ts>/eval/` on both pass and fail paths, so the PR
    body (on pass) or the post-mortem (on fail) always has evidence on disk.
    `gate_summary.png` is the unified one-glance verdict: price+signals,
    equity+drawdown, rolling win rate, and a metrics text panel in a single
    figure (rendered by `src/evaluation/plots/summary.py`).
4c. **Integration** (`run_integration`, separate subprocess): only invoked
    after a passing gate. Creates the branch and opens the GitHub PR via
    MCP — never through a local `git` call against sibling repos. The
    integration agent must emit `INTEGRATION_PASS` or
    `INTEGRATION_FALLBACK` (stdout or via `agent_integration.md` disk
    fallback). A PR is never opened for a strategy that subsequently fails
    the gate. If integration fails after a passing gate, the registry stays
    at `selected` so the next run can retry integration only.
5. **Archive:** Low-scoring or stale strategies move to `archive/`.

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
- `MIN_SELECTION_SCORE = 6` — conviction floor; btc + proj below this is
  never selected for conversion
- `MAX_DRAWDOWN_PCT = 50.0` — author-reported drawdown above this →
  deterministic reject in `evaluator.py` (mirrored belt-and-braces in
  `strategy_selector.md`). `profit_factor < 1.0` is also an
  unconditional reject.
- `MAX_SKIP_COUNT = 2` — archive after being skipped this many times
- `MAX_CONVERSION_ATTEMPTS = 3` — reject after this many failed conversions
- `TARGET_STRATEGY_COUNT = 6` — minimum `.pine` files to maintain in `input/`
- `MANUAL_INPUT_DIR = INPUT_DIR / "manual"` — drop-zone for the interactive
  Manual mode. The scrape glob is non-recursive so files placed here are
  invisible to the auto-scrape flow.
- `MIN_SIGNAL_ACTIVITY_PCT = 0.05`, `MIN_WIN_RATE = 0.50`,
  `MIN_TRADE_COUNT = 30` — statistical-gate thresholds
- `EVAL_EXCHANGE/SYMBOL/TIMEFRAME/START/END` — gate evaluation window
  (binance BTC/USDT 15m, 2018-01-01 → 2023-12-31)
- `OHLCV_MIN_COVERAGE = 0.95` — minimum fraction of expected candles the
  gate requires after download. The ccxt fetch in `src/evaluation/ohlcv.py`
  is tolerant of user-placed parquets in `data/ohlcv_cache/`: non-canonical
  filenames and FinRL-style schemas (RangeIndex + `date`/`tic` columns) are
  normalized to the canonical UTC-indexed OHLCV layout and re-saved.

## Key Files & Directories

| Path | Purpose |
|---|---|
| `main.py` | Pipeline entry point and orchestrator trigger. |
| `src/cli/` | Presentation only — `ui.py` (Rich theme + helpers), `interactive_menu.py` (Manual/Scrape menu, file picker, run confirmation), `phase_reporter.py` (`print_phase_summary` value-laden phase lines). |
| `src/pipeline/` | Pipeline core modules (`registry.py`, `evaluator.py`, `orchestrator.py`, `statistical_gate.py`, `manual_ingest.py`, `scraper.py`, ...). No UI / matplotlib in this package. |
| `src/scrapers/tradingview.py` | TradingView Selenium scraper + Pine source extraction. Heavy single-purpose module — moved out of `src/utils/` (where it does not belong). |
| `src/evaluation/` | Statistical-gate primitives — `runner.py` (contract enforcement), `loader.py` (dynamic strategy import), `ohlcv.py` (paginated ccxt fetch + parquet cache), `variance.py`, `winrate.py` (compute-only), `metrics.py`. |
| `src/evaluation/plots/` | All gate rendering — `heatmap.py`, `winrate_curve.py` (extracted from `winrate.py`), `summary.py` (the unified `gate_summary.png`). |
| `input/manual/` | Drop-zone for the interactive Manual mode. `python main.py` → pick Manual → drop a `.pine` here → press Enter to rescan and pick. |
| `data/strategies_registry.json` | State tracker (`new → evaluated → selected → completed / failed → archived / rejected / statistically_rejected`). |
| `data/ohlcv_cache/` | Parquet cache of historical candles, downloaded once per (exchange, symbol, timeframe, range). |
| `src/base_strategy.py` | **Immutable** abstract base. Both `generate_all_signals` and `step` are required. |
| `src/utils/resampling.py` | MTF utilities — required for all `request.security` conversions. |
| `tests/conftest.py` | Shared `sample_ohlcv_data` fixture (1,100 candles with warmup / sideways / bull / bear phases). |
| `archive/` | Archived `.pine` sources. |
| `archive/old_strategies/` | Pre-statistical-gate strategies and tests, kept for reference (do NOT import from here). |
| `output/<safe_name>/<timestamp>/` | Per-run snapshot: generated code, tests, agent logs (`agent_transpiler.md`, `agent_validator.md`, `agent_test_generator.md`, `agent_integration.md`), `eval/stats_report.json`, `eval/signal_heatmap.png`, `eval/winrate_curve.png`, `eval/gate_summary.png`. |
| `scripts/rerun_statistical_gate.py` | Standalone gate re-run: regenerates all four eval artifacts and updates the registry (`completed` on pass, `statistically_rejected` on fail, `conversion_attempts` reset to 0). |
| `scripts/rank_strategies.py` | Cross-strategy leaderboard: scans `output/*/*/eval/stats_report.json`, filters to gate-passed strategies, ranks by `win_rate * avg_pnl_bps * sqrt(trades / min_trades)`, writes `leaderboard.md` + `leaderboard.json` + `winrate_comparison.png` locally (never pushed to rl-training). |

## `/convert` Slash Command
To bypass scraping and evaluation for a specific file, drop a `.pine` file in
`input/` and run:
```bash
/convert input/MyStrategy.pine
```

The interactive Manual mode (`python main.py` → pick Manual) is the
preferred plug-and-play path — it performs the same bypass but adds
identification, confirmation, and live phase summaries on top.

## Current State (post-reset)
`src/strategies/` has been wiped — every previously generated strategy was
written to the old `run()`-based contract and is incompatible with the new
two-method abstract surface. They live under `archive/old_strategies/` for
reference. The pipeline starts fresh from the next conversion.