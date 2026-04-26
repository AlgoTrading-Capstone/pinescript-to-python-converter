"""Rank all gate-evaluated strategies so the RL trainer can pick the best.

Scans `output/*/*/eval/stats_report.json` (the per-run artifacts written by
`run_statistical_gate`), keeps the most recent report per `safe_name`,
filters to gate-passed strategies, computes a composite RL-fitness score,
and emits:

  * `output/leaderboard/leaderboard.md`          — ranked markdown table
  * `output/leaderboard/leaderboard.json`        — machine-readable ranking
  * `output/leaderboard/winrate_comparison.png`  — cross-strategy bar chart

Composite RL-fitness score (higher is better):
    score = win_rate * avg_pnl_bps * sqrt(total_trades / min_trades)
where avg_pnl_bps = avg_pnl * 10_000.

This favors strategies that combine a directional edge (avg_pnl) with
a consistent hit-rate (win_rate) and enough samples for RL to generalize
(total_trades).

Usage:
    .venv/Scripts/python.exe scripts/rank_strategies.py
    .venv/Scripts/python.exe scripts/rank_strategies.py --include-rejected
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if isinstance(_stream, io.TextIOWrapper) and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8", errors="replace")

from src.pipeline import MIN_TRADE_COUNT, OUTPUT_DIR
from src.evaluation.plots.winrate_curve import create_winrate_barchart


LEADERBOARD_DIR = OUTPUT_DIR / "leaderboard"


@dataclass
class Entry:
    safe_name: str
    strategy_name: str
    passed: bool
    reason: str | None
    win_rate: float
    total_trades: int
    avg_pnl: float
    signal_activity_pct: float
    evaluated_at: str
    report_path: Path
    score: float

    def to_row(self) -> dict:
        return {
            "safe_name": self.safe_name,
            "strategy_name": self.strategy_name,
            "passed": self.passed,
            "reason": self.reason,
            "win_rate": self.win_rate,
            "total_trades": self.total_trades,
            "avg_pnl": self.avg_pnl,
            "signal_activity_pct": self.signal_activity_pct,
            "score": self.score,
            "evaluated_at": self.evaluated_at,
            "report_path": str(self.report_path),
        }


def _fitness_score(win_rate: float, avg_pnl: float, total_trades: int) -> float:
    """Composite RL-fitness score (see module docstring)."""
    if total_trades <= 0:
        return 0.0
    avg_pnl_bps = avg_pnl * 10_000.0
    trade_sqrt = math.sqrt(max(total_trades, 1) / max(MIN_TRADE_COUNT, 1))
    return win_rate * avg_pnl_bps * trade_sqrt


def _parse_safe_name_from_path(report_path: Path) -> str:
    # Path shape: output/<safe_name>/<timestamp>/eval/stats_report.json
    try:
        return report_path.parents[2].name
    except IndexError:
        return report_path.stem


def _load_report(path: Path) -> Entry | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"[warn] skipping unreadable {path}: {exc}", file=sys.stderr)
        return None

    winrate = payload.get("winrate", {}) or {}
    variance = payload.get("variance", {}) or {}
    safe_name = _parse_safe_name_from_path(path)
    wr = float(winrate.get("win_rate", 0.0) or 0.0)
    trades = int(winrate.get("total_trades", 0) or 0)
    avg_pnl = float(winrate.get("avg_pnl", 0.0) or 0.0)

    return Entry(
        safe_name=safe_name,
        strategy_name=payload.get("strategy_name", safe_name),
        passed=bool(payload.get("passed", False)),
        reason=payload.get("reason"),
        win_rate=wr,
        total_trades=trades,
        avg_pnl=avg_pnl,
        signal_activity_pct=float(variance.get("signal_activity_pct", 0.0) or 0.0),
        evaluated_at=str(payload.get("evaluated_at", "")),
        report_path=path,
        score=_fitness_score(wr, avg_pnl, trades),
    )


def _keep_latest_per_safe_name(entries: list[Entry]) -> list[Entry]:
    latest: dict[str, Entry] = {}
    for e in entries:
        prior = latest.get(e.safe_name)
        if prior is None or e.evaluated_at > prior.evaluated_at:
            latest[e.safe_name] = e
    return list(latest.values())


def _render_markdown(entries: list[Entry]) -> str:
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Strategy Leaderboard",
        "",
        f"_Generated {ts}. Ranked by composite RL-fitness score "
        f"`win_rate * avg_pnl_bps * sqrt(total_trades / {MIN_TRADE_COUNT})`._",
        "",
        "| Rank | Strategy | Gate | Win rate | Trades | Avg PnL (bps) | Activity | Score |",
        "|------|----------|------|----------|--------|---------------|----------|-------|",
    ]
    for rank, e in enumerate(entries, start=1):
        gate = "PASS" if e.passed else f"REJECT ({e.reason or '—'})"
        lines.append(
            f"| {rank} | `{e.safe_name}` | {gate} | "
            f"{e.win_rate:.1%} | {e.total_trades:,} | "
            f"{e.avg_pnl * 10_000:+.2f} | {e.signal_activity_pct:.2%} | "
            f"{e.score:+.2f} |"
        )
    lines.append("")
    lines.append("## Report paths")
    lines.append("")
    for e in entries:
        lines.append(f"- `{e.safe_name}` -> `{e.report_path.as_posix()}`")
    return "\n".join(lines) + "\n"


def _write_barchart(entries: list[Entry], out_path: Path) -> Path | None:
    if not entries:
        return None
    all_stats = {
        e.safe_name: {
            "win_rate": e.win_rate,
            "total_trades": e.total_trades,
            "avg_pnl": e.avg_pnl,
        }
        for e in entries
    }
    return create_winrate_barchart(
        all_stats=all_stats,
        out_path=out_path,
        title="Gate-evaluated strategies — win rate",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-rejected",
        action="store_true",
        help="Include gate-rejected strategies in the leaderboard (default: PASS only).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=OUTPUT_DIR,
        help="Root directory to scan for eval/stats_report.json (default: output/).",
    )
    args = parser.parse_args()

    report_paths = sorted(args.output_root.glob("*/*/eval/stats_report.json"))
    if not report_paths:
        print(f"No reports found under {args.output_root}/*/*/eval/stats_report.json",
              file=sys.stderr)
        return 1

    raw_entries = [_load_report(p) for p in report_paths]
    entries = [e for e in raw_entries if e is not None]
    entries = _keep_latest_per_safe_name(entries)

    if not args.include_rejected:
        entries = [e for e in entries if e.passed]

    entries.sort(key=lambda e: (e.score, e.win_rate, e.total_trades), reverse=True)

    LEADERBOARD_DIR.mkdir(parents=True, exist_ok=True)

    md_path = LEADERBOARD_DIR / "leaderboard.md"
    md_path.write_text(_render_markdown(entries), encoding="utf-8")

    json_path = LEADERBOARD_DIR / "leaderboard.json"
    json_path.write_text(
        json.dumps([e.to_row() for e in entries], indent=2, default=str),
        encoding="utf-8",
    )

    chart_path = _write_barchart(entries, LEADERBOARD_DIR / "winrate_comparison.png")

    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    if chart_path is not None:
        print(f"Wrote {chart_path}")

    if not entries:
        print("(leaderboard is empty — no strategies matched the filter)")
    else:
        print("\nTop strategies for RL:")
        for rank, e in enumerate(entries[:10], start=1):
            gate = "PASS" if e.passed else "REJECT"
            print(
                f"  {rank:>2}. {e.safe_name:<40s} "
                f"{gate:<6s} wr={e.win_rate:.1%} "
                f"trades={e.total_trades:>6,d} "
                f"avg_pnl={e.avg_pnl * 10_000:+.2f}bps "
                f"score={e.score:+.3f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
