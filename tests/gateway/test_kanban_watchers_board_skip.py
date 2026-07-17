"""Regression tests — the gateway dispatcher must skip boards whose DB
file is absent instead of connecting to them.

Companion to ``tests/hermes_cli/test_kanban_no_legacy_db_resurrection.py``:
the dispatcher's per-tick ``list_boards()`` enumeration always includes the
``default`` slug (``board_exists('default')`` is True by design), and its
``connect(board=slug)`` was the live caller that lazily recreated an
archived legacy ``<root>/kanban.db`` (2026-07-17, ~23s after archival —
one dispatcher tick). With ``connect()``'s resurrection guard in place the
same shape surfaced as a "tick failed on board default" ERROR every 60s.
``_board_dispatchable`` fixes both: a DB-less board is skipped, quietly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.kanban_watchers import _board_dispatchable
from hermes_cli import kanban_db as kb


@pytest.fixture
def boards_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Boards-layout HERMES_HOME: real board ``obs`` current, no legacy DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board("obs")
    kb.set_current_board("obs")
    return home


def test_dbless_default_board_is_not_dispatchable(boards_home: Path) -> None:
    legacy = boards_home / "kanban.db"
    assert not legacy.exists()
    assert _board_dispatchable("default") is False
    assert not legacy.exists(), "the probe itself must not create the file"


def test_real_board_with_db_is_dispatchable(boards_home: Path) -> None:
    kb.init_db(board="obs")
    assert _board_dispatchable("obs") is True


def test_default_board_with_existing_legacy_db_is_dispatchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-boards installs (legacy DB present) keep dispatching normally."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    assert (home / "kanban.db").exists()
    assert _board_dispatchable("default") is True
