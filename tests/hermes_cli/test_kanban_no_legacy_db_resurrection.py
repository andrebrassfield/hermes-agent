"""Regression tests — ``connect()`` must not resurrect the legacy
``<root>/kanban.db`` on an install that has adopted the boards layout.

The bug (observed live 2026-07-17): ~23 seconds after the stale legacy
``~/.hermes/kanban.db`` was archived on a boards-layout install (live
board ``obs-board``), a reader that resolved board slug ``default``
(``board_exists("default")`` is always True by design) called ``connect()``
and lazily recreated an empty zero-table file at the legacy path —
undoing the consolidation and re-opening the split-brain-board hazard.

Contract pinned here:

* On a post-boards install (>=1 real board under ``boards/``) with no
  legacy DB on disk, resolving ``default`` implicitly must FAIL LOUD
  (``FileNotFoundError``) and must not create the file.
* The active non-default board keeps working, and never touches the
  legacy path.
* A genuinely fresh install (no boards) keeps the lazy-create behaviour
  — first ``connect()`` initialises ``<root>/kanban.db`` as always.
* An operator who explicitly switched the current board to ``default``
  (current file content == "default") retains creation — the guard only
  refuses IMPLICIT resurrection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def boards_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME on the boards layout: one real board
    (``obs``) set current, and NO legacy ``<root>/kanban.db``."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board("obs")
    kb.set_current_board("obs")
    legacy = home / "kanban.db"
    assert not legacy.exists(), "fixture must start without a legacy DB"
    return home


def test_default_board_connect_fails_loud_and_creates_nothing(
    boards_home: Path,
) -> None:
    legacy = boards_home / "kanban.db"
    with pytest.raises(FileNotFoundError):
        kb.connect(board="default")
    assert not legacy.exists(), "connect() must not resurrect the legacy DB"


def test_current_board_connect_works_and_leaves_legacy_alone(
    boards_home: Path,
) -> None:
    legacy = boards_home / "kanban.db"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="works on the real board")
        assert kb.get_task(conn, tid) is not None
    assert not legacy.exists()


def test_fresh_install_keeps_lazy_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect() as conn:
        conn.execute("SELECT 1").fetchone()
    assert (home / "kanban.db").exists(), "fresh installs must still lazy-init"


def test_explicit_current_default_retains_creation(
    boards_home: Path,
) -> None:
    """Operator intent wins: current file explicitly set to 'default'
    re-enables the legacy path."""
    kb.current_board_path().write_text("default", encoding="utf-8")
    try:
        with kb.connect(board="default") as conn:
            conn.execute("SELECT 1").fetchone()
        assert (boards_home / "kanban.db").exists()
    finally:
        kb.set_current_board("obs")
