"""
PineScript-to-Python Converter Pipeline

Lifecycle:
  new -> evaluated -> selected -> converted
  new / evaluated (score < ARCHIVE_SCORE_THRESHOLD) -> archived
"""

import json
import os
import shutil
import subprocess
import sys
import logging
import threading
from datetime import datetime, UTC
from pathlib import Path

# Subprocess environment with CLAUDECODE stripped so nested `claude` calls are allowed.
_SUBPROCESS_ENV = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REGISTRY_PATH           = Path("strategies_registry.json")
INPUT_DIR               = Path("input")
ARCHIVE_DIR             = Path("archive")
OUTPUT_DIR              = Path("output")
LOGS_ROOT               = Path("logs")
ARCHIVE_SCORE_THRESHOLD = 4   # btc + proj < this → archive; >= this → keep
TARGET_STRATEGY_COUNT   = 6   # minimum .pine files to keep in input/ (3 Popular + 3 Editor's Picks)
MAX_SEARCH_LOOPS        = 5   # retry cap for auto-selection before giving up
SEEN_URLS_PATH          = Path("seen_urls.json")   # persisted global URL dedup store
_EXCLUDED_PINE_FILES    = {"source_strategy.pine"}   # placeholder files to ignore


# ---------------------------------------------------------------------------
# Logging  (file-only; terminal gets clean print() UI)
# ---------------------------------------------------------------------------
def _setup_file_logger() -> logging.Logger:
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    log_file = LOGS_ROOT / f"runner_{ts}.log"

    lg = logging.getLogger("runner")
    lg.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    ))
    lg.addHandler(fh)
    lg.propagate = False   # don't leak to root logger (tv_scraper sets basicConfig)
    return lg


logger = _setup_file_logger()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _verdict(btc: int, proj: int) -> str:
    """
        Calculates the overall recommendation verdict for a strategy.

        This function evaluates a trading strategy's viability by combining its
        Bitcoin compatibility score and its project convertibility score into a
        single metric (max 10 points). It returns a human-readable categorization.

        Args:
            btc (int): The strategy's suitability for Bitcoin trading (scale 0-5).
            proj (int): The strategy's code cleanliness and ease of conversion (scale 0-5).

        Returns:
            str: A formatted string representing the final verdict:
                - 8 to 10: [+++] RECOMMENDED (Ideal for conversion and BTC trading)
                - 6 to 7:  [++]  GOOD (Solid strategy, minor adjustments needed)
                - 4 to 5:  [+]   OK (Passable, but may require manual fixes)
                - 2 to 3:  [!]   COMPLEX (Hard to convert or poor BTC fit)
                - 0 to 1:  [-]   SKIP (Not viable for this pipeline)
        """
    total = btc + proj
    # ANSI Color Codes
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    RESET = '\033[0m'

    if total >= 8: return f"{GREEN}[ RECOMMENDED ]{RESET}"
    if total >= 6: return f"{GREEN}[ GOOD ]{RESET}"
    if total >= 4: return f"{YELLOW}[ OK ]{RESET}"
    if total >= 2: return f"{YELLOW}[ COMPLEX ]{RESET}"
    return f"{RED}[ SKIP ]{RESET}"

def _div(char="─", width=70) -> str:
    """
        Generates a horizontal divider line for terminal UI formatting.

        This helper function creates a string of repeating characters used to
        visually separate sections in the console output.

        Args:
            char (str, optional): The character to repeat. Defaults to "─".
            width (int, optional): The total length of the divider line. Defaults to 70.

        Returns:
            str: A string containing the repeated character.
        """
    return char * width


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def load_registry() -> dict:
    """
        Loads the strategy registry from a JSON file into a Python dictionary.

        If the registry file exists, it reads and parses the JSON data. It also
        acts as a self-healing mechanism by purging any excluded placeholder
        files (like 'source_strategy.pine') that might have been accidentally
        saved in previous runs. If the file does not exist, it returns an empty dictionary.

        Returns:
            dict: The current state of the strategies registry.
        """
    if REGISTRY_PATH.exists():
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        # Purge any excluded placeholder files that crept in from earlier runs.
        for key in _EXCLUDED_PINE_FILES:
            if key in data:
                del data[key]
                logger.info(f"Purged excluded entry: {key}")
        logger.debug(f"Registry loaded: {len(data)} entries")
        return data
    return {}


def save_registry(registry: dict) -> None:
    """
        Serializes and saves the strategy registry dictionary to a JSON file.

        This function overwrites the existing registry file with the current state
        of the dictionary. It formats the JSON with an indentation of 2 spaces
        for human readability and ensures that non-ASCII characters (like emojis
        or special symbols) are preserved exactly as they are.

        Args:
            registry (dict): The current state of the strategies registry to be saved.

        Returns:
            None
        """
    # Atomic write: write to a temp file then rename to avoid corruption on crash.
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)
    logger.debug("Registry saved.")


def scan_and_register(registry: dict) -> dict:
    """
        Scans the input directory for new PineScript files and registers them.

        This function looks for all '.pine' files in the designated input folder.
        It ignores any files present in the exclusion list (e.g., manual placeholders).
        For every valid file that is not already tracked in the registry, it creates
        a new entry with a 'new' status and a timestamp.

        Args:
            registry (dict): The current strategies registry dictionary.

        Returns:
            dict: The updated registry dictionary containing the newly found files.
        """
    added = 0
    for pine_file in sorted(INPUT_DIR.glob("*.pine")):
        key = pine_file.name
        if key in _EXCLUDED_PINE_FILES:
            continue
        if key not in registry:
            # Extract scrape source from the injected PineScript comment (first line).
            try:
                first_line = pine_file.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            except OSError:
                first_line = ""
            scrape_source = "unknown"
            if first_line.startswith("// SOURCE:"):
                scrape_source = first_line.removeprefix("// SOURCE:").strip()

            registry[key] = {
                "file_path":     str(pine_file),
                "status":        "new",
                "registered_at": _now_iso(),
                "scrape_source": scrape_source,
            }
            logger.info(f"Registered: {key} (source: {scrape_source})")
            added += 1
    if added:
        print(f"  Registered {added} new file(s).")
    return registry


# ---------------------------------------------------------------------------
# TV Scraper fallback (runs when input/ has no .pine files)
# ---------------------------------------------------------------------------
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
    SEEN_URLS_PATH.write_text(json.dumps(sorted(seen_urls), indent=2), encoding="utf-8")


def run_tv_scraper(max_results: int = 6) -> None:
    """
    Populates the input directory by scraping public TradingView strategies.

    Fetches strategies from two sources (Popular + Editor's Picks) split evenly,
    using a persisted seen_urls.json to guarantee globally unique results across
    runs. Paginating past previously-seen pages ensures freshness at scale.

    Args:
        max_results (int): Target number of new strategy files to download.
            Evenly split between Popular (max_results // 2) and Editor's Picks
            (max_results // 2). Defaults to 6.

    Raises:
        SystemExit: If dependencies are missing, scraper fails, or 0 files saved.
    """
    print(f"\n[SCRAPER] input/ has fewer than {TARGET_STRATEGY_COUNT} strategies.")
    print(f"          Fetching {max_results} more strategy file(s) from TradingView...")
    print(f"          Sources: Popular (x{max_results // 2}) + Editor's Picks (x{max_results // 2})")
    print(_div())

    # Block tv_scraper's logging.basicConfig from adding a root StreamHandler.
    # basicConfig is a no-op when the root logger already has handlers.
    _root_log = logging.getLogger()
    if not _root_log.handlers:
        _root_log.addHandler(logging.NullHandler())

    try:
        from src.utils.tv_scraper import TradingViewScraper
    except ImportError as exc:
        print(f"\n[!] Cannot import TradingViewScraper: {exc}")
        print("    Install missing deps: pip install selenium webdriver-manager")
        sys.exit(1)

    # Redirect scraper / driver logs to our file handler — off the terminal.
    for _lgr_name in ("TV_Scraper", "WDM", "selenium", "urllib3"):
        _lgr = logging.getLogger(_lgr_name)
        _lgr.handlers.clear()
        for _h in logger.handlers:
            _lgr.addHandler(_h)
        _lgr.propagate = False

    # Load global URL dedup store (O(1) lookups).
    seen_urls = _load_seen_urls()
    logger.info(f"Loaded {len(seen_urls)} previously-seen URL(s) from {SEEN_URLS_PATH}")

    n_per_source = max(1, max_results // 2)
    saved = 0
    failed = 0

    try:
        with TradingViewScraper(headless=False) as scraper:
            urls = scraper.fetch_from_two_sources(
                n_per_source=n_per_source,
                seen_urls=seen_urls,
            )
            logger.info(f"TV scraper found {len(urls)} new strategy URL(s) across both sources")

            for url, scrape_source in urls:
                if saved >= max_results:
                    break

                slug = TradingViewScraper._extract_strategy_slug(url)
                dest = INPUT_DIR / f"{slug}.pine"

                if dest.exists():
                    logger.info(f"Skipping already-downloaded: {slug}")
                    seen_urls.add(url)
                    continue

                print(f"  [{saved + 1}/{max_results}] {slug} [{scrape_source}] ... ", end="", flush=True)

                try:
                    pine = scraper.fetch_pinescript(url)
                    scraper.save_to_input(pine, url, source=scrape_source)
                    print(f"[OK]  ({len(pine):,} chars)")
                    logger.info(f"Scraped: {slug} [{scrape_source}] ({len(pine)} chars)")
                    seen_urls.add(url)
                    saved += 1
                except NotImplementedError as exc:
                    first_line = str(exc).splitlines()[0]
                    print(f"[SKIP]  {first_line}")
                    logger.warning(f"Skipped {slug}: {first_line}")
                    failed += 1
                except Exception as exc:
                    print(f"[FAIL]  {exc}")
                    logger.exception(f"Error scraping {slug}: {exc}")
                    failed += 1

    except RuntimeError as exc:
        print(f"\n[FATAL] Scraper runtime error: {exc}")
        logger.error(f"TV scraper runtime error: {exc}")
        sys.exit(1)
    finally:
        # Always persist the updated seen set — even on partial success.
        _save_seen_urls(seen_urls)
        logger.info(f"Saved {len(seen_urls)} URL(s) to {SEEN_URLS_PATH}")

    print(_div())
    print(f"  Scraped {saved} strategy file(s) -> input/")
    if failed:
        print(f"  Skipped {failed} file(s) (private or unsupported)")

    if saved == 0:
        print("\n[FAIL] No strategies could be scraped.")
        print("       Manual fallback: paste PineScript into input/source_strategy.pine")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Phase 0 — Isolated Evaluation
# ---------------------------------------------------------------------------
def _parse_json_from_output(raw: str) -> dict:
    """
    Defensively parses a JSON object from raw LLM text output.

    Large Language Models often wrap JSON responses in Markdown code blocks
    or include extraneous conversational text. This function safely extracts
    the core JSON object by stripping Markdown fences and slicing the string
    from the first opening curly brace to the last closing curly brace.

    Args:
        raw (str): The raw text output generated by the AI agent.

    Returns:
        dict: The parsed JSON object as a Python dictionary.

    Raises:
        ValueError: If no curly braces representing a JSON object are found
            within the raw string.
    """
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    start   = cleaned.find("{")
    end     = cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in agent output")
    return json.loads(cleaned[start:end])


def evaluate_strategy(pine_file: Path) -> dict | None:
    """
    Spawns an isolated AI agent process to evaluate a single PineScript strategy.

    This function runs the 'strategy_selector' Claude agent in a non-interactive
    subprocess. It securely passes the file path to the agent and uses specific
    CLI flags (e.g., '--dangerously-skip-permissions') to prevent the agent from
    blocking on interactive tool-approval prompts. The agent's standard output
    is streamed in real-time to the logger. A strict 180-second timeout is
    enforced to prevent infinite hangs.

    Args:
        pine_file (Path): The pathlib.Path object pointing to the specific
            .pine file to be evaluated.

    Returns:
        dict | None: A dictionary containing the structured JSON evaluation
            (including 'btc_score', 'project_score', and 'pine_metadata') if
            successful. Returns None if the process fails, times out, or if
            the output cannot be parsed as valid JSON.
    """
    try:
        raw = pine_file.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning(f"Cannot read {pine_file}: {e}")
        return None

    # Log what Claude will receive so the full file content is auditable.
    logger.info(f"Evaluating: {pine_file.name} ({len(raw)} chars)")
    logger.info(f"CLAUDE input (file path sent to agent):\n{pine_file.resolve()}")

    # Invoke the strategy_selector agent via the documented CLI flags:
    #   -p                         -> print (non-interactive) mode
    #   --agent strategy_selector   -> loads .claude/agents/strategy_selector.md
    #   --dangerously-skip-permissions -> auto-approves all tool calls
    #   --no-session-persistence    -> don't write session files for throwaway evals
    #
    # File content is embedded directly in the prompt so the agent needs no
    # Read tool call. This avoids tool-invocation hangs in non-TTY subprocesses.
    prompt = (
        f"Evaluate this PineScript strategy. File: {pine_file.name}\n\n"
        f"{raw}\n\n"
        "Output ONLY a raw JSON object matching this exact schema — no markdown, no extra fields:\n"
        '{"pine_metadata": {"name": "...", "safe_name": "...", "timeframe": "...", "lookback_bars": 0}, '
        '"btc_score": 0, "project_score": 0, "recommendation_reason": "..."}'
    )
    command = [
        "claude", "-p",
        "--agent", "strategy_selector",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        prompt,
    ]

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
            env=_SUBPROCESS_ENV,
        )
    except FileNotFoundError:
        logger.error("'claude' command not found for selector.")
        print("'claude' CLI not found.", flush=True)
        return None

    collected: list[str] = []
    killed_by_watchdog = threading.Event()

    def _kill_on_timeout():
        killed_by_watchdog.set()
        try:
            process.kill()
        except OSError:
            pass  # already exited

    watchdog = threading.Timer(180, _kill_on_timeout)
    try:
        watchdog.start()
        for line in process.stdout:
            stripped = line.rstrip()
            if stripped:
                print(f"    {stripped}", flush=True)
                logger.info(f"CLAUDE [SELECTOR]: {stripped}")
            collected.append(line)
        process.wait()
    except KeyboardInterrupt:
        process.kill()
        raise
    except Exception:
        # Process was killed by watchdog or other error
        try:
            process.kill()
        except OSError:
            pass
        process.wait()
    finally:
        watchdog.cancel()
        # terminate() is the cleanup path when an exception bypassed process.wait()
        if process.poll() is None:
            process.terminate()

    if killed_by_watchdog.is_set():
        logger.warning(f"Selector timed out (180s) for {pine_file.name}")
        print("TIMED OUT", flush=True)
        return None

    full_output = "".join(collected)
    if process.returncode != 0:
        logger.warning(f"Selector non-zero exit for {pine_file.name}")

    try:
        return _parse_json_from_output(full_output)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"JSON parse error for {pine_file.name}: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error evaluating {pine_file.name}: {e}")

    return None


def run_evaluations(registry: dict) -> dict:
    """
        Manages the batch evaluation process for all pending strategies.

        This function identifies strategies in the registry that need evaluation
        (either completely 'new' ones, or previously failed ones that scored 0).
        It iterates through the pending list, invoking the isolated evaluation
        agent for each file. It validates the output, updates the registry with
        scores and metadata, and performs an incremental, crash-safe save after
        each file to prevent data loss in case of an interruption.

        Args:
            registry (dict): The current strategies registry dictionary.

        Returns:
            dict: The updated registry dictionary with evaluation results and
                modified statuses.
        """
    new_entries = [(k, v) for k, v in registry.items() if v["status"] == "new"]
    # Also retry previously-failed evaluations (both scores = 0).
    retry_entries = [
        (k, v) for k, v in registry.items()
        if v["status"] == "evaluated"
        and v.get("btc_score", 0) == 0
        and v.get("project_score", 0) == 0
    ]
    to_evaluate = new_entries + retry_entries
    if not to_evaluate:
        return registry

    print(f"\n Evaluating {len(to_evaluate)} strategy file(s)...")
    print(_div())

    for key, rec in to_evaluate:
        print(f"    {key} ... ", end="", flush=True)
        result = evaluate_strategy(Path(rec["file_path"]))

        required = {"pine_metadata", "btc_score", "project_score"}
        if result and required.issubset(result):
            btc  = result["btc_score"]
            proj = result["project_score"]
            meta = result["pine_metadata"]
            # Ensure required sub-fields exist — generate them if the agent omitted them.
            if not meta.get("safe_name"):
                raw_name = meta.get("name", key.replace(".pine", ""))
                meta["safe_name"] = "".join(
                    c if c.isalnum() else "_" for c in raw_name
                ).strip("_")
            if not meta.get("timeframe"):
                meta["timeframe"] = "1h"
            if not meta.get("lookback_bars"):
                meta["lookback_bars"] = 100
            registry[key].update({
                "status":                "evaluated",
                "evaluated_at":          _now_iso(),
                "pine_metadata":         meta,
                "btc_score":             btc,
                "project_score":         proj,
                "recommendation_reason": result.get("recommendation_reason", ""),
            })
            print(f"BTC: {'*' * btc}  Proj: {'*' * proj}  {_verdict(btc, proj)}")
        else:
            registry[key].update({
                "status":                "evaluated",
                "evaluated_at":          _now_iso(),
                "pine_metadata":         {
                    "name": key, "safe_name": "", "timeframe": "?", "lookback_bars": 0
                },
                "btc_score":             0,
                "project_score":         0,
                "recommendation_reason": "Evaluation failed — scored 0.",
            })
            print("   FAILED (scored 0)")
            logger.warning(f"Evaluation failed for {key}")

        save_registry(registry)   # crash-safe: save after each file

    print(_div())
    return registry


# ---------------------------------------------------------------------------
# Phase 1 — User Selection
# ---------------------------------------------------------------------------
def auto_select_strategy(registry: dict) -> tuple[str | None, dict | None]:
    """
    Automatically selects the highest-scoring evaluated strategy for conversion.

    Filters the registry for evaluated (or previously failed) strategies whose
    source files still exist on disk, ranks them by combined score
    (btc_score + project_score) descending, and returns the top entry.
    Ties are broken by insertion order (stable sort).

    Args:
        registry (dict): The current strategies registry dictionary.

    Returns:
        tuple[str | None, dict | None]: The selected strategy's filename key and
            record, or (None, None) if no evaluated strategies are available
            (caller is responsible for retrying with fresh scraper results).
    """
    evaluated = {
        k: v for k, v in registry.items()
        if v["status"] in ("evaluated", "failed")  # include failed strategies for retry
        and k not in _EXCLUDED_PINE_FILES
        and Path(v["file_path"]).exists()
    }
    if not evaluated:
        return None, None

    ranked = sorted(
        evaluated.items(),
        key=lambda kv: kv[1].get("btc_score", 0) + kv[1].get("project_score", 0),
        reverse=True,
    )

    print(f"\n{'═' * 70}")
    print("  STRATEGY ANALYSIS REPORT")
    print(f"{'═' * 70}")
    print(f"  {'#':<4} {'Strategy':<40} {'BTC':>3} {'Proj':>4}  Verdict")
    print(_div())
    for i, (key, rec) in enumerate(ranked, 1):
        btc  = rec.get("btc_score", 0)
        proj = rec.get("project_score", 0)
        name = key.replace(".pine", "")[:39]
        failed_tag = "  [RETRY]" if rec.get("status") == "failed" else ""
        print(
            f"  [{i}] {name:<40} "
            f"{'*' * btc:>3} {'*' * proj:>4}  {_verdict(btc, proj)}{failed_tag}"
        )
    print(_div())

    chosen_key, chosen_rec = ranked[0]
    print(f"\n  Auto-selected : {chosen_key}  (highest combined score)")
    print(f"  Reason        : {chosen_rec.get('recommendation_reason', 'N/A')}\n")
    return chosen_key, chosen_rec


# ---------------------------------------------------------------------------
# Phase 2-5 — Orchestrator
# ---------------------------------------------------------------------------
def _setup_strategy_logger(strategy_name: str) -> tuple[logging.Logger, Path]:
    """
        Initializes an isolated, timestamped logger for a specific conversion run.

        To maintain clean organization and facilitate easy debugging, this function
        creates a dedicated directory structure for each strategy conversion attempt
        (e.g., 'logs/<safe_strategy_name>/<timestamp>/'). It attaches two separate
        file handlers: one for the complete trace log (DEBUG level) and one
        strictly for fatal issues (ERROR level). Propagation is disabled to prevent
        these detailed logs from bleeding into the main terminal output.

        Args:
            strategy_name (str): The original name of the strategy to be sanitized
                and used as the directory name.

        Returns:
            tuple[logging.Logger, Path]: A tuple containing the configured Logger
                instance and the Path object pointing to the newly created run directory.
        """
    ts       = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    safe     = strategy_name.replace(" ", "_").replace("/", "-")
    run_dir  = LOGS_ROOT / safe / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    lg  = logging.getLogger(f"runner.orch.{safe}.{ts}")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S")

    for path, level in [
        (run_dir / "run.log",    logging.DEBUG),
        (run_dir / "errors.log", logging.ERROR),
    ]:
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        lg.addHandler(fh)

    return lg, run_dir


def run_orchestrator(
    pine_file: Path,
    meta: dict,
    output_dir: Path,
) -> tuple[bool, Path]:
    """
    Executes the main AI orchestrator agent to convert a PineScript strategy.

    This function triggers the Claude CLI in a subprocess with the orchestrator
    agent loaded. It streams the standard output in real-time, functioning as a
    state machine to parse logging protocol triggers (e.g., 'handing over to:').
    This dynamically routes the log prefixes so that actions from sub-agents
    (Transpiler, Validator, etc.) are clearly identified in both the terminal
    and the log files. A strict 15-minute (900s) timeout is enforced.

    Args:
        pine_file (Path): The file path to the source PineScript strategy.
        meta (dict): The strategy metadata containing its name, timeframe, etc.
        output_dir (Path): The target directory where the converted Python
            artifacts should be saved.

    Returns:
        tuple[bool, Path]: A boolean flag indicating whether the orchestrator
            completed its run successfully (return code 0), and the Path object
            pointing to the dedicated log directory for this run.
    """
    strat_logger, run_dir = _setup_strategy_logger(meta["name"])
    print(f"     Launching orchestrator for '{meta['name']}'...")
    print(f"     Log : {run_dir / 'run.log'}")

    prompt = (
        "Start the conversion workflow.\n\n"
        f"Strategy name  : {meta['name']}\n"
        f"Timeframe      : {meta['timeframe']}\n"
        f"Lookback bars  : {meta['lookback_bars']}\n"
        f"Output snapshot: {output_dir}\n\n"
        f"PineScript file: {pine_file}\n"
        "(Use your Read tool to load the file from disk.)"
    )
    # Invoke the orchestrator agent via documented CLI flags:
    #   -p                          → print (non-interactive) mode
    #   --agent orchestrator        → loads .claude/agents/orchestrator.md
    #   --dangerously-skip-permissions → auto-approves all tool calls
    #   --verbose                   → full turn-by-turn output to log
    command = [
        "claude", "-p",
        "--agent", "orchestrator",
        "--dangerously-skip-permissions",
        "--verbose",
        prompt,
    ]
    strat_logger.info(f"Prompt sent to orchestrator (file={pine_file})")

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
            env=_SUBPROCESS_ENV,
        )
        current_agent = "ORCHESTRATOR"
        killed_by_watchdog = threading.Event()

        def _kill_on_timeout():
            killed_by_watchdog.set()
            try:
                process.kill()
            except OSError:
                pass  # already exited

        watchdog = threading.Timer(900, _kill_on_timeout)
        try:
            watchdog.start()
            for line in process.stdout:
                stripped = line.rstrip()
                if not stripped:
                    continue

                lower_line = stripped.lower()
                if "handing over to: transpiler" in lower_line or "agent transpiler" in lower_line:
                    current_agent = "TRANSPILER"
                elif "handing over to: validator" in lower_line or "agent validator" in lower_line:
                    current_agent = "VALIDATOR"
                elif "handing over to: test_generator" in lower_line or "agent test_generator" in lower_line:
                    current_agent = "QA_AGENT"
                elif "handing over to: integration" in lower_line or "agent integration" in lower_line:
                    current_agent = "INTEGRATION"
                elif "control returned to: orchestrator" in lower_line:
                    current_agent = "ORCHESTRATOR"

                print(f"  [{current_agent}] {stripped}", flush=True)
                strat_logger.info(f"[{current_agent}] {stripped}")

            process.wait()
        except KeyboardInterrupt:
            process.kill()
            raise
        except Exception:
            # Process was killed by watchdog or other error
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
            strat_logger.error("Orchestrator timed out after 900s.")
            print("\n    Orchestrator timed out (15 min).", flush=True)
            return False, run_dir

        if process.returncode == 0:
            strat_logger.info("Orchestrator completed successfully.")
            return True, run_dir
        strat_logger.error(f"Orchestrator exited with code {process.returncode}")
        return False, run_dir

    except FileNotFoundError:
        strat_logger.error("'claude' command not found.")
        print("\n    'claude' CLI not found. Is Claude Code installed and in PATH?")
        return False, run_dir
    except Exception as e:
        strat_logger.exception(f"Unexpected error: {e}")
        return False, run_dir


def copy_artifacts(meta: dict, output_dir: Path, run_dir: Path) -> None:
    """
        Gathers and packages the final generated Python artifacts.

        Once the orchestrator completes its workflow, this function searches
        the designated project directories ('src/strategies' and 'tests/strategies')
        for the generated Python code and its corresponding unit tests using
        the strategy's sanitized safe name. It copies these files, along with
        the execution trace log, into a single, organized snapshot directory.
        The copied files are renamed to standard conventions ('strategy.py'
        and 'test_strategy.py') for consistency.

        Args:
            meta (dict): The strategy metadata containing the 'safe_name' used
                to identify the generated files.
            output_dir (Path): The target snapshot directory where the
                packaged artifacts will be stored.
            run_dir (Path): The temporary logging directory containing the
                'run.log' file for this specific conversion attempt.

        Returns:
            None
        """
    safe = meta.get("safe_name", "")
    for src in Path("src/strategies").glob(f"*{safe}*.py"):
        shutil.copy2(src, output_dir / "strategy.py")
        logger.info(f"Copied strategy: {src}")
        break
    for src in Path("tests/strategies").glob(f"test_*{safe}*.py"):
        shutil.copy2(src, output_dir / "test_strategy.py")
        logger.info(f"Copied test: {src}")
        break
    run_log = run_dir / "run.log"
    if run_log.exists():
        shutil.copy2(run_log, output_dir / "run.log")


# ---------------------------------------------------------------------------
# Phase 5 — Smart Archiving
# ---------------------------------------------------------------------------
def archive_remaining(registry: dict, selected_key: str) -> dict:
    """
    Archives low-scoring strategies while retaining viable candidates for future runs.

    This function iterates over all evaluated strategies in the registry. It skips
    the strategy that was just selected for conversion. For the remaining strategies,
    it calculates the total evaluation score. If the score is below the predefined
    ARCHIVE_SCORE_THRESHOLD, the physical '.pine' file is moved to the 'archive/'
    directory, and its registry status is updated to 'archived'. Strategies meeting
    or exceeding the threshold are left untouched in the 'input/' directory with
    their 'evaluated' status intact, allowing them to populate the CLI menu in
    subsequent runs without requiring re-evaluation.

    Args:
        registry (dict): The current strategies registry dictionary.
        selected_key (str): The filename (key) of the strategy that the user
            selected for conversion during the current run.

    Returns:
        dict: The updated registry dictionary reflecting the new file paths
            and 'archived' statuses.
    """
    ARCHIVE_DIR.mkdir(exist_ok=True)
    archived = 0

    for key, rec in registry.items():
        if key == selected_key:
            continue
        if rec["status"] not in ("new", "evaluated", "failed"):
            continue

        total = rec.get("btc_score", 0) + rec.get("project_score", 0)
        if total >= ARCHIVE_SCORE_THRESHOLD:
            logger.info(
                f"Keeping '{key}' in input/ "
                f"(total={total} >= threshold={ARCHIVE_SCORE_THRESHOLD})"
            )
            continue   # Stay 'evaluated', available on next run

        src = Path(rec["file_path"])
        if src.exists():
            dest = ARCHIVE_DIR / src.name
            shutil.move(str(src), dest)
            rec["file_path"] = str(dest)
            logger.info(f"Archived: {key} → {dest}")

        rec["status"]      = "archived"
        rec["archived_at"] = _now_iso()
        archived += 1

    if archived:
        print(f"  Archived {archived} low-scoring file(s) → archive/")
    return registry


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(_div("═"))
    print("  PineScript → Python Converter")
    print(_div("═"))

    # Step 0 — Ensure at least TARGET_STRATEGY_COUNT real .pine files in input/
    INPUT_DIR.mkdir(exist_ok=True)
    existing = [f for f in INPUT_DIR.glob("*.pine") if f.name not in _EXCLUDED_PINE_FILES]
    if len(existing) < TARGET_STRATEGY_COUNT:
        needed = TARGET_STRATEGY_COUNT - len(existing)
        run_tv_scraper(max_results=needed)

    # Step 1 — Scan & Register
    print("\n  Scanning input/ for .pine files...")
    registry = load_registry()
    registry = scan_and_register(registry)
    save_registry(registry)

    # Step 2 — Evaluate new strategies (isolated, one at a time)
    registry = run_evaluations(registry)

    # Step 3 — Auto-select the highest-scoring strategy.
    # If no evaluated strategies exist yet, fetch a fresh batch and retry
    # (up to MAX_SEARCH_LOOPS times) before giving up.
    chosen_key, chosen_rec = None, None
    for _attempt in range(MAX_SEARCH_LOOPS):
        chosen_key, chosen_rec = auto_select_strategy(registry)
        if chosen_key is not None:
            break
        print(f"\n  No valid strategies found (attempt {_attempt + 1}/{MAX_SEARCH_LOOPS}).")
        print("  Fetching a fresh batch from TradingView...")
        run_tv_scraper(max_results=TARGET_STRATEGY_COUNT)
        registry = scan_and_register(registry)
        registry = run_evaluations(registry)
    else:
        print(f"\n[FAIL] Could not find a valid strategy after {MAX_SEARCH_LOOPS} attempts.")
        sys.exit(1)

    registry[chosen_key]["status"] = "selected"
    save_registry(registry)

    # Step 4 — Transpile
    meta      = chosen_rec["pine_metadata"]
    ts        = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    safe_name = meta.get("safe_name") or chosen_key.replace(".pine", "")
    out_dir   = OUTPUT_DIR / safe_name / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    success, run_dir = run_orchestrator(Path(chosen_rec["file_path"]), meta, out_dir)

    if success:
        copy_artifacts(meta, out_dir, run_dir)

        # Post-conversion cleanup: move .pine out of input/ so it no longer
        # counts toward the scraper threshold on subsequent runs.
        ARCHIVE_DIR.mkdir(exist_ok=True)
        pine_src = Path(chosen_rec["file_path"])
        new_file_path = str(pine_src)
        if pine_src.exists():
            pine_dest = ARCHIVE_DIR / pine_src.name
            shutil.move(str(pine_src), pine_dest)
            new_file_path = str(pine_dest)

        # Single, atomic registry update
        registry[chosen_key].update({
            "status":       "completed",
            "converted_at": _now_iso(),
            "archived_at":  _now_iso(),
            "output_dir":   str(out_dir),
            "file_path":    new_file_path,
        })
        save_registry(registry)
        print(f"\n  Conversion complete!")
        print(f"    Artifacts → {out_dir}")
    else:
        registry[chosen_key].update({
            "status":    "failed",
            "failed_at": _now_iso(),
        })
        save_registry(registry)
        print(f"\n  Orchestrator failed. See: {run_dir / 'run.log'}")
        sys.exit(1)

    # Step 5 — Smart archive
    print("\n  Archiving low-scoring strategies...")
    registry = archive_remaining(registry, chosen_key)
    save_registry(registry)
    print("\n  Done.\n")
