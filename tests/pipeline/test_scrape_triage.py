from pathlib import Path

from src.pipeline.triage import (
    TriageDecision,
    event_from_decision,
    quality_weight,
    triage_pine_source,
    triage_strategy_metadata,
    update_source_quality,
)


def _meta(**metrics):
    return {
        "description": "Clean BTC trend strategy",
        "backtest_metrics": {
            "total_trades": metrics.get("total_trades", 300),
            "profit_factor": metrics.get("profit_factor", 1.4),
            "max_drawdown_pct": metrics.get("max_drawdown_pct", 12.0),
            "sharpe_ratio": metrics.get("sharpe_ratio", 0.4),
        },
    }


def _pine(body: str = "") -> str:
    return "\n".join([
        "//@version=5",
        "strategy('Clean BTC Strategy', overlay=true)",
        "fast = ta.ema(close, 12)",
        "slow = ta.ema(close, 26)",
        "longCondition = ta.crossover(fast, slow)",
        "shortCondition = ta.crossunder(fast, slow)",
        "atr = ta.atr(14)",
        "stopLong = close - atr * 2.0",
        "takeLong = close + atr * 3.0",
        "stopShort = close + atr * 2.0",
        "takeShort = close - atr * 3.0",
        body or "strategy.entry('L', strategy.long, when=longCondition)",
    ])


def test_metadata_triage_accepts_promising_candidate():
    decision = triage_strategy_metadata(_meta())
    assert decision.accepted is True
    assert decision.reason_code == "accepted"


def test_metadata_triage_rejects_missing_strategy_report():
    decision = triage_strategy_metadata({"description": "No metrics", "backtest_metrics": {}})
    assert decision.accepted is False
    assert decision.reason_code == "missing_strategy_report"


def test_metadata_triage_rejects_bad_author_metrics():
    assert triage_strategy_metadata(_meta(total_trades=22)).reason_code == "low_trade_count"
    assert triage_strategy_metadata(_meta(profit_factor=0.9)).reason_code == "unprofitable_author_report"
    assert triage_strategy_metadata(_meta(max_drawdown_pct=75)).reason_code == "excessive_drawdown"


def test_metadata_triage_allows_alert_bot_wording_with_valid_metrics():
    meta = _meta()
    meta["description"] = "Webhook bot automation template for alerts"
    decision = triage_strategy_metadata(meta)
    assert decision.accepted is True


def test_metadata_triage_rejects_hard_execution_framework_descriptions():
    meta = _meta()
    meta["description"] = "3Commas execution framework connector"
    decision = triage_strategy_metadata(meta)
    assert decision.accepted is False
    assert decision.reason_code == "execution_framework"


def test_source_triage_rejects_fake_strategy_state():
    decision = triage_pine_source(_pine("long = strategy.equity > strategy.equity[1]"), _meta())
    assert decision.accepted is False
    assert decision.reason_code == "fake_strategy_state"


def test_source_triage_rejects_clipboard_or_non_pine_text():
    decision = triage_pine_source("Repair documentation drift: keep base_strategy API unchanged.", _meta())
    assert decision.accepted is False
    assert decision.reason_code == "invalid_pine_source"


def test_source_quality_tracks_rejections_and_promotions():
    events = [
        event_from_decision(
            url="https://example.com/a",
            source="popular",
            slug="a",
            stage="metadata",
            decision=TriageDecision(False, "low_trade_count", "bad"),
        ),
        event_from_decision(
            url="https://example.com/b",
            source="popular",
            slug="b",
            stage="source",
            decision=TriageDecision(True, "accepted", "ok"),
        ),
    ]
    quality = update_source_quality({}, events)
    assert quality["popular"]["discovered"] == 2
    assert quality["popular"]["metadata_rejected"] == 1
    assert quality["popular"]["promoted"] == 1
    assert 0.25 <= quality_weight(quality["popular"]) <= 1.0
