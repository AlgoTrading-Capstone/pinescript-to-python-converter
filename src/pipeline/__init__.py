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
MIN_SELECTION_SCORE     = 6     # btc + proj below this → never converted (conviction floor)
MAX_DRAWDOWN_PCT        = 50.0  # author's own backtest drawdown (%) above this → deterministic reject
TARGET_STRATEGY_COUNT   = 6     # minimum .pine files to keep in input/
MAX_SEARCH_LOOPS        = 5     # retry cap for auto-selection before giving up
MAX_SKIP_COUNT          = 2     # archive evaluated strategies after this many skips
MAX_CONVERSION_ATTEMPTS = 3     # reject strategy after this many failed conversions
TERMINAL_STATUSES       = frozenset({"completed", "rejected", "statistically_rejected"})
_EXCLUDED_PINE_FILES    = {"source_strategy.pine"}

# ---------------------------------------------------------------------------
# Statistical Gate Configuration
# ---------------------------------------------------------------------------
# Evaluation dataset — must mirror the RL training window so strategies that
# pass the gate are validated over the same market regimes the RL env will see.
EVAL_EXCHANGE            = "binance"
EVAL_SYMBOL              = "BTC/USDT"
EVAL_TIMEFRAME           = "15m"
EVAL_START               = "2018-01-01T00:00:00Z"
EVAL_END                 = "2023-12-31T23:59:59Z"

# Pass/fail thresholds applied in src/pipeline/statistical_gate.py
MIN_SIGNAL_ACTIVITY_PCT  = 0.05  # LONG+SHORT bars must cover ≥5% of history
MIN_WIN_RATE             = 0.50  # ≥50% of detected trades must close positive
MIN_TRADE_COUNT          = 30    # below this the win-rate is statistically noisy

# OHLCV cache — paginated ccxt fetch writes here once, reused across runs
OHLCV_CACHE_DIR          = DATA_DIR / "ohlcv_cache"
# Minimum fraction of expected candles that must be present after download
# before the gate trusts the cached parquet (guards against exchange gaps)
OHLCV_MIN_COVERAGE       = 0.95

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