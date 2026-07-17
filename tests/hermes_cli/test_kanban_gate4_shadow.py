"""Shadow-mode tests for Gate 4 — dirty-tree check at kanban_complete.

Mirrors test_kanban_gate3_shadow.py's structure/scope, adapted to Gate 4's
git-dirty-tree verdict shape (vs Gate 3's claim-reframing shape).

Covers the gap surfaced 2026-07-16: the mode file had zero seed-caller
(fleet-wide Gate4ConfigError on every kanban_complete), the status CLI
undercounted blocks against the compact-JSON ledger, and pass verdicts
were never logged at all (so shadow data could not distinguish "checked
and clean" from "nothing was checkable").

Reference: Decision 2026-07-16 §2 (Gate 4, shadow-first, mirrors Gate 3
mechanics exactly).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_gate4 as gate4


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated home dir with a fresh kanban DB and isolated gate4 state.

    Gate 4 resolves its mode/ledger paths via get_hermes_home() (fresh on
    every call — see the fix to kanban_gate4.py's path resolution), so
    the standard per-test HERMES_HOME env var (also set globally by the
    root conftest's autouse `_hermetic_environment` fixture) is
    sufficient isolation — same as Gate 3. Set explicitly here too so
    this fixture is self-contained / order-independent.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Never let a real HERMES_KANBAN_WORKSPACE from the dev shell leak in.
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    kb.init_db()
    return home


def _card(conn, *, title="x", assignee="alice"):
    tid = kb.create_task(conn, title=title, assignee=assignee)
    kb.claim_task(conn, tid)
    return tid


def _git(*args: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True,
    )


def _clean_repo(tmp_path: Path, name: str, env: dict) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git("init", cwd=repo, env=env)
    _git("config", "user.email", "test@test.com", cwd=repo, env=env)
    _git("config", "user.name", "Test", cwd=repo, env=env)
    (repo / "README.md").write_text("init")
    _git("add", ".", cwd=repo, env=env)
    _git("commit", "-m", "initial", cwd=repo, env=env)
    return repo


def _dirty_repo(tmp_path: Path, name: str, env: dict) -> Path:
    repo = _clean_repo(tmp_path, name, env)
    (repo / "README.md").write_text("modified, uncommitted")
    return repo


@pytest.fixture
def git_env():
    import os
    return {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


# ---------------------------------------------------------------------------
# Mode file — fix (a): seed regression, fix (d): missing-file config error
# ---------------------------------------------------------------------------

class TestModeFile:
    def test_fresh_install_creates_shadow(self, hermes_home):
        """init_db() must create gate4_mode with default 'shadow', mirroring
        Gate 3's seed-on-init behavior. Regression for the seed-caller gap:
        ensure_gate4_mode_file() previously had zero callers anywhere in the
        codebase, which is why the file was never created."""
        path = gate4._gate4_mode_file()
        assert path.exists()
        assert path.read_text().strip() == "shadow"

    def test_ensure_does_not_overwrite_existing_mode(self, hermes_home):
        """The seed call must never clobber an operator's explicit setting."""
        gate4.flip_gate4_mode("enforce")
        gate4.ensure_gate4_mode_file()
        assert gate4._gate4_mode_file().read_text().strip() == "enforce"

    def test_missing_file_at_eval_raises(self, hermes_home):
        """The exact failure that blocked kanban_complete fleet-wide on
        2026-07-16: delete the mode file, call at eval time, must raise."""
        gate4._gate4_mode_file().unlink()
        with pytest.raises(gate4.Gate4ConfigError) as exc_info:
            gate4.gate4_effective_mode(at_eval=True)
        assert "missing" in str(exc_info.value).lower()

    def test_missing_file_display_only_returns_shadow(self, hermes_home):
        """CLI status display (at_eval=False) treats a missing file as
        'shadow' for friendly display — this does NOT bypass fail-closed
        at real eval time (see test_missing_file_at_eval_raises)."""
        gate4._gate4_mode_file().unlink()
        assert gate4.gate4_effective_mode(at_eval=False) == "shadow"

    def test_invalid_mode_content_raises(self, hermes_home):
        gate4._gate4_mode_file().write_text("garbage")
        with pytest.raises(gate4.Gate4ConfigError) as exc_info:
            gate4.gate4_effective_mode(at_eval=True)
        assert "garbage" in str(exc_info.value).lower()

    def test_atomic_flip_round_trip(self, hermes_home):
        gate4.flip_gate4_mode("enforce")
        assert gate4.gate4_effective_mode() == "enforce"
        gate4.flip_gate4_mode("shadow")
        assert gate4.gate4_effective_mode() == "shadow"


# ---------------------------------------------------------------------------
# JSONL ledger — fix (c): pass verdicts now log, distinguishing
# "zero repos checkable" from "repos checked and clean"
# ---------------------------------------------------------------------------

class TestLedger:
    def test_config_error_at_complete_task_raises_and_logs(self, hermes_home):
        """Missing mode file at real completion time: raises (fleet-wide
        block, as designed — this is fail-closed, not a crash) AND still
        writes a ledger row with effective_mode='<unreadable>'."""
        gate4._gate4_mode_file().unlink()
        ledger = gate4._gate4_ledger_file()
        with kb.connect() as conn:
            tid = _card(conn, title="ops check decision")
            with pytest.raises(gate4.Gate4ConfigError):
                kb.complete_task(conn, tid, summary="done")
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        assert len(rows) == 1
        assert rows[0]["effective_mode"] == "<unreadable>"
        assert rows[0]["status"] == "block"

    def test_shadow_pass_zero_repos_checked_writes_ledger(self, hermes_home):
        """No declared_repo, no on-disk workspace_path, no
        HERMES_KANBAN_WORKSPACE => verdict passes with dirty_repos == [].
        Before the fix this row was never written at all — a pass over
        zero repos and a pass over an actually-clean repo were both
        silently absent from the ledger, indistinguishable from each
        other and from "gate never ran." This test locks in that a
        zero-repos pass is now visible, and visibly empty."""
        ledger = gate4._gate4_ledger_file()
        with kb.connect() as conn:
            tid = _card(conn, title="ops check decision")
            ok = kb.complete_task(conn, tid, summary="done")
        assert ok is True
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        assert len(rows) == 1
        assert rows[0]["status"] == "pass"
        assert rows[0]["effective_mode"] == "shadow"
        assert rows[0]["dirty_repos"] == []

    def test_shadow_pass_clean_repo_checked_writes_ledger(self, hermes_home, tmp_path, monkeypatch, git_env):
        """A genuinely clean, checked repo also logs a pass row — but
        with dirty_repos populated (dirty=False entries), visibly
        distinct from the zero-repos-checked case above."""
        repo = _clean_repo(tmp_path, "clean_ws", git_env)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
        ledger = gate4._gate4_ledger_file()
        with kb.connect() as conn:
            tid = _card(conn, title="ops check decision")
            ok = kb.complete_task(conn, tid, summary="done")
        assert ok is True
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        assert len(rows) == 1
        assert rows[0]["status"] == "pass"
        assert rows[0]["dirty_repos"] != []
        assert all(r["dirty"] is False for r in rows[0]["dirty_repos"])

    def test_shadow_block_writes_ledger_and_commits(self, hermes_home, tmp_path, monkeypatch, git_env):
        """Dirty repo detected via HERMES_KANBAN_WORKSPACE: shadow logs
        the block with the advisory prefix, but completion still commits
        (shadow never raises on a dirty tree — only enforce does)."""
        repo = _dirty_repo(tmp_path, "dirty_ws", git_env)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
        ledger = gate4._gate4_ledger_file()
        with kb.connect() as conn:
            tid = _card(conn, title="ops check decision")
            ok = kb.complete_task(conn, tid, summary="done")
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        assert len(rows) == 1
        assert rows[0]["status"] == "block"
        assert rows[0]["effective_mode"] == "shadow"
        assert rows[0]["reason"].startswith("[GATE4 SHADOW — advisory only, not blocking] ")

    def test_enforce_block_raises(self, hermes_home, tmp_path, monkeypatch, git_env):
        """Enforce + dirty tree = Gate4ConfigError raised, completion does
        NOT commit. (Exercised here only to confirm the raise path still
        works after the pass/block restructure — enforce itself stays
        unflipped in the live fleet.)"""
        repo = _dirty_repo(tmp_path, "dirty_ws_enforce", git_env)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
        gate4.flip_gate4_mode("enforce")
        with kb.connect() as conn:
            tid = _card(conn, title="ops check decision")
            with pytest.raises(gate4.Gate4ConfigError):
                kb.complete_task(conn, tid, summary="done")
            task = kb.get_task(conn, tid)
            assert task.status != "done"


# ---------------------------------------------------------------------------
# Status CLI — fix (b): compact-JSON block counter
# ---------------------------------------------------------------------------

class TestStatusLines:
    def test_status_counts_block_rows_from_compact_json(self, hermes_home):
        """Regression for the undercount bug: gate4_status_lines() used to
        string-match '"status": "block"' (with a space) against a ledger
        serialized compact (json.dumps(..., separators=(",", ":"))), so it
        always reported zero blocks regardless of actual content. Write
        rows in the real compact shape and confirm the count is now
        derived by parsing JSON, not string-matching."""
        ledger = gate4._gate4_ledger_file()
        rows = [
            {"status": "block", "task_id": "t1"},
            {"status": "block", "task_id": "t2"},
            {"status": "pass", "task_id": "t3"},
        ]
        ledger.write_text(
            "\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n"
        )
        lines = gate4.gate4_status_lines()
        assert "total ledger rows: 3" in lines
        assert "total would-block rows: 2" in lines

    def test_status_ignores_malformed_lines(self, hermes_home):
        """A malformed/truncated line must not crash status."""
        ledger = gate4._gate4_ledger_file()
        ledger.write_text('{"status":"block"}\nnot json\n')
        lines = gate4.gate4_status_lines()
        assert "total ledger rows: 2" in lines
        assert "total would-block rows: 1" in lines
