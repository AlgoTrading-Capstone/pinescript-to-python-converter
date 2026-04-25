import json

from scripts.build_strategy_dashboards import build_dashboards


def test_dashboard_builder_renders_winrate_chart_without_signals(tmp_path):
    report_dir = tmp_path / "output" / "demo_strategy" / "2026-01-01_00-00-00" / "eval"
    report_dir.mkdir(parents=True)
    (report_dir / "stats_report.json").write_text(
        json.dumps(
            {
                "passed": False,
                "reason": "winrate_below_threshold",
                "strategy_name": "DemoStrategy",
                "evaluated_at": "2026-01-01T00:00:00+00:00",
                "variance": {"signal_activity_pct": 0.2},
                "winrate": {
                    "win_rate": 0.42,
                    "total_trades": 100,
                    "avg_pnl": -0.001,
                },
            }
        ),
        encoding="utf-8",
    )

    summary = build_dashboards(
        output_root=tmp_path / "output",
        dashboard_dir=tmp_path / "dash",
        render_signals=False,
    )

    assert (tmp_path / "dash" / "raw_strategy_winrates.png").exists()
    assert (tmp_path / "dash" / "raw_strategy_dashboard.json").exists()
    assert summary["reports"][0]["safe_name"] == "demo_strategy"
