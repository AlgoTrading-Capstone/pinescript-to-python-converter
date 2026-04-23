"""Re-run the statistical gate for one or more already-transpiled strategies.

Use when the gate crashed or was skipped in a prior run and benchmarks
(`stats_report.json`, `signal_heatmap.png`) need to be produced without
re-running the full converter pipeline. Loads each strategy dynamically
by `safe_name`, runs `run_statistical_gate`, and updates the registry
`evaluation` block to reflect the outcome.

Usage:
    .venv/Scripts/python.exe scripts/rerun_statistical_gate.py \
        ema5_breakout_target_shifting_mtf gold_mtf_dashboard
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

# Ensure `python scripts/...` also works when executed from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Windows default stdout (cp1252) can't emit non-ASCII characters that downstream
# loggers may produce; force UTF-8 so logs round-trip cleanly.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if isinstance(_stream, io.TextIOWrapper) and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8", errors="replace")

from src.evaluation.loader import StrategyLoadError, load_strategy_by_safe_name
from src.pipeline import OUTPUT_DIR
from src.pipeline.registry import load_registry, save_registry
from src.pipeline.statistical_gate import GateResult, run_statistical_gate


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _find_registry_key(registry: dict, safe_name: str) -> str | None:
    for key, entry in registry.items():
        meta = entry.get("pine_metadata") or {}
        if meta.get("safe_name") == safe_name:
            return key
    return None


def _apply_gate_result_to_registry(
    registry: dict,
    key: str,
    safe_name: str,
    result: GateResult,
    output_dir: Path,
) -> None:
    entry = registry[key]
    evaluation_block = result.to_registry_block()
    if result.passed:
        entry.update({
            "status":      "completed",
            "converted_at": _now_iso(),
            "output_dir":  str(output_dir),
            "evaluation":  evaluation_block,
        })
    else:
        entry.update({
            "status":       "statistically_rejected",
            "rejected_at":  _now_iso(),
            "output_dir":   str(output_dir),
            "evaluation":   evaluation_block,
            # Gate failure is not a conversion failure — reset the counter
            # so the strategy can be retried later if scoring thresholds change.
            "conversion_attempts": 0,
        })


def rerun_one(safe_name: str, registry: dict) -> GateResult:
    ts = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = OUTPUT_DIR / safe_name / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[RERUN] {safe_name} -> {out_dir}")
    strategy = load_strategy_by_safe_name(safe_name)
    result = run_statistical_gate(strategy, out_dir)

    key = _find_registry_key(registry, safe_name)
    if key is None:
        print(f"  WARNING: no registry entry with safe_name={safe_name}; skipping registry update")
    else:
        _apply_gate_result_to_registry(registry, key, safe_name, result, out_dir)

    print(
        f"  passed={result.passed} reason={result.reason or 'OK'}\n"
        f"  signal_activity_pct={result.variance.get('signal_activity_pct', 0):.2%} "
        f"(threshold {result.variance.get('threshold', 0):.0%})\n"
        f"  win_rate={result.winrate.get('win_rate', 0):.1%} "
        f"over {result.winrate.get('total_trades', 0)} trades "
        f"(≥{result.winrate.get('min_trades_threshold', 0)} required)\n"
        f"  signal_counts={result.signal_counts}"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "safe_names",
        nargs="+",
        help="One or more strategy safe_names (file stems in src/strategies/ "
             "without the trailing _strategy.py).",
    )
    args = parser.parse_args()

    _configure_logging()
    registry = load_registry()

    any_failure = False
    for safe_name in args.safe_names:
        try:
            rerun_one(safe_name, registry)
        except StrategyLoadError as e:
            print(f"[ERROR] load failed for {safe_name}: {e}", file=sys.stderr)
            any_failure = True
        except Exception as e:
            print(f"[ERROR] gate crashed for {safe_name}: {e}", file=sys.stderr)
            any_failure = True

    save_registry(registry)
    print("\nRegistry saved.")
    return 1 if any_failure else 0


if __name__ == "__main__":
    sys.exit(main())
