# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

An AI-driven pipeline that converts TradingView Pine Script v5 strategies into vectorized Python strategies. The pipeline uses a multi-agent architecture (via Claude subagents) to transpile, validate, test, and submit a GitHub PR for human review.

## Commands

```bash
# Run the full pipeline (requires Claude CLI in PATH)
python runner.py

# Run tests
pytest tests/strategies/ -v

# Run a single test file
pytest tests/strategies/test_<name>.py -v
```

**Dependencies:** TA-Lib requires the C library to be installed separately before `pip install -r requirements.txt`. On Windows use a pre-built wheel; on Linux build from source (see `ci.yml` for the build steps).

## Pipeline Flow

`runner.py` is the single entry point. It orchestrates these phases:

1. **Scrape** — If `input/` has fewer than 5 `.pine` files, `TradingViewScraper` (Selenium) auto-downloads public strategies from TradingView.
2. **Evaluate** — Spawns `claude -p --agent strategy_selector` as a subprocess for each new `.pine` file. Returns a JSON score (`btc_score` + `project_score`, each 0–5). Results are persisted to `strategies_registry.json`.
3. **Select** — Displays a ranked CLI menu; user picks one strategy to convert.
4. **Convert** — Spawns `claude -p --agent orchestrator` which delegates sequentially to four sub-agents:
   - **Transpiler** → writes `src/strategies/<name>.py`
   - **Validator** → static analysis (lookahead bias, forbidden functions, class contract)
   - **Test Generator** → writes `tests/strategies/test_<name>.py`
   - **Integration** → git branch + GitHub MCP PR
5. **Archive** — Low-scoring strategies (combined score < 4) are moved to `archive/`; higher-scoring ones remain in `input/` for future runs.

Agent subprocesses use `--dangerously-skip-permissions` so tool-approval prompts don't block them. The `CLAUDECODE` env var is stripped so nested `claude` calls are allowed.

## Agent System

All agent definitions live in `.claude/agents/`. The orchestrator reads `.claude/skills/CONVERSION_FLOW/SKILL.md` as its master playbook.

**Required logging protocol:** When the Orchestrator hands off to a sub-agent it must print `[SYSTEM] Handing over to: <AgentName>`. On return: `[SYSTEM] Control returned to: Orchestrator`. `runner.py` parses these strings to prefix log lines with the active agent name.

**Strategy Selector output contract:** Must be raw JSON only (no markdown fences). Schema: `{ pine_metadata, btc_score, project_score, recommendation_reason }`.

## Strategy Contract (`BaseStrategy`)

All generated strategies must inherit from `src/base_strategy.py`:

```python
class MyStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(name="...", description="...", timeframe="15m", lookback_hours=48)

    def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
        # Must return StrategyRecommendation(signal=SignalType.LONG/SHORT/FLAT/HOLD, timestamp=timestamp)
```

- Strategy receives a full OHLCV DataFrame + a UTC `timestamp`. It returns only a signal — no position sizing, fees, or broker logic.
- All columns are lowercase: `open`, `high`, `low`, `close`, `volume`, `date` (UTC-aware).

## Anti-Lookahead Bias Rules

1. All `shift()` operations must use positive integers (backward-looking only).
2. Multi-timeframe logic **must** use `src/utils/resampling.py`:
   - `resample_to_interval(df, interval)` — upsample base data to higher TF
   - `resampled_merge(original, resampled)` — merge back with `ffill`, shifting resampled timestamps to prevent future-peeking
3. **Forbidden:** `df.resample()` directly inside a strategy, `future_shift`, `barstate.isrealtime`.

## Missing TA-Lib Indicators

Implement in pure Pandas when TA-Lib lacks them:
- **RMA:** `df.ewm(alpha=1/length, adjust=False).mean()`
- **Supertrend:** Custom ATR band logic
- **VWAP:** `(cumulative price×volume) / cumulative volume`

## Key Files

| File | Purpose |
|---|---|
| `runner.py` | Pipeline entry point |
| `strategies_registry.json` | State tracker for all `.pine` files (new → evaluated → selected → converted → archived) |
| `src/base_strategy.py` | Abstract base class all strategies must inherit |
| `src/utils/resampling.py` | MTF utilities (`resample_to_interval`, `resampled_merge`) |
| `src/utils/tv_scraper.py` | Selenium scraper for TradingView public strategies |
| `tests/conftest.py` | `sample_ohlcv_data` fixture (500 candles: sideways/bull/bear phases) |
| `.claude/agents/` | Agent persona definitions |

## `/convert` Slash Command

Drop a `.pine` file in `input/` then run:
```
/convert input/MyStrategy.pine
```
This invokes the orchestrator sub-agent directly, skipping the evaluation/selection phases of `runner.py`.
