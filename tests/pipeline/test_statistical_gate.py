"""
Boundary-value tests for the strict-compliance lane assignment.

These tests target the pure decision function `assign_lane(GateMetrics)` —
no OHLCV, no monkeypatching, no I/O. The gate's end-to-end wiring (variance
short-circuit, artifact writing, registry interaction) lives in
`tests/evaluation/test_statistical_gate_artifacts.py`.

Baseline metrics object passes the strict lane. Each test perturbs ONE knob
to land it on a specific side of a specific threshold and asserts the
resulting lane.
"""

from __future__ import annotations

import pytest

from src.pipeline.statistical_gate import (
    EXPECTANCY_FLOOR,
    GateMetrics,
    MDD_CEILING_FLOOR,
    MDD_CEILING_STRICT,
    PF_FLOOR,
    PF_OVERFIT_CAP,
    PF_STRICT,
    SHARPE_FLOOR,
    SHARPE_OVERFIT_CAP,
    SHARPE_STRICT,
    SORTINO_FLOOR,
    SORTINO_OVERFIT_CAP,
    SORTINO_STRICT,
    TRADE_COUNT_FLOOR,
    TRADE_COUNT_STRICT,
    WIN_RATE_FLOOR,
    WIN_RATE_OVERFIT_CAP,
    WIN_RATE_STRICT,
    assign_lane,
)


def _m(**overrides) -> GateMetrics:
    """Strict-passing baseline. Override ONE field to test a boundary."""
    base = dict(
        profit_factor=1.5,
        win_rate=0.50,
        max_drawdown=0.20,
        total_trades=250,
        sharpe=1.0,
        sortino=1.2,
        expectancy=0.001,
    )
    base.update(overrides)
    return GateMetrics(**base)


# ---------------------------------------------------------------------------
# 1. Overfit / cheater caps — REJECT (lane = None)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value,reason_prefix",
    [
        ("profit_factor", 2.51, "overfit_profit_factor"),
        ("win_rate",      0.71, "overfit_win_rate"),
        ("sharpe",        2.01, "overfit_sharpe"),
        ("sortino",       2.51, "overfit_sortino"),
    ],
)
def test_reject_overfit_cheater(field, value, reason_prefix):
    lane, reason = assign_lane(_m(**{field: value}))
    assert lane is None
    assert reason.startswith(reason_prefix)


# ---------------------------------------------------------------------------
# 2. Hard floors — REJECT (lane = None)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value,reason_prefix",
    [
        ("profit_factor", 1.19,  "profit_factor_below_floor"),
        ("win_rate",      0.34,  "win_rate_below_floor"),
        ("max_drawdown",  0.31,  "max_drawdown_above_ceiling"),
        ("total_trades",  149,   "trade_count_below_floor"),
        ("sharpe",        0.49,  "sharpe_below_floor"),
        ("sortino",       0.69,  "sortino_below_floor"),
        ("expectancy",    0.0,   "expectancy_non_positive"),
    ],
)
def test_reject_hard_floor(field, value, reason_prefix):
    lane, reason = assign_lane(_m(**{field: value}))
    assert lane is None
    assert reason.startswith(reason_prefix)


# ---------------------------------------------------------------------------
# 3. Strict lane — PASS with lane = "strict"
# ---------------------------------------------------------------------------


def test_strict_lane_pass():
    lane, reason = assign_lane(_m())
    assert lane == "strict"
    assert reason is None


# ---------------------------------------------------------------------------
# 4. Research lane — passes the floors, fails one or more strict bars
# ---------------------------------------------------------------------------


def test_research_lane_pass():
    # MDD = 0.28 sits between the strict bar (0.25) and the floor (0.30).
    lane, reason = assign_lane(_m(max_drawdown=0.28))
    assert lane == "research"
    assert reason is None


# ---------------------------------------------------------------------------
# 5. Inclusive-boundary tests — pin down >= / <= semantics so a future
#    refactor can't silently flip an inequality.
# ---------------------------------------------------------------------------


def test_strict_boundary_inclusive():
    """Hitting each strict bar exactly is still 'strict' (>=, <=)."""
    lane, reason = assign_lane(GateMetrics(
        profit_factor=PF_STRICT,             # 1.3
        win_rate=WIN_RATE_STRICT,            # 0.40
        max_drawdown=MDD_CEILING_STRICT,     # 0.25
        total_trades=TRADE_COUNT_STRICT,     # 200
        sharpe=SHARPE_STRICT,                # 0.7
        sortino=SORTINO_STRICT,              # 0.9
        expectancy=0.001,
    ))
    assert lane == "strict"
    assert reason is None


def test_floor_boundary_inclusive_pass():
    """Hitting each floor exactly is 'research' (>= floor, <= MDD ceiling)."""
    lane, reason = assign_lane(GateMetrics(
        profit_factor=PF_FLOOR,             # 1.2
        win_rate=WIN_RATE_FLOOR,            # 0.35
        max_drawdown=MDD_CEILING_FLOOR,     # 0.30
        total_trades=TRADE_COUNT_FLOOR,     # 150
        sharpe=SHARPE_FLOOR,                # 0.5
        sortino=SORTINO_FLOOR,              # 0.7
        expectancy=0.0001,                  # > EXPECTANCY_FLOOR (0.0)
    ))
    assert lane == "research"
    assert reason is None


def test_overfit_caps_inclusive_pass():
    """Hitting each overfit cap exactly is NOT a reject (cap uses strict >)."""
    lane, _reason = assign_lane(_m(
        profit_factor=PF_OVERFIT_CAP,       # 2.5
        win_rate=WIN_RATE_OVERFIT_CAP,      # 0.70
        sharpe=SHARPE_OVERFIT_CAP,          # 2.0
        sortino=SORTINO_OVERFIT_CAP,        # 2.5
    ))
    assert lane is not None  # one of strict/research, not rejected


def test_expectancy_floor_uses_strict_inequality():
    """Expectancy floor is strict (> 0), so 0.0 rejects but 0.0001 passes."""
    lane_zero, reason_zero = assign_lane(_m(expectancy=EXPECTANCY_FLOOR))
    assert lane_zero is None
    assert reason_zero.startswith("expectancy_non_positive")

    lane_eps, reason_eps = assign_lane(_m(expectancy=0.0001))
    assert lane_eps == "strict"
    assert reason_eps is None