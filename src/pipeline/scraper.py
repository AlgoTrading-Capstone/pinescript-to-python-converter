"""
Scraper bridge — Wraps TradingViewScraper for pipeline use.
"""

from collections import Counter
import json
import logging
import sys
from datetime import datetime, UTC
from pathlib import Path

from rich.table import Table

from src.pipeline import INPUT_DIR, OUTPUT_DIR, SEEN_URLS_PATH, TARGET_STRATEGY_COUNT
from src.pipeline.triage import (
    TriageDecision,
    event_from_decision,
    load_scrape_rejections,
    load_source_quality,
    quality_weight,
    remember_rejection,
    save_scrape_rejections,
    save_source_quality,
    triage_pine_source,
    triage_strategy_metadata,
    update_source_quality,
)
from src.cli.ui import console, print_error, print_info, print_section, print_success, print_warning
from src.scrapers.tradingview import SOURCE_URLS

logger = logging.getLogger("runner")


def _load_seen_urls() -> set[str]:
    """Load the persisted global URL dedup store (O(1) lookup set)."""
    if SEEN_URLS_PATH.exists():
        try:
            return set(json.loads(SEEN_URLS_PATH.read_text(encoding="utf-8-sig")))
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


def _weighted_source_targets(max_results: int, quality: dict[str, dict[str, int]]) -> dict[str, int]:
    """Allocate discovery budget using historical source promotion rates."""
    sources = list(SOURCE_URLS.keys())
    if max_results <= 0 or not sources:
        return {name: 0 for name in sources}
    weights = {source: quality_weight(quality.get(source)) for source in sources}
    total_weight = sum(weights.values()) or 1.0
    raw = {
        source: int(max_results * (weights[source] / total_weight))
        for source in sources
    }
    # Keep each source alive with at least one slot, then distribute remainder
    # to the highest-weight sources.
    targets = {source: max(1, raw[source]) for source in sources}
    while sum(targets.values()) > max_results:
        source = min(targets, key=lambda s: (weights[s], -targets[s]))
        if targets[source] <= 1:
            break
        targets[source] -= 1
    while sum(targets.values()) < max_results:
        source = max(targets, key=lambda s: weights[s])
        targets[source] += 1
    return targets


def _write_scrape_report(
    *,
    events: list[dict],
    requested: int,
    source_targets: dict[str, int],
    saved: int,
    failed: int,
    skipped_existing: int,
) -> Path:
    """Write JSON/Markdown/PNG evidence for a scrape run."""
    ts = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    report_dir = OUTPUT_DIR / "scrape_reports" / ts
    report_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "requested": requested,
        "saved": saved,
        "failed": failed,
        "skipped_existing": skipped_existing,
        "source_targets": source_targets,
        "events": events,
    }
    (report_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    promoted = [e for e in events if e.get("status") == "promoted"]
    rejected = [e for e in events if e.get("status") != "promoted"]
    lines = [
        "# Scrape Triage Report",
        "",
        f"- Requested saves: {requested}",
        f"- Promoted to input: {saved}",
        f"- Rejected before promotion: {len(rejected)}",
        f"- Failures: {failed}",
        "",
        "## Promoted candidates",
        "",
        "| Source | Slug | Trades | PF | DD% | Sharpe |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for event in promoted:
        metrics = event.get("metrics") or {}
        lines.append(
            f"| {event.get('source', '')} | `{event.get('slug', '')}` | "
            f"{metrics.get('total_trades', '')} | {metrics.get('profit_factor', '')} | "
            f"{metrics.get('max_drawdown_pct', '')} | {metrics.get('sharpe_ratio', '')} |"
        )
    lines.extend(["", "## Rejections", "", "| Source | Slug | Reason |", "|---|---|---|"])
    for event in rejected:
        lines.append(
            f"| {event.get('source', '')} | `{event.get('slug', '')}` | "
            f"{event.get('reason_code', '')}: {event.get('reason', '')} |"
        )
    (report_dir / "shortlist.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return report_dir

    by_source: Counter[str] = Counter(e.get("source", "unknown") for e in events)
    promoted_by_source: Counter[str] = Counter(
        e.get("source", "unknown") for e in promoted
    )
    if by_source:
        labels = list(by_source.keys())
        rejected_counts = [by_source[l] - promoted_by_source[l] for l in labels]
        promoted_counts = [promoted_by_source[l] for l in labels]
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.bar(labels, rejected_counts, color="#e74c3c", label="Rejected")
        ax.bar(labels, promoted_counts, bottom=rejected_counts, color="#2ecc71", label="Promoted")
        ax.set_title("Scrape Funnel by Source", fontweight="bold")
        ax.set_ylabel("Candidates")
        ax.legend()
        fig.tight_layout()
        fig.savefig(report_dir / "funnel_by_source.png", dpi=150, bbox_inches="tight")
        plt.close(fig)

    reasons = Counter(e.get("reason_code", "accepted") for e in rejected)
    if reasons:
        labels = list(reasons.keys())
        values = [reasons[l] for l in labels]
        fig, ax = plt.subplots(figsize=(9, max(3, len(labels) * 0.35)))
        ax.barh(labels, values, color="#c0392b")
        ax.set_title("Scrape Rejection Reasons", fontweight="bold")
        ax.set_xlabel("Candidates")
        fig.tight_layout()
        fig.savefig(report_dir / "rejection_reasons.png", dpi=150, bbox_inches="tight")
        plt.close(fig)

    metric_events = [
        e for e in events
        if (e.get("metrics") or {}).get("profit_factor") is not None
        and (e.get("metrics") or {}).get("max_drawdown_pct") is not None
    ]
    if metric_events:
        colors = ["#2ecc71" if e.get("status") == "promoted" else "#e74c3c" for e in metric_events]
        x = [float((e.get("metrics") or {}).get("profit_factor") or 0) for e in metric_events]
        y = [float((e.get("metrics") or {}).get("max_drawdown_pct") or 0) for e in metric_events]
        fig, ax = plt.subplots(figsize=(7, 5))
        ax.scatter(x, y, c=colors, alpha=0.75, edgecolors="#333333", linewidths=0.3)
        ax.axvline(1.0, color="#555555", linestyle="--", linewidth=1)
        ax.axhline(50.0, color="#555555", linestyle="--", linewidth=1)
        ax.set_title("Candidate Metrics: PF vs Drawdown", fontweight="bold")
        ax.set_xlabel("Profit factor")
        ax.set_ylabel("Max drawdown (%)")
        fig.tight_layout()
        fig.savefig(report_dir / "metrics_scatter.png", dpi=150, bbox_inches="tight")
        plt.close(fig)

    return report_dir


def run_tv_scraper(max_results: int = 6, *, exit_on_empty: bool = True) -> int:
    """
    Populate input/ by scraping public TradingView strategies.

    Fetches from Popular + Editor's Picks, using data/seen_urls.json for dedup.
    """
    if max_results <= 0:
        return 0

    quality = load_source_quality()
    discovery_budget = max(max_results, max_results * 4)
    source_targets = _weighted_source_targets(discovery_budget, quality)
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
        from src.scrapers.tradingview import TradingViewScraper
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
    scrape_rejections = load_scrape_rejections()
    seen_urls.update(scrape_rejections.keys())
    logger.info(f"Loaded {len(seen_urls)} previously-seen URL(s) from {SEEN_URLS_PATH}")

    saved = 0
    failed = 0
    skipped_existing = 0
    discovered_counts: Counter[str] = Counter()
    processed_counts: Counter[str] = Counter()
    urls: list[tuple[str, str]] = []
    events: list[dict] = []

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
                    meta = scraper.fetch_strategy_metadata(url)
                    meta_decision = triage_strategy_metadata(meta)
                    if not meta_decision.accepted:
                        remember_rejection(
                            scrape_rejections,
                            url=url,
                            source=scrape_source,
                            decision=meta_decision,
                        )
                        seen_urls.add(url)
                        events.append(event_from_decision(
                            url=url,
                            source=scrape_source,
                            slug=slug,
                            stage="metadata",
                            decision=meta_decision,
                        ))
                        console.print(
                            f"[muted][triage][/muted] {slug} [{scrape_source}] "
                            f"[warning][REJECT][/warning] {meta_decision.reason_code}: "
                            f"{meta_decision.reason}"
                        )
                        logger.info(
                            "Metadata rejected %s [%s]: %s",
                            slug,
                            scrape_source,
                            meta_decision.reason,
                        )
                        continue

                    pine = scraper.fetch_pinescript(url)
                    source_decision = triage_pine_source(pine, meta)
                    if not source_decision.accepted:
                        remember_rejection(
                            scrape_rejections,
                            url=url,
                            source=scrape_source,
                            decision=source_decision,
                            status="source_rejected",
                        )
                        seen_urls.add(url)
                        event = event_from_decision(
                            url=url,
                            source=scrape_source,
                            slug=slug,
                            stage="source",
                            decision=source_decision,
                        )
                        event["status"] = "source_rejected"
                        events.append(event)
                        console.print(
                            f"[muted][source][/muted] {slug} [{scrape_source}] "
                            f"[warning][REJECT][/warning] {source_decision.reason_code}: "
                            f"{source_decision.reason}"
                        )
                        logger.info(
                            "Source rejected %s [%s]: %s",
                            slug,
                            scrape_source,
                            source_decision.reason,
                        )
                        continue

                    scraper.save_to_input(pine, url, source=scrape_source, metadata=meta)
                    processed_counts[scrape_source] += 1
                    event = event_from_decision(
                        url=url,
                        source=scrape_source,
                        slug=slug,
                        stage="source",
                        decision=meta_decision,
                    )
                    event["status"] = "promoted"
                    events.append(event)
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
                    decision = TriageDecision(
                        accepted=False,
                        reason_code="extraction_failed",
                        reason=first_line,
                    )
                    remember_rejection(
                        scrape_rejections,
                        url=url,
                        source=scrape_source,
                        decision=decision,
                        status="extraction_failed",
                    )
                    seen_urls.add(url)
                    event = event_from_decision(
                        url=url,
                        source=scrape_source,
                        slug=slug,
                        stage="extraction",
                        decision=decision,
                    )
                    event["status"] = "extraction_failed"
                    events.append(event)
                    console.print(
                        f"[muted][{saved + 1}/{max_results}][/muted] "
                        f"{slug} [{scrape_source}] [warning][SKIP][/warning] {first_line}"
                    )
                    logger.warning(f"Skipped {slug}: {first_line}")
                    failed += 1
                except Exception as exc:
                    decision = TriageDecision(
                        accepted=False,
                        reason_code="scrape_error",
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                    remember_rejection(
                        scrape_rejections,
                        url=url,
                        source=scrape_source,
                        decision=decision,
                        status="scrape_error",
                    )
                    seen_urls.add(url)
                    event = event_from_decision(
                        url=url,
                        source=scrape_source,
                        slug=slug,
                        stage="extraction",
                        decision=decision,
                    )
                    event["status"] = "scrape_error"
                    events.append(event)
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
        save_scrape_rejections(scrape_rejections)
        quality = update_source_quality(quality, events)
        save_source_quality(quality)
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

    report_dir = _write_scrape_report(
        events=events,
        requested=max_results,
        source_targets=source_targets,
        saved=saved,
        failed=failed,
        skipped_existing=skipped_existing,
    )
    print_info(f"Scrape report -> {report_dir}")

    print_success(f"Scraped {saved} strategy file(s) -> input/")
    if failed:
        print_warning(f"Skipped {failed} file(s) (private or unsupported)")

    if saved == 0:
        print_error("No strategies could be scraped.")
        print_info("Manual fallback: paste PineScript into input/source_strategy.pine")
        if exit_on_empty:
            sys.exit(1)
        return 0
    return saved
