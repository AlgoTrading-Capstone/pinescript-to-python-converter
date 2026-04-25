"""
register_strategies.py

CI/CD helper: registers newly deployed strategy files in the RL training repo.

Reads NEW_FILES_LIST from the environment (one file path per line, relative to
the converter repo root).  For each new strategy it:
  1. Updates rl-repo/strategies/registry.py  — adds the import line and
     the _STRATEGIES dict entry.
  2. Updates rl-repo/config.py               — appends the class name to
     STRATEGY_LIST.

class_name / module metadata is sourced from the already-updated
rl-repo/strategies/strategies_registry.json so there is no need to
re-parse strategy source files here.

All insertions are idempotent: running the script twice on the same file
list is safe.
"""

import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (relative to CWD, which is the converter repo root in CI)
# ---------------------------------------------------------------------------
REGISTRY_PY   = Path("rl-repo/strategies/registry.py")
CONFIG_PY     = Path("rl-repo/config.py")
REG_JSON_PATH = Path("rl-repo/strategies/strategies_registry.json")

# ---------------------------------------------------------------------------
# Input — injected as an env var by the workflow step
# ---------------------------------------------------------------------------
NEW_FILES = os.environ.get("NEW_FILES_LIST", "").strip()
if not NEW_FILES:
    print("NEW_FILES_LIST is empty — nothing to register.")
    sys.exit(0)

# ---------------------------------------------------------------------------
# Load the JSON registry written by the preceding workflow step
# ---------------------------------------------------------------------------
if not REG_JSON_PATH.exists():
    print(
        f"WARNING: {REG_JSON_PATH} not found — cannot resolve class names. "
        "Skipping registry.py / config.py update.",
        file=sys.stderr,
    )
    sys.exit(0)

with REG_JSON_PATH.open("r", encoding="utf-8-sig") as fh:
    reg_json: dict = json.load(fh)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def insert_before_close(
    content: str,
    open_char: str,
    close_char: str,
    anchor_pattern: str,
    insertion: str,
) -> str:
    """Find the block opened by *anchor_pattern* and insert *insertion* just
    before its matching closing bracket/brace.

    Uses depth counting so it handles nested brackets correctly.
    Returns the original *content* unchanged (with a warning) if the anchor
    or the matching close cannot be found.
    """
    m = re.search(anchor_pattern, content, re.S)
    if not m:
        print(
            f"WARNING: anchor pattern '{anchor_pattern}' not found — "
            "skipping insertion.",
            file=sys.stderr,
        )
        return content

    start = content.index(open_char, m.start())
    depth, close_pos = 0, None
    for i, ch in enumerate(content[start:]):
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                close_pos = start + i
                break

    if close_pos is None:
        print(
            f"WARNING: unmatched '{open_char}' in block matched by "
            f"'{anchor_pattern}' — skipping insertion.",
            file=sys.stderr,
        )
        return content

    return content[:close_pos] + insertion + content[close_pos:]


def update_registry_py(class_name: str, module: str) -> None:
    """Insert import line and _STRATEGIES dict entry into registry.py."""
    if not REGISTRY_PY.exists():
        print(f"WARNING: {REGISTRY_PY} not found — skipping.", file=sys.stderr)
        return

    content = REGISTRY_PY.read_text(encoding="utf-8")

    # -- 1. Add import line ------------------------------------------------
    import_line = f"from {module} import {class_name}"
    if import_line in content:
        print(f"  import already present for {class_name} — skipping.")
    else:
        class_match = re.search(r"^class StrategyRegistry", content, re.M)
        if class_match:
            pre_class = content[: class_match.start()]
            import_matches = list(
                re.finditer(r"^(?:from|import)\s+\S+.*$", pre_class, re.M)
            )
            if import_matches:
                last_end = import_matches[-1].end()
                content = content[:last_end] + "\n" + import_line + content[last_end:]
            else:
                # No existing imports found before the class — prepend.
                content = import_line + "\n" + content
        else:
            print(
                f"WARNING: 'class StrategyRegistry' not found in {REGISTRY_PY}.",
                file=sys.stderr,
            )

    # -- 2. Add _STRATEGIES dict entry -------------------------------------
    dict_entry = f'        "{class_name}": {class_name},'
    if dict_entry in content:
        print(f"  dict entry already present for {class_name} — skipping.")
    else:
        content = insert_before_close(
            content,
            "{",
            "}",
            r"_STRATEGIES\s*[:\w\[\],\s]*=\s*\{",
            f'        "{class_name}": {class_name},\n    ',
        )

    REGISTRY_PY.write_text(content, encoding="utf-8")
    print(f"  registry.py updated: {class_name} <- {module}")


def update_config_py(class_name: str) -> None:
    """Append class name to STRATEGY_LIST in config.py."""
    if not CONFIG_PY.exists():
        print(f"WARNING: {CONFIG_PY} not found — skipping.", file=sys.stderr)
        return

    content = CONFIG_PY.read_text(encoding="utf-8")

    if f'"{class_name}"' in content:
        print(f"  {class_name} already in config.py STRATEGY_LIST — skipping.")
        return

    content = insert_before_close(
        content,
        "[",
        "]",
        r"STRATEGY_LIST\s*=\s*\[",
        f'    "{class_name}",\n',
    )
    CONFIG_PY.write_text(content, encoding="utf-8")
    print(f"  config.py STRATEGY_LIST updated: added {class_name}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

for line in NEW_FILES.splitlines():
    src_file = line.strip()
    if not src_file:
        continue

    stem   = Path(src_file).stem          # e.g. "my_new_strategy"
    module = f"strategies.{stem}"         # e.g. "strategies.my_new_strategy"

    # Resolve class_name from the JSON registry by matching on module name.
    entry = next(
        (v for v in reg_json.values() if v.get("module") == module), None
    )
    if entry is None:
        print(
            f"WARNING: no registry.json entry for module '{module}' — "
            "skipping registry.py / config.py update.",
            file=sys.stderr,
        )
        continue

    class_name = entry["class_name"]
    print(f"Registering {class_name} ({module})...")
    update_registry_py(class_name, module)
    update_config_py(class_name)

print("register_strategies.py complete.")