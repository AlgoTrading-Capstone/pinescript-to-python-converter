"""
PineScript-to-Python Converter Pipeline

Shared constants, environment, and helpers used across all pipeline modules.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_DIR                = Path("data")
REGISTRY_PATH           = DATA_DIR / "strategies_registry.json"
CATEGORY_COUNTS_PATH    = DATA_DIR / "category_counts.json"
INPUT_DIR               = Path("input")
ARCHIVE_DIR             = Path("archive")
OUTPUT_DIR              = Path("output")
LOGS_ROOT               = Path("logs")
SEEN_URLS_PATH          = DATA_DIR / "seen_urls.json"
ARCHIVE_SCORE_THRESHOLD = 4     # btc + proj < this → archive; >= this → keep
TARGET_STRATEGY_COUNT   = 6     # minimum .pine files to keep in input/
MAX_SEARCH_LOOPS        = 5     # retry cap for auto-selection before giving up
MAX_SKIP_COUNT          = 2     # archive evaluated strategies after this many skips
_EXCLUDED_PINE_FILES    = {"source_strategy.pine"}

# Subprocess environment with CLAUDECODE stripped so nested `claude` calls are allowed.
SUBPROCESS_ENV = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _div(char: str = "─", width: int = 70) -> str:
    return char * width


def _verdict(btc: int, proj: int) -> str:
    total = btc + proj
    if total >= 8:
        return "[RECOMMENDED]"
    if total >= 6:
        return "[GOOD]"
    if total >= 4:
        return "[OK]"
    if total >= 2:
        return "[COMPLEX]"
    return "[SKIP]"