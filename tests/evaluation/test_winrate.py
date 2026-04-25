import pandas as pd

from src.evaluation.winrate import compute_trades, compute_winrate, resolve_effective_positions


def test_hold_carries_previous_position_for_winrate():
    closes = pd.Series([100.0, 110.0, 120.0, 90.0])
    signals = pd.Series(["LONG", "HOLD", "FLAT", "FLAT"])

    stats = compute_winrate(closes, signals)

    assert stats["total_trades"] == 1
    assert stats["win_rate"] == 1.0
    assert stats["trades"] == [0.2]


def test_leading_hold_resolves_to_flat():
    signals = pd.Series(["HOLD", "HOLD", "LONG", "HOLD", "FLAT"])

    effective = resolve_effective_positions(signals)

    assert effective.tolist() == ["FLAT", "FLAT", "LONG", "LONG", "FLAT"]


def test_compute_trades_matches_hold_position_semantics():
    index = pd.date_range("2024-01-01", periods=5, freq="15min")
    closes = pd.Series([100.0, 110.0, 120.0, 115.0, 115.0], index=index)
    signals = pd.Series(["FLAT", "SHORT", "HOLD", "FLAT", "FLAT"], index=index)

    trades = compute_trades(closes, signals)

    assert len(trades) == 1
    assert trades.iloc[0]["side"] == "SHORT"
    assert trades.iloc[0]["entry_price"] == 110.0
    assert trades.iloc[0]["exit_price"] == 115.0
    assert bool(trades.iloc[0]["win"]) is False
