"""
Archiver — Move low-scoring or stale strategies to archive/.

Also handles archive recycling when no fresh candidates remain.
"""

import logging
import shutil
from datetime import datetime, UTC
from pathlib import Path

from src.pipeline import ARCHIVE_DIR, ARCHIVE_SCORE_THRESHOLD, MAX_CONVERSION_ATTEMPTS, MAX_SKIP_COUNT
from src.cli.ui import print_info

logger = logging.getLogger("runner")


def archive_strategy_bundle(pine_path: Path, subdir: str = "") -> Path:
    """Move a .pine file and its sidecar into archive/[<subdir>/]<strategy_name>/.

    When ``subdir`` is non-empty (e.g. ``"rejected"``), the bundle lands under
    ``archive/<subdir>/<strategy_name>/`` instead of directly under ``archive/``.
    """
    base = ARCHIVE_DIR / subdir if subdir else ARCHIVE_DIR
    base.mkdir(parents=True, exist_ok=True)
    bundle_dir = base / pine_path.stem
    bundle_dir.mkdir(parents=True, exist_ok=True)

    pine_dest = bundle_dir / pine_path.name
    if pine_path.exists() and pine_path.resolve() != pine_dest.resolve():
        shutil.move(str(pine_path), pine_dest)

    sidecar_src = pine_path.with_suffix(".meta.json")
    sidecar_dest = bundle_dir / sidecar_src.name
    if sidecar_src.exists() and sidecar_src.resolve() != sidecar_dest.resolve():
        shutil.move(str(sidecar_src), sidecar_dest)

    return pine_dest


def purge_rejected_evaluations(registry: dict) -> dict:
    """Move every zero-scored registry entry still sitting in ``input/`` to
    ``archive/rejected/<stem>/`` and flip its status to ``"rejected"``.

    Called right after :func:`run_evaluations` so rejected strategies leave
    ``input/`` on the same run — they do not clutter the next scan.

    An entry is purged when all of the following hold:
      - ``btc_score + project_score == 0``
      - ``status`` is one of ``{"evaluated", "evaluation_failed", "precheck_rejected"}``
      - the file still exists on disk (no-op if already moved)
    """
    purged = 0
    for key, rec in registry.items():
        if rec.get("status") not in ("evaluated", "evaluation_failed", "precheck_rejected"):
            continue
        total = rec.get("btc_score", 0) + rec.get("project_score", 0)
        if total != 0:
            continue

        src = Path(rec.get("file_path", ""))
        if not src.exists():
            continue

        dest = archive_strategy_bundle(src, subdir="rejected")
        rec.update({
            "status":       "rejected",
            "rejected_at":  datetime.now(UTC).isoformat(),
            "file_path":    str(dest),
            "rejection_reason": rec.get("recommendation_reason") or "Zero-score evaluation.",
        })
        logger.info(f"Purged zero-scored: {key} -> {dest}")
        purged += 1

    if purged:
        print_info(f"Purged {purged} zero-scored strategy(ies) → archive/rejected/")
    return registry


def archive_remaining(registry: dict, selected_key: str) -> dict:
    """
    Archive strategies that are low-scoring OR have been skipped too many times.

    Strategies meeting the score threshold AND below the skip limit are left
    in input/ with 'evaluated' status for future runs.
    """
    ARCHIVE_DIR.mkdir(exist_ok=True)
    archived = 0

    for key, rec in registry.items():
        if key == selected_key:
            continue
        if rec["status"] not in ("new", "evaluated", "failed"):
            continue

        total = rec.get("btc_score", 0) + rec.get("project_score", 0)
        skip_count = rec.get("skip_count", 0)

        # Failed strategies: archive or reject based on attempt count
        if rec["status"] == "failed":
            attempts = rec.get("conversion_attempts", 0)
            if attempts >= MAX_CONVERSION_ATTEMPTS:
                reason = f"rejected: {attempts} failed conversion attempts"
            else:
                reason = "conversion_failed"
        elif total >= ARCHIVE_SCORE_THRESHOLD and skip_count < MAX_SKIP_COUNT:
            logger.info(
                f"Keeping '{key}' in input/ "
                f"(total={total} >= {ARCHIVE_SCORE_THRESHOLD}, skips={skip_count})"
            )
            continue
        else:
            reason = (
                f"skip_count={skip_count} >= {MAX_SKIP_COUNT}"
                if skip_count >= MAX_SKIP_COUNT
                else f"total={total} < {ARCHIVE_SCORE_THRESHOLD}"
            )

        src = Path(rec["file_path"])
        subdir = "rejected" if reason.startswith("rejected:") else ""
        if src.exists():
            dest = archive_strategy_bundle(src, subdir=subdir)
            rec["file_path"] = str(dest)
            logger.info(f"Archived: {key} -> {dest} ({reason})")

        if reason.startswith("rejected:"):
            rec["status"] = "rejected"
            rec["rejected_at"] = datetime.now(UTC).isoformat()
        else:
            rec["status"] = "archived"
            rec["archived_at"] = datetime.now(UTC).isoformat()
        archived += 1

    if archived:
        print_info(f"Archived {archived} file(s) → archive/")
    return registry