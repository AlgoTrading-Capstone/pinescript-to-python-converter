"""Recover pipeline state after over-strict filtering or interrupted runs.

By default this is a dry run. Pass ``--apply`` to write changes.

Current recovery actions:
- roll transient ``selected`` entries back to ``evaluated``;
- remove the six candidates wrongly rejected by the old ``total_trades < 150``
  precheck from the registry;
- remove their TradingView URLs from ``data/seen_urls.json``;
- copy their archived ``.pine`` and ``.meta.json`` bundles back into ``input/``
  so the next pipeline run treats them as fresh candidates.

The archive copies are intentionally kept in place as audit evidence.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path("data/strategies_registry.json")
SEEN_URLS_PATH = Path("data/seen_urls.json")
INPUT_DIR = Path("input")
REJECTED_ARCHIVE_DIR = Path("archive/rejected")

PROMOTED_6 = (
    "BEST-Supertrend-Strategy.pine",
    "Bollinger-Matrix-ULTRA-inverted.pine",
    "EMA-Pullback-Speed-Strategy.pine",
    "ms-hypersupertrend.pine",
    "Pro-Swing-Guard-200-EMA-SuperTrend-10-5-Simple-Swing-System.pine",
    "REAL-STRATEGY-Dow-Factor-MFI-RSI-DVOG-Strategy.pine",
)


@dataclass(frozen=True)
class PlannedAction:
    kind: str
    target: str
    detail: str
    source: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _archive_bundle_dir(pine_name: str) -> Path:
    return REJECTED_ARCHIVE_DIR / Path(pine_name).stem


def _archived_meta_path(pine_name: str) -> Path:
    return _archive_bundle_dir(pine_name) / Path(pine_name).with_suffix(".meta.json").name


def _archived_pine_path(pine_name: str) -> Path:
    return _archive_bundle_dir(pine_name) / pine_name


def _load_archived_url(pine_name: str) -> str | None:
    meta_path = _archived_meta_path(pine_name)
    meta = _read_json(meta_path, {})
    url = meta.get("url") if isinstance(meta, dict) else None
    return str(url) if url else None


def _plan(
    registry: dict[str, dict[str, Any]],
    seen_urls: set[str],
) -> list[PlannedAction]:
    actions: list[PlannedAction] = []

    for key, rec in sorted(registry.items()):
        if rec.get("status") == "selected":
            actions.append(PlannedAction(
                "rollback_selected",
                key,
                "reset transient selected status to evaluated",
            ))

    for pine_name in PROMOTED_6:
        if pine_name in registry:
            actions.append(PlannedAction(
                "remove_registry_entry",
                pine_name,
                "remove wrongly precheck-rejected promoted candidate",
            ))

        url = _load_archived_url(pine_name)
        if url and url in seen_urls:
            actions.append(PlannedAction(
                "remove_seen_url",
                url,
                f"allow {pine_name} to be discovered again",
            ))

        pine_src = _archived_pine_path(pine_name)
        pine_dest = INPUT_DIR / pine_name
        if pine_src.exists() and not pine_dest.exists():
            actions.append(PlannedAction(
                "restore_file",
                str(pine_dest),
                f"copy {pine_src} to input/",
                str(pine_src),
            ))

        meta_src = _archived_meta_path(pine_name)
        meta_dest = INPUT_DIR / meta_src.name
        if meta_src.exists() and not meta_dest.exists():
            actions.append(PlannedAction(
                "restore_file",
                str(meta_dest),
                f"copy {meta_src} to input/",
                str(meta_src),
            ))

    return actions


def _apply(
    registry: dict[str, dict[str, Any]],
    seen_urls: set[str],
    actions: list[PlannedAction],
) -> None:
    for action in actions:
        if action.kind == "rollback_selected":
            rec = registry[action.target]
            rec["status"] = "evaluated"
            rec["rolled_back_at"] = _now()
        elif action.kind == "remove_registry_entry":
            registry.pop(action.target, None)
        elif action.kind == "remove_seen_url":
            seen_urls.discard(action.target)
        elif action.kind == "restore_file":
            if action.source is None:
                raise ValueError(f"Missing source for restore action: {action.target}")
            dest = Path(action.target)
            src = Path(action.source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        else:
            raise ValueError(f"Unknown recovery action: {action.kind}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write registry/seen_urls changes and restore files to input/.",
    )
    args = parser.parse_args()

    if not REGISTRY_PATH.exists():
        print(f"ERROR: {REGISTRY_PATH} not found. Run this from the repo root.", file=sys.stderr)
        return 2

    registry = _read_json(REGISTRY_PATH, {})
    if not isinstance(registry, dict):
        print(f"ERROR: {REGISTRY_PATH} does not contain a JSON object.", file=sys.stderr)
        return 2

    raw_seen = _read_json(SEEN_URLS_PATH, [])
    seen_urls = {str(url) for url in raw_seen} if isinstance(raw_seen, list) else set()

    actions = _plan(registry, seen_urls)
    if not actions:
        print("No recovery actions needed.")
        return 0

    print(f"Planned recovery actions ({len(actions)}):")
    for action in actions:
        print(f"  - [{action.kind}] {action.target}")
        print(f"      {action.detail}")

    if not args.apply:
        print("\nDry run - pass --apply to write changes.")
        return 0

    _apply(registry, seen_urls, actions)
    _write_json(REGISTRY_PATH, registry)
    _write_json(SEEN_URLS_PATH, sorted(seen_urls))
    print(f"\nApplied {len(actions)} recovery action(s).")
    print(f"Updated {REGISTRY_PATH} and {SEEN_URLS_PATH}.")
    print(f"Restored available promoted-six bundles to {INPUT_DIR}/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
