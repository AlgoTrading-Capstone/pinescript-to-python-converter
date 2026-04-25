"""
Evaluator — Spawns isolated AI agent processes to score PineScript strategies.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.pipeline import MAX_DRAWDOWN_PCT, SUBPROCESS_ENV, TERMINAL_STATUSES, _verdict
from src.pipeline.claude_cli import has_claude_cli
from src.pipeline.registry import _now_iso, save_registry
from src.pipeline.ui import (
    build_table,
    console,
    print_info,
    print_section,
    print_warning,
    truncate,
    verdict_text,
)

logger = logging.getLogger("runner")

INFRA_FAILURE_STATUSES = {"read_error", "dependency_missing", "timeout", "invalid_json"}
FAKE_STATE_KEYWORDS = (
    "strategy.equity",
    "strategy.grossprofit",
    "strategy.netprofit",
)
POSITION_SIZING_KEYWORDS = (
    "kelly",
    "martingale",
    "grid trading",
    "dca",
    "position sizing",
    "position_size",
    "stake amount",
)
EXECUTION_FRAMEWORK_KEYWORDS = (
    "webhook",
    "bot",
    "automate",
    "framework",
    "execution engine",
)


@dataclass
class BacktestMetrics:
    total_trades: Optional[int] = None
    profit_factor: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    sharpe_ratio: Optional[float] = None


@dataclass
class StrategyMetadata:
    url: str = ""
    description: Optional[str] = None
    backtest_metrics: BacktestMetrics = field(default_factory=BacktestMetrics)


@dataclass
class EvaluationOutcome:
    status: str
    payload: Optional[dict] = None
    reason: str = ""
    display_reason: str = ""


def _load_strategy_metadata(pine_file: Path) -> Optional[StrategyMetadata]:
    sidecar = pine_file.with_suffix(".meta.json")
    if not sidecar.exists():
        return None
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("Sidecar root is not a JSON object.")

        bm_raw = raw.get("backtest_metrics") or {}
        if not isinstance(bm_raw, dict):
            bm_raw = {}

        def _int_or_none(v) -> Optional[int]:
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _float_or_none(v) -> Optional[float]:
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        return StrategyMetadata(
            url=str(raw.get("url", "") or ""),
            description=str(raw.get("description")).strip() if raw.get("description") else None,
            backtest_metrics=BacktestMetrics(
                total_trades=_int_or_none(bm_raw.get("total_trades")),
                profit_factor=_float_or_none(bm_raw.get("profit_factor")),
                max_drawdown_pct=_float_or_none(bm_raw.get("max_drawdown_pct")),
                sharpe_ratio=_float_or_none(bm_raw.get("sharpe_ratio")),
            ),
        )
    except Exception as exc:
        logger.warning(f"Could not load metadata sidecar {sidecar.name}: {exc}")
        return None


def _format_metadata_block(meta: StrategyMetadata) -> str:
    bm = meta.backtest_metrics
    lines = [
        "--- BACKTEST_METADATA (scraped from TradingView) ---",
        f"total_trades:     {bm.total_trades if bm.total_trades is not None else 'N/A'}",
        f"profit_factor:    {bm.profit_factor if bm.profit_factor is not None else 'N/A'}",
        f"max_drawdown_pct: {bm.max_drawdown_pct if bm.max_drawdown_pct is not None else 'N/A'}",
        f"sharpe_ratio:     {bm.sharpe_ratio if bm.sharpe_ratio is not None else 'N/A'}",
    ]
    if meta.description:
        desc = meta.description[:1500]
        if len(meta.description) > 1500:
            desc += " [truncated]"
        lines.append(f"author_description: |\n  {desc.replace(chr(10), chr(10) + '  ')}")
    lines.append("--- END BACKTEST_METADATA ---")
    return "\n".join(lines)


def _normalize_timeframe(value: str | None) -> str:
    if not value:
        return "1h"

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


def _parse_json_from_output(raw: str) -> dict:
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in agent output")
    return json.loads(cleaned[start:end])


_REJECTION_SIGNAL_RE = re.compile(
    r"\b("
    r"skip|reject|abort|disqualif|incompatible|broken|not viable|"
    r"cannot be converted|not recommended|structurally broken|"
    r"no viable|unresolvable|immediate(?:ly)? abort|"
    r"recommend(?:ed)?\s+(?:skip|archiv)|"
    r"low(?:\s+rl)?\s+(?:value|feature)|lookahead\s+bias|penali[sz]ed|"
    r"not\s+a\s+signal|execution\s+framework|execution\s+bot|"
    r"webhook|bot\s+framework|not\s+suitable\s+for\s+rl"
    r")\b",
    re.IGNORECASE,
)


def _detect_score_reason_dissonance(
    btc_score: int, project_score: int, reason: str
) -> bool:
    if btc_score <= 0 and project_score <= 0:
        return False
    return bool(_REJECTION_SIGNAL_RE.search(reason))


def _safe_name(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value).strip("_")


def _infer_strategy_name(raw: str, fallback: str) -> str:
    match = re.search(r"strategy\s*\(\s*['\"]([^'\"]+)['\"]", raw)
    return match.group(1).strip() if match else fallback


def _best_effort_metadata(raw: str, pine_file: Path) -> dict:
    name = _infer_strategy_name(raw, pine_file.stem)
    return {
        "name": name,
        "safe_name": _safe_name(name or pine_file.stem),
        "timeframe": _normalize_timeframe(None),
        "lookback_bars": 100,
    }


def _summary_for_meta(meta: StrategyMetadata | None) -> str:
    if meta is None:
        return "No sidecar metadata found."
    bm = meta.backtest_metrics
    summary = [
        f"trades={bm.total_trades if bm.total_trades is not None else 'N/A'}",
        "description=yes" if meta.description else "description=no",
    ]
    return ", ".join(summary)


def _contains_any(text: str, keywords: tuple[str, ...]) -> Optional[str]:
    lowered = text.lower()
    return next((keyword for keyword in keywords if keyword in lowered), None)


def _detect_heavy_historical_loop(raw: str) -> Optional[str]:
    lowered = raw.lower()
    loop_matches = re.finditer(r"for\s+\w+\s*=\s*([^\n]+)", lowered)
    for match in loop_matches:
        loop_body = match.group(0)
        if "bar_index" in loop_body or "last_bar_index" in loop_body:
            return "Uses a loop bounded by bar_index / last_bar_index."
        if re.search(r"to\s+\d{3,}", loop_body):
            return "Uses a loop with a large fixed historical bound."
        if re.search(r"to\s+\w*(lookback|history|barsback|bars_back|length)\w*", loop_body):
            return "Uses a loop over dynamic historical lookback data."
    return None


def _deterministic_rejection(raw: str, meta: StrategyMetadata | None) -> Optional[str]:
    total_trades = meta.backtest_metrics.total_trades if meta else None
    if total_trades is not None and total_trades < 30:
        return f"Rejected before selector: total_trades={total_trades} is below the RL minimum of 30."

    profit_factor = meta.backtest_metrics.profit_factor if meta else None
    if profit_factor is not None and profit_factor < 1.0:
        return (
            f"REJECTED: backtest is unprofitable "
            f"(profit_factor={profit_factor:.3f} < 1.0). "
            "RL target is profitable signal generators only."
        )

    max_dd = meta.backtest_metrics.max_drawdown_pct if meta else None
    if max_dd is not None and max_dd > MAX_DRAWDOWN_PCT:
        return (
            f"REJECTED: author backtest drawdown is catastrophic "
            f"(max_drawdown_pct={max_dd:.2f}% > {MAX_DRAWDOWN_PCT:.1f}%). "
            "A strategy that blows up its own backtest cannot be a reliable RL signal source."
        )

    fake_state = _contains_any(raw, FAKE_STATE_KEYWORDS)
    if fake_state:
        return f"Rejected before selector: signal logic depends on self-evaluation state via `{fake_state}`."

    heavy_loop_reason = _detect_heavy_historical_loop(raw)
    if heavy_loop_reason:
        return f"Rejected before selector: {heavy_loop_reason}"

    # Reject execution frameworks, webhook bots, and automation templates outright.
    # These are not mathematical signal generators and cause infinite loops in the transpiler.
    description_text = (meta.description or "") if meta else ""
    combined_text = f"{raw[:500]} {description_text}".lower()
    framework_hit = _contains_any(combined_text, EXECUTION_FRAMEWORK_KEYWORDS)
    if framework_hit:
        return (
            f"REJECTED: Script is an execution framework/automation bot (matched keyword: '{framework_hit}'). "
            "This is not a mathematical signal generator and cannot be transpiled for RL feature extraction."
        )

    return None


def evaluate_strategy(pine_file: Path) -> EvaluationOutcome:
    try:
        raw = pine_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(f"Cannot read {pine_file}: {exc}")
        return EvaluationOutcome(
            status="read_error",
            reason=f"Could not read {pine_file.name}: {exc}",
            display_reason="Read error",
        )

    logger.info(f"Evaluating: {pine_file.name} ({len(raw)} chars)")
    logger.info(f"CLAUDE input (file path sent to agent):\n{pine_file.resolve()}")

    strategy_meta = _load_strategy_metadata(pine_file)
    print_info(f"{pine_file.name}: {_summary_for_meta(strategy_meta)}")

    rejection_reason = _deterministic_rejection(raw, strategy_meta)
    if rejection_reason:
        logger.info(f"Deterministic rejection for {pine_file.name}: {rejection_reason}")
        return EvaluationOutcome(
            status="precheck_rejected",
            payload={
                "pine_metadata": _best_effort_metadata(raw, pine_file),
                "category": "Other",
                "btc_score": 0,
                "project_score": 0,
                "recommendation_reason": rejection_reason,
            },
            reason=rejection_reason,
            display_reason="Rejected by precheck",
        )

    meta_block = ""
    if strategy_meta is not None:
        meta_block = _format_metadata_block(strategy_meta) + "\n\n"

    prompt = (
        f"Evaluate this PineScript strategy. File: {pine_file.name}\n\n"
        f"{meta_block}"
        f"{raw}\n\n"
        "Output ONLY a raw JSON object matching this exact schema — no markdown, no extra fields:\n"
        '{"pine_metadata": {"name": "...", "safe_name": "...", "timeframe": "...", "lookback_bars": 0}, '
        '"category": "Trend", "btc_score": 0, "project_score": 0, '
        '"recommendation_reason": "..."}'
    )
    command = [
        "claude",
        "-p",
        "--agent",
        "strategy_selector",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        prompt,
    ]

    if not has_claude_cli():
        logger.error("'claude' command not found for selector.")
        return EvaluationOutcome(
            status="dependency_missing",
            reason="Claude CLI is not installed or not on PATH.",
            display_reason="Claude CLI missing",
        )

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=SUBPROCESS_ENV,
        )
    except FileNotFoundError:
        logger.error("'claude' command not found for selector.")
        return EvaluationOutcome(
            status="dependency_missing",
            reason="Claude CLI is not installed or not on PATH.",
            display_reason="Claude CLI missing",
        )

    collected: list[str] = []
    killed_by_watchdog = threading.Event()

    def _kill_on_timeout() -> None:
        killed_by_watchdog.set()
        try:
            process.kill()
        except OSError:
            pass

    watchdog = threading.Timer(180, _kill_on_timeout)
    try:
        watchdog.start()
        for line in process.stdout:
            stripped = line.rstrip()
            if stripped:
                console.print(f"[muted]    selector>[/muted] {stripped}")
                logger.info(f"CLAUDE [SELECTOR]: {stripped}")
            collected.append(line)
        process.wait()
    except KeyboardInterrupt:
        process.kill()
        raise
    except Exception:
        try:
            process.kill()
        except OSError:
            pass
        process.wait()
    finally:
        watchdog.cancel()
        if process.poll() is None:
            process.terminate()

    if killed_by_watchdog.is_set():
        logger.warning(f"Selector timed out (180s) for {pine_file.name}")
        return EvaluationOutcome(
            status="timeout",
            reason=f"Selector timed out after 180 seconds for {pine_file.name}.",
            display_reason="Selector timed out",
        )

    full_output = "".join(collected)
    if process.returncode != 0:
        logger.warning(f"Selector non-zero exit for {pine_file.name}: {process.returncode}")

    try:
        parsed = _parse_json_from_output(full_output)
        return EvaluationOutcome(status="scored", payload=parsed)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(f"JSON parse error for {pine_file.name}: {exc}")
        return EvaluationOutcome(
            status="invalid_json",
            reason=f"Selector returned invalid JSON for {pine_file.name}: {exc}",
            display_reason="Selector output was not valid JSON",
        )
    except Exception as exc:
        logger.exception(f"Unexpected error evaluating {pine_file.name}: {exc}")
        return EvaluationOutcome(
            status="invalid_json",
            reason=f"Unexpected selector parsing error for {pine_file.name}: {exc}",
            display_reason="Unexpected selector parsing error",
        )


def run_evaluations(registry: dict) -> dict:
    """Evaluate all pending strategies and retry only infrastructure failures."""
    new_entries = [
        (k, v) for k, v in registry.items()
        if v["status"] == "new" and v["status"] not in TERMINAL_STATUSES
    ]
    retry_entries = [
        (k, v)
        for k, v in registry.items()
        if v["status"] == "evaluation_failed" and v["status"] not in TERMINAL_STATUSES
    ]
    to_evaluate = new_entries + retry_entries
    if not to_evaluate:
        return registry

    print_section("Evaluation")
    print_info(f"Evaluating {len(to_evaluate)} strategy file(s).")

    summary_rows: list[list[object]] = []

    for key, rec in to_evaluate:
        outcome = evaluate_strategy(Path(rec["file_path"]))
        required = {"pine_metadata", "category", "btc_score", "project_score"}

        if outcome.payload and required.issubset(outcome.payload):
            result = outcome.payload
            raw_btc = result["btc_score"]
            raw_proj = result["project_score"]
            btc = max(0, min(5, int(raw_btc)))
            proj = max(0, min(5, int(raw_proj)))
            if btc != raw_btc or proj != raw_proj:
                logger.warning(
                    f"Score clamping for {key}: raw btc={raw_btc} proj={raw_proj} "
                    f"clamped to btc={btc} proj={proj}"
                )
            meta = result["pine_metadata"]
            category = result["category"]
            if not meta.get("safe_name"):
                raw_name = meta.get("name", key.replace(".pine", ""))
                meta["safe_name"] = _safe_name(raw_name)
            meta["timeframe"] = _normalize_timeframe(meta.get("timeframe"))
            if not meta.get("lookback_bars"):
                meta["lookback_bars"] = 100

            reason = result.get("recommendation_reason", "")
            if not reason:
                logger.warning(f"Selector returned no recommendation_reason for {key}")

            if _detect_score_reason_dissonance(btc, proj, reason):
                logger.warning(
                    f"Score/reason dissonance for {key}: scores btc={btc} proj={proj} "
                    f"but reason signals rejection. Overriding to 0/0."
                )
                print_warning(
                    f"{key}: selector gave scores {btc}/{proj} but reason rejects the strategy. "
                    f"Overriding to 0/0."
                )
                btc = 0
                proj = 0

            registry[key].update(
                {
                    "status": "evaluated",
                    "evaluated_at": _now_iso(),
                    "evaluation_status": outcome.status,
                    "pine_metadata": meta,
                    "category": category,
                    "btc_score": btc,
                    "project_score": proj,
                    "recommendation_reason": reason,
                }
            )
            summary_rows.append(
                [
                    key.replace(".pine", ""),
                    btc,
                    proj,
                    btc + proj,
                    category,
                    verdict_text(_verdict(btc, proj)),
                    truncate(result.get("recommendation_reason", ""), 70),
                ]
            )
        else:
            registry[key].update(
                {
                    "status": "evaluation_failed",
                    "evaluated_at": _now_iso(),
                    "evaluation_status": outcome.status,
                    "pine_metadata": _best_effort_metadata("", Path(rec["file_path"])),
                    "category": "Other",
                    "btc_score": 0,
                    "project_score": 0,
                    "recommendation_reason": outcome.reason or "Evaluation failed.",
                }
            )
            summary_rows.append(
                [
                    key.replace(".pine", ""),
                    0,
                    0,
                    0,
                    "Other",
                    verdict_text("[SKIP]"),
                    truncate(outcome.display_reason or outcome.reason or "Evaluation failed.", 70),
                ]
            )
            logger.warning(f"Evaluation infrastructure failure for {key}: {outcome.reason}")

        save_registry(registry)

    table = build_table(
        "Evaluation Results",
        [
            ("Strategy", "left"),
            ("BTC", "right"),
            ("Proj", "right"),
            ("Total", "right"),
            ("Category", "left"),
            ("Verdict", "left"),
            ("Reason", "left"),
        ],
        summary_rows,
    )
    console.print(table)

    infra_failures = sum(1 for rec in registry.values() if rec.get("status") == "evaluation_failed")
    if infra_failures:
        print_warning(f"{infra_failures} strategy file(s) had evaluation infrastructure failures and will be retried later.")

    return registry
