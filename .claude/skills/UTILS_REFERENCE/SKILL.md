# Utils Reference

When translating PineScript to Python, you must use the following pre-built utility functions from the `src.utils` package. 

## Multi-Timeframe Resampling (`src.utils.resampling`)
If the PineScript code uses `request.security` to fetch data from a higher timeframe, you MUST use these exact functions to avoid lookahead bias.

**Imports:**
```python
from src.utils.resampling import resample_to_interval, resampled_merge
```
# 1. Resample the base dataframe to a higher timeframe (e.g., 4 hours)
resampled_df = resample_to_interval(df, "4h")

# 2. Compute higher-timeframe indicators on the resampled dataframe
# Example: resampled_df['sma'] = ta.trend.sma_indicator(resampled_df['close'], window=14)

# 3. Merge the higher-timeframe data back to the base dataframe
merged_df = resampled_merge(original=df, resampled=resampled_df, fill_na=True)

# The higher timeframe columns will be prefixed automatically (e.g., 'resample_240_sma')