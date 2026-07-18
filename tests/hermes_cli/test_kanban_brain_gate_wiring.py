"""Wiring tests for the brain-gate (Gate 1) integration in complete_task.

Planted regression (Dre approval 2026-07-18, HoE standup finding
2026-07-18T14:03Z): the Gate-1 WIP nested BOTH the brain-gate call AND
the pre-existing Gate-4 dirty-tree enforcement raise inside a branch
gated on Gate 3's effective mode (`_mode`), so with gate3_mode=shadow:

  1. the brain-gate call was dead code (its shadow ledger never wrote), and
  2. flipping gate4_mode to enforce would silently NOT raise on a dirty
     tree — Gate 4 enforcement defanged behind Gate 3's mode file.

These tests fail on the pre-fix code and pass on the fixed code
(CLAUDE.md Section A rule 1). They mirror the fixture/structure of
tests/hermes_cli/test_kanban_gate4_shadow.py.

Also covered: kanban_brain_gate_effective_mode() fail-closed behavior —
missing/unreadable/invalid mode file at eval time must raise
BrainGateConfigError, not silently default to shadow (the exact
deletion-regression hole kanban_gate4.py fails closed against).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_brain_gate as brain_gate
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_gate4 as gate4


# ---------------------------------------------------------------------------
# Fixtures (mirror test_kanban_gate4_shadow.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated home dir with a fresh kanban DB and isolated gate state."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    kb.init_db()
    # Seed the gate mode files deterministically. init_db()'s seeding is
    # best-effort (swallows exceptions) and can be skipped under
    # full-suite import order, so don't rely on it here — the wiring
    # under test needs gate3=shadow explicitly.
    (home / "gate3_mode").write_text("shadow")
    gate4.ensure_gate4_mode_file()
    brain_gate.ensure_brain_gate_mode_file()
    return home


@pytest.fixture
def git_env():
    import os
    return {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _card(conn, *, title="x", assignee="alice"):
    tid = kb.create_task(conn, title=title, assignee=assignee)
    kb.claim_task(conn, tid)
    return tid


def _git(*args: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True,
    )


def _dirty_repo(tmp_path: Path, name: str, env: dict) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git("init", cwd=repo, env=env)
    _git("config", "user.email", "test@test.com", cwd=repo, env=env)
    _git("config", "user.name", "Test", cwd=repo, env=env)
    (repo / "README.md").write_text("init")
    _git("add", ".", cwd=repo, env=env)
    _git("commit", "-m", "initial", cwd=repo, env=env)
    (repo / "README.md").write_text("modified, uncommitted")
    return repo


# ---------------------------------------------------------------------------
# THE planted regression: gate4=enforce + gate3=shadow + dirty tree → raise
# ---------------------------------------------------------------------------

class TestGate4EnforceNotDefangedByGate3Mode:
    def test_gate4_enforce_gate3_shadow_dirty_tree_raises(
        self, hermes_home, tmp_path, monkeypatch, git_env
    ):
        """gate4_mode=enforce + gate3_mode=shadow + dirty tree MUST raise.

        Pre-fix: the raise was nested under `if _mode == "enforce"` where
        _mode is Gate 3's effective mode — with gate3=shadow the raise
        was unreachable and completion committed. Post-fix: the raise is
        gated directly (and only) on _g4_mode.
        """
        # init_db seeded gate3=shadow; flip gate4 to enforce.
        assert (hermes_home / "gate3_mode").read_text().strip() == "shadow"
        gate4.flip_gate4_mode("enforce")
        repo = _dirty_repo(tmp_path, "dirty_ws", git_env)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
        with kb.connect() as conn:
            tid = _card(conn, title="ops check decision")
            with pytest.raises(gate4.Gate4ConfigError, match="dirty tree"):
                kb.complete_task(conn, tid, summary="done")
            # Completion must NOT have committed.
            assert kb.get_task(conn, tid).status != "done"

    def test_brain_gate_ledger_writes_on_shadow_dirty_block(
        self, hermes_home, tmp_path, monkeypatch, git_env
    ):
        """The brain-gate call must actually execute on the Gate-4
        block path (self-gating in shadow → a ledger row), regardless of
        Gate 3's mode. Pre-fix it was dead code and this ledger stayed
        empty forever."""
        repo = _dirty_repo(tmp_path, "dirty_ws2", git_env)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
        ledger = hermes_home / "kanban_brain_gate_ledger.jsonl"
        with kb.connect() as conn:
            tid = _card(conn, title="ops check decision")
            ok = kb.complete_task(conn, tid, summary="done")  # gate4 shadow: commits
        assert ok is True
        assert ledger.exists(), "brain-gate shadow ledger row missing — call is dead code"
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        assert len(rows) == 1

    def test_metadata_none_does_not_crash_brain_gate_path(
        self, hermes_home, tmp_path, monkeypatch, git_env
    ):
        """complete_task with metadata=None (the common CLI call shape)
        must not AttributeError inside the brain-gate wiring
        (pre-fix WIP used `metadata.get(...)` unguarded)."""
        repo = _dirty_repo(tmp_path, "dirty_ws3", git_env)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
        with kb.connect() as conn:
            tid = _card(conn, title="ops check decision")
            assert kb.complete_task(conn, tid, summary="done", metadata=None) is True


# ---------------------------------------------------------------------------
# Brain-gate fail-closed mode handling (same standard as kanban_gate4.py)
# ---------------------------------------------------------------------------

class TestBrainGateFailClosed:
    def test_ensure_seeds_shadow(self, hermes_home):
        """ensure_brain_gate_mode_file() (called from init_db and the
        fixture) seeds 'shadow' — and resolves via get_hermes_home(),
        so the seeded file lands in the isolated HERMES_HOME."""
        path = brain_gate._get_brain_gate_mode_path()
        assert str(path).startswith(str(hermes_home))
        assert path.exists()
        assert path.read_text().strip() == "shadow"

    def test_ensure_does_not_overwrite_existing_mode(self, hermes_home):
        brain_gate._atomic_write_mode_file(
            brain_gate._get_brain_gate_mode_path(), "enforce"
        )
        brain_gate.ensure_brain_gate_mode_file()
        assert brain_gate._get_brain_gate_mode_path().read_text().strip() == "enforce"

    def test_missing_file_at_eval_raises(self, hermes_home):
        brain_gate._get_brain_gate_mode_path().unlink()
        with pytest.raises(brain_gate.BrainGateConfigError, match="missing"):
            brain_gate.kanban_brain_gate_effective_mode(at_eval=True)

    def test_invalid_mode_content_raises(self, hermes_home):
        brain_gate._get_brain_gate_mode_path().write_text("garbage")
        with pytest.raises(brain_gate.BrainGateConfigError, match="garbage"):
            brain_gate.kanban_brain_gate_effective_mode(at_eval=True)

    def test_missing_file_display_only_returns_shadow(self, hermes_home):
        brain_gate._get_brain_gate_mode_path().unlink()
        assert brain_gate.kanban_brain_gate_effective_mode(at_eval=False) == "shadow"

    def test_missing_file_blocks_completion_on_gate4_block_path(
        self, hermes_home, tmp_path, monkeypatch, git_env
    ):
        """Deleting the brain-gate mode file must fail closed at the real
        completion callsite (dirty tree → gate4 block path → brain gate
        eval), not silently no-op."""
        brain_gate._get_brain_gate_mode_path().unlink()
        repo = _dirty_repo(tmp_path, "dirty_ws4", git_env)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
        with kb.connect() as conn:
            tid = _card(conn, title="ops check decision")
            with pytest.raises(brain_gate.BrainGateConfigError):
                kb.complete_task(conn, tid, summary="done")
            assert kb.get_task(conn, tid).status != "done"
