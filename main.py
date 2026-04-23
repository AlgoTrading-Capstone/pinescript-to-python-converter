"""
PineScript-to-Python Converter Pipeline

Entry point. Orchestrates: scrape → evaluate → select → convert → archive.

Lifecycle:
  new -> evaluated -> selected -> converted
  new / evaluated (score < threshold or skipped 2x) -> archived
"""

import logging
import sys
from datetime import datetime, UTC
from pathlib import Path

from src.pipeline import (
    INPUT_DIR,
    LOGS_ROOT,
    MAX_SEARCH_LOOPS,
    OUTPUT_DIR,
    TARGET_STRATEGY_COUNT,
    _EXCLUDED_PINE_FILES,
)
from src.evaluation.loader import StrategyLoadError, load_strategy_by_safe_name
from src.pipeline.archiver import archive_remaining, archive_strategy_bundle, purge_rejected_evaluations
from src.pipeline.category_counts import increment_category_count
from src.pipeline.claude_cli import get_claude_cli_path
from src.pipeline.evaluator import run_evaluations
from src.pipeline.orchestrator import copy_artifacts, run_integration, run_orchestrator, verify_artifacts
from src.pipeline.pr_sync import sync_pr_closure_to_registry
from src.pipeline.registry import _now_iso, load_registry, save_registry, scan_and_register
from src.pipeline.scraper import run_tv_scraper
from src.pipeline.selector import auto_select_strategy
from src.pipeline.statistical_gate import run_statistical_gate
from src.pipeline.ui import print_banner, print_error, print_info, print_section, print_success, print_warning


# ---------------------------------------------------------------------------
# Logging  (file-only; terminal gets clean print() UI)
# ---------------------------------------------------------------------------
def _setup_file_logger() -> logging.Logger:
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    log_file = LOGS_ROOT / f"runner_{ts}.log"

    lg = logging.getLogger("runner")
    lg.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    ))
    lg.addHandler(fh)
    lg.propagate = False
    return lg


def _rollback_transient_states(registry: dict, logger: logging.Logger) -> int:
    """Revert any entry left in a transient status back to a safe re-entry point.

    Currently handles only 'selected' -> 'evaluated'. The evaluation scores on
    the record are preserved, so the strategy becomes eligible for auto-selection
    again on the next run. Returns the number of entries reverted.
    """
    reverted = 0
    for key, rec in registry.items():
        if rec.get("status") == "selected":
            rec["status"] = "evaluated"
            rec["rolled_back_at"] = _now_iso()
            logger.warning(f"[SHUTDOWN] Reverted '{key}': selected -> evaluated")
            reverted += 1
    return reverted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    start_time = datetime.now(UTC)
    logger = _setup_file_logger()

    print_banner("PineScript -> Python Converter")
    print_info(f"Pipeline started at {start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    # Held in the outer scope so the shutdown handlers can sanitize it.
    registry: dict = {}

    try:
        # Step 0 — Ensure at least TARGET_STRATEGY_COUNT real .pine files in input/
        INPUT_DIR.mkdir(exist_ok=True)
        existing = [f for f in INPUT_DIR.glob("*.pine") if f.name not in _EXCLUDED_PINE_FILES]
        if len(existing) < TARGET_STRATEGY_COUNT:
            needed = TARGET_STRATEGY_COUNT - len(existing)
            run_tv_scraper(max_results=needed)

        # Step 1 — Scan & Register
        print_section("Registry")
        print_info("Scanning input/ for .pine files...")
        registry = load_registry()
        registry, pr_sync_updates = sync_pr_closure_to_registry(registry)
        if pr_sync_updates:
            save_registry(registry)
            print_info(
                f"GitHub PR sync: updated {pr_sync_updates} registry record(s); "
                "strategies whose PR was closed without merge are permanently rejected."
            )
        registry = scan_and_register(registry)
        save_registry(registry)

        claude_path = get_claude_cli_path()
        if claude_path is None:
            print_error("Claude CLI is required for evaluation and conversion but was not found in PATH.")
            print_info("Install Claude Code and ensure the `claude` command is available before running the pipeline.")
            sys.exit(1)
        print_info(f"Claude CLI detected at {claude_path}")

        # Step 2 — Evaluate new strategies (isolated, one at a time)
        registry = run_evaluations(registry)

        # Step 2b — Purge zero-scored strategies from input/ immediately, so
        # the next run does not re-scan them. Covers LLM rejection,
        # precheck_rejected, and the dissonance-override path.
        registry = purge_rejected_evaluations(registry)
        save_registry(registry)

        # Step 3 — Auto-select the highest-scoring strategy.
        # If no evaluated strategies exist, fetch a fresh batch and retry.
        chosen_key, chosen_rec = None, None
        for _attempt in range(MAX_SEARCH_LOOPS):
            chosen_key, chosen_rec = auto_select_strategy(registry)
            if chosen_key is not None:
                break
            print_warning(f"No valid strategies found (attempt {_attempt + 1}/{MAX_SEARCH_LOOPS}).")
            print_info("Fetching a fresh batch from TradingView...")
            run_tv_scraper(max_results=TARGET_STRATEGY_COUNT)
            registry = scan_and_register(registry)
            registry = run_evaluations(registry)
        else:
            print_error(f"Could not find a valid strategy after {MAX_SEARCH_LOOPS} attempts.")
            sys.exit(1)

        registry[chosen_key]["status"] = "selected"
        save_registry(registry)

        # Step 4 — Transpile (orchestrator → transpiler → validator → test gen → integration)
        meta      = chosen_rec["pine_metadata"]
        ts        = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
        safe_name = meta.get("safe_name") or chosen_key.replace(".pine", "")
        out_dir   = OUTPUT_DIR / safe_name / ts
        out_dir.mkdir(parents=True, exist_ok=True)

        print_section("Conversion")
        success, run_dir = run_orchestrator(Path(chosen_rec["file_path"]), meta, out_dir)
        gate_result = None
        gate_rejected = False

        if success:
            pine_file = Path(chosen_rec["file_path"])
            copy_artifacts(meta, out_dir, run_dir, pine_file)

            # Defense-in-depth: verify artifacts exist even if token was found
            if not verify_artifacts(safe_name, out_dir):
                logger.error("Token indicated success but artifacts are missing.")
                success = False

        if success:
            # Step 4b — Statistical Gate (variance + win-rate on real BTC data).
            # A dead-on-data strategy is terminal ('statistically_rejected') and does
            # NOT consume a conversion attempt — the code was correct, the strategy
            # was just unprofitable. A loader crash or gate exception is a real
            # failure and falls through to the failed-retry path below.
            print_section("Statistical Gate")
            try:
                strategy = load_strategy_by_safe_name(safe_name)
                gate_result = run_statistical_gate(strategy, out_dir)
            except StrategyLoadError as e:
                logger.error(f"Could not load strategy '{safe_name}' for gate: {e}")
                print_error(f"Strategy loader failed: {e}")
                success = False
            except Exception as e:
                logger.exception(f"Statistical gate crashed for '{safe_name}': {e}")
                print_error(f"Statistical gate crashed: {e}")
                success = False

            if gate_result is not None and not gate_result.passed:
                print_warning(f"Strategy rejected by statistical gate: {gate_result.reason}")
                # Move the .pine to archive/rejected/ NOW so it disappears
                # from input/ on this same run.
                pine_src = Path(chosen_rec["file_path"])
                new_file_path = str(pine_src)
                if pine_src.exists():
                    try:
                        new_file_path = str(archive_strategy_bundle(pine_src, subdir="rejected"))
                    except OSError as archive_err:
                        logger.warning(f"Could not archive gate-rejected .pine: {archive_err}")
                registry[chosen_key].update({
                    "status":       "statistically_rejected",
                    "rejected_at":  _now_iso(),
                    "output_dir":   str(out_dir),
                    "file_path":    new_file_path,
                    "evaluation":   gate_result.to_registry_block(),
                })
                save_registry(registry)
                print_info(f"Eval artifacts -> {out_dir / 'eval'}")
                gate_rejected = True
                success = False
            elif gate_result is not None and gate_result.passed:
                print_success(
                    f"Statistical gate PASSED — "
                    f"activity={gate_result.variance.get('signal_activity_pct', 0):.1%}, "
                    f"winrate={gate_result.winrate.get('win_rate', 0):.1%} "
                    f"over {gate_result.winrate.get('total_trades', 0)} trades"
                )

        if success:
            # Step 4c — Integration (branch + PR) runs AFTER a passing gate,
            # in its own subprocess. A PR is never opened for a strategy that
            # subsequently fails the gate, and the gate's .png / .json
            # artifacts are already on disk for the PR body.
            print_section("Integration")
            integration_ok = run_integration(
                strategy_path   = Path("src/strategies") / f"{safe_name}_strategy.py",
                test_path       = Path("tests/strategies") / f"test_{safe_name}_strategy.py",
                output_snapshot = out_dir,
                safe_name       = safe_name,
            )
            if not integration_ok:
                # Tests passed, gate passed, but PR push failed. Keep the
                # registry entry 'selected' so the next run can retry
                # integration only. Do NOT mark completed, do NOT archive.
                print_error("Integration failed after a passing gate.")
                sys.exit(1)

            pine_src = Path(chosen_rec["file_path"])
            archived_pine = archive_strategy_bundle(pine_src)
            new_file_path = str(archived_pine)

            # Single, atomic registry update
            registry[chosen_key].update({
                "status":       "completed",
                "converted_at": _now_iso(),
                "archived_at":  _now_iso(),
                "output_dir":   str(out_dir),
                "file_path":    new_file_path,
                "evaluation":   gate_result.to_registry_block() if gate_result else {},
            })
            increment_category_count(registry[chosen_key].get("category"))
            save_registry(registry)
            print_success("Conversion complete!")
            print_info(f"Artifacts -> {out_dir}")
        elif gate_rejected:
            # Terminal: dead-on-data. Skip archive + PR. Do not retry.
            end_time = datetime.now(UTC)
            print_info(f"Pipeline ended at {end_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            print_info(f"Pipeline took {(end_time - start_time).total_seconds():.1f} seconds")
            sys.exit(0)
        else:
            rec = registry[chosen_key]
            rec["conversion_attempts"] = rec.get("conversion_attempts", 0) + 1
            rec.update({
                "status":    "failed",
                "failed_at": _now_iso(),
            })
            save_registry(registry)
            print_error(f"Orchestrator failed. See: {run_dir / 'run.log'}")
            sys.exit(1)

        # Step 5 — Smart archive
        print_section("Archive")
        print_info("Archiving low-scoring / stale strategies...")
        registry = archive_remaining(registry, chosen_key)
        save_registry(registry)
        end_time = datetime.now(UTC)
        print_success("Done.")
        print_info(f"Pipeline ended at {end_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print_info(f"Pipeline took {(end_time - start_time).total_seconds():.1f} seconds")

    except KeyboardInterrupt:
        print_warning("\n[SHUTDOWN] Ctrl+C received — sanitizing registry state...")
        logger.warning("Pipeline interrupted by KeyboardInterrupt.")
        reverted = _rollback_transient_states(registry, logger)
        try:
            save_registry(registry)
            if reverted:
                print_info(f"[SHUTDOWN] Reverted {reverted} transient entry/entries; registry saved.")
            else:
                print_info("[SHUTDOWN] No transient state found; registry saved.")
        except Exception as save_err:
            logger.exception(f"[SHUTDOWN] Failed to save registry during shutdown: {save_err}")
            print_error(f"[SHUTDOWN] Could not save registry: {save_err}")
        print_warning("[SHUTDOWN] Pipeline aborted safely.")
        sys.exit(1)

    except SystemExit:
        # Explicit sys.exit() calls inside the body have already persisted their
        # own terminal state. Let them pass through untouched.
        raise

    except Exception as e:
        logger.exception(f"Unhandled pipeline error: {e}")
        print_error(f"\n[SHUTDOWN] Pipeline crashed: {type(e).__name__}: {e}")
        reverted = _rollback_transient_states(registry, logger)
        try:
            save_registry(registry)
            if reverted:
                print_info(f"[SHUTDOWN] Reverted {reverted} transient entry/entries; registry saved.")
            else:
                print_info("[SHUTDOWN] No transient state found; registry saved.")
        except Exception as save_err:
            logger.exception(f"[SHUTDOWN] Failed to save registry during crash cleanup: {save_err}")
            print_error(f"[SHUTDOWN] Could not save registry: {save_err}")
        print_error("[SHUTDOWN] Pipeline crashed; state reverted where possible.")
        sys.exit(1)


if __name__ == "__main__":
    main()