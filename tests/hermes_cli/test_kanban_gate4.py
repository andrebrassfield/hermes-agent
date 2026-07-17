"""Regression tests for Gate 4 — dirty-tree check at kanban_complete.

Verifies the dirty-tree detection logic in kanban_gate4.py.
These tests use temp git repos to create real dirty-tree scenarios.

Decision 2026-07-16 §2.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from hermes_cli import kanban_gate4 as g4


class TestGate4RepoInspection:
    """Unit tests for _check_repo_for_dirty_tree."""

    @pytest.fixture(autouse=True)
    def isolated_git_env(self, tmp_path: Path) -> None:
        """Isolate git config by nulling GLOBAL/SYSTEM config vars."""
        import os as _os
        self._env = {
            **_os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }

    def _git(self, *args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self._env,
            capture_output=True,
            text=True,
            check=check,
        )

    def test_clean_repo_returns_pass(self, tmp_path: Path) -> None:
        """A completely clean repo (no uncommitted, no untracked) → not dirty."""
        self._git("init", cwd=tmp_path)
        self._git("config", "user.email", "test@test.com", cwd=tmp_path)
        self._git("config", "user.name", "Test", cwd=tmp_path)
        # Create the file before trying to add it
        (tmp_path / "README.md").write_text("initial content")
        self._git("add", "README.md", cwd=tmp_path)
        self._git("commit", "-m", "initial", cwd=tmp_path)

        is_dirty, dirty_files = g4._check_repo_for_dirty_tree(tmp_path)
        assert not is_dirty
        assert dirty_files == []

    def test_modified_file_detected(self, tmp_path: Path) -> None:
        """Uncommitted tracked file change → dirty."""
        self._git("init", cwd=tmp_path)
        self._git("config", "user.email", "test@test.com", cwd=tmp_path)
        self._git("config", "user.name", "Test", cwd=tmp_path)
        readme = tmp_path / "README.md"
        readme.write_text("initial")
        self._git("add", ".", cwd=tmp_path)
        self._git("commit", "-m", "initial", cwd=tmp_path)
        # Modify without committing
        readme.write_text("modified")

        is_dirty, dirty_files = g4._check_repo_for_dirty_tree(tmp_path)
        assert is_dirty
        assert len(dirty_files) >= 1
        statuses = [d.status for d in dirty_files]
        assert "modified" in statuses

    def test_untracked_source_file_detected(self, tmp_path: Path) -> None:
        """Untracked .py file → dirty (matches source pattern)."""
        self._git("init", cwd=tmp_path)
        self._git("config", "user.email", "test@test.com", cwd=tmp_path)
        self._git("config", "user.name", "Test", cwd=tmp_path)
        # Create a committed file
        (tmp_path / "README.md").write_text("init")
        self._git("add", ".", cwd=tmp_path)
        self._git("commit", "-m", "initial", cwd=tmp_path)
        # Add untracked .py file
        (tmp_path / "new_script.py").write_text("print('hello')")

        is_dirty, dirty_files = g4._check_repo_for_dirty_tree(tmp_path)
        assert is_dirty
        untracked = [d for d in dirty_files if d.status == "untracked"]
        assert any("new_script.py" in d.path for d in untracked)

    def test_scratch_file_not_flagged(self, tmp_path: Path) -> None:
        """.bak / __pycache__ / .DS_Store are excluded from untracked checks."""
        self._git("init", cwd=tmp_path)
        self._git("config", "user.email", "test@test.com", cwd=tmp_path)
        self._git("config", "user.name", "Test", cwd=tmp_path)
        (tmp_path / "README.md").write_text("init")
        self._git("add", ".", cwd=tmp_path)
        self._git("commit", "-m", "initial", cwd=tmp_path)
        # Scratch / backup files
        (tmp_path / "script.py.bak").write_text("backup")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / ".DS_Store").write_text("")

        is_dirty, dirty_files = g4._check_repo_for_dirty_tree(tmp_path)
        # None of the scratch files should appear
        dirty_paths = [d.path for d in dirty_files]
        assert not any("script.py.bak" in p or "__pycache__" in p or ".DS_Store" in p for p in dirty_paths)

    def test_gitignored_file_not_flagged(self, tmp_path: Path) -> None:
        """.gitignore'd files are not flagged as untracked."""
        self._git("init", cwd=tmp_path)
        self._git("config", "user.email", "test@test.com", cwd=tmp_path)
        self._git("config", "user.name", "Test", cwd=tmp_path)
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / "README.md").write_text("init")
        self._git("add", ".", cwd=tmp_path)
        self._git("commit", "-m", "initial", cwd=tmp_path)
        # Untracked but gitignored
        (tmp_path / "debug.log").write_text("log output")

        is_dirty, dirty_files = g4._check_repo_for_dirty_tree(tmp_path)
        # debug.log is gitignored → not dirty
        dirty_paths = [d.path for d in dirty_files]
        assert not any("debug.log" in p for p in dirty_paths)


class TestGate4Verdict:
    """Tests for gate4_dirty_tree_check verdict logic."""

    def test_verdict_pass_on_clean_repo(self, tmp_path: Path) -> None:
        """Clean repo → verdict.status = 'pass'."""
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path, check=True, capture_output=True,
        )
        (tmp_path / "README.md").write_text("init")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

        verdict = g4.gate4_dirty_tree_check(
            workspace_path=str(tmp_path),
            declared_repo=None,
            kanban_task_id="t_test123",
        )
        assert verdict.passed
        assert verdict.status == "pass"

    def test_verdict_block_on_modified(self, tmp_path: Path) -> None:
        """Modified tracked file → verdict.status = 'block'."""
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path, check=True, capture_output=True,
        )
        readme = tmp_path / "README.md"
        readme.write_text("initial")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
        readme.write_text("changed")

        verdict = g4.gate4_dirty_tree_check(
            workspace_path=str(tmp_path),
            declared_repo=None,
            kanban_task_id="t_test456",
        )
        assert not verdict.passed
        assert verdict.status == "block"
        reason = verdict.reason or ""
        assert "dirty" in reason.lower() or "modified" in reason.lower()
