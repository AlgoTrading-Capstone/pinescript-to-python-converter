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
TARGET_STRATEGY_COUNT   = 5   # minimum .pine files to keep in input/
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
    total = btc + proj
    if total >= 8: return "✅ RECOMMENDED"
    if total >= 6: return "👍 GOOD"
    if total >= 4: return "👌 OK"
    if total >= 2: return "⚠️ COMPLEX"
    return "❌ SKIP"


def _div(char="─", width=70) -> str:
    return char * width


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def load_registry() -> dict:
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
    REGISTRY_PATH.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.debug("Registry saved.")


def scan_and_register(registry: dict) -> dict:
    added = 0
    for pine_file in sorted(INPUT_DIR.glob("*.pine")):
        key = pine_file.name
        if key in _EXCLUDED_PINE_FILES:
            continue
        if key not in registry:
            registry[key] = {
                "file_path":     str(pine_file),
                "status":        "new",
                "registered_at": _now_iso(),
            }
            logger.info(f"Registered: {key}")
            added += 1
    if added:
        print(f"  Registered {added} new file(s).")
    return registry


# ---------------------------------------------------------------------------
# TV Scraper fallback (runs when input/ has no .pine files)
# ---------------------------------------------------------------------------
def run_tv_scraper(max_results: int = 5) -> None:
    """
    Populate input/ by scraping public TradingView strategies.
    Exits with a message if no strategies could be downloaded.
    """
    print(f"\n🌐  input/ has fewer than {TARGET_STRATEGY_COUNT} strategies. Launching TradingView scraper...")
    print(f"  Fetching {max_results} more strategy file(s) from TradingView...")
    print(_div())

    # Block tv_scraper's logging.basicConfig from adding a root StreamHandler.
    # basicConfig is a no-op when the root logger already has handlers.
    _root_log = logging.getLogger()
    if not _root_log.handlers:
        _root_log.addHandler(logging.NullHandler())

    try:
        from src.utils.tv_scraper import TradingViewScraper
    except ImportError as exc:
        print(f"\n Cannot import TradingViewScraper: {exc}")
        print("  Install missing deps: pip install selenium webdriver-manager")
        sys.exit(1)

    # Redirect scraper / driver logs to our file handler — off the terminal.
    for _lgr_name in ("TV_Scraper", "WDM", "selenium", "urllib3"):
        _lgr = logging.getLogger(_lgr_name)
        _lgr.handlers.clear()
        for _h in logger.handlers:
            _lgr.addHandler(_h)
        _lgr.propagate = False

    saved = 0
    failed = 0

    try:
        with TradingViewScraper(headless=False) as scraper:
            # Request extra URLs so we have enough after skipping duplicates.
            urls = scraper.fetch_strategy_list(max_results=max_results * 3)
            logger.info(f"TV scraper found {len(urls)} strategy URL(s)")

            for i, url in enumerate(urls, 1):
                if saved >= max_results:
                    break
                slug = TradingViewScraper._extract_strategy_slug(url)
                dest = INPUT_DIR / f"{slug}.pine"
                if dest.exists():
                    logger.info(f"Skipping already-downloaded: {slug}")
                    continue
                print(f"  [{saved + 1}/{max_results}] {slug} ... ", end="", flush=True)
                try:
                    pine = scraper.fetch_pinescript(url)
                    scraper.save_to_input(pine, url)
                    print(f"✅  ({len(pine):,} chars)")
                    logger.info(f"Scraped: {slug} ({len(pine)} chars)")
                    saved += 1
                except NotImplementedError as exc:
                    first_line = str(exc).splitlines()[0]
                    print(f"⚠️  SKIP — {first_line}")
                    logger.warning(f"Skipped {slug}: {first_line}")
                    failed += 1
                except Exception as exc:
                    print(f"❌  ERROR — {exc}")
                    logger.exception(f"Error scraping {slug}: {exc}")
                    failed += 1

    except RuntimeError as exc:
        print(f"\n  ❌  Scraper error: {exc}")
        logger.error(f"TV scraper runtime error: {exc}")
        sys.exit(1)

    print(_div())
    print(f"  Scraped {saved} strategy file(s) → input/")
    if failed:
        print(f"  Skipped {failed} file(s) (private or unsupported)")

    if saved == 0:
        print("\n  ❌  No strategies could be scraped.")
        print("  Manual fallback: paste PineScript into input/source_strategy.pine")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Phase 0 — Isolated Evaluation
# ---------------------------------------------------------------------------
def _parse_json_from_output(raw: str) -> dict:
    """Defensively strip markdown fences then parse the outer JSON object."""
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    start   = cleaned.find("{")
    end     = cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in agent output")
    return json.loads(cleaned[start:end])


def evaluate_strategy(pine_file: Path) -> dict | None:
    """
    Spawn an isolated selector agent for one .pine file.
    File content is embedded in the prompt so the agent needs no Read tool —
    this avoids interactive permission prompts that block in subprocess mode.
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
    #   -p                          → print (non-interactive) mode
    #   --agent strategy_selector   → loads .claude/agents/strategy_selector.md
    #   --dangerously-skip-permissions → auto-approves all tool calls (Read, etc.)
    #                                 without this the Read tool blocks forever
    #                                 in a non-TTY subprocess
    #   --no-session-persistence    → don't write session files for throwaway evals
    prompt = (
        f"Read and evaluate the PineScript file at this exact path:\n"
        f"{pine_file.resolve()}\n\n"
        "Output the JSON object only."
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
        print("❌  'claude' CLI not found.", flush=True)
        return None

    collected: list[str] = []
    try:
        for line in process.stdout:
            stripped = line.rstrip()
            if stripped:
                print(f"    {stripped}", flush=True)
                logger.info(f"CLAUDE selector: {stripped}")
            collected.append(line)
        process.wait(timeout=180)
    except subprocess.TimeoutExpired:
        process.kill()
        logger.warning(f"Selector timed out for {pine_file.name}")
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

    print(f"\n⏳ Evaluating {len(to_evaluate)} strategy file(s)...")
    print(_div())

    for key, rec in to_evaluate:
        print(f"  ⚙️  {key} ... ", end="", flush=True)
        result = evaluate_strategy(Path(rec["file_path"]))

        required = {"pine_metadata", "btc_score", "project_score"}
        if result and required.issubset(result):
            btc  = result["btc_score"]
            proj = result["project_score"]
            registry[key].update({
                "status":                "evaluated",
                "evaluated_at":          _now_iso(),
                "pine_metadata":         result["pine_metadata"],
                "btc_score":             btc,
                "project_score":         proj,
                "recommendation_reason": result.get("recommendation_reason", ""),
            })
            print(f"BTC: {'⭐' * btc}  Proj: {'⭐' * proj}  {_verdict(btc, proj)}")
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
            print("❌  FAILED (scored 0)")
            logger.warning(f"Evaluation failed for {key}")

        save_registry(registry)   # crash-safe: save after each file

    print(_div())
    return registry


# ---------------------------------------------------------------------------
# Phase 1 — User Selection
# ---------------------------------------------------------------------------
def display_menu(registry: dict) -> tuple[str, dict]:
    evaluated = {
        k: v for k, v in registry.items()
        if v["status"] == "evaluated"
        and k not in _EXCLUDED_PINE_FILES
        and Path(v["file_path"]).exists()
    }
    if not evaluated:
        print("\n❌  No evaluated strategies found. Exiting.")
        sys.exit(1)

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
        print(
            f"  [{i}] {name:<40} "
            f"{'⭐' * btc:>3} {'⭐' * proj:>4}  {_verdict(btc, proj)}"
        )
    print(_div())

    best_key, best_rec = ranked[0]
    print(f"\n  Recommended : {best_key}")
    print(f"  Reason      : {best_rec.get('recommendation_reason', 'N/A')}\n")

    raw = input("  Enter number to convert [Enter = #1 recommended]: ").strip()
    if not raw:
        idx = 0
    else:
        try:
            idx = int(raw) - 1
            if not (0 <= idx < len(ranked)):
                raise ValueError
        except ValueError:
            print("  ⚠️  Invalid input — defaulting to #1.")
            idx = 0

    chosen_key, chosen_rec = ranked[idx]
    print(f"\n  ✅  Selected: {chosen_key}\n")
    return chosen_key, chosen_rec


# ---------------------------------------------------------------------------
# Phase 2-5 — Orchestrator
# ---------------------------------------------------------------------------
def _setup_strategy_logger(strategy_name: str) -> tuple[logging.Logger, Path]:
    ts       = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    safe     = strategy_name.replace(" ", "_").replace("/", "-")
    run_dir  = LOGS_ROOT / safe / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    lg  = logging.getLogger(f"runner.orch.{safe}.{ts}")
    lg.setLevel(logging.DEBUG)
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
    Invoke orchestrator sub-agent.
    Terminal shows only progress; subprocess output goes to log files.
    Returns (success, run_dir).
    """
    strat_logger, run_dir = _setup_strategy_logger(meta["name"])
    print(f"  ⚙️  Launching orchestrator for '{meta['name']}'...")
    print(f"  📁  Log : {run_dir / 'run.log'}")

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
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_SUBPROCESS_ENV,
        )
        for line in process.stdout:
            stripped = line.rstrip()
            if stripped:
                print(f"  {stripped}", flush=True)
                strat_logger.info(f"CLAUDE response: {stripped}")
        process.wait(timeout=900)   # 15 min hard cap

        if process.returncode == 0:
            strat_logger.info("Orchestrator completed successfully.")
            return True, run_dir
        strat_logger.error(f"Orchestrator exited with code {process.returncode}")
        return False, run_dir

    except subprocess.TimeoutExpired:
        process.kill()
        strat_logger.error("Orchestrator timed out after 900s.")
        print("\n  ❌  Orchestrator timed out (15 min).", flush=True)
        return False, run_dir
    except FileNotFoundError:
        strat_logger.error("'claude' command not found.")
        print("\n  ❌  'claude' CLI not found. Is Claude Code installed and in PATH?")
        return False, run_dir
    except Exception as e:
        strat_logger.exception(f"Unexpected error: {e}")
        return False, run_dir


def copy_artifacts(meta: dict, output_dir: Path, run_dir: Path) -> None:
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
    Archive only LOW-SCORING strategies (btc + proj < ARCHIVE_SCORE_THRESHOLD).
    High-scoring unselected strategies remain 'evaluated' for future runs.
    """
    ARCHIVE_DIR.mkdir(exist_ok=True)
    archived = 0

    for key, rec in registry.items():
        if key == selected_key:
            continue
        if rec["status"] not in ("new", "evaluated"):
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
    print("\n📂  Scanning input/ for .pine files...")
    registry = load_registry()
    registry = scan_and_register(registry)
    save_registry(registry)

    # Step 2 — Evaluate new strategies (isolated, one at a time)
    registry = run_evaluations(registry)

    # Step 3 — User selection
    chosen_key, chosen_rec = display_menu(registry)
    registry[chosen_key]["status"] = "selected"
    save_registry(registry)

    # Step 4 — Transpile
    meta    = chosen_rec["pine_metadata"]
    ts      = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = OUTPUT_DIR / meta["safe_name"] / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    success, run_dir = run_orchestrator(Path(chosen_rec["file_path"]), meta, out_dir)

    if success:
        copy_artifacts(meta, out_dir, run_dir)
        registry[chosen_key].update({
            "status":       "converted",
            "converted_at": _now_iso(),
            "output_dir":   str(out_dir),
        })
        save_registry(registry)
        print(f"\n✅  Conversion complete!")
        print(f"    Artifacts → {out_dir}")
    else:
        print(f"\n❌  Orchestrator failed. See: {run_dir / 'run.log'}")
        sys.exit(1)

    # Step 5 — Smart archive
    print("\n🗂️  Archiving low-scoring strategies...")
    registry = archive_remaining(registry, chosen_key)
    save_registry(registry)
    print("\n✅  Done.\n")
