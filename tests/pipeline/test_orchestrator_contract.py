from pathlib import Path

from src.pipeline.orchestrator import line_has_integration_token, missing_agent_logs


def test_conversion_expected_logs_do_not_include_integration(tmp_path):
    for name in (
        "agent_transpiler.md",
        "agent_validator.md",
        "agent_test_generator.md",
    ):
        (tmp_path / name).write_text("ok", encoding="utf-8")

    assert missing_agent_logs(tmp_path) == []


def test_orchestrator_detects_premature_integration_tokens():
    assert line_has_integration_token("INTEGRATION_PASS")
    assert line_has_integration_token("done INTEGRATION_FALLBACK")
    assert not line_has_integration_token("CONVERSION_PASS")
