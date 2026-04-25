"""Add a manually copied TradingView Pine strategy to input/."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pipeline.manual_ingest import (  # noqa: E402
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_TIMEFRAME,
    ManualIngestError,
    prepare_manual_strategy_file,
    prepare_manual_strategy_source,
    read_clipboard_text,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--clipboard", action="store_true", help="Read PineScript source from the system clipboard.")
    source.add_argument("--file", type=Path, help="Read PineScript source from a file.")
    parser.add_argument("--name", help="Override the strategy name used for the generated filename and metadata.")
    parser.add_argument("--url", default="", help="Optional TradingView URL to store in the metadata sidecar.")
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME, help=f"Strategy timeframe metadata. Default: {DEFAULT_TIMEFRAME}.")
    parser.add_argument(
        "--lookback-bars",
        type=int,
        default=DEFAULT_LOOKBACK_BARS,
        help=f"Warmup/lookback metadata. Default: {DEFAULT_LOOKBACK_BARS}.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.file is not None:
            manual = prepare_manual_strategy_file(
                args.file,
                name=args.name,
                url=args.url,
                timeframe=args.timeframe,
                lookback_bars=args.lookback_bars,
            )
        else:
            source = read_clipboard_text() if args.clipboard else sys.stdin.read()
            if not source.strip():
                raise ManualIngestError("No PineScript source provided. Use --clipboard, --file, or pipe stdin.")
            manual = prepare_manual_strategy_source(
                source,
                name=args.name,
                url=args.url,
                timeframe=args.timeframe,
                lookback_bars=args.lookback_bars,
            )
    except ManualIngestError as exc:
        print(f"Manual strategy rejected: {exc}", file=sys.stderr)
        return 1

    print(f"Manual strategy added: {manual.pine_path}")
    print(f"Strategy name: {manual.metadata['name']}")
    print(f"Safe name: {manual.metadata['safe_name']}")
    print(f"Next: .venv/Scripts/python.exe main.py --manual {manual.pine_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
