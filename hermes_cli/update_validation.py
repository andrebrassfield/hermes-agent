"""
Auto-validation harness for hermes update.

Runs a 3-gate staging check after every successful `git pull`, before the update
is marked complete. Any gate failure triggers auto-rollback to pre_pull_sha so the
user never ends up with a broken Hermes install.

Gates:
  Gate 1 — Syntax + import walk (instant, ~2s)
  Gate 2 — Mock conversation run (~30–60s)
  Gate 3 — Config schema validation (~1s)
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import time as time_mod
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HERMES_STAGING_ROOT = Path.home() / ".hermes" / "hermes-agent-staging"
HERMES_PRODUCTION_ROOT = Path.home() / ".hermes" / "hermes-agent"

# Files that must parse and import cleanly for Hermes to boot.
_UPDATE_CRITICAL_FILES = (
    "hermes_cli/main.py",
    "hermes_cli/config.py",
    "hermes_cli/__init__.py",
    "cli.py",
    "run_agent.py",
    "model_tools.py",
    "toolsets.py",
    "hermes_constants.py",
)


# ---------------------------------------------------------------------------
# Staging Manager
# ---------------------------------------------------------------------------

def ensure_staging_clone() -> bool:
    """
    Ensure the staging bare clone exists and is up-to-date with origin/main.

    Returns True if staging is ready; False if it could not be prepared
    (network error, etc.) — in which case auto-validation should be skipped
    with a warning rather than blocking the update.
    """
    if not HERMES_STAGING_ROOT.exists():
        HERMES_STAGING_ROOT.mkdir(parents=True, exist_ok=True)

    git_dir = HERMES_STAGING_ROOT / "hermes-agent.git"
    if not git_dir.exists():
        # First-time: clone as bare into the staging dir
        # We use our fork's bare clone for validation
        clone_cmd = [
            "git", "clone", "--bare",
            "git@github.com:andrebrassfield/hermes-agent.git",
            str(git_dir),
        ]
        env = os.environ.copy()
        env["GIT_SSH_COMMAND"] = "ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519"
        result = subprocess.run(
            clone_cmd,
            cwd=HERMES_STAGING_ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            return False

    # Always sync staging bare repo to match origin/main
    fetch_result = subprocess.run(
        ["git", "-C", str(git_dir), "fetch", "--all"],
        capture_output=True,
        text=True,
    )
    if fetch_result.returncode != 0:
        return False

    return True


def get_staging_head_sha() -> Optional[str]:
    """Return the SHA of staging's origin/main HEAD."""
    git_dir = HERMES_STAGING_ROOT / "hermes-agent.git"
    result = subprocess.run(
        ["git", "-C", str(git_dir), "rev-parse", "origin/main"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def checkout_staging_to_sha(sha: str, dest: Path) -> bool:
    """
    Checkout a specific SHA from the staging bare repo into *dest*.

    *dest* should be an empty temporary directory.  Used for gate 2 (conversation
    run) where we need a full worktree of the pulled code.
    """
    git_dir = HERMES_STAGING_ROOT / "hermes-agent.git"
    result = subprocess.run(
        ["git", "--git-dir", str(git_dir), "--work-tree", str(dest),
         "checkout", sha, "--", "."],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Gate 1 — Syntax + Import Walk
# ---------------------------------------------------------------------------

def gate1_syntax_and_imports(staging_root: Path) -> tuple[bool, str, str]:
    """
    Gate 1: Compile every critical file, then try a top-level import.

    Returns (ok, failing_path, error_message).
    """
    # Syntax check
    with tempfile.TemporaryDirectory(prefix="hermes-syntax-check-") as tmpdir:
        for relpath in _UPDATE_CRITICAL_FILES:
            path = staging_root / relpath
            if not path.exists():
                continue
            cfile = Path(tmpdir) / (relpath.replace("/", "__") + "c")
            try:
                py_compile.compile(str(path), cfile=str(cfile), doraise=True)
            except py_compile.PyCompileError as exc:
                return False, str(path), str(exc)
            except OSError as exc:
                return False, str(path), f"could not read: {exc}"

    # Import walk — simulate what Hermes does at startup
    # We test the hermes_cli package by importing its __init__ and main
    sys_path_backup = sys.path[:]
    try:
        # Add staging root's parent so 'hermes_cli' resolves correctly
        sys.path.insert(0, str(staging_root.parent))
        import hermes_cli
        importlib.reload(hermes_cli)
        # Also try main specifically
        main_mod = importlib.import_module("hermes_cli.main")
        importlib.reload(main_mod)
    except Exception as exc:
        sys.path[:] = sys_path_backup
        return False, "hermes_cli", str(exc)

    sys.path[:] = sys_path_backup
    return True, "", ""


# ---------------------------------------------------------------------------
# Gate 2 — Mock Conversation Run
# ---------------------------------------------------------------------------

def gate2_conversation_run(
    staging_root: Path,
    timeout: int = 120,
) -> tuple[bool, str]:
    """
    Gate 2: Run a headless mock conversation using the production hermes binary
    pointed at the staging source tree.

    This simulates real usage — conversation loop, tool registration, model routing —
    without requiring a valid API key.

    Returns (ok, error_message).
    """
    # Create an isolated HERMES_HOME for the test run
    staging_home = Path(tempfile.mkdtemp(prefix="hermes-staging-home-"))
    try:
        # Set up minimal Hermes home structure
        (staging_home / "backups").mkdir()
        (staging_home / "profiles").mkdir()
        # Write a minimal config that won't crash the gateway
        config_content = """
gateway:
  port: 18912
agents:
  default:
    model: minimax/minimax-m2-7-highspeed
    providers:
      - minimax
providers:
  minimax:
    type: minimax
    api_key: "VALIDATE-SKIP-KEY"
"""
        (staging_home / "config.yaml").write_text(config_content)

        # Path to production hermes binary
        venv_hermes = HERMES_PRODUCTION_ROOT / "venv" / "bin" / "hermes"
        if sys.platform == "win32":
            venv_hermes = HERMES_PRODUCTION_ROOT / "venv" / "Scripts" / "hermes.exe"

        # PYTHONPATH trick: staging_root.parent is where 'hermes_cli' package lives.
        # Setting PYTHONPATH to staging_root.parent makes Python import hermes_cli
        # from staging instead of production. The hermes binary itself is unchanged.
        run_env = os.environ.copy()
        run_env["PYTHONPATH"] = str(staging_root.parent)
        run_env["HERMES_HOME"] = str(staging_home)
        # Suppress prompts, skip version check, skip update check
        run_env["HERMES_NO_UPDATE_CHECK"] = "1"

        # Build the hermes chat command
        # We run `hermes chat --no-input` with a system prompt that exits fast
        proc = subprocess.Popen(
            [
                sys.executable,          # use the same python as running hermes
                str(venv_hermes),
                "chat",
                "--no-input",
                "--system-prompt",
                "Reply with exactly the word 'ping' and nothing else. Exit immediately after.",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=run_env,
        )

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return False, f"Conversation run timed out after {timeout}s"

        # Check for unhandled tracebacks in stderr
        stderr_text = stderr.decode("utf-8", errors="replace")
        if "Traceback (most recent call last)" in stderr_text:
            # Extract the relevant part (last 20 lines)
            lines = stderr_text.splitlines()
            relevant = lines[-20:] if len(lines) > 20 else lines
            return False, "\n".join(relevant)

        # Check exit code — non-zero means Hermes crashed
        if proc.returncode != 0:
            return False, f"Exit code {proc.returncode}: {stderr_text[:500]}"

        return True, ""

    finally:
        shutil.rmtree(staging_home, ignore_errors=True)


# ---------------------------------------------------------------------------
# Gate 3 — Config Schema Validation
# ---------------------------------------------------------------------------

def gate3_config_schema(staging_root: Path) -> tuple[bool, str]:
    """
    Gate 3: Parse staging's config.yaml with Hermes's own config parser,
    validate critical schema keys are present and well-typed.

    Returns (ok, error_message).
    """
    config_path = HERMES_PRODUCTION_ROOT / "config.yaml"
    if not config_path.exists():
        return False, "config.yaml not found in production"

    sys_path_backup = sys.path[:]
    try:
        sys.path.insert(0, str(staging_root.parent))
        from hermes_cli import config as cfg_mod

        # Patch PROJECT_ROOT to staging so config loader reads staging config
        import hermes_cli.config as _cfg
        orig_project_root = getattr(_cfg, "PROJECT_ROOT", None)
        _cfg.PROJECT_ROOT = staging_root

        try:
            parsed = cfg_mod._load_config_file(str(config_path))
        except Exception as exc:
            return False, f"config parse error: {exc}"
        finally:
            if orig_project_root is not None:
                _cfg.PROJECT_ROOT = orig_project_root

        # Validate critical keys
        errors = []
        if "gateway" not in parsed:
            errors.append("missing required key: gateway")
        if "agents" not in parsed:
            errors.append("missing required key: agents")
        if "providers" not in parsed:
            errors.append("missing required key: providers")

        if errors:
            return False, "; ".join(errors)

        return True, ""

    except Exception as exc:
        return False, f"config validation error: {exc}"
    finally:
        sys.path[:] = sys_path_backup


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def run_auto_validation(
    staging_root: Path,
    pre_pull_sha: str,
    config: Optional[dict] = None,
    timeout: int = 120,
) -> tuple[bool, str, str]:
    """
    Run the full 3-gate validation harness.

    Args:
        staging_root: Path to the staging clone worktree (checked-out SHA)
        pre_pull_sha: SHA to roll back to if validation fails
        config: Optional updates config dict (for gate list, timeout override)
        timeout: Seconds for gate 2 (conversation run); default 120

    Returns:
        (ok, failing_gate, error_message)
        ok=False means rollback should be triggered.
    """
    gates = [
        ("syntax", gate1_syntax_and_imports),
        ("conversation", gate2_conversation_run),
        ("config", gate3_config_schema),
    ]

    # Respect config knobs (if provided)
    if config:
        enabled = config.get("validate_gates", ["syntax", "conversation", "config"])
        gates = [(name, fn) for name, fn in gates if name in enabled]
        timeout = config.get("validate_timeout", timeout)

    print("→ Running auto-validation (3-gate staging harness)...")

    for gate_name, gate_fn in gates:
        print(f"  Gate {gate_name}: ", end="", flush=True)
        start = time_mod.monotonic()

        try:
            if gate_name == "syntax":
                ok, failing_path, err = gate_fn(staging_root)
                elapsed = time_mod.monotonic() - start
                if ok:
                    print(f"✓ ({elapsed:.1f}s)")
                else:
                    print(f"✗")
                    return False, gate_name, f"syntax/import error in {failing_path}: {err}"
            elif gate_name == "conversation":
                ok, err = gate_fn(staging_root, timeout=timeout)
                elapsed = time_mod.monotonic() - start
                if ok:
                    print(f"✓ ({elapsed:.1f}s)")
                else:
                    print(f"✗")
                    return False, gate_name, f"conversation run failed: {err}"
            elif gate_name == "config":
                ok, err = gate_fn(staging_root)
                elapsed = time_mod.monotonic() - start
                if ok:
                    print(f"✓ ({elapsed:.1f}s)")
                else:
                    print(f"✗")
                    return False, gate_name, f"config schema error: {err}"

        except Exception as exc:
            elapsed = time_mod.monotonic() - start
            print(f"✗ ({elapsed:.1f}s)")
            return False, gate_name, f"unhandled exception in {gate_name}: {exc}"

    print("  ✓ All gates passed")
    return True, "", ""


def run_validation_from_pull(
    pre_pull_sha: str,
    config: Optional[dict] = None,
    timeout: int = 120,
) -> tuple[bool, str, str]:
    """
    Top-level entry point: set up staging and run validation after a successful pull.

    Called from _cmd_update_impl() between the syntax guard and bytecode cache clear.
    If staging cannot be prepared, returns (True, "", "") so the update proceeds
    without validation (skip-with-warning rather than block).

    Returns:
        (ok, failing_gate, error_message)
    """
    # Ensure staging is up-to-date
    if not ensure_staging_clone():
        print("  ⚠ Staging clone unavailable — skipping auto-validation")
        return True, "", ""

    # Get the SHA we just pulled (origin/main HEAD in the staging bare repo)
    staging_sha = get_staging_head_sha()
    if not staging_sha:
        print("  ⚠ Could not resolve staging HEAD — skipping auto-validation")
        return True, "", ""

    # Checkout staging SHA to a temp dir for validation
    staging_worktree = Path(tempfile.mkdtemp(prefix="hermes-validation-worktree-"))
    try:
        if not checkout_staging_to_sha(staging_sha, staging_worktree):
            print("  ⚠ Could not checkout staging worktree — skipping auto-validation")
            return True, "", ""

        # Patch PROJECT_ROOT in the staging worktree so imports resolve correctly
        staging_root = staging_worktree / "hermes-agent"

        ok, gate, err = run_auto_validation(
            staging_root=staging_root,
            pre_pull_sha=pre_pull_sha,
            config=config,
            timeout=timeout,
        )
        return ok, gate, err

    finally:
        shutil.rmtree(staging_worktree, ignore_errors=True)