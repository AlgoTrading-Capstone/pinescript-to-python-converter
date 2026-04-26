# Adding Strategies

This project supports three ways to add TradingView Pine strategies:

1. Manual CLI helper from clipboard, file, or stdin.
2. Manual `.pine` file placed in `input/`.
3. Automated TradingView scraper through the normal pipeline.

Use the manual direct path when you already copied a strategy from TradingView and want to bypass the selector. The strategy will still go through conversion, validation, generated tests, the statistical gate, and integration/PR after the gate passes.

## Manual CLI Helper

The helper validates Pine source, creates a safe filename under `input/`, writes a metadata sidecar, and prints the exact conversion command.

### From Clipboard

Copy the Pine source from TradingView, then run:

```powershell
.venv/Scripts/python.exe scripts/add_manual_strategy.py --clipboard
```

Then run the `Next:` command printed by the helper, which will look like:

```powershell
.venv/Scripts/python.exe main.py --manual input/My_Strategy.pine
```

### From An Existing File

```powershell
.venv/Scripts/python.exe scripts/add_manual_strategy.py --file C:\path\to\strategy.pine
```

Then run:

```powershell
.venv/Scripts/python.exe main.py --manual input/<created_file>.pine
```

### From Stdin

```powershell
Get-Clipboard | .venv/Scripts/python.exe scripts/add_manual_strategy.py --name "My Strategy"
```

### Optional Metadata

```powershell
.venv/Scripts/python.exe scripts/add_manual_strategy.py --clipboard --name "My Strategy" --url "https://www.tradingview.com/script/..." --timeframe 15m --lookback-bars 200
```

Defaults:

- `--timeframe 15m`
- `--lookback-bars 100`

## Manual Direct Conversion

To bypass scraping, selector scoring, and auto-selection, run:

```powershell
.venv/Scripts/python.exe main.py --manual input/My_Strategy.pine
```

This path skips:

- TradingView scraper refill.
- `strategy_selector` scoring.
- Auto-selection against other scraped candidates.

This path still runs:

- Orchestrator conversion.
- Transpiler.
- Validator.
- Generated strategy tests.
- Statistical gate.
- Integration/PR after the statistical gate passes.

Registry entries created by this path are marked with:

- `origin: manual`
- `selection_mode: manual_direct`
- `evaluation_status: bypassed_selector`
- `btc_score: null`
- `project_score: null`

## Manual File Only

You can also save a copied strategy directly as a `.pine` file:

```text
input/My_Strategy.pine
```

Then run:

```powershell
.venv/Scripts/python.exe main.py --manual input/My_Strategy.pine
```

The file must contain real Pine strategy source, including:

- A Pine version header such as `//@version=5`.
- A `strategy(...)` declaration.
- Enough source code to pass deterministic source triage.

If the file is outside `input/`, the manual path copies it into `input/` and creates a sidecar metadata file.

## Automated Scraper Flow

To use the normal automated pipeline:

```powershell
.venv/Scripts/python.exe main.py
```

The normal flow:

1. Checks how many `.pine` files are in `input/`.
2. If fewer than `TARGET_STRATEGY_COUNT`, scrapes TradingView strategy sources.
3. Registers new files in `data/strategies_registry.json`.
4. Evaluates them with the selector.
5. Auto-selects the best candidate above the conviction floor.
6. Converts, validates, tests, runs the statistical gate, and integrates only after the gate passes.

Scraper candidates do not bypass the selector. Use `--manual` when you want a copied TradingView strategy to go straight to conversion.

## Quick Decision Guide

- Already copied the Pine source: use `scripts/add_manual_strategy.py --clipboard`.
- Already have a `.pine` file: use `scripts/add_manual_strategy.py --file ...`, or run `main.py --manual ...` directly.
- Want the system to discover strategies: run `main.py` without `--manual`.

