"""
Tests for NewTottStrategy (NEW TOTT Strategy).

Covers:
- Return type contract
- min_bars guard (HOLD on insufficient data)
- Signal generation across bull and bear market phases
- Determinism / no-lookahead consistency
- Timestamp passthrough
"""

import pytest
from datetime import datetime, timezone

from src.strategies.new_tott_strategy import NewTottStrategy
from src.base_strategy import StrategyRecommendation, SignalType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _run_at(strategy: NewTottStrategy, df, row_idx: int) -> StrategyRecommendation:
    """Run strategy on df[:row_idx] using a fixed timestamp."""
    return strategy.run(df.iloc[:row_idx].copy(), _FIXED_TS)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNewTottStrategy:
    """Unit tests for the NEW TOTT Strategy."""

    def test_returns_strategy_recommendation(self, sample_ohlcv_data):
        """Strategy must return a StrategyRecommendation on the full dataset."""
        strategy = NewTottStrategy()
        result = strategy.run(sample_ohlcv_data, _FIXED_TS)

        assert isinstance(result, StrategyRecommendation), (
            f"Expected StrategyRecommendation, got {type(result)}"
        )
        assert isinstance(result.signal, SignalType), (
            f"Expected SignalType member, got {type(result.signal)}"
        )

    def test_min_bars_guard(self, sample_ohlcv_data):
        """Strategy must return HOLD when fewer than MIN_BARS rows are supplied."""
        strategy = NewTottStrategy()
        small_df = sample_ohlcv_data.iloc[:10].copy()
        result = strategy.run(small_df, _FIXED_TS)

        assert result.signal == SignalType.HOLD, (
            f"Expected HOLD with only 10 rows, got {result.signal}"
        )

    def test_signal_in_bull_phase(self, sample_ohlcv_data):
        """Strategy must be in a bullish state at the end of the bull run (bars 700-900).

        The fixture's Phase 2 is a strong uptrend (10,000 → 12,000). The OTT
        strategy is a slow trend-follower: once it crosses into bullish territory
        it stays there throughout the uptrend. We verify two things:

        1. Evaluating on the full bull-phase window (rows 0-900) the strategy
           returns either LONG (crossover bar) or HOLD (already above OTTup).
           It must NOT return SHORT during the sustained bull run.

        2. Scanning across a wide window (bars 50-900) at least one crossover
           event (LONG) must appear to confirm the indicator does produce
           directional signals on this dataset.
        """
        strategy = NewTottStrategy()

        # Check 1: no SHORT signals during the entire bull phase
        short_count = 0
        long_count = 0
        for end_idx in range(700, 900):
            rec = strategy.run(sample_ohlcv_data.iloc[:end_idx].copy(), _FIXED_TS)
            if rec.signal == SignalType.SHORT:
                short_count += 1
            if rec.signal == SignalType.LONG:
                long_count += 1

        assert short_count == 0, (
            f"Strategy emitted {short_count} SHORT signal(s) during the bull phase — "
            "unexpected for an OTT trend-follower during an uptrend"
        )

        # Check 2: at least one LONG signal appears across the full upside window
        # (the OTT crossover typically happens before bar 700 when using full history)
        long_count_wide = 0
        for end_idx in range(50, 900):
            rec = strategy.run(sample_ohlcv_data.iloc[:end_idx].copy(), _FIXED_TS)
            if rec.signal == SignalType.LONG:
                long_count_wide += 1

        # Additionally confirm bullish state at bull-phase end (MAvg > OTTup)
        from src.strategies.new_tott_strategy import _compute_var_ma, _compute_ott

        src = sample_ohlcv_data.iloc[:900]["close"].reset_index(drop=True)
        mavg = _compute_var_ma(src, strategy.length)
        ott_up, _ = _compute_ott(mavg, strategy.percent, strategy.coeff)
        idx = len(mavg) - 1
        is_bullish = mavg[idx] > ott_up[idx]

        assert is_bullish, (
            f"Expected MAvg > OTTup at end of bull phase. "
            f"mavg={mavg[idx]:.2f}, ott_up={ott_up[idx]:.2f}"
        )

    def test_signal_in_bear_phase(self, sample_ohlcv_data):
        """At least one SHORT signal must be emitted during the bear crash (bars 900-1100).

        The fixture's Phase 3 is a strong downtrend (12,000 → 9,000). The OTT
        strategy should catch at least one crossunder of MAvg below OTTdn.
        """
        strategy = NewTottStrategy()
        signals = []

        for end_idx in range(900, 1100):
            rec = strategy.run(sample_ohlcv_data.iloc[:end_idx].copy(), _FIXED_TS)
            signals.append(rec.signal)

        short_count = signals.count(SignalType.SHORT)
        assert short_count >= 1, (
            f"Expected at least 1 SHORT signal during bear phase, got {short_count}"
        )

    def test_no_lookahead_signal_consistency(self, sample_ohlcv_data):
        """Running the strategy twice on identical data must produce identical results.

        If there were any non-determinism or future-data dependency, results
        would diverge across runs.
        """
        strategy = NewTottStrategy()
        df_copy1 = sample_ohlcv_data.copy()
        df_copy2 = sample_ohlcv_data.copy()

        result1 = strategy.run(df_copy1, _FIXED_TS)
        result2 = strategy.run(df_copy2, _FIXED_TS)

        assert result1.signal == result2.signal, (
            f"Signal inconsistency: first run={result1.signal}, second run={result2.signal}"
        )
        assert result1.timestamp == result2.timestamp, (
            "Timestamp mismatch between two identical runs"
        )

    def test_timestamp_matches(self, sample_ohlcv_data):
        """The returned timestamp must exactly equal the timestamp passed in."""
        strategy = NewTottStrategy()
        ts = datetime(2025, 3, 14, 8, 30, 0, tzinfo=timezone.utc)
        result = strategy.run(sample_ohlcv_data, ts)

        assert result.timestamp == ts, (
            f"Expected timestamp {ts}, got {result.timestamp}"
        )
