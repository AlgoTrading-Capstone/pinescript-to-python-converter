"""Clean active pipeline state after reviewing audit output.

Default mode is dry-run.  Use ``--apply`` to mutate files/registry.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, UTC
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.pipeline import REGISTRY_PATH, TERMINAL_STATUSES
from src.pipeline.archiver import archive_strategy_bundle


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8-sig")) if REGISTRY_PATH.exists() else {}


def _save_registry(registry: dict) -> None:
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)


def cleanup_registry(registry: dict, *, apply: bool = False) -> list[dict]:
    actions: list[dict] = []
    for key, rec in registry.items():
        status = rec.get("status")
        file_path = Path(str(rec.get("file_path") or ""))
        score = int(rec.get("btc_score", 0) or 0) + int(rec.get("project_score", 0) or 0)

        if status == "evaluated" and score == 0 and file_path.exists():
            action = {
                "action": "archive_zero_score_evaluated",
                "key": key,
                "from": str(file_path),
                "to_status": "rejected",
            }
            if apply:
                dest = archive_strategy_bundle(file_path, subdir="rejected")
                rec.update({
                    "status": "rejected",
                    "rejected_at": _now_iso(),
                    "file_path": str(dest),
                    "rejection_reason": rec.get("recommendation_reason") or "Zero-score evaluation.",
                })
                action["to"] = str(dest)
            actions.append(action)
            continue

        if status in TERMINAL_STATUSES and str(file_path).startswith("input") and file_path.exists():
            subdir = "rejected" if status in {"rejected", "statistically_rejected"} else ""
            action = {
                "action": "archive_terminal_input_file",
                "key": key,
                "status": status,
                "from": str(file_path),
            }
            if apply:
                dest = archive_strategy_bundle(file_path, subdir=subdir)
                rec["file_path"] = str(dest)
                rec["archived_at"] = rec.get("archived_at") or _now_iso()
                action["to"] = str(dest)
            actions.append(action)
            continue

        if file_path and str(file_path) != "." and not file_path.exists():
            action = {
                "action": "mark_missing_file_reference",
                "key": key,
                "status": status,
                "missing": str(file_path),
            }
            if apply and status == "evaluated" and score == 0:
                rec.update({
                    "status": "rejected",
                    "rejected_at": _now_iso(),
                    "rejection_reason": rec.get("recommendation_reason") or "Missing zero-score input file.",
                })
                action["to_status"] = "rejected"
            actions.append(action)
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually mutate registry/files.")
    parser.add_argument("--json", action="store_true", help="Print actions as JSON.")
    args = parser.parse_args()

    registry = _load_registry()
    actions = cleanup_registry(registry, apply=args.apply)
    if args.apply and actions:
        _save_registry(registry)

    if args.json:
        print(json.dumps({"applied": args.apply, "actions": actions}, indent=2, ensure_ascii=False))
    else:
        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"Pipeline cleanup ({mode}): {len(actions)} action(s)")
        for action in actions[:50]:
            print(json.dumps(action, ensure_ascii=False))
        if len(actions) > 50:
            print(f"... {len(actions) - 50} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
