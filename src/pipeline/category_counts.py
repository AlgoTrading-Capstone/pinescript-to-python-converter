"""
Helpers for loading and updating strategy category counts.
"""

import json
import logging

from src.pipeline import CATEGORY_COUNTS_PATH

logger = logging.getLogger("runner")

CATEGORY_NAMES = ("Trend", "MeanReversion", "Volatility", "Volume", "Other")


def _empty_counts() -> dict[str, int]:
    return {name: 0 for name in CATEGORY_NAMES}


def normalize_category(category: str | None) -> str:
    if category in CATEGORY_NAMES:
        return category
    return "Other"


def load_category_counts() -> dict[str, int]:
    if not CATEGORY_COUNTS_PATH.exists():
        return _empty_counts()

    try:
        raw = json.loads(CATEGORY_COUNTS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"Failed to load category counts: {exc}")
        return _empty_counts()

    counts = _empty_counts()
    for name in CATEGORY_NAMES:
        value = raw.get(name, 0)
        counts[name] = value if isinstance(value, int) and value >= 0 else 0
    return counts


def save_category_counts(counts: dict[str, int]) -> None:
    CATEGORY_COUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: int(max(0, counts.get(name, 0))) for name in CATEGORY_NAMES}
    CATEGORY_COUNTS_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def increment_category_count(category: str | None) -> dict[str, int]:
    counts = load_category_counts()
    normalized = normalize_category(category)
    counts[normalized] += 1
    save_category_counts(counts)
    logger.info(f"Incremented strategy category count: {normalized} -> {counts[normalized]}")
    return counts
