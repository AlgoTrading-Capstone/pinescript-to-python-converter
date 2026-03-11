import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


@pytest.fixture
def sample_ohlcv_data():
    """
    Generates a synthetic DataFrame with structured market regimes for testing.

    Structure:
    - Phase 0 (0-600 candles):   Warmup — flat at 10,000 so that recursive
                                  indicators (EMA-200, ATR-14, etc.) fully stabilize.
                                  Strategies must return HOLD for slices shorter
                                  than their min_bars guard.
    - Phase 1 (600-700 candles): Sideways / Accumulation (low volatility).
    - Phase 2 (700-900 candles): Bull Run (strong uptrend, 10,000 → 12,000).
    - Phase 3 (900-1100 candles): Bear Crash (strong downtrend, 12,000 → 9,000).

    Total: 1,100 candles (15m interval).
    Data is strictly UTC-aware to simulate ISO format compliance.
    """
    WARMUP   = 600
    SIDEWAYS = 100
    BULL     = 200
    BEAR     = 200
    periods  = WARMUP + SIDEWAYS + BULL + BEAR   # 1100
    start_date = datetime(2024, 1, 1, 0, 0, 0)

    # 1. Generate UTC Timestamps
    dates = [start_date + timedelta(minutes=15 * i) for i in range(periods)]

    # 2. Generate Structured Price Movements (Regimes)
    x = np.linspace(0, 50, periods)

    trend = np.zeros(periods)

    # Phase 0: Warmup — flat reference price; indicators converge here
    trend[:WARMUP] = 10000

    # Phase 1: Sideways (low volatility around 10,000)
    s1 = WARMUP
    e1 = WARMUP + SIDEWAYS
    trend[s1:e1] = 10000 + np.sin(x[s1:e1]) * 50

    # Phase 2: Bull Market (linear increase 10,000 → 12,000)
    s2 = e1
    e2 = e1 + BULL
    trend[s2:e2] = np.linspace(10000, 12000, BULL) + (np.sin(x[s2:e2]) * 100)

    # Phase 3: Bear Market (linear decrease 12,000 → 9,000)
    s3 = e2
    trend[s3:] = np.linspace(12000, 9000, BEAR) + (np.sin(x[s3:]) * 150)

    # 3. Add Noise (Randomness)
    np.random.seed(42)  # Deterministic for consistent tests
    noise = np.random.normal(0, 20, periods)
    close_price = trend + noise

    # 4. Construct OHLC
    # Open is roughly the previous close
    open_price = np.roll(close_price, 1)
    open_price[0] = close_price[0]  # Fix first value because it rolled from the end

    # High/Low derived from Open/Close with some expansion
    high_price = np.maximum(open_price, close_price) + np.abs(np.random.normal(0, 10, periods))
    low_price = np.minimum(open_price, close_price) - np.abs(np.random.normal(0, 10, periods))

    # Volume: Higher volume on trend changes
    volume = np.abs(np.random.normal(100, 50, periods)) * (1 + np.abs(np.gradient(close_price)) / 10)

    data = {
        'date': dates,
        'open': open_price,
        'high': high_price,
        'low': low_price,
        'close': close_price,
        'volume': volume
    }

    df = pd.DataFrame(data)

    # 5. Enforce Data Integrity
    # High must be strictly >= Low, Open, Close
    df['high'] = df[['open', 'close', 'high']].max(axis=1)
    df['low'] = df[['open', 'close', 'low']].min(axis=1)

    # 6. Ensure Date is UTC (ISO-8601 compatible internally)
    df['date'] = pd.to_datetime(df['date'], utc=True)

    return df