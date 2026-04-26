"""Build cross-strategy signal and win-rate dashboards from gate reports.

Outputs:
  output/leaderboard/raw_strategy_signals_heatmap.png
  output/leaderboard/raw_strategy_winrates.png
  output/leaderboard/raw_strategy_dashboard.json

The signal heatmap can only include strategies that still exist in
``src/strategies`` and can be loaded by the dynamic loader.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import matplotlib

matplotlib.use("Agg")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.evaluation.loader import StrategyLoadError, load_strategy_by_safe_name
from src.evaluation.ohlcv import fetch_range
from src.evaluation.plots.heatmap import render_heatmap
from src.evaluation.plots.winrate_curve import create_winrate_barchart
from src.evaluation.runner import StrategyContractError, generate_signals_for_strategy
from src.evaluation.winrate import compute_winrate
from src.pipeline import EVAL_END, EVAL_EXCHANGE, EVAL_START, EVAL_SYMBOL, EVAL_TIMEFRAME, OUTPUT_DIR


LEADERBOARD_DIR = OUTPUT_DIR / "leaderboard"


@dataclass
class ReportEntry:
    safe_name: str
    strategy_name: str
    passed: bool
    reason: str | None
    evaluated_at: str
    report_path: Path
    winrate: dict
    variance: dict


def _safe_name_from_report(path: Path) -> str:
    try:
        return path.parents[2].name
    except IndexError:
        return path.stem


def _load_latest_reports(output_root: Path) -> list[ReportEntry]:
    latest: dict[str, ReportEntry] = {}
    for path in sorted(output_root.glob("*/*/eval/stats_report.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        safe_name = _safe_name_from_report(path)
        entry = ReportEntry(
            safe_name=safe_name,
            strategy_name=str(payload.get("strategy_name") or safe_name),
            passed=bool(payload.get("passed", False)),
            reason=payload.get("reason"),
            evaluated_at=str(payload.get("evaluated_at") or ""),
            report_path=path,
            winrate=dict(payload.get("winrate") or {}),
            variance=dict(payload.get("variance") or {}),
        )
        prior = latest.get(safe_name)
        if prior is None or entry.evaluated_at > prior.evaluated_at:
            latest[safe_name] = entry
    return sorted(latest.values(), key=lambda e: e.evaluated_at, reverse=True)


def _stats_for_barchart(entries: list[ReportEntry]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for entry in entries:
        if not entry.winrate:
            continue
        stats[entry.strategy_name] = {
            "win_rate": float(entry.winrate.get("win_rate", 0.0) or 0.0),
            "total_trades": int(entry.winrate.get("total_trades", 0) or 0),
            "avg_pnl": float(entry.winrate.get("avg_pnl", 0.0) or 0.0),
        }
    return stats


def build_dashboards(
    *,
    output_root: Path = OUTPUT_DIR,
    dashboard_dir: Path = LEADERBOARD_DIR,
    max_strategies: int | None = None,
    render_signals: bool = True,
) -> dict:
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    entries = _load_latest_reports(output_root)
    if max_strategies is not None:
        entries = entries[:max_strategies]

    summary = {
        "reports": [
            {
                "safe_name": e.safe_name,
                "strategy_name": e.strategy_name,
                "passed": e.passed,
                "reason": e.reason,
                "report_path": str(e.report_path),
                "win_rate": e.winrate.get("win_rate"),
                "total_trades": e.winrate.get("total_trades"),
                "avg_pnl": e.winrate.get("avg_pnl"),
                "signal_activity_pct": e.variance.get("signal_activity_pct"),
            }
            for e in entries
        ],
        "artifacts": {},
        "signal_errors": {},
    }

    stats = _stats_for_barchart(entries)
    chart_path = create_winrate_barchart(
        all_stats=stats,
        out_path=dashboard_dir / "raw_strategy_winrates.png",
        title="Raw/gate signal win rate by strategy",
    )
    if chart_path is not None:
        summary["artifacts"]["winrates"] = str(chart_path)

    if render_signals and entries:
        ohlcv = fetch_range(
            exchange_name=EVAL_EXCHANGE,
            symbol=EVAL_SYMBOL,
            timeframe=EVAL_TIMEFRAME,
            start=EVAL_START,
            end=EVAL_END,
        )
        signal_cols: dict[str, pd.Series] = {}
        for entry in entries:
            try:
                strategy = load_strategy_by_safe_name(entry.safe_name)
                signal_cols[strategy.__class__.__name__] = generate_signals_for_strategy(
                    strategy, ohlcv
                )
            except (StrategyLoadError, StrategyContractError, Exception) as exc:
                summary["signal_errors"][entry.safe_name] = f"{type(exc).__name__}: {exc}"

        if signal_cols:
            signals_df = pd.DataFrame(signal_cols, index=ohlcv.index)
            heatmap_path = render_heatmap(
                signals_df=signals_df,
                strategy_cols=list(signal_cols.keys()),
                closes=ohlcv["close"],
                timestamps=pd.Series(ohlcv.index, index=ohlcv.index),
                output_path=dashboard_dir / "raw_strategy_signals_heatmap.png",
            )
            if heatmap_path is not None:
                summary["artifacts"]["signals_heatmap"] = str(heatmap_path)

            # Keep JSON stats in sync with the actually rendered signal set.
            summary["rendered_signal_stats"] = {
                name: compute_winrate(ohlcv["close"], series)
                for name, series in signal_cols.items()
            }

    summary_path = dashboard_dir / "raw_strategy_dashboard.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    summary["artifacts"]["summary"] = str(summary_path)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--dashboard-dir", type=Path, default=LEADERBOARD_DIR)
    parser.add_argument("--max-strategies", type=int, default=None)
    parser.add_argument(
        "--no-signals",
        action="store_true",
        help="Only render the win-rate chart; skip strategy loading/OHLCV signal generation.",
    )
    args = parser.parse_args()
    summary = build_dashboards(
        output_root=args.output_root,
        dashboard_dir=args.dashboard_dir,
        max_strategies=args.max_strategies,
        render_signals=not args.no_signals,
    )
    for name, path in summary.get("artifacts", {}).items():
        print(f"{name}: {path}")
    if summary.get("signal_errors"):
        print("Signal render errors:")
        for safe_name, error in summary["signal_errors"].items():
            print(f"  {safe_name}: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
