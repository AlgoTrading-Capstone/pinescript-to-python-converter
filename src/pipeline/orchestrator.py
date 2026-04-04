"""
Orchestrator — Spawns the Claude orchestrator agent and streams its output.

Includes token-based success detection: the orchestrator must emit
INTEGRATION_PASS or INTEGRATION_FALLBACK for the run to be considered successful.
"""

import logging
import shutil
import subprocess
import threading
from pathlib import Path

from src.pipeline import LOGS_ROOT, SUBPROCESS_ENV
from src.pipeline.ui import print_error, print_info, print_warning

logger = logging.getLogger("runner")

# Tokens the Integration Agent must emit to signal workflow completion.
_SUCCESS_TOKENS = {"INTEGRATION_PASS", "INTEGRATION_FALLBACK"}

# Tokens each sub-agent must emit after writing its agent_*.md decision log.
# Detected passively in the stream; missing tokens trigger a soft warning (not hard failure).
_LOG_TOKENS = {
    "TRANSPILER_LOG_WRITTEN",
    "VALIDATOR_LOG_WRITTEN",
    "TEST_GENERATOR_LOG_WRITTEN",
    "INTEGRATION_LOG_WRITTEN",
}
_EXPECTED_AGENT_LOGS = (
    "agent_transpiler.md",
    "agent_validator.md",
    "agent_test_generator.md",
    "agent_integration.md",
)


def _setup_strategy_logger(strategy_name: str) -> tuple[logging.Logger, Path]:
    """Create a dedicated timestamped logger + log directory for a conversion run."""
    from datetime import datetime, UTC

    ts       = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    safe     = "".join(c if c.isalnum() or c in "-_." else "_" for c in strategy_name).strip("_")
    run_dir  = LOGS_ROOT / safe / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    lg  = logging.getLogger(f"runner.orch.{safe}.{ts}")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S")

    for path, level in [
        (run_dir / "run.log",    logging.DEBUG),
        (run_dir / "errors.log", logging.ERROR),
    ]:
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        lg.addHandler(fh)

    return lg, run_dir


def run_orchestrator(
    pine_file: Path,
    meta: dict,
    output_dir: Path,
) -> tuple[bool, Path]:
    """
    Execute the orchestrator agent to convert a PineScript strategy.

    Streams stdout in real-time, routing log prefixes by agent handoff markers.
    Returns (success, log_dir). Success requires BOTH:
      - Process exit code 0
      - INTEGRATION_PASS or INTEGRATION_FALLBACK token found in output
    """
    strat_logger, run_dir = _setup_strategy_logger(meta["name"])
    print_info(f"Launching orchestrator for '{meta['name']}'")
    print_info(f"Run log: {run_dir / 'run.log'}")

    prompt = (
        "Start the conversion workflow.\n\n"
        f"Strategy name  : {meta['name']}\n"
        f"Timeframe      : {meta['timeframe']}\n"
        f"Lookback bars  : {meta['lookback_bars']}\n"
        f"Output snapshot: {output_dir}\n\n"
        f"PineScript file: {pine_file}\n"
        "(Use your Read tool to load the file from disk.)"
    )
    command = [
        "claude", "-p",
        "--agent", "orchestrator",
        "--dangerously-skip-permissions",
        "--verbose",
        prompt,
    ]
    strat_logger.info(f"Prompt sent to orchestrator (file={pine_file})")

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=SUBPROCESS_ENV,
        )
        current_agent = "ORCHESTRATOR"
        killed_by_watchdog = threading.Event()
        completion_token_found = False
        seen_log_tokens: set[str] = set()

        def _kill_on_timeout():
            killed_by_watchdog.set()
            try:
                process.kill()
            except OSError:
                pass

        watchdog = threading.Timer(900, _kill_on_timeout)
        try:
            watchdog.start()
            for line in process.stdout:
                stripped = line.rstrip()
                if not stripped:
                    continue

                # --- Token detection ---
                for token in _SUCCESS_TOKENS:
                    if token in stripped:
                        completion_token_found = True
                        strat_logger.info(f"Completion token detected: {token}")

                for token in _LOG_TOKENS:
                    if token in stripped:
                        seen_log_tokens.add(token)
                        strat_logger.info(f"Log token detected: {token}")

                # --- Agent routing ---
                lower_line = stripped.lower()
                if "handing over to: transpiler" in lower_line or "agent transpiler" in lower_line:
                    current_agent = "TRANSPILER"
                elif "handing over to: validator" in lower_line or "agent validator" in lower_line:
                    current_agent = "VALIDATOR"
                elif "handing over to: test_generator" in lower_line or "agent test_generator" in lower_line:
                    current_agent = "QA_AGENT"
                elif "handing over to: integration" in lower_line or "agent integration" in lower_line:
                    current_agent = "INTEGRATION"
                elif "control returned to: orchestrator" in lower_line:
                    current_agent = "ORCHESTRATOR"

                print_info(f"[{current_agent}] {stripped}")
                strat_logger.info(f"[{current_agent}] {stripped}")

            process.wait()
        except KeyboardInterrupt:
            process.kill()
            raise
        except Exception:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()
        finally:
            watchdog.cancel()
            if process.poll() is None:
                process.terminate()

        if killed_by_watchdog.is_set():
            strat_logger.error("Orchestrator timed out after 900s.")
            print_error("Orchestrator timed out after 15 minutes.")
            return False, run_dir

        missing_log_tokens = _LOG_TOKENS - seen_log_tokens
        if missing_log_tokens:
            strat_logger.warning(
                f"Sub-agent log tokens missing: {', '.join(sorted(missing_log_tokens))}"
            )
            print_warning(
                "Agent log tokens not seen in output: "
                + ", ".join(sorted(missing_log_tokens))
            )

        if process.returncode == 0 and completion_token_found:
            strat_logger.info("Orchestrator completed successfully (token verified).")
            return True, run_dir
        elif process.returncode == 0 and not completion_token_found:
            strat_logger.error(
                "Orchestrator exited 0 but no INTEGRATION_PASS/FALLBACK token found. "
                "Workflow likely stopped mid-pipeline. Treating as failure."
            )
            return False, run_dir
        else:
            strat_logger.error(f"Orchestrator exited with code {process.returncode}")
            return False, run_dir

    except FileNotFoundError:
        strat_logger.error("'claude' command not found.")
        print_error("'claude' CLI not found. Is Claude Code installed and in PATH?")
        return False, run_dir
    except Exception as e:
        strat_logger.exception(f"Unexpected error: {e}")
        return False, run_dir


def copy_artifacts(meta: dict, output_dir: Path, run_dir: Path, pine_file: Path) -> None:
    """Copy generated strategy, test, and log files to the output snapshot directory."""
    safe = meta.get("safe_name", "")
    for src in Path("src/strategies").glob(f"*{safe}*.py"):
        shutil.copy2(src, output_dir / "strategy.py")
        logger.info(f"Copied strategy: {src}")
        break
    for src in Path("tests/strategies").glob(f"test_*{safe}*.py"):
        shutil.copy2(src, output_dir / "test_strategy.py")
        logger.info(f"Copied test: {src}")
        break
    run_log = run_dir / "run.log"
    if run_log.exists():
        shutil.copy2(run_log, output_dir / "run.log")

    sidecar = pine_file.with_suffix(".meta.json")
    if sidecar.exists():
        shutil.copy2(sidecar, output_dir / "metadata.json")
        logger.info(f"Copied metadata sidecar: {sidecar}")

    missing = missing_agent_logs(output_dir)
    if missing:
        logger.warning(
            f"[LOGGING] Missing agent decision logs in {output_dir}: "
            + ", ".join(missing)
        )
        print_warning(
            f"Agent logs missing from output snapshot: {', '.join(missing)}"
        )


def missing_agent_logs(output_dir: Path) -> list[str]:
    return [name for name in _EXPECTED_AGENT_LOGS if not (output_dir / name).exists()]


def verify_artifacts(safe_name: str, output_dir: Path | None = None) -> bool:
    """
    Verify that the expected strategy and test files exist on disk and that
    the output snapshot contains the expected sub-agent logs.

    Returns True if both are found.
    """
    # Guard against double suffix: if safe_name already ends with '_strategy', don't append again
    if safe_name.endswith("_strategy"):
        filename = f"{safe_name}.py"
    else:
        filename = f"{safe_name}_strategy.py"
    strategy_file = Path("src/strategies") / filename
    test_files = list(Path("tests/strategies").glob(f"test_*{safe_name}*.py"))

    if not strategy_file.exists():
        logger.error(f"Artifact check failed: strategy file not found at {strategy_file}")
        return False
    if not test_files:
        logger.error(f"Artifact check failed: no test file found for {safe_name}")
        return False

    if output_dir is not None:
        missing = missing_agent_logs(output_dir)
        if missing:
            logger.warning(
                f"Agent decision logs missing from snapshot (non-fatal): {', '.join(missing)}"
            )

    logger.info(f"Artifact check passed: {strategy_file}, {test_files[0]}")
    return True