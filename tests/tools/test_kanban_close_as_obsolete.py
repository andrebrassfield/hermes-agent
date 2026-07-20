"""Regression tests for the ``close_as_obsolete`` admin verb.

The verb is an atomic ``blocked → done`` transition used by orchestrators
to retire cards whose work is no longer relevant (superseded, removed
from scope, otherwise moot). The single-shot write closes a real
dispatcher-wedge: without it, a worker-initiated ``kanban_block`` would
visit ``ready`` long enough for a tick to claim it before the next step
in an orchestrator's flow could complete it.

These tests pin down the seven behaviors that matter for the verb:

1. Happy path on a blocked card — terminal state, payload, side effects.
2. Refusal on active ``claim_lock`` — never yank a card from under a live worker.
3. Refusal on open children — never silently drop a dependency edge.
4. Refusal on non-blocked card — no-op return False, no side effects.
5. Refusal on missing task — no-op return False, no events emitted.
6. ``expected_run_id`` CAS guard — a stale caller cannot close a card whose
   current_run_id has moved on.
7. Tool-layer translation — ``CardHasActiveClaimError`` /
   ``CardHasOpenChildrenError`` raised by the kernel surface as
   ``tool_error`` with the offending ids named; a card already in ``done``
   is treated idempotently.

Tests run against an isolated ``HERMES_HOME`` with a freshly initialized
kanban DB so they don't pollute the live task store. The fixture mirrors
``tests/hermes_cli/test_kanban_blocked_sticky.py``'s ``kanban_home``.
"""

from __future__ import annotations

import json
from typing import Optional, Tuple
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated ``HERMES_HOME`` with an empty kanban DB.

    Mirrors the convention from the hermes_cli kanban tests: redirect
    ``HERMES_HOME`` to a tmp scratch dir, fake ``Path.home()`` so the
    kanban_db module picks up the override, and ``init_db()`` once.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_blocked_card(
    kanban_home: Path,
    *,
    with_children: bool = False,
) -> Tuple[str, Optional[str]]:
    """Seed a card parked in ``blocked`` via the kernel's
    ``initial_status="blocked"`` shortcut (not via claim+block, which
    would entangle us with the claim_lock branch in tests that don't
    want to exercise it).

    Returns ``(parent_id, child_id_or_None)``. When ``with_children=True``
    the child is created in ``blocked`` status under the parent via the
    ``parents=`` argument of ``create_task`` — that means the child is
    not in a terminal status, so the verb must refuse with
    ``CardHasOpenChildrenError``.
    """
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn, title="to be obsoleted", assignee="devops",
            initial_status="blocked",
        )
        # Sanity: parent is in blocked with no claim_lock.
        parent = kb.get_task(conn, parent_id)
        assert parent is not None
        assert parent.status == "blocked"
        assert parent.claim_lock is None

        child_id: Optional[str] = None
        if with_children:
            child_id = kb.create_task(
                conn, title="open child", assignee="devops",
                parents=[parent_id],
                initial_status="blocked",
            )
            child = kb.get_task(conn, child_id)
            assert child is not None and child.status == "blocked"
        return parent_id, child_id


# ---------------------------------------------------------------------------
# 1. Happy path on a blocked card
# ---------------------------------------------------------------------------


def test_close_as_obsolete_happy_path_atomic_blocked_to_done(
    kanban_home: Path,
) -> None:
    """A blocked card transitions to ``done`` in a single call, with
    the obsolete payload stamped on the completed event."""
    parent_id, _ = _make_blocked_card(kanban_home)

    with kb.connect() as conn:
        assert kb.close_as_obsolete(
            conn, parent_id,
            reason="superseded by t_abcdef",
            closed_by="orchestrator:test",
        ) is True

        task = kb.get_task(conn, parent_id)
        assert task is not None
        # Terminal state
        assert task.status == "done"
        assert task.completed_at is not None
        # Side-effect column updates
        assert task.result == "obsolete: superseded by t_abcdef"
        assert task.claim_lock is None
        assert task.worker_pid is None
        assert task.block_kind is None
        assert task.block_recurrences == 0

        # Completed event emitted with the obsolete discriminator.
        events = [
            e for e in kb.list_events(conn, parent_id)
            if e.kind == "completed"
        ]
        assert len(events) == 1, "exactly one completed event"
        payload = events[0].payload or {}
        assert payload.get("obsolete") is True
        assert payload.get("verb") == "close_as_obsolete"
        assert payload.get("reason") == "superseded by t_abcdef"
        assert payload.get("closed_by") == "orchestrator:test"
        assert payload.get("summary_preview") == "superseded by t_abcdef"


# ---------------------------------------------------------------------------
# 2. Refusal on active claim_lock (live worker)
# ---------------------------------------------------------------------------


def test_close_as_obsolete_refuses_when_claim_lock_held(
    kanban_home: Path,
) -> None:
    """Closing must not yank a card out from under a live worker.

    The failure mode this guards: a card was force-blocked by some path
    other than ``block_task`` (e.g. a circuit-breaker, manual SQL, or a
    future feature) while a worker still holds ``claim_lock``. Closing it
    as obsolete would yank it from under that worker. The verb's
    ``claim_lock`` pre-flight catches this even though the card is
    blocked — the wrong-status check is insufficient on its own because
    a ``blocked`` card SHOULD have ``claim_lock IS NULL`` but might not.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="force-blocked with stale lock", assignee="devops",
            initial_status="blocked",
        )
        # Simulate the race: a blocked card with a non-null claim_lock
        # attached (claim_task + force write would do it on a running
        # card; for a blocked card this is the manual-edit / regression
        # shape the docstring calls out).
        conn.execute(
            "UPDATE tasks SET claim_lock = ?, claim_expires = ?, worker_pid = ? "
            "WHERE id = ?",
            ("Mac.home.local:99999", 9_999_999_999, 99999, tid),
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert task.claim_lock is not None

        with pytest.raises(kb.CardHasActiveClaimError) as exc_info:
            kb.close_as_obsolete(
                conn, tid,
                reason="should not work",
                closed_by="orchestrator:test",
            )
        # Error names the offending claim_lock so the orchestrator can
        # wait / kill / force-clear.
        assert tid in str(exc_info.value)
        assert task.claim_lock in str(exc_info.value)

        # State unchanged.
        post = kb.get_task(conn, tid)
        assert post is not None
        assert post.status == "blocked"


# ---------------------------------------------------------------------------
# 3. Refusal on open children
# ---------------------------------------------------------------------------


def test_close_as_obsolete_refuses_when_open_children(
    kanban_home: Path,
) -> None:
    """Closing a parent with unresolved children silently drops the
    dependency edge — refuse loudly with the child ids named."""
    parent_id, child_id = _make_blocked_card(kanban_home, with_children=True)
    assert child_id is not None

    with kb.connect() as conn:
        with pytest.raises(kb.CardHasOpenChildrenError) as exc_info:
            kb.close_as_obsolete(
                conn, parent_id,
                reason="premature close",
                closed_by="orchestrator:test",
            )
        assert child_id in exc_info.value.open_children
        assert parent_id in str(exc_info.value)
        # State unchanged.
        assert kb.get_task(conn, parent_id).status == "blocked"


# ---------------------------------------------------------------------------
# 4. Refusal on non-blocked card (no-op, no exception)
# ---------------------------------------------------------------------------


def test_close_as_obsolete_noop_on_ready_card(
    kanban_home: Path,
) -> None:
    """A card in ``ready`` is not closeable as obsolete (use
    ``kanban_complete``). The verb returns False, raises nothing, and
    emits no completed event."""
    with kb.connect() as conn:
        # Default status of a parentless task is ``ready``.
        tid = kb.create_task(
            conn, title="ready card", assignee="devops",
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"

        assert kb.close_as_obsolete(
            conn, tid,
            reason="oops, wrong verb",
            closed_by="orchestrator:test",
        ) is False

        # No completed event, no status change.
        events = [
            e for e in kb.list_events(conn, tid) if e.kind == "completed"
        ]
        assert events == []
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# 5. Refusal on missing task (no-op, no events)
# ---------------------------------------------------------------------------


def test_close_as_obsolete_returns_false_for_missing_task(
    kanban_home: Path,
) -> None:
    """A task_id that doesn't exist is silently no-op (no exception,
    no event row, no side effect) — matches the ``complete_task``
    contract for the same condition."""
    with kb.connect() as conn:
        assert kb.close_as_obsolete(
            conn, "t_does_not_exist",
            reason="phantom target",
            closed_by="orchestrator:test",
        ) is False

        rows = conn.execute(
            "SELECT id FROM task_events WHERE id = ?", ("t_does_not_exist",),
        ).fetchall()
        assert rows == []


# ---------------------------------------------------------------------------
# 6. expected_run_id CAS — stale caller cannot close
# ---------------------------------------------------------------------------


def test_close_as_obsolete_expected_run_id_must_match(
    kanban_home: Path,
) -> None:
    """The CAS guard on ``current_run_id`` prevents a stale caller from
    closing a card whose run has already moved on (e.g. a retry spawned
    a new run between the orchestrator's read and its close call)."""
    parent_id, _ = _make_blocked_card(kanban_home)

    with kb.connect() as conn:
        # Pass an expected_run_id that cannot match. The card has no
        # current_run_id while blocked (initial_status="blocked" never
        # sets it), so any non-None int fails the CAS.
        assert kb.close_as_obsolete(
            conn, parent_id,
            reason="stale caller",
            closed_by="orchestrator:test",
            expected_run_id=9999,  # never matches
        ) is False

        # State unchanged.
        assert kb.get_task(conn, parent_id).status == "blocked"
        events = [
            e for e in kb.list_events(conn, parent_id) if e.kind == "completed"
        ]
        assert events == []


# ---------------------------------------------------------------------------
# 7. Tool-layer translation of kernel refusals
# ---------------------------------------------------------------------------


def test_close_as_obsolete_tool_handler_translates_claim_and_children_errors(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool handler must surface ``CardHasActiveClaimError`` and
    ``CardHasOpenChildrenError`` as ``tool_error`` strings that name the
    offending ids, not as raw exceptions (which would crash the agent
    loop). Idempotency: a card already in ``done`` returns success with
    the "already closed" note rather than an error."""
    # Make the tool layer think it's running in an orchestrator context
    # so it may target any task_id (no _enforce_worker_task_ownership).
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER", raising=False)

    # --- 7a. CardHasActiveClaimError → tool_error naming the claim_lock ---
    # We construct a blocked card with a stale claim_lock (the force-block
    # race the verb's pre-flight catches — not a normal "running" card,
    # because that path returns False from the wrong-status branch
    # BEFORE the claim_lock check has a chance to fire).
    with kb.connect() as conn:
        live_tid = kb.create_task(
            conn, title="force-blocked live worker", assignee="devops",
            initial_status="blocked",
        )
        conn.execute(
            "UPDATE tasks SET claim_lock = ?, claim_expires = ?, worker_pid = ? "
            "WHERE id = ?",
            ("Mac.home.local:88888", 9_999_999_999, 88888, live_tid),
        )
        live_task = kb.get_task(conn, live_tid)
        assert live_task is not None
        live_lock = live_task.claim_lock
        assert live_lock is not None

    out = kanban_tools._handle_close_as_obsolete(
        {"task_id": live_tid, "reason": "test", "closed_by": "test-orch"},
    )
    parsed = json.loads(out)
    assert "error" in parsed, out
    assert live_tid in parsed["error"]
    assert live_lock in parsed["error"], (
        "claim_lock must be named in the error message"
    )

    # --- 7b. CardHasOpenChildrenError → tool_error naming the child ids ---
    parent_id, child_id = _make_blocked_card(kanban_home, with_children=True)
    assert child_id is not None
    out = kanban_tools._handle_close_as_obsolete(
        {"task_id": parent_id, "reason": "test", "closed_by": "test-orch"},
    )
    parsed = json.loads(out)
    assert "error" in parsed, out
    assert parent_id in parsed["error"]
    assert child_id in parsed["error"], "open child id must be named"

    # --- 7c. Missing task_id arg → tool_error (targeted, not defaulted) ---
    out = kanban_tools._handle_close_as_obsolete(
        {"reason": "no target", "closed_by": "test-orch"},
    )
    parsed = json.loads(out)
    assert "error" in parsed, out
    assert "task_id is required" in parsed["error"]

    # --- 7d. After a successful close, a SECOND call returns idempotent
    # ok with the "already closed" note instead of an error.
    target_id, _ = _make_blocked_card(kanban_home)
    with kb.connect() as conn:
        kb.close_as_obsolete(
            conn, target_id,
            reason="real close",
            closed_by="test-orch",
        )
    out = kanban_tools._handle_close_as_obsolete(
        {"task_id": target_id, "reason": "second call", "closed_by": "test-orch"},
    )
    parsed = json.loads(out)
    # The idempotent path returns _ok(...) which is JSON-encoded too,
    # and crucially does NOT contain an "error" key.
    assert "error" not in parsed, out
    assert parsed.get("status") == "done"
    assert parsed.get("obsolete") is True
    assert "already closed" in parsed.get("note", "")
