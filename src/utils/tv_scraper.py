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

import re
import time
import random
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

# ── Listing page ──────────────────────────────────────────────────────────────
STRATEGIES_LISTING_URL = "https://www.tradingview.com/scripts/?script_type=strategies"
_LISTING_ANCHOR_SELECTOR = "a[href*='/script/']"
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
            self.driver.quit()
            self.driver = None
            logger.info("WebDriver closed.")

    def __enter__(self):
        self.start_driver()
        return self

    def __exit__(self, *_):
        self.close_driver()

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_strategy_list(
        self,
        page_url: str = STRATEGIES_LISTING_URL,
        max_results: int = 10,
    ) -> list[str]:
        """Scrape listing page and return up to max_results strategy URLs."""
        if not self.driver:
            self.start_driver()

        # Randomise the page so each run fetches different strategies.
        page_num = random.randint(1, 10)
        sep = "&" if "?" in page_url else "?"
        url_with_page = f"{page_url}{sep}page={page_num}"
        logger.info(f"Fetching strategy list from: {url_with_page}")
        self.driver.get(url_with_page)

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

        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
        time.sleep(2)

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

        logger.info(f"Found {len(urls)} strategy URL(s).")
        return urls

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
            "    and run runner.py directly."
        )

    def save_to_input(
        self,
        pine_source: str,
        strategy_url: str,
        input_dir: str = _PROJECT_INPUT_DIR,
    ) -> Path:
        """Save PineScript to input/{strategy_slug}.pine."""
        slug = self._extract_strategy_slug(strategy_url)
        output_path = Path(input_dir) / f"{slug}.pine"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(pine_source, encoding="utf-8")
        logger.info(f"Saved: {output_path}")
        return output_path

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
        """
        # Re-click copy so the content is fresh in the OS clipboard.
        if not self._click_copy_button():
            return None
        time.sleep(1)

        try:
            result = subprocess.run(
                ["powershell", "-command", "Get-Clipboard"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            content = result.stdout.strip()
            if content and len(content) > 10:
                return content
        except Exception as e:
            logger.debug(f"PowerShell clipboard read failed: {e}")
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