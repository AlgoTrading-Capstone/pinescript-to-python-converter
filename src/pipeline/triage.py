"""Fast metadata/source triage for TradingView scrape candidates.

This module rejects obvious non-starters before the expensive conversion
pipeline sees them.  The rules are intentionally deterministic: if a candidate
does not clear the scrape triage, it should never reach ``input/``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from src.pipeline import (
    MAX_DRAWDOWN_PCT,
    SCRAPE_REJECTIONS_PATH,
    SOURCE_QUALITY_PATH,
)


MIN_AUTHOR_TRADES = 30
MIN_AUTHOR_PROFIT_FACTOR = 1.0
MARGINAL_PROFIT_FACTOR = 1.05

_HARD_FRAMEWORK_KEYWORDS = (
    "autobot",
    "automate",
    "execution framework",
    "execution engine",
    "alertatron",
    "3commas",
    "broker connector",
    "exchange connector",
)
_SOFT_FRAMEWORK_KEYWORDS = (
    "webhook",
    "bot",
    "automation",
    "alert",
)
_SESSION_BOUND_KEYWORDS = (
    "nasdaq",
    "nifty",
    "banknifty",
    "sensex",
    "xauusd",
    "xau",
    "mnq",
    "nq",
    "us30",
    "tqqq",
    "new york session",
    "america/new_york",
    "session.isfirstbar",
    "session.islastbar",
    "regular session",
)
_CRYPTO_CONTEXT_KEYWORDS = (
    "btc",
    "bitcoin",
    "crypto",
    "cryptotrading",
    "ethereum",
    "eth",
    "usdt",
)
_FAKE_STATE_KEYWORDS = (
    "strategy.equity",
    "strategy.grossprofit",
    "strategy.netprofit",
)
_PINE_STRATEGY_RE = re.compile(r"\bstrategy\s*\(", re.IGNORECASE)
_PINE_VERSION_RE = re.compile(r"^\s*//\s*@version\s*=", re.IGNORECASE | re.MULTILINE)


@dataclass
class TriageDecision:
    """Decision for one discovered TradingView URL."""

    accepted: bool
    reason_code: str
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "promoted" if self.accepted else "metadata_rejected"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _metric(metrics: dict[str, Any], key: str) -> Any:
    return (metrics or {}).get(key)


def _text(meta: dict[str, Any] | None, source_text: str = "") -> str:
    desc = ""
    if meta:
        desc = str(meta.get("description") or "")
    return f"{desc}\n{source_text}".lower()


def _has_crypto_context(text: str) -> bool:
    return any(keyword in text for keyword in _CRYPTO_CONTEXT_KEYWORDS)


def triage_strategy_metadata(meta: dict[str, Any] | None) -> TriageDecision:
    """Reject weak candidates using only metadata/description.

    This runs before Pine source extraction.  Missing Strategy Report metrics
    are rejected because they were the dominant cause of wasted downstream
    time: the selector had little reliable signal and most candidates failed
    deterministic prechecks later.
    """
    meta = meta or {}
    metrics = dict(meta.get("backtest_metrics") or {})
    text = _text(meta)

    if not any(v is not None for v in metrics.values()):
        return TriageDecision(
            False,
            "missing_strategy_report",
            "Strategy Report metrics were unavailable; skipping before source download.",
            metrics,
        )

    for keyword in _HARD_FRAMEWORK_KEYWORDS:
        if keyword in text:
            return TriageDecision(
                False,
                "execution_framework",
                f"Description indicates a hard execution-framework dependency: {keyword!r}.",
                metrics,
            )

    total_trades = _metric(metrics, "total_trades")
    if total_trades is not None and int(total_trades) < MIN_AUTHOR_TRADES:
        return TriageDecision(
            False,
            "low_trade_count",
            f"Author report has {int(total_trades)} trades < {MIN_AUTHOR_TRADES}.",
            metrics,
        )

    profit_factor = _metric(metrics, "profit_factor")
    if profit_factor is not None and float(profit_factor) < MIN_AUTHOR_PROFIT_FACTOR:
        return TriageDecision(
            False,
            "unprofitable_author_report",
            f"Author profit factor {float(profit_factor):.3f} < {MIN_AUTHOR_PROFIT_FACTOR:.1f}.",
            metrics,
        )

    max_dd = _metric(metrics, "max_drawdown_pct")
    if max_dd is not None and float(max_dd) > MAX_DRAWDOWN_PCT:
        return TriageDecision(
            False,
            "excessive_drawdown",
            f"Author max drawdown {float(max_dd):.2f}% > {MAX_DRAWDOWN_PCT:.1f}%.",
            metrics,
        )

    sharpe = _metric(metrics, "sharpe_ratio")
    if (
        profit_factor is not None
        and sharpe is not None
        and float(profit_factor) <= MARGINAL_PROFIT_FACTOR
        and float(sharpe) <= 0
    ):
        return TriageDecision(
            False,
            "marginal_edge",
            (
                f"Author report is near-random: profit factor {float(profit_factor):.3f}, "
                f"Sharpe {float(sharpe):.3f}."
            ),
            metrics,
        )

    has_crypto_context = _has_crypto_context(text)
    for keyword in _SESSION_BOUND_KEYWORDS:
        if keyword in text and not has_crypto_context:
            return TriageDecision(
                False,
                "non_btc_session_dependency",
                f"Description is tied to non-BTC/session-specific behavior: {keyword!r}.",
                metrics,
            )

    return TriageDecision(True, "accepted", "Candidate passed metadata triage.", metrics)


def triage_pine_source(source_text: str, meta: dict[str, Any] | None = None) -> TriageDecision:
    """Second deterministic screen after source extraction but before saving."""
    source_text = source_text or ""
    text = _text(meta, source_text)
    metrics = dict((meta or {}).get("backtest_metrics") or {})

    if len(source_text.strip()) < 300 or len(source_text.splitlines()) < 5:
        return TriageDecision(
            False,
            "invalid_pine_source",
            "Extracted source is too short to be a real Pine strategy.",
            metrics,
        )
    if not _PINE_VERSION_RE.search(source_text):
        return TriageDecision(
            False,
            "invalid_pine_source",
            "Extracted source is missing a Pine //@version header.",
            metrics,
        )
    if not _PINE_STRATEGY_RE.search(source_text):
        return TriageDecision(
            False,
            "not_a_strategy_source",
            "Extracted Pine source does not declare strategy(...).",
            metrics,
        )

    for keyword in _HARD_FRAMEWORK_KEYWORDS:
        if keyword in text:
            return TriageDecision(
                False,
                "execution_framework_source",
                f"Pine source indicates a hard execution-framework dependency: {keyword!r}.",
                metrics,
            )
    for keyword in _FAKE_STATE_KEYWORDS:
        if keyword in text:
            return TriageDecision(
                False,
                "fake_strategy_state",
                f"Pine source depends on self-evaluation state: {keyword!r}.",
                metrics,
            )
    if "bar_index" in text and ("for " in text or "for\t" in text):
        return TriageDecision(
            False,
            "heavy_historical_loop",
            "Pine source appears to loop over bar_index/history.",
            metrics,
        )
    return TriageDecision(True, "accepted", "Candidate passed source triage.", metrics)


def load_scrape_rejections(path: Path = SCRAPE_REJECTIONS_PATH) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_scrape_rejections(rejections: dict[str, dict[str, Any]], path: Path = SCRAPE_REJECTIONS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rejections, indent=2, ensure_ascii=False), encoding="utf-8")


def remember_rejection(
    rejections: dict[str, dict[str, Any]],
    *,
    url: str,
    source: str,
    decision: TriageDecision,
    status: str = "metadata_rejected",
) -> None:
    rejections[url] = {
        "url": url,
        "source": source,
        "status": status,
        "reason_code": decision.reason_code,
        "reason": decision.reason,
        "metrics": decision.metrics,
        "rejected_at": _now_iso(),
    }


def load_source_quality(path: Path = SOURCE_QUALITY_PATH) -> dict[str, dict[str, int]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(source): {
            "discovered": int(stats.get("discovered", 0) or 0),
            "metadata_rejected": int(stats.get("metadata_rejected", 0) or 0),
            "source_rejected": int(stats.get("source_rejected", 0) or 0),
            "promoted": int(stats.get("promoted", 0) or 0),
        }
        for source, stats in raw.items()
        if isinstance(stats, dict)
    }


def save_source_quality(quality: dict[str, dict[str, int]], path: Path = SOURCE_QUALITY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(quality, indent=2, ensure_ascii=False), encoding="utf-8")


def update_source_quality(
    quality: dict[str, dict[str, int]],
    events: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    for event in events:
        source = str(event.get("source") or "unknown")
        stats = quality.setdefault(
            source,
            {"discovered": 0, "metadata_rejected": 0, "source_rejected": 0, "promoted": 0},
        )
        status = str(event.get("status") or "")
        stats["discovered"] += 1
        if status in stats:
            stats[status] += 1
    return quality


def quality_weight(stats: dict[str, int] | None) -> float:
    """Weight source allocation by historical promotion rate.

    Keep a non-zero floor so a source can recover if TradingView's current
    listings improve.
    """
    if not stats:
        return 1.0
    discovered = max(1, int(stats.get("discovered", 0) or 0))
    promoted = int(stats.get("promoted", 0) or 0)
    reject_rate = 1.0 - (promoted / discovered)
    return max(0.25, 1.0 - reject_rate)


def event_from_decision(
    *,
    url: str,
    source: str,
    slug: str,
    stage: str,
    decision: TriageDecision,
) -> dict[str, Any]:
    payload = asdict(decision)
    payload.update({
        "url": url,
        "source": source,
        "slug": slug,
        "stage": stage,
        "status": "promoted" if decision.accepted else "metadata_rejected",
    })
    return payload
