# SKILL: Standardized Logging & Communication Protocol

## Purpose
This project relies on a multi-agent architecture (Orchestrator, Transpiler, Validator, TestGenerator, Integration). To allow the external Python `runner.py` to route and display logs correctly, ALL agents must strictly adhere to this communication protocol.

## 1. State Transitions (Handoffs)
The Python backend acts as a state machine. It relies on exact trigger phrases to know which agent is currently active. 

When the Orchestrator delegates a task to a sub-agent, it MUST print:
`[SYSTEM] Handing over to: <AgentName>`
*(Valid Agent Names: TRANSPILER, VALIDATOR, TEST_GENERATOR, INTEGRATION, SELECTOR)*

When a sub-agent finishes its task and returns control, the Orchestrator MUST print:
`[SYSTEM] Control returned to: ORCHESTRATOR`

## 2. Action Logging (Tracing)
When performing significant actions (reading a file, converting a block of code, running a test), clearly state what you are doing using brief, technical log lines.
- **DO NOT** use conversational filler (e.g., avoid "I am now going to...", "Here is the code...").
- **DO** use clear action statements:
  - `[INFO] Parsing PineScript strategy...`
  - `[INFO] Mapping ta.sma to Pandas rolling mean...`
  - `[INFO] Running pytest on the generated file...`

## 3. Error Reporting
If you encounter a syntax error, validation failure, or missing dependency, log it explicitly before taking corrective action:
`[ERROR] Validation failed: Lookahead bias detected in future_shift function.`
`[WARNING] TA-Lib lacks an RMA function. Implementing custom Pandas EMA workaround.`

## 4. Final Output Formatting
- Never wrap your internal thought processes in Markdown code blocks unless you are actually writing a code file.
- Keep terminal output clean, concise, and focused purely on the pipeline's progress.