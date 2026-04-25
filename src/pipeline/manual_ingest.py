"""Manual PineScript ingestion helpers.

The automated scraper/evaluator path keeps its own metadata and selector
contracts. Manual input is simpler: validate that the pasted source is a real
Pine strategy, derive a stable local name, and provide the minimal metadata the
orchestrator needs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src.pipeline import INPUT_DIR
from src.pipeline.triage import triage_pine_source


DEFAULT_TIMEFRAME = "15m"
DEFAULT_LOOKBACK_BARS = 100

_STRATEGY_NAME_RE = re.compile(r"\bstrategy\s*\(\s*(['\"])(?P<name>.*?)\1", re.IGNORECASE | re.DOTALL)


class ManualIngestError(ValueError):
    """Raised when manual PineScript source cannot enter the conversion path."""


@dataclass(frozen=True)
class ManualStrategy:
    pine_path: Path
    metadata: dict
    source_triage_reason: str


def read_clipboard_text() -> str:
    """Read clipboard text without adding a third-party dependency."""
    commands = []
    if sys.platform.startswith("win"):
        commands.append(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"])
    elif sys.platform == "darwin":
        commands.append(["pbpaste"])
    else:
        commands.extend((["xclip", "-selection", "clipboard", "-out"], ["xsel", "--clipboard", "--output"]))

    last_error = "no clipboard command was available"
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        except FileNotFoundError as exc:
            last_error = str(exc)
            continue
        if result.returncode == 0:
            return result.stdout
        last_error = result.stderr.strip() or f"{command[0]} exited with {result.returncode}"

    raise ManualIngestError(f"Could not read clipboard: {last_error}")


def extract_strategy_name(source: str, fallback: str = "manual_strategy") -> str:
    match = _STRATEGY_NAME_RE.search(source or "")
    if match:
        name = " ".join(match.group("name").split())
        if name:
            return name
    return fallback


def safe_strategy_name(value: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in value).strip("_")
    safe = re.sub(r"_+", "_", safe)
    return safe or "manual_strategy"


def normalize_timeframe(value: str | None) -> str:
    if not value:
        return DEFAULT_TIMEFRAME
    cleaned = str(value).strip()
    mapping = {
        "1": "1m",
        "3": "3m",
        "5": "5m",
        "15": "15m",
        "30": "30m",
        "60": "1h",
        "120": "2h",
        "240": "4h",
        "D": "1d",
        "1D": "1d",
        "d": "1d",
        "1d": "1d",
        "H": "1h",
        "1H": "1h",
        "h": "1h",
        "1h": "1h",
    }
    return mapping.get(cleaned, cleaned.lower())


def validate_manual_source(source: str) -> str:
    decision = triage_pine_source(source, {"backtest_metrics": {}})
    if not decision.accepted:
        raise ManualIngestError(f"{decision.reason_code}: {decision.reason}")
    return decision.reason


def build_manual_metadata(
    source: str,
    *,
    name: str | None = None,
    source_path: Path | None = None,
    url: str = "",
    timeframe: str = DEFAULT_TIMEFRAME,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
) -> dict:
    fallback = source_path.stem if source_path is not None else "manual_strategy"
    display_name = name or extract_strategy_name(source, fallback=fallback)
    safe_name = safe_strategy_name(display_name)
    return {
        "name": display_name,
        "safe_name": safe_name,
        "timeframe": normalize_timeframe(timeframe),
        "lookback_bars": int(lookback_bars or DEFAULT_LOOKBACK_BARS),
        "origin": "manual",
        "url": url or "",
    }


def unique_input_path(safe_name: str, input_dir: Path = INPUT_DIR) -> Path:
    input_dir.mkdir(parents=True, exist_ok=True)
    base = safe_strategy_name(safe_name)
    candidate = input_dir / f"{base}.pine"
    suffix = 2
    while candidate.exists():
        candidate = input_dir / f"{base}_{suffix}.pine"
        suffix += 1
    return candidate


def _write_sidecar(pine_path: Path, metadata: dict, *, url: str = "") -> None:
    sidecar = pine_path.with_suffix(".meta.json")
    payload = {
        "url": url or "",
        "description": "Manual TradingView paste.",
        "backtest_metrics": {},
        "pine_metadata": metadata,
        "origin": "manual",
    }
    sidecar.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def prepare_manual_strategy_source(
    source: str,
    *,
    name: str | None = None,
    url: str = "",
    timeframe: str = DEFAULT_TIMEFRAME,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    input_dir: Path = INPUT_DIR,
) -> ManualStrategy:
    triage_reason = validate_manual_source(source)
    metadata = build_manual_metadata(
        source,
        name=name,
        url=url,
        timeframe=timeframe,
        lookback_bars=lookback_bars,
    )
    pine_path = unique_input_path(metadata["safe_name"], input_dir)
    metadata["safe_name"] = pine_path.stem
    pine_path.write_text(source, encoding="utf-8")
    _write_sidecar(pine_path, metadata, url=url)
    return ManualStrategy(pine_path=pine_path, metadata=metadata, source_triage_reason=triage_reason)


def prepare_manual_strategy_file(
    source_path: Path,
    *,
    name: str | None = None,
    url: str = "",
    timeframe: str = DEFAULT_TIMEFRAME,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    input_dir: Path = INPUT_DIR,
) -> ManualStrategy:
    source_path = Path(source_path)
    if not source_path.exists():
        raise ManualIngestError(f"Manual PineScript file does not exist: {source_path}")
    source = source_path.read_text(encoding="utf-8-sig", errors="replace")
    triage_reason = validate_manual_source(source)
    metadata = build_manual_metadata(
        source,
        name=name,
        source_path=source_path,
        url=url,
        timeframe=timeframe,
        lookback_bars=lookback_bars,
    )

    input_dir = Path(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    try:
        already_in_input = source_path.resolve().parent == input_dir.resolve()
    except OSError:
        already_in_input = False

    if already_in_input:
        pine_path = source_path
    else:
        pine_path = unique_input_path(metadata["safe_name"], input_dir)
        metadata["safe_name"] = pine_path.stem
        shutil.copy2(source_path, pine_path)

    _write_sidecar(pine_path, metadata, url=url)
    return ManualStrategy(pine_path=pine_path, metadata=metadata, source_triage_reason=triage_reason)
