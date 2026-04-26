from pathlib import Path

import pytest

import main
from src.pipeline import MANUAL_INPUT_DIR
from src.pipeline.manual_ingest import (
    ManualIngestError,
    prepare_manual_strategy_file,
    prepare_manual_strategy_source,
)


def _valid_pine(name: str = "Manual TV Strategy") -> str:
    return "\n".join([
        "//@version=5",
        f"strategy('{name}', overlay=true)",
        "fast = ta.ema(close, 12)",
        "slow = ta.ema(close, 26)",
        "longCondition = ta.crossover(fast, slow)",
        "shortCondition = ta.crossunder(fast, slow)",
        "atr = ta.atr(14)",
        "stopLong = close - atr * 2.0",
        "takeLong = close + atr * 3.0",
        "stopShort = close + atr * 2.0",
        "takeShort = close - atr * 3.0",
        "strategy.entry('L', strategy.long, when=longCondition)",
        "strategy.entry('S', strategy.short, when=shortCondition)",
        "strategy.exit('XL', 'L', stop=stopLong, limit=takeLong)",
        "strategy.exit('XS', 'S', stop=stopShort, limit=takeShort)",
        "plot(fast)",
        "plot(slow)",
        "// padding keeps this representative of copied TradingView source",
        "// " + ("manual-source " * 30),
    ])


def test_manual_ingest_writes_valid_pine_and_metadata(tmp_path):
    manual = prepare_manual_strategy_source(
        _valid_pine(),
        input_dir=tmp_path,
        timeframe="15",
        lookback_bars=123,
    )

    assert manual.pine_path == tmp_path / "Manual_TV_Strategy.pine"
    assert manual.pine_path.exists()
    assert manual.metadata["origin"] == "manual"
    assert manual.metadata["timeframe"] == "15m"
    assert manual.metadata["lookback_bars"] == 123
    assert manual.pine_path.with_suffix(".meta.json").exists()


def test_manual_ingest_rejects_non_pine_text(tmp_path):
    with pytest.raises(ManualIngestError, match="invalid_pine_source"):
        prepare_manual_strategy_source("Repair documentation drift.", input_dir=tmp_path)


def test_manual_ingest_writes_to_manual_input_dir(tmp_path):
    """`prepare_manual_strategy_file(input_dir=MANUAL_INPUT_DIR)` lands the
    .pine and its sidecar inside the dedicated `input/manual/` drop-zone, not
    at the top-level `input/`."""
    manual_dir = tmp_path / "manual"
    src = tmp_path / "scratch.pine"
    src.write_text(_valid_pine("Drop Zone"), encoding="utf-8")

    manual = prepare_manual_strategy_file(src, input_dir=manual_dir)

    assert manual.pine_path.parent == manual_dir
    assert manual.pine_path.name == "Drop_Zone.pine"
    assert manual.pine_path.exists()
    assert manual.pine_path.with_suffix(".meta.json").exists()


def test_manual_input_dir_is_subdir_of_input():
    """Sanity: MANUAL_INPUT_DIR is `input/manual` so the scrape glob (which
    is non-recursive) skips files placed there."""
    assert MANUAL_INPUT_DIR.parts[-2:] == ("input", "manual")


def test_manual_main_bypasses_scraper_evaluator_and_selector(monkeypatch, tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    input_dir.mkdir()
    pine_path = input_dir / "ManualBypass.pine"
    pine_path.write_text(_valid_pine("Manual Bypass"), encoding="utf-8")
    saved_registry = {}

    class DummyGateResult:
        passed = True
        reason = "passed"
        variance = {"signal_activity_pct": 0.12}
        winrate = {"win_rate": 0.61, "total_trades": 44}

        def to_registry_block(self):
            return {"passed": True}

    class DummyLogger:
        def error(self, *args, **kwargs):
            pass

        def exception(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

    def forbidden(*args, **kwargs):
        raise AssertionError("manual direct path should bypass this call")

    monkeypatch.setattr(main, "INPUT_DIR", input_dir)
    monkeypatch.setattr(main, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(main, "LOGS_ROOT", logs_dir)
    monkeypatch.setattr(main, "_setup_file_logger", lambda: (DummyLogger(), logs_dir / "runner.log"))
    monkeypatch.setattr(main, "load_registry", lambda: {})
    monkeypatch.setattr(main, "save_registry", lambda reg: saved_registry.clear() or saved_registry.update(reg))
    monkeypatch.setattr(main, "run_tv_scraper", forbidden)
    monkeypatch.setattr(main, "run_evaluations", forbidden)
    monkeypatch.setattr(main, "auto_select_strategy", forbidden)
    monkeypatch.setattr(main, "get_claude_cli_path", forbidden)
    monkeypatch.setattr(main, "run_orchestrator", lambda pine, meta, out_dir: (True, tmp_path / "run_logs"))
    monkeypatch.setattr(main, "copy_artifacts", lambda meta, out_dir, run_dir, pine_file: None)
    monkeypatch.setattr(main, "verify_artifacts", lambda safe_name, out_dir: True)
    monkeypatch.setattr(main, "load_strategy_by_safe_name", lambda safe_name: object())
    monkeypatch.setattr(main, "run_statistical_gate", lambda strategy, out_dir: DummyGateResult())
    monkeypatch.setattr(main, "run_integration", lambda **kwargs: True)
    monkeypatch.setattr(main, "archive_strategy_bundle", lambda pine, subdir="": tmp_path / "archive" / Path(pine).name)
    monkeypatch.setattr(main, "increment_category_count", lambda category: None)

    with pytest.raises(SystemExit) as exc:
        main.main(["--manual", str(pine_path)])

    assert exc.value.code == 0
    rec = saved_registry["ManualBypass.pine"]
    assert rec["origin"] == "manual"
    assert rec["selection_mode"] == "manual_direct"
    assert rec["evaluation_status"] == "bypassed_selector"
    assert rec["btc_score"] is None
    assert rec["project_score"] is None
    assert rec["status"] == "completed"
