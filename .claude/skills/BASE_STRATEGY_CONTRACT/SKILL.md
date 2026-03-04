# Base Strategy Contract

All generated Python strategies MUST strictly adhere to the `BaseStrategy` interface defined in `src/base_strategy.py`. 
You are strictly forbidden from modifying `src/base_strategy.py`.

## Core Implementation Rules:

1. **Inheritance:** Every generated strategy class MUST inherit from `BaseStrategy`.
2. **Required Imports:** Your generated strategy must include these exact imports:
   ```python
   from datetime import datetime
   import pandas as pd
   from src.base_strategy import BaseStrategy, StrategyRecommendation, SignalType
   ```
### Initialization (`__init__`)
The strategy class must implement an `__init__` method that explicitly calls the parent class constructor with the required parameters.

```python
def __init__(self):
    # The child class must define the specific strategy details
    super().__init__(
        name="StrategyName",
        description="Strategy Description",
        timeframe="15m",  # Example, extract from PineScript
        lookback_hours=24  # Example, extract from PineScript
    )
```

### The `run` Method
The strategy MUST implement the abstract `run` method with the exact signature:

```python
def run(self, df: pd.DataFrame, timestamp: datetime) -> StrategyRecommendation:
```

**Return Type:** The `run` method MUST return a `StrategyRecommendation` object. The signal must be one of the `SignalType` enums: `LONG`, `SHORT`, `FLAT`, or `HOLD`.

Validation:
Any generated strategy that does not fulfill this exact contract is considered a FAILURE and must be rejected by the Validator Agent.