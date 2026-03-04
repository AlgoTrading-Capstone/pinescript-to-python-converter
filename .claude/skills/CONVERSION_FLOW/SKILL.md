# PineScript to Python Conversion Workflow

This document defines the step-by-step execution flow for the Orchestrator Agent.

## Phase 0: Strategy Selection (Pre-flight — handled by runner.py)
- `runner.py` scans `input/` and registers all .pine files in
  `strategies_registry.json`.
- Each new file is evaluated **in isolation** by the `strategy_selector` agent,
  which returns a JSON object with BTC + project scores.
- The user picks one strategy from a ranked table.
- Low-scoring strategies (total < 4) are moved to `archive/`.
- High-scoring unselected strategies remain `"evaluated"` for future runs.

The Orchestrator is only invoked AFTER this step and starts at Phase 1.

## Phase 1: Ingestion & Transpilation
1. **Input:** Receive PineScript code and Metadata.
2. **Action:** Call **Transpiler Agent**.
   - Task: Convert PineScript to `src/strategies/<name>.py`.
   - Reference: `.claude/skills/PINESCRIPT_REFERENCE`.

## Phase 2: Validation (Gatekeeper 1)
3. **Action:** Call **Validator Agent**.
   - Task: Review code against `.claude/skills/VALIDATION_CHECKLIST`.
   - **Condition:**
     - If FAIL: Stop and ask user for guidance.
     - If PASS: Proceed.

## Phase 3: Test Generation (QA)
4. **Action:** Call **Test Generator Agent**.
   - Task: Create `tests/strategies/test_<name>.py` using `sample_ohlcv_data`.

## Phase 4: Delivery (GitHub Integration)
5. **Action:** Call **Integration Agent**.
   - Task: Push code and **Open Pull Request** using GitHub MCP.
   - **Goal:** Prepare the code for human review.

## Phase 5: Human Code Review
6. **Output:** Provide user with:
   - **PR Link** (Created by Integration Agent).
   - Status: **WAITING FOR CODE REVIEW**.
   - Instruction: "Please review the PR on GitHub. Once approved and merged, the CI pipeline will run the tests."