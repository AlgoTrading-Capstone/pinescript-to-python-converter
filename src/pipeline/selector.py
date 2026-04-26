"""
Selector — Auto-select the highest-scoring evaluated strategy for conversion.
"""

import logging
from pathlib import Path

from src.pipeline import (
    ARCHIVE_SCORE_THRESHOLD,
    MAX_CONVERSION_ATTEMPTS,
    MIN_SELECTION_SCORE,
    _EXCLUDED_PINE_FILES,
    _verdict,
)
from src.pipeline.evaluator import INFRA_FAILURE_STATUSES
from src.cli.ui import build_table, console, print_info, print_section, print_warning, truncate, verdict_text

logger = logging.getLogger("runner")


def auto_select_strategy(registry: dict) -> tuple[str | None, dict | None]:
    """
    Select the highest-scoring evaluated strategy for conversion.

    Also increments skip_count for non-selected strategies.
    If no evaluated strategies exist, attempts to recycle from archive.

    Returns (key, record) or (None, None).
    """
    # Display set: everything worth showing in the analysis table
    displayable = {
        k: v for k, v in registry.items()
        if v["status"] in ("evaluated", "failed", "evaluation_failed")
        and k not in _EXCLUDED_PINE_FILES
        and Path(v["file_path"]).exists()
    }

    # Selection set: only strategies that CAN actually be picked.
    # Conviction floor: btc + proj >= MIN_SELECTION_SCORE. Anything below is
    # low-conviction — converting it wastes a pipeline slot on a strategy the
    # evaluator itself flagged as weak.
    candidates = {
        k: v for k, v in registry.items()
        if v["status"] == "evaluated"
        and v.get("evaluation_status") not in INFRA_FAILURE_STATUSES
        and k not in _EXCLUDED_PINE_FILES
        and Path(v["file_path"]).exists()
        and (v.get("btc_score", 0) + v.get("project_score", 0)) >= MIN_SELECTION_SCORE
    }

    # Fallback: recycle from archive if no selectable candidates
    if not candidates:
        recycled = _recycle_from_archive(registry)
        if recycled:
            candidates = {
                k: v for k, v in recycled.items()
                if (v.get("btc_score", 0) + v.get("project_score", 0)) >= MIN_SELECTION_SCORE
            }
            displayable.update(recycled)
        if not candidates:
            if displayable:
                _print_analysis_table(displayable)
            infra_issues = sum(
                1 for v in displayable.values() if v.get("status") == "evaluation_failed"
            )
            if infra_issues:
                print_warning(
                    f"{infra_issues} strategy file(s) are blocked by evaluation infrastructure issues."
                )
            print_info(
                f"No strategies scored at or above the conviction floor "
                f"(total >= {MIN_SELECTION_SCORE}). Fetching a fresh batch."
            )
            return None, None

    displayable.update(candidates)
    _print_analysis_table(displayable)

    ranked_candidates = sorted(
        candidates.items(),
        key=lambda kv: kv[1].get("btc_score", 0) + kv[1].get("project_score", 0),
        reverse=True,
    )
    chosen_key, chosen_rec = ranked_candidates[0]
    print_info(f"Auto-selected: {chosen_key}")
    print_info(f"Reason: {chosen_rec.get('recommendation_reason', 'N/A')}")

    # Increment skip_count for all non-selected evaluated strategies
    for key, rec in registry.items():
        if key != chosen_key and rec["status"] == "evaluated":
            rec["skip_count"] = rec.get("skip_count", 0) + 1

    return chosen_key, chosen_rec


def _print_analysis_table(entries: dict) -> None:
    """Print the ranked strategy analysis table."""
    ranked = sorted(
        entries.items(),
        key=lambda kv: kv[1].get("btc_score", 0) + kv[1].get("project_score", 0),
        reverse=True,
    )
    rows = []
    for key, rec in ranked:
        btc = rec.get("btc_score", 0)
        proj = rec.get("project_score", 0)
        total = btc + proj
        status = rec.get("status", "unknown")
        if status == "failed":
            status = "conversion_failed"
        rows.append(
            [
                key.replace(".pine", ""),
                btc,
                proj,
                total,
                rec.get("category", "Other"),
                verdict_text(_verdict(btc, proj)),
                status,
                truncate(rec.get("recommendation_reason", ""), 80),
            ]
        )

    print_section("Strategy Analysis Report")
    console.print(
        build_table(
            "Ranked Strategies",
            [
                ("Strategy", "left"),
                ("BTC", "right"),
                ("Proj", "right"),
                ("Total", "right"),
                ("Category", "left"),
                ("Verdict", "left"),
                ("State", "left"),
                ("Reason", "left"),
            ],
            rows,
        )
    )


def _recycle_from_archive(registry: dict) -> dict:
    """
    Find recyclable strategies in the archive (previously OK-scored).

    Resets their status to 'evaluated' and skip_count to 0.
    Returns the subset that was recycled (may be empty).
    """
    archived = {
        k: v for k, v in registry.items()
        if v["status"] == "archived"
        and v.get("recycle_eligible", True)
        and v.get("conversion_attempts", 0) < MAX_CONVERSION_ATTEMPTS
        and v.get("btc_score", 0) + v.get("project_score", 0) >= ARCHIVE_SCORE_THRESHOLD
        and Path(v["file_path"]).exists()
    }

    if not archived:
        return {}

    recycled_count = 0
    for key, rec in archived.items():
        rec["status"] = "evaluated"
        rec["skip_count"] = 0
        logger.info(f"Recycled from archive: {key}")
        recycled_count += 1

    if recycled_count:
        print_info(f"Recycled {recycled_count} strategy(ies) from archive.")

    return archived