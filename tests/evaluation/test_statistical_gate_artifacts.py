import json

import numpy as np
import pandas as pd

from src.base_strategy import BaseStrategy, SignalType
from src.pipeline.statistical_gate import run_statistical_gate


class LowVarianceStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            name="Low Variance Strategy",
            description="Test strategy",
            timeframe="15m",
            lookback_hours=1,
        )
        self.MIN_CANDLES_REQUIRED = 3
        self._observed = 0

    def generate_all_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(["FLAT"] * len(df), index=df.index, dtype=object)
        if len(df) > self.MIN_CANDLES_REQUIRED:
            signals.iloc[self.MIN_CANDLES_REQUIRED] = "LONG"
        return signals

    def step(self, candle: pd.Series) -> SignalType:
        self._observed += 1
        return SignalType.FLAT


class ActiveStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            name="Active Strategy",
            description="Test strategy",
            timeframe="15m",
            lookback_hours=1,
        )
        self.MIN_CANDLES_REQUIRED = 3

    def generate_all_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(["FLAT"] * len(df), index=df.index, dtype=object)
        signals.iloc[self.MIN_CANDLES_REQUIRED::2] = "LONG"
        return signals

    def step(self, candle: pd.Series) -> SignalType:
        return SignalType.FLAT


def _ohlcv(periods: int = 120) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=periods, freq="15min", tz="UTC")
    close = np.linspace(100.0, 130.0, len(idx))
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 10.0,
        },
        index=idx,
    )


def _stub_artifact_renderers(monkeypatch):
    def _write_png(*, output_path, **_kwargs):
        output_path.write_bytes(b"png")
        return output_path

    monkeypatch.setattr(
        "src.pipeline.statistical_gate.render_heatmap",
        lambda output_path, **kwargs: _write_png(output_path=output_path),
    )
    monkeypatch.setattr(
        "src.pipeline.statistical_gate.render_winrate_curve",
        lambda trades, output_path, title: _write_png(output_path=output_path),
    )
    monkeypatch.setattr(
        "src.pipeline.statistical_gate.render_gate_summary",
        lambda **kwargs: _write_png(output_path=kwargs["output_path"]),
    )


def _stub_gate_stats(
    monkeypatch,
    *,
    win_rate: float,
    total_trades: int,
    avg_pnl: float,
    profit_factor: float = 1.5,
    max_drawdown: float = 0.20,
    sharpe: float = 1.0,
    sortino: float = 1.2,
    expectancy: float | None = None,
):
    """Stub every per-strategy metric the gate computes.

    Defaults form a strict-lane-passing baseline. Override one field to drive
    the gate down a specific reject branch. ``expectancy`` defaults to
    ``avg_pnl`` so existing per-trade assertions stay coherent unless
    explicitly decoupled.
    """
    if expectancy is None:
        expectancy = avg_pnl

    monkeypatch.setattr(
        "src.pipeline.statistical_gate.compute_winrate",
        lambda closes, signals: {
            "win_rate": win_rate,
            "total_trades": total_trades,
            "avg_pnl": avg_pnl,
            "trades": [avg_pnl] * max(1, total_trades),
        },
    )
    monkeypatch.setattr(
        "src.pipeline.statistical_gate.compute_trades",
        lambda closes, signals: pd.DataFrame(
            {
                "exit_time": closes.index[: max(1, total_trades)],
                "pnl": [avg_pnl] * max(1, total_trades),
                "win": [avg_pnl > 0] * max(1, total_trades),
            }
        ),
    )
    monkeypatch.setattr(
        "src.pipeline.statistical_gate.compute_bar_returns",
        lambda closes, signals: pd.Series([0.0] * len(closes), index=closes.index),
    )
    monkeypatch.setattr(
        "src.pipeline.statistical_gate.compute_equity_curve",
        lambda bar_returns: pd.Series([1.0] * len(bar_returns), index=bar_returns.index),
    )
    monkeypatch.setattr(
        "src.pipeline.statistical_gate.compute_max_drawdown",
        lambda equity: max_drawdown,
    )
    monkeypatch.setattr(
        "src.pipeline.statistical_gate.compute_sharpe",
        lambda bar_returns: sharpe,
    )
    monkeypatch.setattr(
        "src.pipeline.statistical_gate.compute_sortino",
        lambda bar_returns: sortino,
    )
    monkeypatch.setattr(
        "src.pipeline.statistical_gate.compute_profit_factor",
        lambda trade_pnl: profit_factor,
    )
    monkeypatch.setattr(
        "src.pipeline.statistical_gate.compute_expectancy",
        lambda trade_pnl: expectancy,
    )


def test_gate_stats_report_lists_itself_as_artifact(tmp_path):
    result = run_statistical_gate(LowVarianceStrategy(), tmp_path, ohlcv_df=_ohlcv())

    assert result.passed is False
    assert result.lane is None
    assert "stats_report" in result.artifacts
    stats_path = tmp_path / result.artifacts["stats_report"]
    payload = json.loads(stats_path.read_text(encoding="utf-8"))
    assert payload["lane"] is None
    assert payload["artifacts"]["stats_report"].replace("\\", "/") == "eval/stats_report.json"


def test_gate_marks_strict_lane_for_positive_expectancy_high_winrate(tmp_path, monkeypatch):
    _stub_artifact_renderers(monkeypatch)
    # All defaults clear every strict bar (PF=1.5, MDD=0.20, Sharpe=1.0, Sortino=1.2).
    _stub_gate_stats(monkeypatch, win_rate=0.50, total_trades=250, avg_pnl=0.001)

    result = run_statistical_gate(ActiveStrategy(), tmp_path, ohlcv_df=_ohlcv())

    assert result.passed is True
    assert result.lane == "strict"
    payload = json.loads((tmp_path / result.artifacts["stats_report"]).read_text(encoding="utf-8"))
    assert payload["lane"] == "strict"
    assert payload["metrics"]["profit_factor"] == 1.5
    # Unified gate-summary plot must be written alongside the existing two PNGs.
    assert result.artifacts["gate_summary"].replace("\\", "/") == "eval/gate_summary.png"
    assert payload["artifacts"]["gate_summary"].replace("\\", "/") == "eval/gate_summary.png"
    assert (tmp_path / result.artifacts["gate_summary"]).exists()


def test_gate_marks_research_lane_for_positive_expectancy_lower_winrate(tmp_path, monkeypatch):
    _stub_artifact_renderers(monkeypatch)
    # MDD = 0.28 sits between the strict bar (0.25) and the floor (0.30) →
    # passes all floors but misses one strict bar → research.
    _stub_gate_stats(
        monkeypatch,
        win_rate=0.50,
        total_trades=250,
        avg_pnl=0.001,
        max_drawdown=0.28,
    )

    result = run_statistical_gate(ActiveStrategy(), tmp_path, ohlcv_df=_ohlcv())

    assert result.passed is True
    assert result.lane == "research"
    payload = json.loads((tmp_path / result.artifacts["stats_report"]).read_text(encoding="utf-8"))
    assert payload["lane"] == "research"


def test_gate_rejects_non_positive_expectancy_with_null_lane(tmp_path, monkeypatch):
    _stub_artifact_renderers(monkeypatch)
    _stub_gate_stats(monkeypatch, win_rate=0.50, total_trades=250, avg_pnl=0.0)

    result = run_statistical_gate(ActiveStrategy(), tmp_path, ohlcv_df=_ohlcv())

    assert result.passed is False
    assert result.lane is None
    assert result.reason.startswith("expectancy_non_positive")
    payload = json.loads((tmp_path / result.artifacts["stats_report"]).read_text(encoding="utf-8"))
    assert payload["lane"] is None


def test_gate_rejects_too_few_trades_with_null_lane(tmp_path, monkeypatch):
    _stub_artifact_renderers(monkeypatch)
    _stub_gate_stats(monkeypatch, win_rate=0.50, total_trades=149, avg_pnl=0.001)

    result = run_statistical_gate(ActiveStrategy(), tmp_path, ohlcv_df=_ohlcv())

    assert result.passed is False
    assert result.lane is None
    assert result.reason == "trade_count_below_floor: 149 < 150"