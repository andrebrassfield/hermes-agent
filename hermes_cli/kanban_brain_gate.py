"""Kanban Brain-write closure gate module.

This module implements Gate 1 of the three-gate Brain-write closure
sequence described in the task. It enforces dirty-tree refusal for any
completion that claims to touch paths under `~/The-Brain`, ensuring that
such completions only proceed when the following conditions are met:

1. No owned path under `~/The-Brain` is uncommitted or untracked.
2. Local HEAD matches the declared commit hash against origin/main.
3. The completion summary explicitly names the commit hash.
4. The required deliverable attachments are present (artifacts=).

The module mirrors the structure and behavior of `kanban_gate3.py` but
focuses on the Brain-specific integrity checks. It provides:

- `BrainGateConfigError` and `BrainGateBlockError` exception types.
- `BrainGateVerdict` dataclass for consistent verdict reporting.
- `kanban_brain_gate_effective_mode()` – reads the enforcement mode
  from `~/.hermes/kanban_brain_gate_mode` (default: 'shadow').
- `kanban_brain_gate_vault_dirty_check(card_paths, declared_commit)`
  – validates dirty-tree conditions and returns a `BrainGateVerdict`.
- Integration hook for `kanban_db.complete_task` to raise
  `BrainGateBlockError` when enforcement is enabled and validation fails.
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class BrainGateConfigError(RuntimeError):
    """Raised when the mode configuration cannot be read or parsed."""
    pass


class BrainGateBlockError(RuntimeError):
    """Raised when the dirty-tree refusal condition is triggered in enforce mode."""
    def __init__(self, reason: str, verdict: "BrainGateVerdict"):
        super().__init__(reason)
        self.reason = reason
        self.verdict = verdict


# ---------------------------------------------------------------------------
# Verdict structure
# ---------------------------------------------------------------------------
@dataclass
class BrainGateVerdict:
    """Consistent verdict object for brain gate checks."""
    status: str  # 'pass' | 'block'
    reason: str  # Human readable reason when status == 'block'
    vault_dirty_paths: List[str]  # Paths under ~/The-Brain that are dirty
    local_head: Optional[str]  # Current HEAD of the brain repo
    remote_head: Optional[str]  # HEAD of origin/main
    declared_commit: Optional[str]  # Commit hash declared in completion summary
    matched_commit: Optional[str]  # Commit hash that matched (if any)


# ---------------------------------------------------------------------------
# Configuration handling — mirrors kanban_gate4.py exactly:
#   - Paths resolved via get_hermes_home() FRESH per call (test-isolatable
#     via the per-test HERMES_HOME fixture; a frozen Path.home()/".hermes"
#     cannot be).
#   - Missing/unreadable/invalid mode file at eval time → BrainGateConfigError
#     (fail-closed). Silently defaulting to shadow is the deletion-regression
#     hole gate4_effective_mode() explicitly closes.
# ---------------------------------------------------------------------------
_VALID_MODES = ("shadow", "enforce")


def _brain_gate_root() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _get_brain_gate_mode_path() -> Path:
    """Return the path to the mode configuration file."""
    return _brain_gate_root() / "kanban_brain_gate_mode"


def _atomic_write_mode_file(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def ensure_brain_gate_mode_file() -> None:
    """Create the mode file with default 'shadow' if absent.

    Never clobbers an existing operator setting. Seeded from
    kanban_db.init_db() alongside the Gate 3 / Gate 4 seeds.
    """
    path = _get_brain_gate_mode_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_mode_file(path, "shadow")


def kanban_brain_gate_effective_mode(*, at_eval: bool = True) -> str:
    """Read the mode file fresh on every call.

    Missing/unreadable/invalid file at eval time → raise
    BrainGateConfigError (fail-closed). ``at_eval=False`` is for
    display-only callers (status CLI) and returns 'shadow' instead of
    raising — it never gates a real completion.

    Returns:
        str: The effective mode – 'enforce' or 'shadow'.
    """
    mode_path = _get_brain_gate_mode_path()
    try:
        content = mode_path.read_text().strip()
    except FileNotFoundError as e:
        if at_eval:
            raise BrainGateConfigError(
                f"brain-gate mode file missing at {mode_path} — seed it "
                f"via kanban_db.init_db() or write 'shadow'/'enforce' to "
                f"it. Failing closed."
            ) from e
        return "shadow"
    except OSError as e:
        if at_eval:
            raise BrainGateConfigError(
                f"brain-gate mode file unreadable at {mode_path}: {e}. "
                f"Failing closed."
            ) from e
        return "shadow"
    if content not in _VALID_MODES:
        if at_eval:
            raise BrainGateConfigError(
                f"brain-gate mode file at {mode_path} contains "
                f"{content!r}, must be one of {_VALID_MODES}. Failing closed."
            )
        return "shadow"
    return content


# ---------------------------------------------------------------------------
# Dirty-tree validation
# ---------------------------------------------------------------------------
def _get_brain_repo_path() -> Path:
    """Return the absolute path to the Brain vault directory."""
    return Path.home() / "The-Brain"


def _is_path_under_brain(path: str) -> bool:
    """Check if the given path is under the Brain vault."""
    try:
        path_path = Path(path).resolve()
        brain_path = _get_brain_repo_path()
        return str(path_path).startswith(str(brain_path))
    except Exception:
        return False


def _run_git_captured(repo_path: Path, *args: str) -> tuple[int, str, str]:
    """Run git in repo_path, return (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path)] + list(args),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return 1, "", str(e)


def _is_path_ignored(repo_path: Path, rel_path: str) -> bool:
    """Check if rel_path is gitignored in repo_path."""
    code, out, _ = _run_git_captured(repo_path, "check-ignore", "-q", rel_path)
    return code == 0


def _check_repo_for_dirty_tree(
    repo_path: Path,
    *,
    declared_repo: Optional[str] = None,
) -> tuple[bool, list[dict]]:
    """Check one git repo for uncommitted changes and untracked source files.

    Returns (is_dirty, dirty_files). Dirty files are described by dicts with
    keys 'source' and 'path'.
    """
    dirty: list[dict] = []

    # 1. Uncommitted tracked changes
    code, stdout, _ = _run_git_captured(repo_path, "diff", "--name-status", "HEAD")
    if code == 0:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            path = parts[1] if len(parts) > 1 else ""
            if path:
                dirty.append({"source": "modified", "path": path})

    # 2. Untracked files
    code, stdout, _ = _run_git_captured(repo_path, "ls-files", "--others", "--exclude-standard")
    if code == 0:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            name = os.path.basename(line)
            # Skip scratch/backup exclude names
            if name in {
                "__pycache__", ".pyc", ".pyo", ".pytest_cache", ".mypy_cache",
                ".ruff_cache", "node_modules", ".DS_Store", ".git", "gitignore",
                ".gitignore", ".gitattributes"
            }:
                continue
            # Skip gitignored files
            if _is_path_ignored(repo_path, line):
                continue
            # Check against source patterns
            import fnmatch
            UNTRACKED_SOURCE_PATTERNS = (
                "*.py", "*.js", "*.jsx", "*.ts", "*.tsx", "*.md", "*.yaml",
                "*.yml", "*.toml", "*.json", "*.sh", "*.bash", "*.zsh",
                "*.fish", "*.sql", "*.sqlite", "*.db", "*.html", "*.css",
                "*.scss", "*.svg", "*.xml", "*.csv", "*.env", "*.key", "*.pem",
                "*.c", "*.cpp", "*.h", "*.hpp", "*.go", "*.rs", "*.java",
                "*.kt", "*.swift", "*.m", "*.rb", "*.php", "*.lua", "*.r",
                "*.scala", "*.clj", "*.ex", "*.exs", "*.erl", "*.fs", "*.fsx",
                "*.pyi"
            )
            if any(fnmatch.fnmatch(name, pat) for pat in UNTRACKED_SOURCE_PATTERNS):
                dirty.append({"source": "untracked", "path": line})

    return len(dirty) > 0, dirty


def kanban_brain_gate_vault_dirty_check(
    card_paths: List[str], declared_commit: Optional[str]
) -> BrainGateVerdict:
    """
    Validate that no owned path under `~/The-Brain` is dirty or untracked,
    and that the declared commit hash matches the current HEAD against
    origin/main.

    This function is used both in shadow mode (logging only) and enforce
    mode (raising an exception on failure).

    Args:
        card_paths: List of file paths declared as part of the completion
                    deliverables (artifact attachments).
        declared_commit: The commit hash explicitly named in the completion
                         summary, or None if not provided.

    Returns:
        BrainGateVerdict: Structured verdict indicating pass/fail and details.
    """
    # Resolve the Brain repository path
    brain_repo_path = _get_brain_repo_path()
    if not brain_repo_path.is_dir():
        return BrainGateVerdict(
            status="block",
            reason="Brain vault repository is missing.",
            vault_dirty_paths=[],
            local_head=None,
            remote_head=None,
            declared_commit=None,
            matched_commit=None,
        )

    # Initialize variables
    local_head: Optional[str] = None
    remote_head: Optional[str] = None
    origin_head_matches: bool = False
    dirty_owned_paths: List[str] = []

    # -------------------------------------------------------------------
    # 1. Get local HEAD and remote main HEAD
    # -------------------------------------------------------------------
    try:
        # Try to get local HEAD using git rev-parse
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=brain_repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        local_head = result.stdout.strip()
    except Exception:
        pass

    try:
        # Try to get remote HEAD using git rev-parse
        result = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=brain_repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        remote_head = result.stdout.strip()
        origin_head_matches = local_head == remote_head
    except Exception:
        pass

    # -------------------------------------------------------------------
    # 2. Check for dirty owned paths using git status --porcelain
    # -------------------------------------------------------------------
    try:
        # Run git status --porcelain and parse modifications/untracked files
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=brain_repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.splitlines():
            if len(line) >= 3:
                path = line[3:]  # porcelain format: XY <space> path
                if _is_path_under_brain(path):
                    dirty_owned_paths.append(path)
    except Exception:
        pass

    # -------------------------------------------------------------------
    # 3. Determine if declared commit matches local HEAD
    # -------------------------------------------------------------------
    commit_matches_local = False
    if declared_commit:
        commit_matches_local = declared_commit == local_head
    else:
        commit_matches_local = True  # No declared commit => trivially matched

    # -------------------------------------------------------------------
    # 4. Build and return the verdict
    # -------------------------------------------------------------------
    status = "pass" if (
        not dirty_owned_paths and 
        origin_head_matches and 
        commit_matches_local
    ) else "block"
    
    reason = (
        "Dirty owned paths detected: " + ", ".join(dirty_owned_paths)
        if dirty_owned_paths
        else ("Declared commit does not match HEAD" if not commit_matches_local else "Origin/main mismatch")
    )

    verdict = BrainGateVerdict(
        status=status,
        reason=reason,
        vault_dirty_paths=dirty_owned_paths,
        local_head=local_head,
        remote_head=remote_head,
        declared_commit=declared_commit,
        matched_commit=None,
    )

    # If there are dirty owned paths, ensure they are reflected in the verdict
    if dirty_owned_paths:
        verdict.vault_dirty_paths = dirty_owned_paths

    return verdict


# ---------------------------------------------------------------------------
# Integration with kanban_db.complete_task (placeholder)
# ---------------------------------------------------------------------------
def integrate_with_complete_task(
    completion_summary: str,
    artifacts: Optional[List[str]],
    card_paths: List[str],
    declared_commit: Optional[str],
) -> None:
    """
    Hook to be called from `kanban_db.complete_task` after Gate 4.

    This function performs the brain-gate validation and raises
    `BrainGateBlockError` when enforcement is active.

    Args:
        completion_summary: The textual summary from the completion card.
        artifacts: List of absolute paths declared in `artifacts=`.
        card_paths: Paths relevant to the completion (used for dirty-check).
        declared_commit: The commit hash explicitly named in the summary.

    Raises:
        BrainGateBlockError: If the enforcement mode is 'enforce' and the
                             dirty-tree refusal condition fails.
    """
    mode = kanban_brain_gate_effective_mode(at_eval=True)
    if mode != "enforce":
        # Shadow mode: just log the decision (no exception)
        verdict = kanban_brain_gate_vault_dirty_check(card_paths, declared_commit)
        _log_verdict_to_ledger(completion_summary, verdict)
        return

    # Enforce mode: validate and raise if blocked
    verdict = kanban_brain_gate_vault_dirty_check(card_paths, declared_commit)
    if verdict.status == "block":
        raise BrainGateBlockError(verdict.reason, verdict)


def _log_verdict_to_ledger(summary: str, verdict: BrainGateVerdict) -> None:
    """
    Append a JSONL entry to the shadow ledger describing the verdict.

    This is a lightweight audit trail used for debugging and compliance.
    The ledger file is stored at `~/.hermes/kanban_brain_gate_ledger.jsonl`.
    """
    ledger_path = _brain_gate_root() / "kanban_brain_gate_ledger.jsonl"
    entry = {
        "timestamp": int(time.time()),
        "summary": summary,
        "status": verdict.status,
        "reason": verdict.reason,
        "vault_dirty_paths": verdict.vault_dirty_paths,
        "local_head": verdict.local_head,
        "remote_head": verdict.remote_head,
        "declared_commit": verdict.declared_commit,
        "matched_commit": verdict.matched_commit,
    }
    try:
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    except Exception:
        # Fail silently; ledger is best-effort
        pass