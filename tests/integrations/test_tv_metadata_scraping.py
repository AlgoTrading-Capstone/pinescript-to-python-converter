"""
Live Integration Test: TradingView Scraper Metadata Pipeline

Verifies the "plumbing" of the scraping pipeline BEFORE data reaches the AI
evaluation stage:

  1. Browser can click the "Strategy report" and "Description" tabs.
  2. Raw KPI strings are extracted from the Strategy Report DOM.
  3. _parse_metric_to_float cleans those strings into typed Python numbers.
  4. fetch_strategy_metadata() returns a well-formed dict with correct types.
  5. save_to_input() writes a valid, parseable .meta.json sidecar file.

No LLM / Claude agent is invoked anywhere in this test.

Run with:
    .venv/Scripts/python.exe -m pytest tests/integrations/test_tv_metadata_scraping.py -v -s

The -s flag keeps stdout live so you can watch the browser and see the printed
JSON output in the terminal.

Mark: integration  (skipped in fast unit-test runs via -m "not integration")
"""

import json
import pathlib
import tempfile

import pytest
from selenium.common.exceptions import WebDriverException

# ---------------------------------------------------------------------------
# Import the scraper and the standalone parsing utility
# ---------------------------------------------------------------------------
from src.utils.tv_scraper import TradingViewScraper, _parse_metric_to_float

# ---------------------------------------------------------------------------
# Test URL — a well-known public strategy with a populated Strategy Report.
# Change this to any other public strategy URL if the original is removed.
# ---------------------------------------------------------------------------
_TEST_URL = "https:/www.tradingview.com/script/9mfAwbgS/"

# ===========================================================================
# Section 1 — Pure-unit tests for _parse_metric_to_float  (no browser)
# ===========================================================================

class TestParseMetricToFloat:
    """Verify the regex-based cleaning utility against TradingView display formats."""

    def test_plain_integer(self):
        assert _parse_metric_to_float("142") == 142.0

    def test_plain_float(self):
        assert _parse_metric_to_float("1.85") == 1.85

    def test_percentage(self):
        assert _parse_metric_to_float("23.4%") == 23.4

    def test_negative_percentage_ascii(self):
        assert _parse_metric_to_float("-10.5%") == -10.5

    def test_negative_unicode_minus(self):
        # TradingView uses the Unicode minus sign U+2212 for negative values
        assert _parse_metric_to_float("\u221210.5%") == -10.5

    def test_negative_en_dash(self):
        assert _parse_metric_to_float("\u201310.5%") == -10.5

    def test_negative_em_dash(self):
        assert _parse_metric_to_float("\u201410.5%") == -10.5

    def test_thousands_comma(self):
        assert _parse_metric_to_float("1,200.5") == 1200.5

    def test_decimal_comma(self):
        assert _parse_metric_to_float("1,5%") == 1.5

    def test_k_suffix_lowercase(self):
        assert _parse_metric_to_float("1.5k") == 1500.0

    def test_k_suffix_uppercase(self):
        assert _parse_metric_to_float("2.3K") == 2300.0

    def test_m_suffix_uppercase(self):
        assert _parse_metric_to_float("1.2M") == 1200000.0

    def test_parenthesis_negation(self):
        # Drawdown is sometimes shown as "(15.3%)"
        assert _parse_metric_to_float("(15.3%)") == -15.3

    def test_dollar_prefix(self):
        assert _parse_metric_to_float("$1.5") == 1.5

    def test_empty_string_returns_none(self):
        assert _parse_metric_to_float("") is None

    def test_none_input_returns_none(self):
        assert _parse_metric_to_float(None) is None

    def test_non_numeric_returns_none(self):
        assert _parse_metric_to_float("N/A") is None

    def test_whitespace_only_returns_none(self):
        assert _parse_metric_to_float("   ") is None

    def test_combined_noise(self):
        # e.g. "−1,234.56%" with Unicode minus and thousands comma
        assert _parse_metric_to_float("\u22121,234.56%") == pytest.approx(-1234.56)


# ===========================================================================
# Section 2 — Live browser integration tests  (requires Chrome + internet)
# ===========================================================================

@pytest.fixture(scope="module")
def scraper_instance():
    """
    Start a single non-headless Chrome session for the entire module using
    the identical setup as the production scraper (headless=False context
    manager).  No extra flags are added so the browser behaves exactly as
    it does during a real pipeline run.

    Teardown is guaranteed via the context manager even if tests fail.
    """
    with TradingViewScraper(headless=False) as scraper:
        yield scraper


@pytest.fixture(scope="module")
def tmp_input_dir():
    """Isolated temporary directory so tests never pollute the real input/ folder."""
    with tempfile.TemporaryDirectory() as d:
        yield pathlib.Path(d)


@pytest.fixture(scope="module")
def scraped_data(scraper_instance, tmp_input_dir):
    """
    Run the full scrape once and cache the results for all tests in this module.
    Returns a dict with keys: pine, metadata, saved_path, slug.

    Network errors (ERR_NAME_NOT_RESOLVED, timeout) are converted to
    pytest.skip so that live-browser tests skip cleanly instead of
    reporting as ERROR when there is no internet access.
    """
    try:
        pine = scraper_instance.fetch_pinescript(_TEST_URL)
    except WebDriverException as exc:
        msg = str(exc)
        if "ERR_NAME_NOT_RESOLVED" in msg or "ERR_INTERNET_DISCONNECTED" in msg:
            pytest.skip(
                f"No network access from ChromeDriver — "
                f"confirm Chrome can reach tradingview.com and re-run. ({msg[:120]})"
            )
        raise

    assert pine and len(pine) > 50, (
        "fetch_pinescript returned empty/short content — "
        "the strategy may be private or the URL has changed."
    )

    metadata = scraper_instance.fetch_strategy_metadata(_TEST_URL)

    slug = TradingViewScraper._extract_strategy_slug(_TEST_URL)
    saved_path = scraper_instance.save_to_input(
        pine_source=pine,
        strategy_url=_TEST_URL,
        input_dir=str(tmp_input_dir),
        source="integration_test",
        metadata=metadata,
    )

    return {
        "pine":       pine,
        "metadata":   metadata,
        "saved_path": saved_path,
        "slug":       slug,
    }


@pytest.mark.integration
class TestPineScriptExtraction:
    """Verify that the raw PineScript source is extracted correctly."""

    def test_pine_is_string(self, scraped_data):
        assert isinstance(scraped_data["pine"], str)

    def test_pine_contains_version_header(self, scraped_data):
        assert "//@version" in scraped_data["pine"], (
            "Extracted content does not look like PineScript — missing //@version header."
        )

    def test_pine_file_written(self, scraped_data):
        assert scraped_data["saved_path"].exists(), (
            f"Expected .pine file at {scraped_data['saved_path']} was not created."
        )


@pytest.mark.integration
class TestMetadataStructure:
    """Verify the shape and top-level keys of the metadata dict."""

    def test_metadata_is_dict(self, scraped_data):
        assert isinstance(scraped_data["metadata"], dict)

    def test_metadata_has_required_keys(self, scraped_data):
        meta = scraped_data["metadata"]
        assert "url" in meta
        assert "description" in meta
        assert "backtest_metrics" in meta

    def test_url_matches(self, scraped_data):
        assert scraped_data["metadata"]["url"] == _TEST_URL

    def test_backtest_metrics_is_dict(self, scraped_data):
        assert isinstance(scraped_data["metadata"]["backtest_metrics"], dict)

    def test_backtest_metrics_has_all_keys(self, scraped_data):
        bm = scraped_data["metadata"]["backtest_metrics"]
        expected = {"total_trades", "profit_factor", "max_drawdown_pct", "sharpe_ratio"}
        assert expected.issubset(bm.keys()), (
            f"backtest_metrics is missing keys: {expected - set(bm.keys())}"
        )


@pytest.mark.integration
class TestMetricTypes:
    """
    The core type-safety assertions: scraped values must arrive as Python
    numbers (or None), never as raw strings.
    """

    def test_total_trades_is_int_or_none(self, scraped_data):
        val = scraped_data["metadata"]["backtest_metrics"]["total_trades"]
        assert val is None or isinstance(val, int), (
            f"total_trades must be int or None, got {type(val).__name__!r}: {val!r}"
        )

    def test_profit_factor_is_float_or_none(self, scraped_data):
        val = scraped_data["metadata"]["backtest_metrics"]["profit_factor"]
        assert val is None or isinstance(val, float), (
            f"profit_factor must be float or None, got {type(val).__name__!r}: {val!r}"
        )

    def test_max_drawdown_is_float_or_none(self, scraped_data):
        val = scraped_data["metadata"]["backtest_metrics"]["max_drawdown_pct"]
        assert val is None or isinstance(val, float), (
            f"max_drawdown_pct must be float or None, got {type(val).__name__!r}: {val!r}"
        )

    def test_sharpe_ratio_is_float_or_none(self, scraped_data):
        val = scraped_data["metadata"]["backtest_metrics"]["sharpe_ratio"]
        assert val is None or isinstance(val, float), (
            f"sharpe_ratio must be float or None, got {type(val).__name__!r}: {val!r}"
        )

    def test_no_metric_is_a_string(self, scraped_data):
        """Catch any value that slipped through as a raw string (regression guard)."""
        bm = scraped_data["metadata"]["backtest_metrics"]
        string_fields = {k: v for k, v in bm.items() if isinstance(v, str)}
        assert not string_fields, (
            f"The following metrics were returned as raw strings instead of "
            f"numeric types: {string_fields}"
        )

    def test_available_metrics_are_positive(self, scraped_data):
        """Sanity check: profit_factor and total_trades should be > 0 when present."""
        bm = scraped_data["metadata"]["backtest_metrics"]
        if bm["profit_factor"] is not None:
            assert bm["profit_factor"] > 0, (
                f"profit_factor should be positive, got {bm['profit_factor']}"
            )
        if bm["total_trades"] is not None:
            assert bm["total_trades"] > 0, (
                f"total_trades should be positive, got {bm['total_trades']}"
            )


@pytest.mark.integration
class TestSidecarFile:
    """Verify the .meta.json sidecar written by save_to_input()."""

    def test_sidecar_exists(self, scraped_data, tmp_input_dir):
        slug = scraped_data["slug"]
        sidecar = tmp_input_dir / f"{slug}.meta.json"
        assert sidecar.exists(), (
            f"Expected sidecar file {sidecar} was not created by save_to_input()."
        )

    def test_sidecar_is_valid_json(self, scraped_data, tmp_input_dir):
        slug = scraped_data["slug"]
        sidecar = tmp_input_dir / f"{slug}.meta.json"
        raw = sidecar.read_text(encoding="utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            pytest.fail(f"Sidecar file is not valid JSON: {exc}\n\nContent:\n{raw[:500]}")
        assert isinstance(parsed, dict), "Sidecar root should be a JSON object."

    def test_sidecar_backtest_metrics_types(self, scraped_data, tmp_input_dir):
        """Re-parse the sidecar from disk and confirm numeric types survived serialisation."""
        slug = scraped_data["slug"]
        sidecar = tmp_input_dir / f"{slug}.meta.json"
        parsed = json.loads(sidecar.read_text(encoding="utf-8"))
        bm = parsed.get("backtest_metrics", {})

        if bm.get("total_trades") is not None:
            assert isinstance(bm["total_trades"], int), (
                f"total_trades in sidecar should be int, got {type(bm['total_trades'])}"
            )
        if bm.get("profit_factor") is not None:
            assert isinstance(bm["profit_factor"], float), (
                f"profit_factor in sidecar should be float, got {type(bm['profit_factor'])}"
            )

    def test_sidecar_printed_for_visual_verification(self, scraped_data, tmp_input_dir):
        """
        Not a real assertion — prints the full sidecar JSON so you can eyeball
        it in the terminal when running with -s.
        """
        slug = scraped_data["slug"]
        sidecar = tmp_input_dir / f"{slug}.meta.json"
        content = json.loads(sidecar.read_text(encoding="utf-8"))

        separator = "=" * 60
        print(f"\n{separator}")
        print(f"  SIDECAR OUTPUT: {sidecar.name}")
        print(separator)
        print(json.dumps(content, indent=2, ensure_ascii=False))
        print(separator)

        # Truncated description preview
        desc = content.get("description")
        if desc:
            print(f"\n  DESCRIPTION PREVIEW (first 300 chars):")
            print(f"  {desc[:300].replace(chr(10), chr(10) + '  ')}")
        else:
            print("\n  DESCRIPTION: not extracted (indicator/private, or tab absent)")
        print()
