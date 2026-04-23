"""Registry recovery helper for the 2026-04-23 BB+RSI false-failure run.

The half-applied refactor on `feat/supertrend_ema_rejection_strategy` made
`main.py` exit 1 on what was actually a successful conversion. Before exiting,
the old failure path in main.py bumped ``BB-RSI-1-2-Rentable-en-4-horas.pine``
to ``status="failed"`` with ``conversion_attempts=1``. The strategy code, tests,
and PR #42 are all real; only the registry drifted.

This one-off script does two things:

1. **General zombie sweep** — any entry stuck in the transient ``"selected"``
   status is reset to ``"evaluated"`` (same rollback ``main.py`` runs on
   Ctrl+C).
2. **Targeted BB-RSI reset** — flip
   ``BB-RSI-1-2-Rentable-en-4-horas.pine`` back to ``"evaluated"``, clear
   ``conversion_attempts`` / ``failed_at``, so the next pipeline run treats it
   as a fresh candidate. With the new ``max_drawdown_pct > 50.0`` deterministic
   rule in :func:`src.pipeline.evaluator._deterministic_rejection`, its 107.32%
   drawdown will cause it to be rejected before the selector LLM is even
   called — which is the correct outcome.

Usage:

.. code-block:: bash

    .venv/Scripts/python.exe scripts/recovery.py            # dry-run, prints plan
    .venv/Scripts/python.exe scripts/recovery.py --apply    # write changes
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REGISTRY_PATH = Path("data/strategies_registry.json")
BB_RSI_KEY = "BB-RSI-1-2-Rentable-en-4-horas.pine"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def plan_changes(registry: dict) -> list[tuple[str, str, dict]]:
    """Return a list of (key, reason, patch) tuples describing what will change."""
    changes: list[tuple[str, str, dict]] = []

    for key, rec in registry.items():
        if rec.get("status") == "selected":
            changes.append((
                key,
                "zombie 'selected' status — roll back to 'evaluated'",
                {"status": "evaluated", "rolled_back_at": _now()},
            ))

    bb_rsi = registry.get(BB_RSI_KEY)
    if bb_rsi and bb_rsi.get("status") == "failed":
        changes.append((
            BB_RSI_KEY,
            f"false-failure from 2026-04-23 run "
            f"(attempts={bb_rsi.get('conversion_attempts', 0)}) — reset to 'evaluated'",
            {
                "status": "evaluated",
                "conversion_attempts": 0,
                "failed_at": None,
                "rolled_back_at": _now(),
            },
        ))

    return changes


def apply_changes(registry: dict, changes: list[tuple[str, str, dict]]) -> None:
    for key, _reason, patch in changes:
        rec = registry[key]
        for k, v in patch.items():
            if v is None:
                rec.pop(k, None)
            else:
                rec[k] = v


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to data/strategies_registry.json. Without this flag, runs dry.",
    )
    args = parser.parse_args()

    if not REGISTRY_PATH.exists():
        print(f"ERROR: {REGISTRY_PATH} not found. Run this from the repo root.", file=sys.stderr)
        return 2

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    changes = plan_changes(registry)

    if not changes:
        print("No changes needed — registry is clean.")
        return 0

    print(f"Planned changes ({len(changes)}):")
    for key, reason, patch in changes:
        print(f"  - {key}")
        print(f"      reason: {reason}")
        print(f"      patch : {patch}")

    if not args.apply:
        print("\nDry run — pass --apply to write changes.")
        return 0

    apply_changes(registry, changes)
    REGISTRY_PATH.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nApplied {len(changes)} change(s) to {REGISTRY_PATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())