"""
GitHub PR sync — align registry with closed / merged PRs (via `gh` CLI).

Integration opens PRs from branches `feat/<safe_name>`.
When a PR is closed without merge, we set status to 'rejected' so archive
recycling does not resurrect rejected strategies.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from src.pipeline import SUBPROCESS_ENV

logger = logging.getLogger("runner")


def git_repo_root(start: Path | None = None) -> Path | None:
    cwd = start or Path.cwd()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=30, check=False,
            env=SUBPROCESS_ENV,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return Path(out.stdout.strip()).resolve()
    except OSError:
        return None


def gh_available() -> bool:
    try:
        r = subprocess.run(["gh", "--version"], capture_output=True, timeout=10,
                           check=False, env=SUBPROCESS_ENV)
        return r.returncode == 0
    except OSError:
        return False


def _fetch_all_prs(repo_root: Path) -> list[dict[str, Any]]:
    cmd = ["gh", "pr", "list", "--state", "all", "--limit", "500",
           "--json", "number,headRefName,state,mergedAt,closedAt"]
    try:
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True,
                               timeout=120, check=False, env=SUBPROCESS_ENV)
    except OSError as e:
        logger.warning("Could not run gh pr list: %s", e)
        return []
    if proc.returncode != 0:
        logger.warning("gh pr list failed (exit %s): %s", proc.returncode,
                       (proc.stderr or proc.stdout or "").strip()[:500])
        return []
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        logger.warning("gh pr list returned invalid JSON")
        return []
    return data if isinstance(data, list) else []


def _group_by_head(prs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_head: dict[str, list[dict[str, Any]]] = {}
    for p in prs:
        head = p.get("headRefName")
        if head and isinstance(head, str):
            by_head.setdefault(head, []).append(p)
    return by_head


def _resolve_branch_status(prs: list[dict[str, Any]]) -> dict[str, Any] | None:
    prs = sorted(prs, key=lambda p: int(p.get("number") or 0), reverse=True)
    for p in prs:
        if p.get("state") == "MERGED":
            return {"kind": "merged", "pr": p}
    for p in prs:
        if p.get("state") == "CLOSED" and not p.get("mergedAt"):
            return {"kind": "closed_unmerged", "pr": p}
    for p in prs:
        if p.get("state") == "OPEN":
            return {"kind": "open", "pr": p}
    return None


def sync_pr_closure_to_registry(
    registry: dict,
    repo_root: Path | None = None,
) -> tuple[dict, int]:
    """
    For each registry entry with a pine_metadata.safe_name, look up feat/<safe_name> on GitHub.
    - Closed without merge: set status='rejected', recycle_eligible=False.
    - Merged: record PR number and merged state.
    Returns (registry, number_of_records_updated).
    """
    root = repo_root or git_repo_root()
    if root is None:
        logger.debug("pr_sync: not inside a git repo, skipping")
        return registry, 0
    if not gh_available():
        logger.debug("pr_sync: gh CLI not available, skipping")
        return registry, 0

    prs = _fetch_all_prs(root)
    by_head = _group_by_head(prs)
    updated = 0
    now = datetime.now(UTC).isoformat()

    for key, rec in registry.items():
        meta = rec.get("pine_metadata") or {}
        safe_name = str(meta.get("safe_name") or "").strip()
        if not safe_name:
            continue
        branch = f"feat/{safe_name}"
        if branch not in by_head:
            continue

        resolved = _resolve_branch_status(by_head[branch])
        if resolved is None:
            continue

        pr = resolved["pr"]
        kind = resolved["kind"]
        num = pr.get("number")
        state = pr.get("state")
        changed = False

        if num is not None and rec.get("github_pr_number") != num:
            rec["github_pr_number"] = num
            changed = True
        if state and rec.get("github_pr_state") != state:
            rec["github_pr_state"] = state
            changed = True

        if kind == "merged":
            merged_at = pr.get("mergedAt")
            if merged_at and rec.get("github_pr_merged_at") != merged_at:
                rec["github_pr_merged_at"] = merged_at
                changed = True
            for field in ("github_pr_closed_without_merge_at", "github_pr_rejection_note"):
                if rec.pop(field, None) is not None:
                    changed = True
        elif kind == "closed_unmerged":
            if rec.get("recycle_eligible") is not False:
                rec["recycle_eligible"] = False
                changed = True
            if rec.get("status") != "rejected":
                rec["status"] = "rejected"
                rec["rejected_at"] = now
                changed = True
            if not rec.get("github_pr_closed_without_merge_at"):
                rec["github_pr_closed_without_merge_at"] = now
                changed = True
            note = "PR closed without merge; strategy permanently rejected."
            if rec.get("github_pr_rejection_note") != note:
                rec["github_pr_rejection_note"] = note
                changed = True
        elif kind == "open":
            if rec.pop("github_pr_closed_without_merge_at", None) is not None:
                changed = True

        if changed:
            updated += 1
            logger.info("pr_sync: %s branch=%s -> %s PR#%s", key, branch, kind, num)

    return registry, updated


if __name__ == "__main__":
    import sys
    from src.pipeline.registry import load_registry, save_registry
    logging.basicConfig(level=logging.INFO)
    reg = load_registry()
    _, n = sync_pr_closure_to_registry(reg)
    if n:
        save_registry(reg)
        print(f"Updated {n} registry record(s).")
    else:
        print("No registry updates.")
    sys.exit(0)
