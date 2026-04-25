"""
PineScript-to-Python Converter Pipeline

Entry point. Orchestrates: scrape → evaluate → select → convert → archive.

Lifecycle:
  new -> evaluated -> selected -> converted
  new / evaluated (score < threshold or skipped 2x) -> archived
"""

import argparse
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
    TERMINAL_STATUSES,
    _EXCLUDED_PINE_FILES,
)
from src.evaluation.loader import StrategyLoadError, load_strategy_by_safe_name
from src.pipeline.archiver import archive_remaining, archive_strategy_bundle, purge_rejected_evaluations
from src.pipeline.category_counts import increment_category_count
from src.pipeline.claude_cli import get_claude_cli_path
from src.pipeline.evaluator import run_evaluations
from src.pipeline.manual_ingest import (
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_TIMEFRAME,
    ManualIngestError,
    prepare_manual_strategy_file,
)
from src.pipeline.orchestrator import copy_artifacts, run_integration, run_orchestrator, verify_artifacts
from src.pipeline.pr_sync import sync_pr_closure_to_registry
from src.pipeline.registry import _now_iso, load_registry, save_registry, scan_and_register
from src.pipeline.scraper import run_tv_scraper
from src.pipeline.selector import auto_select_strategy
from src.pipeline.statistical_gate import run_statistical_gate
from src.pipeline.ui import (
    print_artifact_summary,
    print_banner,
    print_error,
    print_info,
    print_section,
    print_success,
    print_warning,
)


# ---------------------------------------------------------------------------
# Logging  (file-only; terminal gets clean print() UI)
# ---------------------------------------------------------------------------
def _setup_file_logger() -> tuple[logging.Logger, Path]:
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
    return lg, log_file


def _latest_scrape_report() -> Path | None:
    report_root = OUTPUT_DIR / "scrape_reports"
    if not report_root.exists():
        return None
    reports = [p for p in report_root.iterdir() if p.is_dir()]
    if not reports:
        return None
    return max(reports, key=lambda p: p.stat().st_mtime)


def _active_pine_count() -> int:
    if not INPUT_DIR.exists():
        return 0
    return len([f for f in INPUT_DIR.glob("*.pine") if f.name not in _EXCLUDED_PINE_FILES])


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
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the PineScript-to-Python converter pipeline.")
    parser.add_argument(
        "--manual",
        type=Path,
        help="Bypass scraping/selector and convert this manually supplied .pine file directly.",
    )
    parser.add_argument("--name", help="Override the strategy name for --manual metadata.")
    parser.add_argument("--url", default="", help="Optional TradingView URL for --manual metadata.")
    parser.add_argument(
        "--timeframe",
        default=DEFAULT_TIMEFRAME,
        help=f"Timeframe metadata for --manual. Default: {DEFAULT_TIMEFRAME}.",
    )
    parser.add_argument(
        "--lookback-bars",
        type=int,
        default=DEFAULT_LOOKBACK_BARS,
        help=f"Lookback metadata for --manual. Default: {DEFAULT_LOOKBACK_BARS}.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    start_time = datetime.now(UTC)
    logger, runner_log = _setup_file_logger()

    print_banner("PineScript -> Python Converter")
    print_info(f"Pipeline started at {start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print_info(f"Runner log: {runner_log}")

    # Held in the outer scope so the shutdown handlers can sanitize it.
    registry: dict = {}
    run_state: dict[str, object] = {
        "status": "started",
        "runner_log": runner_log,
        "registry": Path("data/strategies_registry.json"),
        "input_dir": INPUT_DIR,
        "logs_dir": LOGS_ROOT,
        "output_dir": OUTPUT_DIR,
    }

    def _finish(status: str, exit_code: int = 0) -> None:
        end_time = datetime.now(UTC)
        run_state["status"] = status
        run_state["ended_at_utc"] = end_time.strftime("%Y-%m-%d %H:%M:%S")
        run_state["duration_seconds"] = f"{(end_time - start_time).total_seconds():.1f}"
        run_state["active_input_candidates"] = str(_active_pine_count())
        latest_report = _latest_scrape_report()
        if latest_report is not None:
            run_state["latest_scrape_report"] = latest_report

        rows = [
            ("Status", run_state.get("status")),
            ("Duration seconds", run_state.get("duration_seconds")),
            ("Runner log", run_state.get("runner_log")),
            ("Registry", run_state.get("registry")),
            ("Input candidates", run_state.get("active_input_candidates")),
            ("Input dir", run_state.get("input_dir")),
            ("Output dir", run_state.get("output_dir")),
            ("Latest scrape report", run_state.get("latest_scrape_report")),
            ("Selected strategy", run_state.get("selected_strategy")),
            ("Output snapshot", run_state.get("output_snapshot")),
            ("Eval artifacts", run_state.get("eval_artifacts")),
            ("Conversion logs", run_state.get("conversion_logs")),
            ("Integration log", run_state.get("integration_log")),
            ("Archived Pine", run_state.get("archived_pine")),
            ("Failure detail", run_state.get("failure_detail")),
            ("Ended UTC", run_state.get("ended_at_utc")),
        ]
        print_artifact_summary("Run Summary", rows)
        raise SystemExit(exit_code)

    print_artifact_summary(
        "Run Setup",
        [
            ("Runner log", runner_log),
            ("Registry", run_state["registry"]),
            ("Input dir", INPUT_DIR),
            ("Output dir", OUTPUT_DIR),
            ("Logs dir", LOGS_ROOT),
        ],
    )

    def _run_conversion_pipeline(chosen_key: str, chosen_rec: dict) -> bool:
        meta = chosen_rec["pine_metadata"]
        ts = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
        safe_name = meta.get("safe_name") or chosen_key.replace(".pine", "")
        out_dir = OUTPUT_DIR / safe_name / ts
        out_dir.mkdir(parents=True, exist_ok=True)
        run_state["output_snapshot"] = out_dir
        run_state["eval_artifacts"] = out_dir / "eval"

        print_section("Conversion")
        success, run_dir = run_orchestrator(Path(chosen_rec["file_path"]), meta, out_dir)
        run_state["conversion_logs"] = run_dir
        gate_result = None
        gate_rejected = False

        if success:
            pine_file = Path(chosen_rec["file_path"])
            copy_artifacts(meta, out_dir, run_dir, pine_file)
            if not verify_artifacts(safe_name, out_dir):
                logger.error("Token indicated success but artifacts are missing.")
                success = False

        if success:
            print_section("Statistical Gate")
            try:
                strategy = load_strategy_by_safe_name(safe_name)
                gate_result = run_statistical_gate(strategy, out_dir)
            except StrategyLoadError as e:
                logger.error(f"Could not load strategy '{safe_name}' for gate: {e}")
                print_error(f"Strategy loader failed: {e}")
                run_state["failure_detail"] = f"Strategy loader failed: {e}"
                success = False
            except Exception as e:
                logger.exception(f"Statistical gate crashed for '{safe_name}': {e}")
                print_error(f"Statistical gate crashed: {e}")
                run_state["failure_detail"] = f"Statistical gate crashed: {e}"
                success = False

            if gate_result is not None and not gate_result.passed:
                print_warning(f"Strategy rejected by statistical gate: {gate_result.reason}")
                pine_src = Path(chosen_rec["file_path"])
                new_file_path = str(pine_src)
                if pine_src.exists():
                    try:
                        new_file_path = str(archive_strategy_bundle(pine_src, subdir="rejected"))
                    except OSError as archive_err:
                        logger.warning(f"Could not archive gate-rejected .pine: {archive_err}")
                run_state["archived_pine"] = new_file_path
                run_state["failure_detail"] = gate_result.reason
                registry[chosen_key].update({
                    "status": "statistically_rejected",
                    "rejected_at": _now_iso(),
                    "output_dir": str(out_dir),
                    "file_path": new_file_path,
                    "evaluation": gate_result.to_registry_block(),
                })
                save_registry(registry)
                print_info(f"Eval artifacts -> {out_dir / 'eval'}")
                gate_rejected = True
                success = False
            elif gate_result is not None and gate_result.passed:
                print_success(
                    f"Statistical gate PASSED - "
                    f"activity={gate_result.variance.get('signal_activity_pct', 0):.1%}, "
                    f"winrate={gate_result.winrate.get('win_rate', 0):.1%} "
                    f"over {gate_result.winrate.get('total_trades', 0)} trades"
                )

        if success:
            print_section("Integration")
            integration_ok = run_integration(
                strategy_path=Path("src/strategies") / f"{safe_name}_strategy.py",
                test_path=Path("tests/strategies") / f"test_{safe_name}_strategy.py",
                output_snapshot=out_dir,
                safe_name=safe_name,
            )
            if not integration_ok:
                print_error("Integration failed after a passing gate.")
                run_state["integration_log"] = out_dir / "agent_integration.md"
                run_state["failure_detail"] = "Integration failed after a passing statistical gate."
                _finish("integration_failed", 1)

            pine_src = Path(chosen_rec["file_path"])
            archived_pine = archive_strategy_bundle(pine_src)
            new_file_path = str(archived_pine)
            run_state["archived_pine"] = new_file_path
            run_state["integration_log"] = out_dir / "agent_integration.md"

            registry[chosen_key].update({
                "status": "completed",
                "converted_at": _now_iso(),
                "archived_at": _now_iso(),
                "output_dir": str(out_dir),
                "file_path": new_file_path,
                "evaluation": gate_result.to_registry_block() if gate_result else {},
            })
            increment_category_count(registry[chosen_key].get("category"))
            save_registry(registry)
            print_success("Conversion complete!")
            print_info(f"Artifacts -> {out_dir}")
            return True

        if gate_rejected:
            _finish("statistically_rejected", 0)

        rec = registry[chosen_key]
        rec["conversion_attempts"] = rec.get("conversion_attempts", 0) + 1
        rec.update({"status": "failed", "failed_at": _now_iso()})
        save_registry(registry)
        print_error(f"Orchestrator failed. See: {run_dir / 'run.log'}")
        run_state["failure_detail"] = f"Orchestrator failed. See {run_dir / 'run.log'}"
        _finish("conversion_failed", 1)
        return False

    try:
        if args.manual is not None:
            print_section("Manual Direct Conversion")
            try:
                manual = prepare_manual_strategy_file(
                    args.manual,
                    name=args.name,
                    url=args.url,
                    timeframe=args.timeframe,
                    lookback_bars=args.lookback_bars,
                    input_dir=INPUT_DIR,
                )
            except ManualIngestError as exc:
                print_error(f"Manual strategy rejected: {exc}")
                run_state["failure_detail"] = str(exc)
                _finish("manual_ingest_failed", 1)

            registry = load_registry()
            chosen_key = manual.pine_path.name
            existing = registry.get(chosen_key, {})
            if existing.get("status") in TERMINAL_STATUSES:
                print_error(
                    f"Manual strategy '{chosen_key}' is already terminal "
                    f"({existing.get('status')}); refusing to rerun it."
                )
                run_state["failure_detail"] = f"Terminal registry status: {existing.get('status')}"
                _finish("manual_terminal_status", 1)

            registry[chosen_key] = {
                **existing,
                "file_path": str(manual.pine_path),
                "status": "selected",
                "registered_at": existing.get("registered_at") or _now_iso(),
                "selected_at": _now_iso(),
                "origin": "manual",
                "selection_mode": "manual_direct",
                "evaluation_status": "bypassed_selector",
                "pine_metadata": manual.metadata,
                "category": "Manual",
                "btc_score": None,
                "project_score": None,
                "recommendation_reason": "Manual direct conversion bypassed selector.",
                "source_triage_status": "accepted",
                "source_triage_reason": manual.source_triage_reason,
            }
            save_registry(registry)
            run_state["selected_strategy"] = chosen_key
            print_info(f"Manual strategy accepted: {manual.pine_path}")
            print_info("Skipping scraper, selector scoring, and auto-selection.")

            if _run_conversion_pipeline(chosen_key, registry[chosen_key]):
                _finish("completed", 0)

        # Step 0 — Ensure at least TARGET_STRATEGY_COUNT real .pine files in input/
        INPUT_DIR.mkdir(exist_ok=True)
        existing = [f for f in INPUT_DIR.glob("*.pine") if f.name not in _EXCLUDED_PINE_FILES]
        scrape_attempt = 0
        while len(existing) < TARGET_STRATEGY_COUNT and scrape_attempt < MAX_SEARCH_LOOPS:
            needed = TARGET_STRATEGY_COUNT - len(existing)
            scrape_attempt += 1
            print_info(
                f"Scrape attempt {scrape_attempt}/{MAX_SEARCH_LOOPS}: "
                f"need {needed} promoted candidate(s)."
            )
            run_tv_scraper(max_results=needed, exit_on_empty=False)
            existing = [f for f in INPUT_DIR.glob("*.pine") if f.name not in _EXCLUDED_PINE_FILES]
        if len(existing) < TARGET_STRATEGY_COUNT:
            print_error(
                f"Only {len(existing)} candidate(s) reached input/ after "
                f"{MAX_SEARCH_LOOPS} scrape attempt(s)."
            )
            run_state["failure_detail"] = "Scraper did not promote enough candidates."
            _finish("scrape_exhausted", 1)

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
            run_state["failure_detail"] = "Claude CLI was not found in PATH."
            _finish("missing_claude_cli", 1)
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
            run_tv_scraper(max_results=TARGET_STRATEGY_COUNT, exit_on_empty=False)
            registry = scan_and_register(registry)
            registry = run_evaluations(registry)
        else:
            print_error(f"Could not find a valid strategy after {MAX_SEARCH_LOOPS} attempts.")
            run_state["failure_detail"] = "No evaluated strategy cleared the selection floor."
            _finish("selection_exhausted", 1)

        registry[chosen_key]["status"] = "selected"
        save_registry(registry)
        run_state["selected_strategy"] = chosen_key

        if _run_conversion_pipeline(chosen_key, chosen_rec):
            print_section("Archive")
            print_info("Archiving low-scoring / stale strategies...")
            registry = archive_remaining(registry, chosen_key)
            save_registry(registry)
            print_success("Done.")
            _finish("completed", 0)


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
        run_state["failure_detail"] = "Interrupted by Ctrl+C."
        _finish("interrupted", 1)

    except SystemExit:
        # _finish() raises SystemExit after printing the final artifact table.
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
        run_state["failure_detail"] = f"{type(e).__name__}: {e}"
        _finish("crashed", 1)


if __name__ == "__main__":
    main()
