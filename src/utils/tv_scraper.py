"""
TradingView Scraper Utility

Uses Selenium to:
  1. Scrape the public strategies listing page and return strategy URLs.
  2. Navigate to each strategy, click "Source code" tab, click the copy button,
     capture the clipboard content, and save to input/{strategy_name}.pine.

Manual-insert fallback:
  Paste PineScript directly into `input/source_strategy.pine` and run
  `runner.py` — no scraper required.

Extraction strategy (in order):
  1. Pine Facade API  — fast, no browser, works for public open-source scripts.
  2. Clipboard intercept — inject JS to capture navigator.clipboard.writeText,
     then click the copy button in the Source code panel (all lines, even
     if the editor uses virtual scrolling so not all lines are in the DOM).
  3. PowerShell clipboard read — Windows fallback after clicking copy button.
  4. DOM JS extraction — last resort for non-virtual editors.
"""

import hashlib
import re
import time
import logging
import subprocess
import urllib.request
import json
from pathlib import Path
from typing import Optional
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TV_Scraper")

# ── Strategy Report / Description tab selectors ───────────────────────────────

# XPath candidates for the "Description" tab on a strategy page.
_DESCRIPTION_TAB_XPATHS = [
    "//*[@role='tab'][normalize-space(.)='Description']",
    "//*[normalize-space(.)='Description' and (self::button or self::a or self::span or self::div)]",
    "//*[normalize-space(.)='Description']",
]

# XPath candidates for the "Strategy report" / "Strategy Tester" tab.
# Priority order: exact label seen in the TradingView UI image (lowercase 'r'),
# then legacy capitalisation variants, then broad element-type fallbacks.
_STRATEGY_TESTER_TAB_XPATHS = [
    "//*[@role='tab'][normalize-space(.)='Strategy report']",
    "//*[@role='tab'][normalize-space(.)='Strategy Tester']",
    "//*[@role='tab'][normalize-space(.)='Strategy Report']",
    "//*[normalize-space(.)='Strategy report' and (self::button or self::a or self::span or self::div)]",
    "//*[normalize-space(.)='Strategy Tester' and (self::button or self::a or self::span or self::div)]",
    "//*[normalize-space(.)='Strategy Report' and (self::button or self::a or self::span or self::div)]",
]

# XPath used for a fast existence probe (3 s) to distinguish strategies from
# indicators before attempting the full Strategy Report extraction.
_STRATEGY_TAB_PROBE_XPATH = (
    "//*[@role='tab' and ("
    "normalize-space(.)='Strategy report' or "
    "normalize-space(.)='Strategy Tester' or "
    "normalize-space(.)='Strategy Report'"
    ")]"
)

# XPath candidates for the "Overview" / "Equity curve" sub-tab that appears
# inside the Strategy Report panel after the tab is clicked.  Presence of any
# of these confirms the panel has fully rendered.
_EQUITY_SUBTAB_XPATHS = [
    "//*[normalize-space(.)='Equity chart']",
    "//*[normalize-space(.)='Performance']",
    "//*[normalize-space(.)='Trades analysis']",
]

# CSS selectors for the description body text inside the description panel.
_DESCRIPTION_BODY_SELECTORS = [
    "[class*='description'] [class*='body']",
    "[class*='Description'] [class*='Body']",
    "[class*='description-body']",
    "[class*='scriptDescription']",
    "[class*='script-description']",
    "[data-name='description']",
]

# Label texts used to locate KPI cells in the Strategy Report overview table.
# All entries are stored in lowercase; the JS snippet matches case-insensitively
# so capitalisation differences across TradingView UI versions are handled.
_KPI_LABEL_MAP = {
    "total_trades":     ["total trades", "кількість угод"],
    "profit_factor":    ["profit factor", "фактор прибутку"],
    "max_drawdown_pct": ["max equity drawdown", "max drawdown", "макс. просадка"],
    "sharpe_ratio":     ["sharpe ratio"],
}

# JS snippet: case-insensitive label scan — returns the adjacent value cell text.
# arguments[0] is the lowercase candidate label string.
_JS_KPI_BY_LABEL = """
(function(labelText) {
    var needle = labelText.toLowerCase();
    var els = document.querySelectorAll('*');
    for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (el.children.length === 0 && el.textContent.trim().toLowerCase() === needle) {
            var parent = el.parentElement;
            if (!parent) continue;
            var siblings = parent.parentElement ? parent.parentElement.children : [];
            for (var j = 0; j < siblings.length; j++) {
                if (siblings[j] !== parent) {
                    var val = siblings[j].textContent.trim();
                    if (val) return val;
                }
            }
            // Fallback: next sibling of the label element itself
            var next = el.nextElementSibling;
            if (next) return next.textContent.trim();
        }
    }
    return null;
})(arguments[0]);
"""

# ── Metric parsing utility ────────────────────────────────────────────────────

# Regex to extract the first signed numeric token from a cleaned string.
_NUMERIC_RE = re.compile(r"[-+]?\d*\.\d+|\d+")

# Unicode minus variants displayed by TradingView (en-dash, em-dash, minus sign).
_UNICODE_DASHES = str.maketrans({
    "\u2013": "-",  # en-dash  –
    "\u2014": "-",  # em-dash  —
    "\u2212": "-",  # minus    −
})


def _parse_metric_to_float(raw_str: Optional[str]) -> Optional[float]:
    """
    Robustly convert a TradingView KPI string to a float.

    Handles:
    - Unicode dash variants (–, —, −) → ASCII hyphen
    - Thousands commas ("1,200.5" → 1200.5)
    - Percentage signs ("−10.5%" → -10.5)
    - Currency symbols ("$1.5" → 1.5)
    - Parenthesis negation ("(5.3)" → -5.3)
    - "K" suffix ("1.5K" → 1500.0)

    Returns None if the input is empty, None, or contains no parseable number.
    """
    if not raw_str:
        return None

    s = raw_str.translate(_UNICODE_DASHES).strip()

    # Convert parenthesis notation "(x)" → "-x" before stripping other chars.
    negative_parens = s.startswith("(") and s.endswith(")")
    s = s.replace("(", "").replace(")", "")
    if negative_parens and not s.startswith("-"):
        s = "-" + s

    # Strip visual noise characters.
    s = s.replace("%", "").replace("$", "").replace(",", "").strip()

    k_suffix = s.upper().endswith("K")
    if k_suffix:
        s = s[:-1].strip()

    match = _NUMERIC_RE.search(s)
    if not match:
        return None

    # The regex [-+]?\d*\.\d+|\d+ captures the sign when it is directly
    # adjacent to the digits.  The guard below handles the rare case where
    # a space separates the minus from the digits (e.g. "- 10.5") so the
    # sign is preserved even if the regex matched only the numeric part.
    if s.lstrip().startswith("-") and not match.group().startswith("-"):
        signed_str = "-" + match.group()
    else:
        signed_str = match.group()

    try:
        value = float(signed_str)
        return value * 1000.0 if k_suffix else value
    except ValueError:
        return None


# ── Listing page constants ─────────────────────────────────────────────────────
STRATEGIES_LISTING_URL = "https://www.tradingview.com/scripts/?script_type=strategies"
EDITORS_PICKS_URL      = "https://www.tradingview.com/scripts/editors-picks/?script_type=strategies"
_MAX_PAGES = 20  # Hard cap for pagination to prevent runaway scraping
_LISTING_ANCHOR_SELECTOR = "a[href*='/script/']"
_LISTING_LOAD_MORE_XPATHS = [
    "//*[self::button or self::a or @role='button'][contains(normalize-space(.), 'Show more publications')]",
    "//*[self::button or self::a or @role='button'][contains(normalize-space(.), 'Load more')]",
    "//*[self::button or self::a or @role='button'][contains(normalize-space(.), 'Show more')]",
]
_SCRIPT_URL_RE   = re.compile(r"/script/[A-Za-z0-9]+-[^/]+/$")
_SCRIPT_PARTS_RE = re.compile(r"/script/([A-Za-z0-9]+)-([^/]+)/?$")

# ── Individual script page ────────────────────────────────────────────────────

# XPath to click the "Source code" tab (text-based, stable across DOM changes).
_SOURCE_TAB_XPATHS = [
    "//*[normalize-space(.)='Source code' and (self::button or self::a or self::span or self::div)]",
    "//*[@role='tab'][normalize-space(.)='Source code']",
    "//*[normalize-space(.)='Source code']",
]

# XPath to click the copy button inside the Source code panel.
# Update if TradingView changes the button's aria-label or title.
_COPY_BTN_XPATHS = [
    "//button[@aria-label='Copy']",
    "//button[@aria-label='Copy source']",
    "//button[@aria-label='Copy source code']",
    "//button[@title='Copy']",
    "//button[@title='Copy source']",
    "//*[@data-name='copy']",
    "//*[contains(@class,'copyButton') or contains(@class,'copy-button')]",
    # Broadest: any button inside the script-preview container
    "//*[contains(@class,'script') or contains(@class,'Script') or "
    "contains(@class,'source') or contains(@class,'Source')]//button",
]

# Clipboard interceptor: override navigator.clipboard.writeText AND
# document.execCommand so we capture whichever mechanism TV uses.
_JS_INTERCEPT_CLIPBOARD = """
(function() {
    window.__tvClipboard = null;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        var orig = navigator.clipboard.writeText.bind(navigator.clipboard);
        navigator.clipboard.writeText = function(text) {
            window.__tvClipboard = text;
            return orig(text);
        };
    }
    var origExec = document.execCommand.bind(document);
    document.execCommand = function(cmd, showUI, value) {
        var result = origExec(cmd, showUI, value);
        if (cmd === 'copy') {
            var sel = window.getSelection();
            if (sel) window.__tvClipboard = sel.toString();
        }
        return result;
    };
})();
"""

# Fallback JS extraction (works when code is fully in DOM, not virtual-scrolled).
_JS_EXTRACT_CODE = """
(function() {
    var cm6 = document.querySelector('.cm-editor');
    if (cm6) {
        var lines = cm6.querySelectorAll('.cm-line');
        if (lines.length > 0)
            return Array.from(lines).map(function(l){return l.textContent;}).join('\\n');
    }
    var cm5el = document.querySelector('.CodeMirror');
    if (cm5el && cm5el.CodeMirror) return cm5el.CodeMirror.getValue();
    var pre = document.querySelector('[class*="tv-script-preview"] pre, [class*="scriptPreview"] pre, pre');
    if (pre) return pre.textContent;
    return null;
})();
"""

_WAIT_TIMEOUT = 15
_PINE_FACADE_URL = (
    "https://pine-facade.tradingview.com/pine-facade/get/{script_id}?fields=source_code"
)

# Absolute path to the project's input/ directory, independent of working directory.
# tv_scraper.py lives at <project>/src/utils/tv_scraper.py → go up 3 levels.
_PROJECT_INPUT_DIR = str(Path(__file__).resolve().parent.parent.parent / "input")


class TradingViewScraper:
    def __init__(self, headless: bool = False):
        self.options = Options()
        if headless:
            self.options.add_argument("--headless=new")
        self.options.add_argument("--disable-blink-features=AutomationControlled")
        self.options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        self.options.add_experimental_option("excludeSwitches", ["enable-automation"])
        self.options.add_experimental_option("useAutomationExtension", False)
        self.driver = None

    # ── Driver lifecycle ──────────────────────────────────────────────────────

    def start_driver(self):
        logger.info("Starting Selenium WebDriver...")
        self.driver = webdriver.Chrome(
            service=ChromeService(ChromeDriverManager().install()),
            options=self.options,
        )
        self.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
        )

    def close_driver(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                # Neutralise __del__ on the uc.Chrome object: after quit() the
                # Windows handle is gone, so a second quit() call from __del__
                # raises OSError WinError 6.  Replacing quit with a no-op
                # prevents that without patching the third-party library.
                try:
                    self.driver.quit = lambda: None
                except Exception:
                    pass
                self.driver = None
            logger.info("WebDriver closed.")

    def __enter__(self):
        self.start_driver()
        return self

    def __exit__(self, *_):
        self.close_driver()

    # ── Public API ────────────────────────────────────────────────────────────

    def _collect_listing_urls(self, max_results: int) -> list[str]:
        elements = self.driver.find_elements(By.CSS_SELECTOR, _LISTING_ANCHOR_SELECTOR)
        seen: set[str] = set()
        urls: list[str] = []
        for el in elements:
            href = el.get_attribute("href") or ""
            if _SCRIPT_URL_RE.search(href) and href not in seen:
                seen.add(href)
                urls.append(href)
                if len(urls) >= max_results:
                    break
        return urls

    def _expand_listing_once(self, current_count: int) -> bool:
        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        try:
            WebDriverWait(self.driver, 5).until(
                lambda d: len(self._collect_listing_urls(current_count + 1)) > current_count
            )
            return True
        except Exception:
            pass

        for xpath in _LISTING_LOAD_MORE_XPATHS:
            try:
                control = WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});",
                    control,
                )
                self.driver.execute_script("arguments[0].click();", control)
                WebDriverWait(self.driver, 8).until(
                    lambda d: len(self._collect_listing_urls(current_count + 1)) > current_count
                )
                return True
            except Exception:
                continue

        return False

    def fetch_strategy_list(
        self,
        page_url: str = STRATEGIES_LISTING_URL,
        max_results: int = 10,
        page: int = 1,
    ) -> list[str]:
        """Scrape listing page and return up to max_results strategy URLs.

        Args:
            page_url:    Base listing URL to scrape.
            max_results: Maximum number of URLs to return.
            page:        Page number to fetch (1-based). Pass explicitly for
                         deterministic pagination; defaults to 1.
        """
        if not self.driver:
            self.start_driver()

        logger.info(f"Fetching strategy list from: {page_url}")
        self.driver.get(page_url)

        try:
            WebDriverWait(self.driver, _WAIT_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, _LISTING_ANCHOR_SELECTOR))
            )
        except Exception:
            raise RuntimeError(
                f"Timed out waiting for strategy links on {page_url}.\n"
                "The page may require login or is rate-limiting.\n"
                "Manual fallback: paste PineScript into input/source_strategy.pine"
            )

        urls = self._collect_listing_urls(max_results)
        expansion_rounds = 0
        while len(urls) < max_results and expansion_rounds < _MAX_PAGES:
            expansion_rounds += 1
            if not self._expand_listing_once(len(urls)):
                break
            updated_urls = self._collect_listing_urls(max_results)
            if len(updated_urls) <= len(urls):
                break
            urls = updated_urls

        logger.info(f"Found {len(urls)} strategy URL(s).")
        return urls

    def _fetch_new_urls(
        self,
        base_url: str,
        target: int,
        seen_urls: set[str],
    ) -> list[str]:
        """Paginate through base_url, returning up to target URLs not in seen_urls.

        Starts at page 1 and increments until target is reached, the listing
        is exhausted (page returns 0 results), or _MAX_PAGES is hit.
        """
        results: list[str] = []
        listing_pool = self.fetch_strategy_list(
            page_url=base_url,
            max_results=max(50, target * 10),
            page=1,
        )
        for url in listing_pool:
            if url not in seen_urls and url not in results:
                results.append(url)
                if len(results) >= target:
                    break

        return results

    def fetch_from_two_sources(
        self,
        popular_target: int,
        editors_target: int,
        seen_urls: set[str],
    ) -> list[tuple[str, str]]:
        """Fetch new URLs from Popular and Editor's Picks with explicit budgets.

        Cross-source deduplication is enforced: an Editor's Picks URL that already
        appeared in the Popular results (or in seen_urls) is skipped, so the combined
        list contains only fully unique, never-before-seen URLs.

        Returns:
            list of (url, source_tag) tuples where source_tag is "popular" or
            "editors_pick". Up to popular_target + editors_target entries
            (may be fewer if listings are exhausted).
        """
        popular = self._fetch_new_urls(STRATEGIES_LISTING_URL, popular_target, seen_urls)
        logger.info(f"Popular source: {len(popular)} new URL(s) found.")

        # Pass the union of persisted seen_urls + freshly collected popular URLs
        # so Editor's Picks deduplication works across both sources.
        combined_seen = seen_urls | set(popular)
        editors = self._fetch_new_urls(EDITORS_PICKS_URL, editors_target, combined_seen)
        logger.info(f"Editor's Picks source: {len(editors)} new URL(s) found.")

        popular_tagged = [(url, "popular") for url in popular]
        editors_tagged = [(url, "editors_pick") for url in editors]
        return popular_tagged + editors_tagged

    def fetch_pinescript(self, strategy_url: str) -> Optional[str]:
        """
        Extract PineScript source from a strategy page.

        Tries in order: API → clipboard intercept → PowerShell clipboard → DOM JS.
        Raises NotImplementedError with diagnostic guidance if all fail.
        """
        # 1. Pine Facade API (no browser needed)
        source = self._fetch_via_api(strategy_url)
        if source:
            logger.info(f"[API] Got {len(source)} chars.")
            return source

        # 2. Browser-based extraction
        if not self.driver:
            self.start_driver()

        logger.info(f"[DOM] Loading: {strategy_url}")
        self.driver.get(strategy_url)
        time.sleep(3)

        if not self._click_source_tab():
            logger.warning("Could not click 'Source code' tab.")
        else:
            time.sleep(2)

        # 3. Clipboard intercept: inject interceptor then click copy button
        source = self._extract_via_clipboard_intercept()
        if source:
            logger.info(f"[CLIP-JS] Got {len(source)} chars.")
            return source

        # 4. PowerShell clipboard read (Windows): click copy button then read OS clipboard
        source = self._extract_via_powershell_clipboard()
        if source:
            logger.info(f"[CLIP-PS] Got {len(source)} chars.")
            return source

        # 5. DOM JS fallback (works only when code is not virtual-scrolled)
        source = self._extract_code_js()
        if source:
            logger.info(f"[DOM-JS] Got {len(source)} chars.")
            return source

        raise NotImplementedError(
            f"Could not extract PineScript from:\n  {strategy_url}\n\n"
            "Possible causes:\n"
            "  • Script is private / invite-only (requires TradingView login).\n"
            "  • TradingView changed their copy button — open the page in\n"
            "    Chrome DevTools, find the copy button, and update\n"
            "    _COPY_BTN_XPATHS at the top of tv_scraper.py.\n\n"
            "  • Manual fallback: paste PineScript into input/source_strategy.pine\n"
            "    and run main.py directly."
        )

    def fetch_strategy_metadata(self, strategy_url: str) -> dict:
        """
        Best-effort extraction of the author's description and Strategy Report
        KPIs (Total Trades, Profit Factor, Max Drawdown %, Sharpe Ratio).

        Assumes the driver is already on the strategy page (called after
        fetch_pinescript).  If the driver is not initialised, starts it and
        navigates to the URL.

        Returns a dict:
            {
                "url": str,
                "description": str | None,
                "backtest_metrics": {
                    "total_trades":     int | None,
                    "profit_factor":    float | None,
                    "max_drawdown_pct": float | None,
                    "sharpe_ratio":     float | None,
                },
            }
        All inner values default to None; extraction errors are logged as
        warnings and never propagate to the caller.
        """
        result: dict = {
            "url": strategy_url,
            "description": None,
            "backtest_metrics": {
                "total_trades":     None,
                "profit_factor":    None,
                "max_drawdown_pct": None,
                "sharpe_ratio":     None,
            },
        }

        if not self.driver:
            self.start_driver()
            try:
                self.driver.get(strategy_url)
                WebDriverWait(self.driver, _WAIT_TIMEOUT).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
                )
            except Exception as exc:
                logger.warning(f"[META] Could not navigate to {strategy_url}: {exc}")
                return result

        result["description"]     = self._extract_description_text()
        result["backtest_metrics"] = self._extract_strategy_report_metrics()
        return result

    def save_to_input(
        self,
        pine_source: str,
        strategy_url: str,
        input_dir: str = _PROJECT_INPUT_DIR,
        source: str = "",
        metadata: Optional[dict] = None,
    ) -> Path:
        """Save PineScript to input/{strategy_slug}.pine.

        Deduplication is always based on the RAW PineScript content (no source
        comment), so the same strategy is correctly identified as a duplicate
        regardless of whether it was scraped or manually pasted.  The
        ``// SOURCE:`` comment is injected only after the uniqueness check
        passes, immediately before writing to disk.

        Args:
            pine_source:  Raw PineScript code as fetched from TradingView.
            strategy_url: URL used to derive the output filename slug.
            input_dir:    Destination directory (default: project ``input/``).
            source:       Scrape origin tag injected as the first line comment
                          (e.g. ``"popular"`` or ``"editors_pick"``).  Empty
                          string (default) means no comment is prepended —
                          used for manually-inserted files.
        """
        slug = self._extract_strategy_slug(strategy_url)
        output_path = Path(input_dir) / f"{slug}.pine"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. Hash the RAW source — source tag is NOT part of content identity.
        raw_hash = hashlib.sha256(pine_source.encode("utf-8")).hexdigest()
        if output_path.exists():
            existing_text = output_path.read_text(encoding="utf-8")
            # Strip the source comment from the on-disk file before hashing so
            # that files saved with a tag compare equal to the same raw content
            # saved without one (e.g. manually-pasted vs. scraped duplicate).
            if existing_text.startswith("// SOURCE:"):
                existing_raw = existing_text.split("\n", 1)[1] if "\n" in existing_text else ""
            else:
                existing_raw = existing_text
            existing_hash = hashlib.sha256(existing_raw.encode("utf-8")).hexdigest()
            if raw_hash == existing_hash:
                logger.info(f"Skipped duplicate (identical content): {output_path}")
                return output_path

        # 2. Uniqueness confirmed — prepend source comment, then write.
        content_to_write = f"// SOURCE: {source}\n{pine_source}" if source else pine_source
        output_path.write_text(content_to_write, encoding="utf-8")
        logger.info(f"Saved: {output_path}")

        # 3. Persist sidecar metadata when provided.
        if metadata is not None:
            sidecar_path = output_path.with_suffix(".meta.json")
            try:
                sidecar_path.write_text(
                    json.dumps(metadata, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.info(f"Saved metadata sidecar: {sidecar_path}")
            except Exception as exc:
                logger.warning(f"Could not write metadata sidecar for {slug}: {exc}")

        return output_path

    # ── Strategy-page metadata helpers ───────────────────────────────────────

    def _is_strategy_page(self) -> bool:
        """
        Fast 3-second probe: returns True only when the 'Strategy report' /
        'Strategy Tester' tab is present in the DOM.

        TradingView shows this tab exclusively for strategy scripts. If it is
        absent after 3 s the page is an Indicator/Study and metric extraction
        should be skipped immediately — no further waiting.
        """
        try:
            WebDriverWait(self.driver, 3).until(
                EC.presence_of_element_located((By.XPATH, _STRATEGY_TAB_PROBE_XPATH))
            )
            return True
        except Exception:
            logger.debug("[META] Strategy report tab not found (likely an Indicator/Study).")
            return False

    def _extract_description_text(self) -> Optional[str]:
        """
        Click the 'Description' tab and return the visible text content.
        Returns None if the tab is absent or text extraction fails.
        """
        try:
            for xpath in _DESCRIPTION_TAB_XPATHS:
                try:
                    tab = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, xpath))
                    )
                    tab.click()
                    logger.debug("Clicked 'Description' tab.")
                    break
                except Exception:
                    continue

            # Wait for any description container to appear.
            for css in _DESCRIPTION_BODY_SELECTORS:
                try:
                    el = WebDriverWait(self.driver, 6).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, css))
                    )
                    text = el.text.strip()
                    if text:
                        logger.debug(f"[DESC] Extracted {len(text)} chars via '{css}'.")
                        return text
                except Exception:
                    continue

            # Fallback 1: generic class-name JS sweep for any description container.
            try:
                body_text = self.driver.execute_script(
                    "var el = document.querySelector('[class*=\"description\"],"
                    "[class*=\"Description\"]');"
                    "return el ? el.innerText : null;"
                )
                if body_text and len(body_text.strip()) > 20:
                    logger.debug("[DESC] Extracted via generic description JS fallback.")
                    return body_text.strip()
            except Exception:
                pass

            # Fallback 2: extract the short description from the script header area.
            # TradingView renders a subtitle / tagline beneath the script title in
            # elements like <h2>, <p>, or a dedicated subtitle container.
            try:
                short_desc = self.driver.execute_script("""
(function() {
    var candidates = [
        document.querySelector('[class*="shortDescription"]'),
        document.querySelector('[class*="short-description"]'),
        document.querySelector('[class*="subtitle"]'),
        document.querySelector('[class*="Subtitle"]'),
        document.querySelector('[class*="scriptTitle"] + *'),
        document.querySelector('h2'),
        document.querySelector('[class*="header"] p'),
    ];
    for (var i = 0; i < candidates.length; i++) {
        if (candidates[i]) {
            var t = candidates[i].innerText.trim();
            if (t && t.length > 10) return t;
        }
    }
    return null;
})();
""")
                if short_desc and len(short_desc.strip()) > 10:
                    logger.debug("[DESC] Extracted short description from header area.")
                    return short_desc.strip()
            except Exception:
                pass

        except Exception as exc:
            logger.warning(f"[META] Description extraction error: {exc}")

        return None

    def _extract_strategy_report_metrics(self) -> dict:
        """
        Click the 'Strategy report' tab, wait for KPI cells to render, then
        extract Total Trades, Profit Factor, Max Drawdown %, and Sharpe Ratio.

        Uses WebDriverWait for every element lookup (no static sleeps).
        Returns a dict with keys matching _KPI_LABEL_MAP; all values default
        to None when extraction fails.
        """
        metrics: dict = {
            "total_trades":     None,
            "profit_factor":    None,
            "max_drawdown_pct": None,
            "sharpe_ratio":     None,
        }

        try:
            # Fast 3-second probe: bail immediately for Indicator/Study pages.
            is_strategy = self._is_strategy_page()
            if not is_strategy:
                logger.info("[META] Not a strategy page — skipping Strategy Report extraction.")
                return metrics

            # Click the Strategy report tab (prioritises exact label from TV image).
            clicked = False
            for xpath in _STRATEGY_TESTER_TAB_XPATHS:
                try:
                    tab = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, xpath))
                    )
                    tab.click()
                    clicked = True
                    logger.debug("Clicked 'Strategy report' tab.")
                    break
                except Exception:
                    continue

            if not clicked:
                logger.warning("[META] 'Strategy report' tab not clickable; skipping metrics.")
                return metrics

            # Wait directly for the "Total trades" KPI label — this is the definitive
            # signal that the Strategy Report panel has fully rendered.
            # (Inner sub-tabs do not use role='tab' so an XPath sub-tab check is unreliable.)
            first_label = _KPI_LABEL_MAP["total_trades"][0]
            panel_ready = False
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located(
                        (By.XPATH,
                         f"//*[translate(normalize-space(text()),"
                         f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
                         f"='{first_label}']")
                    )
                )
                panel_ready = True
                logger.debug("[META] Strategy Report panel confirmed ready (KPI label visible).")
            except Exception:
                logger.warning("[META] KPI cells did not load in 15 s; skipping metrics.")
                return metrics

            # Extract each KPI using Selenium XPath (works with Shadow DOM / React portals
            # where document.querySelectorAll cannot reach).
            # Strategy: find the label element, then navigate to parent's following sibling
            # which contains the value (confirmed by DOM trace).
            for metric_key, label_candidates in _KPI_LABEL_MAP.items():
                raw_value: Optional[str] = None

                for label in label_candidates:
                    # XPath strategy 1: label → parent → following-sibling (e.g. container-DiHajR6I)
                    xpath_s1 = (
                        f"//*[translate(normalize-space(text()),"
                        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='{label}']"
                        f"/parent::*/following-sibling::*[1]"
                    )
                    # XPath strategy 2: label → direct following-sibling (in case structure is flat)
                    xpath_s2 = (
                        f"//*[translate(normalize-space(text()),"
                        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='{label}']"
                        f"/following-sibling::*[1]"
                    )
                    for xp in (xpath_s1, xpath_s2):
                        try:
                            val_el = self.driver.find_element(By.XPATH, xp)
                            raw_value = val_el.text.strip() or None
                            if raw_value:
                                break
                        except Exception:
                            continue
                    if raw_value:
                        break

                if not raw_value:
                    logger.debug(f"[META] KPI '{metric_key}' not found in DOM.")
                    continue

                # For max_drawdown_pct the value cell contains both the absolute
                # dollar amount AND the percentage (e.g. "143.28\nUSD\n4.19%").
                # We always want the percentage — take the last number before "%".
                if metric_key == "max_drawdown_pct" and raw_value and "%" in raw_value:
                    pct_candidates = re.findall(r"[-+]?\d+\.?\d*", raw_value[:raw_value.rfind("%")])
                    parsed = float(pct_candidates[-1]) if pct_candidates else _parse_metric_to_float(raw_value)
                else:
                    parsed = _parse_metric_to_float(raw_value)

                if parsed is None:
                    logger.debug(f"[META] Could not parse '{metric_key}' from {raw_value!r}.")
                    continue

                metrics[metric_key] = int(parsed) if metric_key == "total_trades" else parsed
                logger.debug(f"[META] {metric_key} = {metrics[metric_key]!r}")

            # Sharpe ratio lives on the "Risk-adjusted performance" sub-tab.
            # Click it, wait briefly, then extract using the same XPath strategy.
            if metrics["sharpe_ratio"] is None:
                try:
                    risk_tab_xpath = (
                        "//*[translate(normalize-space(.),"
                        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
                        "='risk-adjusted performance']"
                    )
                    risk_tab = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, risk_tab_xpath))
                    )
                    risk_tab.click()
                    logger.debug("[META] Clicked 'Risk-adjusted performance' sub-tab.")

                    for label in _KPI_LABEL_MAP["sharpe_ratio"]:
                        xpath_s1 = (
                            f"//*[translate(normalize-space(text()),"
                            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='{label}']"
                            f"/parent::*/following-sibling::*[1]"
                        )
                        xpath_s2 = (
                            f"//*[translate(normalize-space(text()),"
                            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='{label}']"
                            f"/following-sibling::*[1]"
                        )
                        # Table layout: label is nested 2 levels deep inside a <td>;
                        # go up 2 parents to the <td> then to the value <td>.
                        xpath_s3 = (
                            f"//*[translate(normalize-space(text()),"
                            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='{label}']"
                            f"/parent::*/parent::*/following-sibling::*[1]"
                        )
                        for xp in (xpath_s3, xpath_s1, xpath_s2):
                            try:
                                val_el = WebDriverWait(self.driver, 5).until(
                                    EC.presence_of_element_located((By.XPATH, xp))
                                )
                                raw_sr = val_el.text.strip() or None
                                if raw_sr:
                                    parsed_sr = _parse_metric_to_float(raw_sr)
                                    if parsed_sr is not None:
                                        metrics["sharpe_ratio"] = parsed_sr
                                        logger.debug(f"[META] sharpe_ratio = {parsed_sr!r}")
                                    break
                            except Exception:
                                continue
                        if metrics["sharpe_ratio"] is not None:
                            break
                except Exception as sr_exc:
                    logger.debug(f"[META] Sharpe ratio sub-tab not found or extraction failed: {sr_exc}")

        except Exception as exc:
            logger.warning(f"[META] Strategy report extraction error: {exc}")

        return metrics

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_script_id(url: str) -> Optional[str]:
        m = _SCRIPT_PARTS_RE.search(url)
        return m.group(1) if m else None

    @staticmethod
    def _extract_strategy_slug(url: str) -> str:
        m = _SCRIPT_PARTS_RE.search(url)
        return m.group(2) if m else "unknown_strategy"

    def _fetch_via_api(self, strategy_url: str) -> Optional[str]:
        script_id = self._extract_script_id(strategy_url)
        if not script_id:
            return None
        api_url = _PINE_FACADE_URL.format(script_id=script_id)
        logger.debug(f"[API] {api_url}")
        try:
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                source = data.get("source_code") or data.get("scriptSource") or ""
                return source.strip() or None
        except Exception as exc:
            logger.debug(f"[API] Failed: {exc}")
            return None

    def _click_source_tab(self) -> bool:
        """Click the 'Source code' tab by text. Returns True on success."""
        for xpath in _SOURCE_TAB_XPATHS:
            try:
                tab = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                tab.click()
                logger.info(f"Clicked 'Source code' tab.")
                return True
            except Exception:
                continue
        return False

    def _click_copy_button(self) -> bool:
        """Click the copy button inside the Source code panel. Returns True on success."""
        for xpath in _COPY_BTN_XPATHS:
            try:
                btn = WebDriverWait(self.driver, 4).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                btn.click()
                logger.info(f"Clicked copy button via XPath.")
                return True
            except Exception:
                continue
        logger.debug("Copy button not found.")
        return False

    def _extract_via_clipboard_intercept(self) -> Optional[str]:
        """
        Inject a clipboard interceptor into the page, click copy, then read
        what navigator.clipboard.writeText received.
        """
        try:
            self.driver.execute_script(_JS_INTERCEPT_CLIPBOARD)
        except Exception as e:
            logger.debug(f"Clipboard intercept inject failed: {e}")
            return None

        if not self._click_copy_button():
            return None

        time.sleep(1)
        try:
            result = self.driver.execute_script("return window.__tvClipboard;")
            if result and len(result.strip()) > 10:
                return result.strip()
        except Exception as e:
            logger.debug(f"Clipboard intercept read failed: {e}")
        return None

    def _extract_via_powershell_clipboard(self) -> Optional[str]:
        """
        Click the copy button then read the Windows OS clipboard via PowerShell.
        Windows-only fallback.

        Tries cp1252 encoding first (native Windows codepage, preserves accented
        characters), then falls back to utf-8 with errors='replace' so it never
        crashes on exotic bytes.
        """
        # Re-click copy so the content is fresh in the OS clipboard.
        if not self._click_copy_button():
            return None
        time.sleep(1)

        cmd = ["powershell", "-command", "Get-Clipboard"]

        # Pass 1: cp1252 — native Windows codepage, handles accented chars natively.
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="cp1252",
                timeout=10,
            )
            content = result.stdout.strip()
            if content and len(content) > 10:
                return content
        except (UnicodeDecodeError, UnicodeError):
            logger.debug("cp1252 clipboard decode failed, retrying with utf-8 replace")
        except Exception as e:
            logger.debug(f"PowerShell clipboard read (cp1252) failed: {e}")

        # Pass 2: utf-8 with replacement — guaranteed not to crash.
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            content = result.stdout.strip()
            if content and len(content) > 10:
                return content
        except Exception as e:
            logger.debug(f"PowerShell clipboard read (utf-8 replace) failed: {e}")
        return None

    def _extract_code_js(self) -> Optional[str]:
        """
        JavaScript extraction: works for non-virtual-scrolled editors.
        Returns None for virtual-scrolled (partially rendered) editors.
        """
        for _ in range(3):
            try:
                result = self.driver.execute_script(_JS_EXTRACT_CODE)
                if result and (len(result.strip()) > 50 or "//@version" in result):
                    return result.strip()
            except Exception:
                pass
            time.sleep(2)
        return None


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    with TradingViewScraper(headless=False) as scraper:
        urls = scraper.fetch_strategy_list(max_results=5)
        print(f"\nFound {len(urls)} strategies.\n")

        for i, url in enumerate(urls, 1):
            slug = TradingViewScraper._extract_strategy_slug(url)
            print(f"[{i}/{len(urls)}] {slug}")
            print(f"         {url}")
            try:
                pine = scraper.fetch_pinescript(url)
                saved = scraper.save_to_input(pine, url)
                print(f"         Saved → {saved}  ({len(pine)} chars)\n")
            except NotImplementedError as e:
                first_line = str(e).splitlines()[0]
                print(f"         SKIP: {first_line}\n", file=sys.stderr)