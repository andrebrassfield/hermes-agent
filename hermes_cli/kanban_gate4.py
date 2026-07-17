"""Gate 4 — dirty-tree check at kanban_complete.

Shadow-mode ONLY (per Decision 2026-07-16): at ``kanban_complete``, if
the task's declared repo or ``$HERMES_KANBAN_WORKSPACE`` (when it is a git
repo) has uncommitted tracked changes or untracked non-ignored files matching
source patterns, that is a gate event.

Mechanics mirror Gate 3 exactly:
  - Mode file ``~/.hermes/gate4_mode``, read FRESH per eval.
  - Default mode: shadow.
  - Fail-closed with a distinct config error (Gate4ConfigError).
  - Shadow logs pass AND block to a JSONL ledger.
  - Verdict computation identical in shadow and enforce; only raise-vs-log differs.
  - Operator CLI: ``hermes kanban gate4 status|flip-enforce|flip-shadow``.

ENFORCE FLIP IS DRE-ONLY (Decision 2026-07-16 §2). This module ships with
enforce permanently gated behind the operator; shadow is the only valid
runtime mode until Dre reviews the ledger.

Decision 2026-07-16.
LOC: ~200.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_MODES = ("shadow", "enforce")

# Default patterns for untracked source files that trigger the gate.
# Only source-code / content files are checked — build artifacts, scratch
# files, and backup files are excluded. These patterns use glob syntax
# (fnmatch) applied to the filename only, not the full path.
_UNTRACKED_SOURCE_PATTERNS: tuple[str, ...] = (
    "*.py",
    "*.js",
    "*.jsx",
    "*.ts",
    "*.tsx",
    "*.md",
    "*.yaml",
    "*.yml",
    "*.toml",
    "*.json",
    "*.sh",
    "*.bash",
    "*.zsh",
    "*.fish",
    "*.sql",
    "*.sqlite",
    "*.db",
    "*.html",
    "*.css",
    "*.scss",
    "*.svg",
    "*.xml",
    "*.csv",
    "*.env",
    "*.key",
    "*.pem",
    "*.c",
    "*.cpp",
    "*.h",
    "*.hpp",
    "*.go",
    "*.rs",
    "*.java",
    "*.kt",
    "*.swift",
    "*.m",
    "*.rb",
    "*.php",
    "*.lua",
    "*.r",
    "*.scala",
    "*.clj",
    "*.ex",
    "*.exs",
    "*.erl",
    "*.fs",
    "*.fsx",
    "*.pyi",
)

# Files always excluded even if they match a source pattern.
_SCRATCH_BACKUP_EXCLUDE_NAMES: frozenset[str] = frozenset({
    "__pycache__",
    ".pyc",
    ".pyo",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".DS_Store",
    ".git",
    ".gitignore",
    ".gitattributes",
})

# ---------------------------------------------------------------------------
# Paths — at HERMES_HOME/gate4_mode + gate4_shadow.jsonl. Resolved via
# get_hermes_home() FRESH on every call — not a module-level constant.
# get_hermes_home() is this codebase's single source of truth for
# ~/.hermes resolution (reads HERMES_HOME, falls back to the platform
# default); a frozen `Path.home() / ".hermes"` computed once at import
# time cannot be test-isolated by the standard per-test HERMES_HOME
# fixture (tests/conftest.py: "Any code ... reading ~/.hermes/* via
# Path.home() / '.hermes' instead of get_hermes_home() is a bug to fix
# at the callsite"). Mirrors kanban_gate3.py's resolution exactly.
# ---------------------------------------------------------------------------


def _gate4_root() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _gate4_mode_file() -> Path:
    return _gate4_root() / "gate4_mode"


def _gate4_ledger_file() -> Path:
    return _gate4_root() / "gate4_shadow.jsonl"


def _ledger_lock_path() -> Path:
    return _gate4_ledger_file().with_suffix(".jsonl.lock")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DirtyFile:
    path: str
    status: str  # 'modified' | 'untracked'


@dataclass
class Gate4Verdict:
    status: str          # 'pass' | 'block'
    dirty_repos: list[dict] = field(default_factory=list)
    reason: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status == "pass"


# ---------------------------------------------------------------------------
# Config errors
# ---------------------------------------------------------------------------

class Gate4ConfigError(RuntimeError):
    """Raised when the gate cannot read its mode file.

    Distinct from Gate4BlockError so the operator can tell
    "gate can't read its setting" from "worker left tree dirty".
    """


# ---------------------------------------------------------------------------
# Mode file helpers (mirrors Gate 3's _atomic_write_mode_file)
# ---------------------------------------------------------------------------

def _atomic_write_mode_file(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def gate4_effective_mode(*, at_eval: bool = True) -> str:
    """Read the mode file fresh on every call.

    Missing/unreadable/invalid file → raise Gate4ConfigError (fail-closed).
    Does NOT silently default to shadow — that is the deletion-regression hole.
    """
    path = _gate4_mode_file()
    try:
        text = path.read_text().strip()
    except FileNotFoundError as e:
        if at_eval:
            raise Gate4ConfigError(
                f"gate4 mode file missing at {path} — operator must "
                f"run `hermes kanban gate4 flip-enforce` or `hermes kanban gate4 "
                f"flip-shadow` to create it. Failing closed."
            ) from e
        return "shadow"
    except OSError as e:
        if at_eval:
            raise Gate4ConfigError(
                f"gate4 mode file unreadable at {path}: {e}. "
                f"Failing closed."
            ) from e
        return "shadow"
    if text not in _VALID_MODES:
        if at_eval:
            raise Gate4ConfigError(
                f"gate4 mode file at {path} contains {text!r}, must be "
                f"one of {_VALID_MODES}. Failing closed."
            )
        return "shadow"
    return text


def ensure_gate4_mode_file() -> None:
    """Create the mode file with default 'shadow' if absent."""
    path = _gate4_mode_file()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_mode_file(path, "shadow")


def flip_gate4_mode(new_mode: str) -> None:
    """Atomic flip of the mode. Validates new_mode, then atomic-writes."""
    if new_mode not in _VALID_MODES:
        raise ValueError(f"invalid mode {new_mode!r}; must be one of {_VALID_MODES}")
    path = _gate4_mode_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_mode_file(path, new_mode)
    print(f"gate4 mode flipped to {new_mode!r} at {path}", file=__import__("sys").stderr)


# ---------------------------------------------------------------------------
# Git tree inspection
# ---------------------------------------------------------------------------

def _run_git_captured(repo_path: Path, *args: str) -> tuple[int, str, str]:
    """Run git in repo_path, return (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path)] + list(args),
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return 1, "", str(e)


def _is_gitignored(repo_path: Path, rel_path: str) -> bool:
    """Check if rel_path is gitignored in repo_path."""
    code, out, _ = _run_git_captured(repo_path, "check-ignore", "-q", rel_path)
    return code == 0


def _check_repo_for_dirty_tree(
    repo_path: Path,
    *,
    declared_repo: Optional[str] = None,
) -> tuple[bool, list[DirtyFile]]:
    """Check one git repo for uncommitted changes and untracked source files.

    Returns (is_dirty, dirty_files).
    """
    dirty: list[DirtyFile] = []

    # 1. Uncommitted tracked changes
    code, stdout, _ = _run_git_captured(repo_path, "diff", "--name-status", "HEAD")
    if code == 0:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            status = parts[0]
            path = parts[1] if len(parts) > 1 else ""
            if path:
                dirty.append(DirtyFile(path=path, status="modified"))

    # 2. Untracked files
    code, stdout, _ = _run_git_captured(repo_path, "ls-files", "--others", "--exclude-standard")
    if code == 0:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            # Skip scratch/backup exclude names
            name = os.path.basename(line)
            if name in _SCRATCH_BACKUP_EXCLUDE_NAMES:
                continue
            # Skip gitignored files
            if _is_gitignored(repo_path, line):
                continue
            # Check against source patterns
            import fnmatch
            if any(fnmatch.fnmatch(name, pat) for pat in _UNTRACKED_SOURCE_PATTERNS):
                dirty.append(DirtyFile(path=line, status="untracked"))

    return len(dirty) > 0, dirty


# ---------------------------------------------------------------------------
# Gate check
# ---------------------------------------------------------------------------

def gate4_dirty_tree_check(
    *,
    workspace_path: Optional[str] = None,
    declared_repo: Optional[str] = None,
    kanban_task_id: str,
    profile: Optional[str] = None,
) -> Gate4Verdict:
    """Check the task's repos for dirty trees.

    Checks both the declared_repo (if provided) and the workspace_path
    (if it is a git repo). Source-pattern untracked files + any uncommitted
    tracked changes trigger a 'block' verdict.

    Mirrors Gate 3 mechanics exactly:
      - Verdict computation is identical in shadow and enforce.
      - The only difference is raise-vs-log at the call site.
    """
    repos_checked: list[dict] = []
    all_dirty: list[dict] = []
    is_dirty = False

    repos_to_check: list[tuple[Optional[str], Path]] = []

    if declared_repo:
        decl_path = Path(declared_repo).resolve()
        if decl_path.is_dir():
            repos_to_check.append(("declared_repo", decl_path))

    if workspace_path:
        ws_path = Path(workspace_path).resolve()
        if ws_path.is_dir():
            # Avoid double-checking the same path
            if not any(p == ws_path for _, p in repos_to_check):
                repos_to_check.append(("workspace", ws_path))

    # Also check HERMES_KANBAN_WORKSPACE if it differs from workspace_path
    kws = os.environ.get("HERMES_KANBAN_WORKSPACE", "")
    if kws:
        kws_path = Path(kws).resolve()
        if kws_path.is_dir() and not any(p == kws_path for _, p in repos_to_check):
            # Only check if it looks like a git repo
            if (kws_path / ".git").is_dir():
                repos_to_check.append(("kanban_workspace", kws_path))

    for source, repo_path in repos_to_check:
        dirty_flag, dirty_files = _check_repo_for_dirty_tree(
            repo_path, declared_repo=declared_repo,
        )
        repos_checked.append({
            "source": source,
            "path": str(repo_path),
            "dirty": dirty_flag,
        })
        if dirty_files:
            all_dirty.extend([
                {"source": source, "path": d.path, "status": d.status}
                for d in dirty_files
            ])
        if dirty_flag:
            is_dirty = True

    if not is_dirty:
        return Gate4Verdict(status="pass", dirty_repos=repos_checked)

    return Gate4Verdict(
        status="block",
        dirty_repos=repos_checked,
        reason=(
            f"Gate4: dirty tree detected in {len([r for r in repos_checked if r['dirty']])} "
            f"repo(s), {len(all_dirty)} dirty file(s): "
            + "; ".join(f"{d['path']} ({d['status']})" for d in all_dirty[:5])
            + (" ..." if len(all_dirty) > 5 else "")
        ),
    )


# ---------------------------------------------------------------------------
# JSONL ledger
# ---------------------------------------------------------------------------

def _append_ledger_row(row: dict) -> None:
    """Append a JSON-encoded row to the ledger. Atomic via temp+rename."""
    path = _gate4_ledger_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    row.setdefault("timestamp", int(time.time()))
    line = json.dumps(row, sort_keys=True, separators=(",", ":"))
    lock = _ledger_lock_path()
    if lock.exists():
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    existing = path.read_text() if path.exists() else ""
    tmp.write_text(existing + line + "\n")
    os.replace(tmp, path)


def _record_gate4_eval(
    *,
    effective_mode: str,
    verdict: Gate4Verdict,
    task_id: str,
    profile: Optional[str],
    workspace_path: Optional[str],
    declared_repo: Optional[str],
) -> None:
    """Emit a JSONL ledger row for one gate eval (pass or block).

    Every non-skip eval writes one row (pass AND block both logged, per
    Decision 2026-07-16 §2).
    """
    reason = verdict.reason or ""
    if effective_mode == "shadow" and verdict.status == "block":
        reason = "[GATE4 SHADOW — advisory only, not blocking] " + reason
    row = {
        "event": "completion_would_block_gate4",
        "effective_mode": effective_mode,
        "task_id": task_id,
        "profile": profile,
        "workspace_path": workspace_path,
        "declared_repo": declared_repo,
        "status": verdict.status,
        "dirty_repos": verdict.dirty_repos,
        "reason": reason,
    }
    _append_ledger_row(row)


# ---------------------------------------------------------------------------
# Convenience re-exports for the CLI status command
# ---------------------------------------------------------------------------

def gate4_status_lines() -> list[str]:
    """Return status lines for ``hermes kanban gate4 status``."""
    try:
        mode = gate4_effective_mode(at_eval=False)
    except Gate4ConfigError:
        mode = "<unreadable>"
    path = _gate4_mode_file()
    ledger = _gate4_ledger_file()
    lines = [
        f"gate4 effective_mode: {mode}",
        f"mode file: {path}",
        f"ledger file: {ledger}",
    ]
    # Recent block count from ledger. Parse each row as JSON rather than
    # string-matching '"status": "block"' — the ledger is serialized
    # compact (no space after ':', see _append_ledger_row), so a naive
    # spaced-key match silently undercounts to zero.
    if ledger.exists():
        try:
            lines_ = ledger.read_text().splitlines()
            blocks = 0
            for _line in lines_:
                if not _line.strip():
                    continue
                try:
                    _row = json.loads(_line)
                except json.JSONDecodeError:
                    continue
                if _row.get("status") == "block":
                    blocks += 1
            lines.append(f"total ledger rows: {len(lines_)}")
            lines.append(f"total would-block rows: {blocks}")
        except OSError:
            pass
    return lines
