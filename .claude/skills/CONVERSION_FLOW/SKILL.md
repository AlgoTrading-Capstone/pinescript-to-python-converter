---
name: conversion-flow
description: Defines the strict multi-agent execution pipeline for converting PineScript to Python. Use this skill to understand the Orchestrator's step-by-step workflow (Phases 0-4), including Transpilation, Validation, and Test Generation. Integration (branch + PR) runs in a SEPARATE subprocess after the statistical gate, NOT inside the orchestrator. Automatically load this when routing tasks between agents, managing the transpilation lifecycle, or determining the next pipeline phase.
---

# PineScript to Python Conversion Workflow

This document defines the step-by-step execution flow for the Orchestrator Agent.

## Phase 0: Strategy Selection (Pre-flight — handled by main.py)
- `main.py` scans `input/` and registers all .pine files in
  `data/strategies_registry.json`.
- Each new file is evaluated **in isolation** by the `strategy_selector` agent,
  which returns a JSON object with BTC + project scores.
- The pipeline auto-selects the highest-scoring strategy whose combined score
  meets the conviction floor (`MIN_SELECTION_SCORE`, currently 6).
- Strategies skipped 2+ times are archived regardless of score.
- When no evaluated candidates remain, the pipeline recycles from archive.

The Orchestrator is only invoked AFTER this step and starts at Phase 1.

## Phase 1: Ingestion & Transpilation
1. **Input:** Receive PineScript code and Metadata.
2. **Action:** Call **Transpiler Agent**.
   - Task: Convert PineScript to `src/strategies/<name>_strategy.py`.
   - Reference: `.claude/skills/PINESCRIPT_REFERENCE`.
   - The Transpiler MUST emit `TRANSPILER_LOG_WRITTEN: <absolute_path>` after
     writing `agent_transpiler.md` to the output snapshot directory.

## Phase 2: Validation (Gatekeeper)
3. **Action:** Call **Validator Agent**.
   - Task: Review code against `.claude/skills/VALIDATION_CHECKLIST`.
   - **On STRUCTURAL failure:** Auto-fix loop — re-invoke Transpiler with fix instructions,
     re-validate. Max 2 retry cycles.
   - **On TRADING LOGIC failure:** Abort immediately (`CONVERSION_FAILED`).
   - **On PASS:** Proceed.
   - The Validator MUST emit `VALIDATOR_LOG_WRITTEN: <absolute_path>` after
     writing `agent_validator.md` to the output snapshot directory.

## Phase 3: Test Generation & Execution (QA)
4. **Action:** Call **Test Generator Agent**.
   - Task: Create `tests/strategies/test_<safe_name>_strategy.py` using `sample_ohlcv_data`.
   - **MANDATORY:** Test Generator MUST run pytest after writing tests.
   - **On test failure (test bug):** Test Generator fixes the test (max 2 attempts).
   - **On test failure (strategy bug):** Test Generator reports `TEST_VALID_STRATEGY_BROKEN`.
     Orchestrator routes back to Transpiler → Validator → Test Generator (max 1 full loop).
   - The Test Generator MUST emit `TEST_GENERATOR_LOG_WRITTEN: <absolute_path>` after
     writing `agent_test_generator.md` to the output snapshot directory.

## Phase 4: Declare Conversion Success
5. **Action:** The Orchestrator declares the conversion pipeline complete.
   - Do NOT invoke the Integration Agent.
   - Do NOT run any `git` or PR command.
   - Copy the Test Generator's `TEST_GENERATOR_LOG_WRITTEN: ...` line verbatim
     as the **second-to-last line** of the orchestrator's final response.
   - Emit `CONVERSION_PASS` as the **very last line** (no code fence, no bullet).
   - Return control to `main.py`.

## What happens after CONVERSION_PASS (not the Orchestrator's responsibility)
- `main.py` copies artifacts to the output snapshot.
- `main.py` runs the **Statistical Gate** (`src/pipeline/statistical_gate.py`)
  on multi-year BTC/USDT data.
- **Only if the gate PASSES** does `main.py` spawn the **Integration Agent**
  in its own subprocess (`run_integration`). Integration opens the GitHub PR
  and emits `INTEGRATION_PASS` / `INTEGRATION_FALLBACK`.
- **If the gate REJECTS**, no PR is ever opened — the strategy is marked
  `statistically_rejected` and archived to `archive/rejected/`.

This "gate-then-integrate" order prevents dead-on-data strategies from
cluttering the downstream repo with abandoned PRs.

## Success Detection
`main.py` verifies orchestrator success by:
1. Scanning stdout for `CONVERSION_PASS` token
2. Falling back to `<output_snapshot>/agent_test_generator.md` if stdout is buffered
3. Verifying strategy file (`src/strategies/<name>_strategy.py`) exists on disk
4. Verifying test file (`tests/strategies/test_<safe_name>_strategy.py`) exists on disk

Exit code 0 alone is NOT sufficient — all checks must pass. The separate
integration subprocess has its own analogous check for
`INTEGRATION_PASS` / `INTEGRATION_FALLBACK` and `agent_integration.md`.