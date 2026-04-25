from src.pipeline.evaluator import (
    BacktestMetrics,
    StrategyMetadata,
    _deterministic_rejection,
)


def _metadata(total_trades: int) -> StrategyMetadata:
    return StrategyMetadata(
        description="Clean BTC trend strategy",
        backtest_metrics=BacktestMetrics(
            total_trades=total_trades,
            profit_factor=1.5,
            max_drawdown_pct=10.0,
            sharpe_ratio=0.4,
        ),
    )


def test_precheck_rejects_fewer_than_30_author_trades():
    reason = _deterministic_rejection("strategy('x')", _metadata(29))

    assert reason is not None
    assert "total_trades=29" in reason
    assert "minimum of 30" in reason


def test_precheck_allows_30_to_149_author_trades_for_selector_scoring():
    for total_trades in (30, 66, 146):
        assert _deterministic_rejection("strategy('x')", _metadata(total_trades)) is None
