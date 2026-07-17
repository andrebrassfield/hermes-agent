"""Regression tests — tasks created with ``initial_status="blocked"`` are a
human-ops hold (HITL gate) and must never be auto-promoted by the
dispatcher's ``recompute_ready``.

The bug (observed live 2026-07-17, obs-board task t_2ee685d6): a card
created via ``hermes kanban create --initial-status blocked`` was promoted
to ``ready`` by the dispatcher ~30s later and a worker was spawned on work
that was explicitly parked for a human. Mechanism: ``create_task`` wrote
only a ``created`` event, so ``_has_sticky_block`` (which looks for the
latest ``blocked``/``unblocked`` event, #28712) saw nothing sticky and
``recompute_ready`` treated the parentless blocked task as promotable.

``create_task``'s own comment states the intent: "unless the caller parks
it directly in blocked for human-ops review". These tests pin that intent:

* A created-blocked task survives arbitrary ``recompute_ready`` ticks.
* Parent completion does not override the hold.
* ``unblock_task`` remains the one legitimate exit (task then promotes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_created_blocked_is_not_auto_promoted(kanban_home: Path) -> None:
    """A parentless task created blocked must stay blocked across
    dispatcher ticks — it is a HITL gate, not a dependency wait."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="await human input", initial_status="blocked"
        )
        assert kb.get_task(conn, tid).status == "blocked"
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "created-blocked task must not auto-promote"
            assert kb.get_task(conn, tid).status == "blocked"


def test_created_blocked_with_done_parents_is_still_held(kanban_home: Path) -> None:
    """Parent completion is ``recompute_ready``'s designed promotion path;
    it must not override an explicit created-blocked hold."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(
            conn, title="held child", parents=[parent],
            initial_status="blocked",
        )
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="parent ok")
        for _ in range(3):
            kb.recompute_ready(conn)
            assert kb.get_task(conn, child).status == "blocked"


def test_created_blocked_unblock_is_the_legitimate_exit(kanban_home: Path) -> None:
    """An explicit ``unblock_task`` clears the hold and the task then
    promotes normally — the gate must be openable, just not self-opening."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="await human input", initial_status="blocked"
        )
        assert kb.unblock_task(conn, tid)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "ready"
