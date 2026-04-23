"""
Scraper bridge — Wraps TradingViewScraper for pipeline use.
"""

from collections import Counter
import json
import logging
import sys
from pathlib import Path

from rich.table import Table

from src.pipeline import INPUT_DIR, SEEN_URLS_PATH, TARGET_STRATEGY_COUNT
from src.pipeline.ui import console, print_error, print_info, print_section, print_success, print_warning
from src.utils.tv_scraper import SOURCE_URLS

logger = logging.getLogger("runner")


def _load_seen_urls() -> set[str]:
    """Load the persisted global URL dedup store (O(1) lookup set)."""
    if SEEN_URLS_PATH.exists():
        try:
            return set(json.loads(SEEN_URLS_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError):
            logger.warning("seen_urls.json is corrupt — starting fresh.")
    return set()


def _save_seen_urls(seen_urls: set[str]) -> None:
    """Persist the global URL dedup store back to disk."""
    SEEN_URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_URLS_PATH.write_text(json.dumps(sorted(seen_urls), indent=2), encoding="utf-8")


def _allocate_source_targets(max_results: int) -> dict[str, int]:
    """Distribute ``max_results`` evenly across :data:`SOURCE_URLS`.

    Iteration order follows ``SOURCE_URLS`` insertion order. When
    ``max_results`` isn't a clean multiple of the source count, the remainder
    is assigned to the earliest sources so the crypto-focused listings win
    on odd totals.
    """
    sources = list(SOURCE_URLS.keys())
    if max_results <= 0 or not sources:
        return {name: 0 for name in sources}
    base = max_results // len(sources)
    extra = max_results - base * len(sources)
    return {s: base + (1 if i < extra else 0) for i, s in enumerate(sources)}


def run_tv_scraper(max_results: int = 6) -> None:
    """
    Populate input/ by scraping public TradingView strategies.

    Fetches from Popular + Editor's Picks, using data/seen_urls.json for dedup.
    """
    if max_results <= 0:
        return

    source_targets = _allocate_source_targets(max_results)
    plan = ", ".join(f"{k}×{v}" for k, v in source_targets.items() if v > 0)
    print_section("Scraper")
    print_info(f"input/ has fewer than {TARGET_STRATEGY_COUNT} strategies.")
    print_info(f"Need {max_results} more strategy file(s) from TradingView.")
    print_info(f"Source allocation: {plan}")

    # Block tv_scraper's logging.basicConfig from adding a root StreamHandler.
    _root_log = logging.getLogger()
    if not _root_log.handlers:
        _root_log.addHandler(logging.NullHandler())

    try:
        from src.utils.tv_scraper import TradingViewScraper
    except ImportError as exc:
        print_error(f"Cannot import TradingViewScraper: {exc}")
        print_info("Install missing deps: pip install selenium webdriver-manager")
        sys.exit(1)

    # Redirect scraper / driver logs to our file handler — off the terminal.
    for _lgr_name in ("TV_Scraper", "WDM", "selenium", "urllib3"):
        _lgr = logging.getLogger(_lgr_name)
        _lgr.handlers.clear()
        for _h in logger.handlers:
            _lgr.addHandler(_h)
        _lgr.propagate = False

    seen_urls = _load_seen_urls()
    logger.info(f"Loaded {len(seen_urls)} previously-seen URL(s) from {SEEN_URLS_PATH}")

    saved = 0
    failed = 0
    skipped_existing = 0
    discovered_counts: Counter[str] = Counter()
    processed_counts: Counter[str] = Counter()
    urls: list[tuple[str, str]] = []

    try:
        with TradingViewScraper(headless=False) as scraper:
            urls = scraper.fetch_from_sources(
                source_targets=source_targets,
                seen_urls=seen_urls,
            )
            discovered_counts.update(source for _, source in urls)
            logger.info(f"TV scraper found {len(urls)} new strategy URL(s) across both sources")

            for url, scrape_source in urls:
                if saved >= max_results:
                    break

                slug = TradingViewScraper._extract_strategy_slug(url)
                dest = INPUT_DIR / f"{slug}.pine"

                if dest.exists():
                    logger.info(f"Skipping already-downloaded: {slug}")
                    seen_urls.add(url)
                    skipped_existing += 1
                    continue

                try:
                    pine = scraper.fetch_pinescript(url)
                    meta = scraper.fetch_strategy_metadata(url)
                    scraper.save_to_input(pine, url, source=scrape_source, metadata=meta)
                    processed_counts[scrape_source] += 1
                    metrics_summary = ""
                    if meta and meta.get("backtest_metrics"):
                        bm = meta["backtest_metrics"]
                        metrics_summary = (
                            f" | trades={bm.get('total_trades')} "
                            f"pf={bm.get('profit_factor')} "
                            f"dd={bm.get('max_drawdown_pct')}%"
                        )
                    console.print(
                        f"[muted][{saved + 1}/{max_results}][/muted] "
                        f"{slug} [{scrape_source}] [success][OK][/success] "
                        f"({len(pine):,} chars{metrics_summary})"
                    )
                    logger.info(f"Scraped: {slug} [{scrape_source}] ({len(pine)} chars{metrics_summary})")
                    seen_urls.add(url)
                    saved += 1
                except NotImplementedError as exc:
                    first_line = str(exc).splitlines()[0]
                    console.print(
                        f"[muted][{saved + 1}/{max_results}][/muted] "
                        f"{slug} [{scrape_source}] [warning][SKIP][/warning] {first_line}"
                    )
                    logger.warning(f"Skipped {slug}: {first_line}")
                    failed += 1
                except Exception as exc:
                    console.print(
                        f"[muted][{saved + 1}/{max_results}][/muted] "
                        f"{slug} [{scrape_source}] [error][FAIL][/error] {exc}"
                    )
                    logger.exception(f"Error scraping {slug}: {exc}")
                    failed += 1

    except RuntimeError as exc:
        print_error(f"Scraper runtime error: {exc}")
        logger.error(f"TV scraper runtime error: {exc}")
        sys.exit(1)
    finally:
        _save_seen_urls(seen_urls)
        logger.info(f"Saved {len(seen_urls)} URL(s) to {SEEN_URLS_PATH}")

    def _breakdown(counts: Counter[str]) -> str:
        return ", ".join(f"{name}={counts.get(name, 0)}" for name in SOURCE_URLS)

    summary = Table(title="Scrape Summary", expand=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value")
    summary.add_row("Requested", str(max_results))
    summary.add_row(
        "Source plan",
        ", ".join(f"{k}={v}" for k, v in source_targets.items()),
    )
    summary.add_row(
        "Discovered URLs",
        f"{len(urls)} total ({_breakdown(discovered_counts)})",
    )
    summary.add_row(
        "Saved files",
        f"{saved} ({_breakdown(processed_counts)})",
    )
    summary.add_row("Existing skips", str(skipped_existing))
    summary.add_row("Failures", str(failed))
    console.print(summary)

    print_success(f"Scraped {saved} strategy file(s) -> input/")
    if failed:
        print_warning(f"Skipped {failed} file(s) (private or unsupported)")

    if saved == 0:
        print_error("No strategies could be scraped.")
        print_info("Manual fallback: paste PineScript into input/source_strategy.pine")
        sys.exit(1)