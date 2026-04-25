"""Audit pipeline state for stale files, contract drift, and bad artifacts.

This is intentionally read-only.  It gives a production-readiness snapshot
without moving files or rewriting the registry.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.pipeline import REGISTRY_PATH


def _load_registry(path: Path = REGISTRY_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def audit_registry_paths(registry: dict) -> list[dict]:
    findings = []
    for key, rec in registry.items():
        file_path = rec.get("file_path")
        if file_path and not Path(file_path).exists():
            findings.append({
                "type": "missing_registry_file",
                "key": key,
                "status": rec.get("status"),
                "path": file_path,
            })
        output_dir = rec.get("output_dir")
        if output_dir and not Path(output_dir).exists():
            findings.append({
                "type": "missing_output_dir",
                "key": key,
                "status": rec.get("status"),
                "path": output_dir,
            })
    return findings


def audit_strategy_tests() -> list[dict]:
    findings = []
    strategies = {p.stem for p in Path("src/strategies").glob("*_strategy.py")}
    for test_path in Path("tests/strategies").glob("test_*_strategy.py"):
        strategy_stem = test_path.stem.removeprefix("test_")
        if strategy_stem not in strategies:
            findings.append({
                "type": "stale_strategy_test",
                "test": str(test_path),
                "missing_strategy": f"src/strategies/{strategy_stem}.py",
            })
    return findings


def audit_agent_contract_prompts() -> list[dict]:
    findings = []
    prompt_path = Path(".claude/agents/test_generator.md")
    if prompt_path.exists():
        text = prompt_path.read_text(encoding="utf-8", errors="replace")
        if "strategy.run(" in text or "`run()`" in text:
            findings.append({
                "type": "old_run_contract_prompt",
                "path": str(prompt_path),
                "detail": "Prompt still references run() instead of generate_all_signals/step.",
            })
        if "StrategyRecommendation" in text:
            findings.append({
                "type": "old_strategy_recommendation_prompt",
                "path": str(prompt_path),
                "detail": "Prompt still references StrategyRecommendation for generated tests.",
            })
    return findings


def audit_eval_artifacts() -> list[dict]:
    findings = []
    for stats_path in Path("output").glob("*/*/eval/stats_report.json"):
        try:
            payload = json.loads(stats_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            findings.append({
                "type": "unreadable_stats_report",
                "path": str(stats_path),
                "detail": str(exc),
            })
            continue
        eval_dir = stats_path.parent
        artifacts = payload.get("artifacts") or {}
        for expected in ("stats_report", "heatmap"):
            if expected not in artifacts:
                findings.append({
                    "type": "missing_artifact_reference",
                    "path": str(stats_path),
                    "artifact": expected,
                })
        if not (eval_dir / "signal_heatmap.png").exists():
            findings.append({
                "type": "missing_eval_file",
                "path": str(eval_dir / "signal_heatmap.png"),
            })
        if (payload.get("winrate") or {}).get("total_trades", 0) and not (
            eval_dir / "winrate_curve.png"
        ).exists():
            findings.append({
                "type": "missing_eval_file",
                "path": str(eval_dir / "winrate_curve.png"),
            })
    return findings


def run_audit() -> dict:
    registry = _load_registry()
    sections = {
        "registry_paths": audit_registry_paths(registry),
        "strategy_tests": audit_strategy_tests(),
        "agent_contract_prompts": audit_agent_contract_prompts(),
        "eval_artifacts": audit_eval_artifacts(),
    }
    return {
        "summary": {name: len(items) for name, items in sections.items()},
        "sections": sections,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()
    report = run_audit()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print("Pipeline State Audit")
    for name, count in report["summary"].items():
        print(f"- {name}: {count}")
    for name, findings in report["sections"].items():
        if not findings:
            continue
        print(f"\n[{name}]")
        for finding in findings[:25]:
            print(json.dumps(finding, ensure_ascii=False))
        if len(findings) > 25:
            print(f"... {len(findings) - 25} more")
    return 1 if any(report["summary"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
