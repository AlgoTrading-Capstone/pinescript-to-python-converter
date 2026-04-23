# Agent Communication & Output Protocols

This rule applies to orchestrating the multi-agent transpilation pipeline.

## 1. Subprocess Topology
The pipeline runs TWO separate `claude -p` subprocesses per conversion:

- **Orchestrator subprocess** (`run_orchestrator` in
  `src/pipeline/orchestrator.py`) — handles Transpile → Validate →
  Test Generation only. Does NOT delegate to Integration. Declares
  success by emitting `CONVERSION_PASS` after Test Generator returns.
- **Integration subprocess** (`run_integration`) — launched by
  `main.py` ONLY after the statistical gate passes. Creates the git
  branch and opens the GitHub PR via MCP. Emits `INTEGRATION_PASS`
  or `INTEGRATION_FALLBACK`.

This split guarantees a PR is never opened for a strategy that will
later fail the gate.

## 2. Handoff Logging Protocol
Inside the orchestrator subprocess, print exact transition markers so the
external Python runner can route log prefixes and track agent state:
- On delegation: `[SYSTEM] Handing over to: <AgentName>`
- On return: `[SYSTEM] Control returned to: ORCHESTRATOR`

## 3. Execution & Error Handling
- Both subprocesses run in non-interactive mode (`-p`).
- **Auto-Fixes:** Structural issues (warmup guards, imports, naming)
  trigger a bounded retry (max 2 cycles).
- **Immediate Abort:** Trading logic failures (e.g., unresolvable
  lookahead bias) MUST abort immediately. No auto-fix.
- **Watchdogs:** orchestrator 1500s, integration 900s. On timeout the
  process is killed and the phase reported as a failure.

## 4. Completion Tokens & Disk Fallback
`claude -p` buffers assistant output in many configurations, so sub-agent
responses may never reach parent stdout. Every token has an on-disk
fallback:

| Token | Emitted by | Disk fallback file (scanned by `main.py`) |
|---|---|---|
| `TRANSPILER_LOG_WRITTEN` | Transpiler | `agent_transpiler.md` |
| `VALIDATOR_LOG_WRITTEN` | Validator | `agent_validator.md` |
| `TEST_GENERATOR_LOG_WRITTEN` | Test Generator | `agent_test_generator.md` |
| `CONVERSION_PASS` | Orchestrator (after Test Generator) | `agent_test_generator.md` |
| `INTEGRATION_LOG_WRITTEN` | Integration | `agent_integration.md` |
| `INTEGRATION_PASS` / `INTEGRATION_FALLBACK` | Integration | `agent_integration.md` |

A missing stdout token whose log file exists on disk is demoted to an
info-level note. A missing stdout token AND missing log file is a
warning — and for completion tokens, a hard failure.

## 5. Output Contracts (Strict Strings)
- **Strategy Selector:** Output MUST be raw JSON only (no markdown code
  blocks). Schema: `{ "pine_metadata": {}, "category": "",
  "btc_score": 0, "project_score": 0, "recommendation_reason": "" }`.
  The selector must also apply the deterministic rejects documented in
  `strategy_selector.md` (profit_factor < 1, max_drawdown_pct > 50%) so
  the evaluator's belt-and-braces check is redundant, not corrective.
- **Orchestrator:** MUST emit `CONVERSION_PASS` to stdout once the Test
  Generator returns. Does NOT emit integration tokens.
- **Integration Agent:** MUST emit exactly `INTEGRATION_PASS` or
  `INTEGRATION_FALLBACK` to stdout (or write it into
  `agent_integration.md`). Without either, `main.py` fails the run
  regardless of exit code. Integration publishes to the rl-training repo
  EXCLUSIVELY through GitHub MCP — never run local `git` commands
  against `C:\Projects\rl-training`.