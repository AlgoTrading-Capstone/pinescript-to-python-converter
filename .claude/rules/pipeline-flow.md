# Architecture & Pipeline Flow

This rule provides the high-level context of the TradingView to Python Transpilation Factory.

## Pipeline Phases (`main.py`)
1. **Scrape:** `TradingViewScraper` fetches `.pine` files if `input/` < 6.
2. **Evaluate:** `strategy_selector` agent evaluates `.pine` files in isolation, generating `btc_score` and `project_score`.
3. **Select:** Highest scoring strategy is selected. Others are skipped (archived after 2 skips).
4. **Convert:** Orchestrator agent delegates to:
   - *Transpiler* -> writes Python code.
   - *Validator* -> static analysis and contract enforcement.
   - *Test Generator* -> writes tests and runs `pytest`.
   - *Integration* -> creates git branch and GitHub PR via MCP.
5. **Archive:** Low-scoring or stale strategies are moved to `archive/`.

## Registry State Machine
Tracked in `data/strategies_registry.json`.
```
new → evaluated → selected → completed
                           → failed → archived (recyclable, up to 3 attempts)
                           → failed (3x) → rejected (TERMINAL)
new/evaluated (low score or skipped 2x) → archived
archived (score >= 4, recycle_eligible) → evaluated (recycled)
PR closed without merge → rejected (TERMINAL)
```
Terminal statuses: `completed`, `rejected` — never re-evaluated or recycled.
`conversion_attempts` counter tracks failures; after `MAX_CONVERSION_ATTEMPTS` (3) → `rejected`.

## Key Commands
- Run pipeline: `python main.py`
- Integration smoke tests: `pytest tests/integrations/ -v`
- Convert specific file (skips Phase 1-3): `/convert input/MyStrategy.pine`