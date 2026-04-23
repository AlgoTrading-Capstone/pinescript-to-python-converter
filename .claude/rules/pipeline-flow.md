# Architecture & Pipeline Flow

This rule provides the high-level context of the TradingView to Python Transpilation Factory.

## Pipeline Phases (`main.py`)
1. **Scrape:** `TradingViewScraper` fetches `.pine` files if `input/` <
   `TARGET_STRATEGY_COUNT` (6). Listings are pulled from a named
   `SOURCE_URLS` catalogue (`src/utils/tv_scraper.py`): `crypto_recent`,
   `cryptotrading`, `popular`, `editors_pick`. Crypto sources come first so
   cross-source dedup biases the pool toward BTC-suitable strategies.
   `_allocate_source_targets` distributes `max_results` evenly across the
   catalogue (odd remainders fall on the earlier crypto sources).
2. **Evaluate:** `strategy_selector` agent evaluates `.pine` files in
   isolation, generating `btc_score` and `project_score`. `evaluator.py`
   then applies deterministic rejects on top of the LLM verdict:
   `profit_factor < 1.0` and `max_drawdown_pct > MAX_DRAWDOWN_PCT` (50%)
   are both unconditional fails.
3. **Select:** Highest-scoring strategy is selected, subject to a
   conviction floor: `btc_score + project_score >= MIN_SELECTION_SCORE`
   (6). Anything below is never converted — the pipeline fetches a fresh
   batch and retries. Non-selected evaluated strategies get `skip_count++`
   and are archived after 2 skips.
4. **Convert** (`run_orchestrator` subprocess): the orchestrator agent
   delegates to
   - *Transpiler* → writes Python code.
   - *Validator* → static analysis and contract enforcement.
   - *Test Generator* → writes tests and runs `pytest`.

   When Test Generator succeeds, the orchestrator emits
   `CONVERSION_PASS`. It does NOT delegate to Integration. If stdout is
   buffered by `claude -p` and the token never surfaces, `main.py` scans
   `agent_test_generator.md` on disk for the token as a fallback.
4b. **Statistical Gate** (`src/pipeline/statistical_gate.py`): loads the
    converted strategy and runs `generate_all_signals` on multi-year
    BTC/USDT 15m candles. Enforces variance (≥5% active bars) AND win rate
    (≥50% over ≥30 trades). On PASS and FAIL, writes `signal_heatmap.png`,
    `winrate_curve.png`, and `stats_report.json` into
    `output/<safe_name>/<ts>/eval/`. A FAIL is **terminal**
    (`statistically_rejected`) and does NOT consume a conversion attempt.
4c. **Integration** (`run_integration`, separate subprocess): only
    launched after the gate passes. Creates the git branch and opens the
    GitHub PR via MCP. Agent emits `INTEGRATION_PASS` or
    `INTEGRATION_FALLBACK` (with `agent_integration.md` disk fallback). A
    PR is never opened for a strategy that later fails the gate. If
    integration itself fails after a passing gate, the record stays at
    `selected` so the next run retries integration only.
5. **Archive:** Low-scoring or stale strategies are moved to `archive/`.

## Registry State Machine
Tracked in `data/strategies_registry.json`.
```
new → evaluated → selected → completed
                           → failed → archived (recyclable, up to 3 attempts)
                           → failed (3x) → rejected (TERMINAL)
                           → statistically_rejected (TERMINAL — gate failure)
new/evaluated (low score or skipped 2x) → archived
archived (score >= 4, recycle_eligible) → evaluated (recycled)
PR closed without merge → rejected (TERMINAL)
```
Terminal statuses: `completed`, `rejected`, `statistically_rejected` —
never re-evaluated or recycled. `conversion_attempts` counts only
conversion failures; gate failures do NOT increment it.

## Key Commands
- Run pipeline: `python main.py`
- Integration smoke tests: `pytest tests/integrations/ -v`
- Convert specific file (skips Phase 1-3): `/convert input/MyStrategy.pine`
- Re-run the gate on an existing strategy: `python scripts/rerun_statistical_gate.py <safe_name>`
- Build cross-strategy leaderboard: `python scripts/rank_strategies.py`