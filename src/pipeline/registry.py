"""
Registry — CRUD operations for data/strategies_registry.json.

Lifecycle: new → evaluated → selected → converted → archived
                                      ↘ failed (retry via menu)
"""

import json
import logging
from datetime import datetime, UTC
from pathlib import Path

from src.pipeline import REGISTRY_PATH, INPUT_DIR, TERMINAL_STATUSES, _EXCLUDED_PINE_FILES
from src.pipeline.ui import print_info

logger = logging.getLogger("runner")
_STALE_PRECHECK_MARKERS = (
    "strategy appears centered on position sizing / trade management",
    "strategy.closedtrades",
    "strategy.wintrades",
)
_SCORE_RANGE = (0, 5)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def load_registry() -> dict:
    """Load the registry, purging any excluded placeholder files."""
    if REGISTRY_PATH.exists():
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        for key in _EXCLUDED_PINE_FILES:
            if key in data:
                del data[key]
                logger.info(f"Purged excluded entry: {key}")
        logger.debug(f"Registry loaded: {len(data)} entries")
        return data
    return {}


def save_registry(registry: dict) -> None:
    """Atomic write: temp file then rename to avoid corruption on crash."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)
    logger.debug("Registry saved.")


def scan_and_register(registry: dict) -> dict:
    """Scan input/ for new .pine files and register them."""
    added = 0
    requeued = 0
    for pine_file in sorted(INPUT_DIR.glob("*.pine")):
        key = pine_file.name
        if key in _EXCLUDED_PINE_FILES:
            continue
        if key not in registry:
            try:
                first_line = pine_file.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            except OSError:
                first_line = ""
            scrape_source = "unknown"
            if first_line.startswith("// SOURCE:"):
                scrape_source = first_line.removeprefix("// SOURCE:").strip()

            registry[key] = {
                "file_path":     str(pine_file),
                "status":        "new",
                "registered_at": _now_iso(),
                "scrape_source": scrape_source,
            }
            logger.info(f"Registered: {key} (source: {scrape_source})")
            added += 1
        else:
            rec = registry[key]
            status = rec.get("status")

            # Terminal statuses: file should not be in input/ — warn and skip
            if status in TERMINAL_STATUSES:
                logger.warning(
                    f"File '{key}' is in input/ but registry status is '{status}'; skipping."
                )
                continue

            # Archived entry found back in input/ — reconcile by re-queuing
            if status == "archived":
                rec["status"] = "new"
                rec["file_path"] = str(pine_file)
                logger.info(f"Reconciled archived entry back to input/: {key}")
                requeued += 1
                continue

            reason = str(rec.get("recommendation_reason", "") or "")
            if status == "evaluated" and any(
                marker in reason for marker in _STALE_PRECHECK_MARKERS
            ):
                rec["status"] = "new"
                logger.info(f"Re-queued stale precheck result: {key}")
                requeued += 1
            elif status in ("evaluated", "failed"):
                btc = rec.get("btc_score", 0)
                proj = rec.get("project_score", 0)
                if (isinstance(btc, (int, float)) and btc > _SCORE_RANGE[1]) or \
                   (isinstance(proj, (int, float)) and proj > _SCORE_RANGE[1]):
                    rec["status"] = "new"
                    logger.info(f"Re-queued unclamped scores: {key} (btc={btc}, proj={proj})")
                    requeued += 1
    if added:
        print_info(f"Registered {added} new file(s).")
    if requeued:
        print_info(f"Re-queued {requeued} stale precheck result(s).")
    return registry