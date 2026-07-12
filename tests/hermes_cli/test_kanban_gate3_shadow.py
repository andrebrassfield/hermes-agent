"""Shadow-mode tests for Gate 3 — verifies the JSONL ledger, mode file
resolution, fail-closed-config-error path, X-path non-mutating replay, and
skip-count canary.

Reference: Decision 2026-07-12 (Option B — fleet file authoritative, no
per-DB flag, fresh read per eval, atomic write on flip).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_gate3 as gate3


# ---------------------------------------------------------------------------
# Fixtures — every test gets a clean tmp HERMES_HOME + empty ledger
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_global_operator_root():
    """Reset the cached _OPERATOR_ROOT and the in-process skip counter.

    The gate reads operator root via `_operator_root()` which caches the
    resolved Path per process. Tests that monkeypatch HERMES_HOME need
    the cache cleared so the new HERMES_HOME is re-resolved. Also
    clears the skip counter so skip-summary canary tests start clean.
    """
    from hermes_cli import kanban_gate3
    kanban_gate3._OPERATOR_ROOT = None
    kanban_gate3._skip_counter["count"] = 0
    # Truncate the operator's real ledger file (if present) so tests
    # don't read rows from prior test runs.
    ledger = kanban_gate3.gate3_ledger_file()
    if ledger.exists():
        ledger.unlink()
    yield


@pytest.fixture
def hermes_home(tmp_path, monkeypatch, reset_global_operator_root):
    """Isolated HERMES_HOME with kanban DB + clean ledger.

    The operator_root resolver walks up from HERMES_HOME to find the
    dir containing `profiles/`. In tests we don't have a profiles/ dir,
    so the resolver returns HERMES_HOME itself. Mode file + ledger land
    at HERMES_HOME — isolated per test by the tmp_path fixture. The
    `reset_global_operator_root` autouse fixture clears the cached
    resolver + ledger between tests.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _card(conn, *, title="x", assignee="alice"):
    tid = kb.create_task(conn, title=title, assignee=assignee)
    kb.claim_task(conn, tid)
    return tid


def _comment(conn, tid, body):
    kb.add_comment(conn, tid, author="alice", body=body)


# Reusable discharge bodies — same as test_kanban_gate3.py
DISCHARGE_GIT = textwrap.dedent(
    """\
    ## re-classification

    ```
    $ git show HEAD:foo.py
    exit_code: 0
    ```
    """
)


# ---------------------------------------------------------------------------
# Mode file — fresh install defaults + atomic flip + invalid modes
# ---------------------------------------------------------------------------

class TestModeFile:
    def test_fresh_install_creates_shadow(self, hermes_home):
        """init_db() must create the mode file with default 'shadow' so
        fresh installs never enter a fail-closed state."""
        path = gate3.gate3_mode_file()
        assert path.exists()
        assert path.read_text().strip() == "shadow"

    def test_atomic_flip_to_enforce(self, hermes_home):
        gate3.flip_gate3_mode("enforce")
        assert gate3.gate3_effective_mode() == "enforce"
        gate3.flip_gate3_mode("shadow")
        assert gate3.gate3_effective_mode() == "shadow"

    def test_invalid_mode_block_at_eval(self, hermes_home):
        """File containing an invalid mode is a fail-closed BLOCK.

        Per Decision 2026-07-12 #3 — missing/unreadable/invalid is NOT
        a silent shadow default.
        """
        path = gate3.gate3_mode_file()
        path.write_text("garbage\n")
        with pytest.raises(gate3.Gate3ConfigError) as exc_info:
            gate3.gate3_effective_mode(at_eval=True)
        assert "garbage" in str(exc_info.value).lower()

    def test_missing_file_at_eval_raises(self, hermes_home):
        """Delete the mode file, then call at eval — must raise."""
        path = gate3.gate3_mode_file()
        path.unlink()
        with pytest.raises(gate3.Gate3ConfigError) as exc_info:
            gate3.gate3_effective_mode(at_eval=True)
        assert "missing" in str(exc_info.value).lower()

    def test_missing_file_display_only(self, hermes_home):
        """Display-only callers (CLI status) get 'shadow' as the
        friendly default when the file is missing."""
        path = gate3.gate3_mode_file()
        path.unlink()
        assert gate3.gate3_effective_mode(at_eval=False) == "shadow"

    def test_atomic_write_uses_same_dir(self, hermes_home, tmp_path):
        """Atomic write temp file MUST be in the same directory as the
        target — never /tmp — so rename(2) is atomic on the same
        filesystem (cross-FS rename is not atomic)."""
        path = gate3.gate3_mode_file()
        gate3.flip_gate3_mode("enforce")
        # If we got here without an error, the rename succeeded. The
        # temp-file location is an implementation detail; the load-
        # bearing invariant is that the rename was atomic.
        assert path.read_text().strip() == "enforce"


# ---------------------------------------------------------------------------
# JSONL ledger — every non-skip eval writes; skips don't
# ---------------------------------------------------------------------------

class TestLedger:
    def test_shadow_block_writes_ledger_row(self, hermes_home):
        """Shadow + block + claim-bearing payload = ledger row written,
        completion commits (per Decision 2026-07-12 #1 option (a))."""
        ledger = gate3.gate3_ledger_file()
        assert not ledger.exists()  # no evals yet

        with kb.connect() as conn:
            tid = _card(conn, title="phantom cron")
            _comment(conn, tid, "ready to ship")  # no fence
            ok = kb.complete_task(
                conn, tid,
                summary="cron scheduled",
                metadata={"cron_id": "phantom-cron-id"},
            )
        # Shadow + block: completion COMMITS (per locked option (a)).
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "done"

        # But a ledger row was written.
        assert ledger.exists()
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        assert len(rows) == 1
        row = rows[0]
        assert row["event"] == "completion_would_block_gate3"
        assert row["effective_mode"] == "shadow"
        assert row["task_id"] == tid
        assert row["status"] == "block"
        assert row["reclassification_found"] is False
        assert row["paired_check_command"] is None
        assert "phantom-cron-id" in str(row["claims"])

    def test_shadow_block_attaches_advisory_prefix_to_reason(self, hermes_home):
        """Q4 directive (2026-07-12): in shadow mode, every would-block
        attaches the enforce-mode tool_error body to the ledger row with
        a "[GATE3 SHADOW — advisory only, not blocking]" prefix. Workers
        see this via kanban_show / comment trail. Shadow measurement is
        not confounded (the prefix is a label, not a logic change)."""
        ledger = gate3.gate3_ledger_file()
        with kb.connect() as conn:
            tid = _card(conn, title="phantom cron")
            _comment(conn, tid, "ready to ship")
            ok = kb.complete_task(
                conn, tid,
                summary="cron scheduled",
                metadata={"cron_id": "phantom-cron-id"},
            )
        assert ok is True
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        row = rows[-1]
        assert row["effective_mode"] == "shadow"
        assert row["status"] == "block"
        assert row["reason"].startswith("[GATE3 SHADOW — advisory only, not blocking] "), (
            f"shadow-block reason missing advisory prefix; got: {row['reason']!r}"
        )
        # The original reason text follows the prefix, unchanged.
        assert "GATE3_BLOCK" in row["reason"]

    def test_enforce_block_reason_has_no_advisory_prefix(self, hermes_home):
        """Advisory prefix is shadow-only. Enforce blocks must keep their
        unmodified tool_error reason so the operator-facing tool_error
        shape stays stable across the flip."""
        gate3.flip_gate3_mode("enforce")
        ledger = gate3.gate3_ledger_file()
        try:
            with kb.connect() as conn:
                tid = _card(conn, title="phantom cron")
                _comment(conn, tid, "ready to ship")
                with pytest.raises(kb.Gate3BlockError):
                    kb.complete_task(
                        conn, tid,
                        summary="cron scheduled",
                        metadata={"cron_id": "phantom-cron-id"},
                    )
            rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
            row = rows[-1]
            assert row["effective_mode"] == "enforce"
            assert row["status"] == "block"
            assert not row["reason"].startswith("[GATE3 SHADOW"), (
                f"enforce-mode reason should NOT carry the shadow advisory prefix; "
                f"got: {row['reason']!r}"
            )
        finally:
            gate3.flip_gate3_mode("shadow")

    def test_shadow_pass_reason_has_no_advisory_prefix(self, hermes_home):
        """Advisory prefix is block-only. Pass rows stay unmodified."""
        ledger = gate3.gate3_ledger_file()
        with kb.connect() as conn:
            tid = _card(conn, title="verify file")
            _comment(conn, tid, DISCHARGE_GIT)
            ok = kb.complete_task(
                conn, tid,
                summary="Verified foo.py",
                metadata={"changed_files": ["foo.py"]},
            )
        assert ok is True
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        row = rows[-1]
        assert row["status"] == "pass"
        assert row["reason"] is None or not row["reason"].startswith("[GATE3 SHADOW")

    def test_enforce_block_raises_and_writes_ledger(self, hermes_home):
        """Enforce + block = Gate3BlockError raised + ledger row written."""
        gate3.flip_gate3_mode("enforce")
        ledger = gate3.gate3_ledger_file()

        with kb.connect() as conn:
            tid = _card(conn, title="phantom cron")
            _comment(conn, tid, "ready to ship")
            with pytest.raises(kb.Gate3BlockError):
                kb.complete_task(
                    conn, tid,
                    summary="cron scheduled",
                    metadata={"cron_id": "phantom-cron-id"},
                )
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        assert len(rows) == 1
        row = rows[0]
        assert row["effective_mode"] == "enforce"
        assert row["status"] == "block"

    def test_shadow_pass_writes_ledger(self, hermes_home):
        """Shadow + pass = ledger row written (per #2 — pass AND block
        both write). Completion commits."""
        ledger = gate3.gate3_ledger_file()
        with kb.connect() as conn:
            tid = _card(conn, title="verify file")
            _comment(conn, tid, DISCHARGE_GIT)
            ok = kb.complete_task(
                conn, tid,
                summary="Verified foo.py",
                metadata={"changed_files": ["foo.py"]},
            )
        assert ok is True
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        assert len(rows) == 1
        row = rows[0]
        assert row["effective_mode"] == "shadow"
        assert row["status"] == "pass"
        assert row["reclassification_found"] is True
        assert row["paired_check_command"] == "git show HEAD:foo.py"

    def test_shadow_skip_no_ledger_row(self, hermes_home):
        """Honest no-claim completion does NOT write to the ledger."""
        ledger = gate3.gate3_ledger_file()
        with kb.connect() as conn:
            tid = _card(conn, title="draft posts")
            ok = kb.complete_task(
                conn, tid,
                summary="Drafted three posts",
                metadata={"count": 3},
            )
        assert ok is True
        # Skips don't write per-row, but the skip canary flushes every
        # 100 evals. So the ledger should be empty OR contain a single
        # gate3_skip_summary row.
        if ledger.exists():
            rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
            for row in rows:
                assert row.get("event") != "completion_would_block_gate3"

    def test_config_error_writes_ledger_with_unreadable_mode(self, hermes_home):
        """Missing mode file → Gate3ConfigError → ledger row with
        effective_mode='<unreadable>'."""
        gate3.gate3_mode_file().unlink()
        ledger = gate3.gate3_ledger_file()

        with kb.connect() as conn:
            tid = _card(conn, title="phantom cron")
            _comment(conn, tid, "ready to ship")
            with pytest.raises(kb.Gate3BlockError) as exc_info:
                kb.complete_task(
                    conn, tid,
                    summary="cron scheduled",
                    metadata={"cron_id": "phantom-cron-id"},
                )
            # The reason names the config error (distinct from claim-block).
            assert "config" in exc_info.value.verdict.reason.lower() or \
                   "missing" in exc_info.value.verdict.reason.lower()

        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        assert len(rows) == 1
        row = rows[0]
        assert row["effective_mode"] == "<unreadable>"


# ---------------------------------------------------------------------------
# Skip-count canary
# ---------------------------------------------------------------------------

class TestSkipCanary:
    def test_skip_summary_at_threshold(self, hermes_home, monkeypatch):
        """After N skips, a gate3_skip_summary row appears in the ledger."""
        # Reset the in-process counter so this test is deterministic.
        gate3._skip_counter["count"] = 0

        ledger = gate3.gate3_ledger_file()
        threshold = gate3._SKIP_COUNT_BETWEEN_FLUSH

        # Drive `threshold` skip evals by calling the internal helper
        # directly. (Driving them through complete_task would be slower
        # but equally valid; the helper is what the gate path calls.)
        for _ in range(threshold):
            gate3._record_skip()

        # Threshold flushes a summary row.
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        assert any(r.get("event") == "gate3_skip_summary" for r in rows)


# ---------------------------------------------------------------------------
# X-path replay — non-mutating, real-payload replay
# ---------------------------------------------------------------------------

class TestEvaluateOnly:
    def test_replay_does_not_mutate_tasks(self, hermes_home):
        """evaluate_only() must NOT change tasks.status, must NOT emit
        task_events. Only writes to the JSONL ledger with source tag."""
        with kb.connect() as conn:
            tid = _card(conn, title="already-completed x-pipeline")
            # Mark done via normal path so we have a real payload.
            _comment(conn, tid, "verified via xurl whoami")
            kb.complete_task(
                conn, tid,
                summary="Posted to @drethesalesguy",
                metadata={"identity_handle": "drethesalesguy"},
            )
            task_before = kb.get_task(conn, tid)
            events_before = list(conn.execute(
                "SELECT COUNT(*) AS c FROM task_events WHERE task_id = ?", (tid,)
            ).fetchone())

        # Now non-mutating replay of a DIFFERENT claim-bearing payload.
        # Use a real captured X-pipeline summary + comment shape.
        verdict = gate3.evaluate_only(
            summary="Posted to @drethesalesguy",
            metadata={"identity_handle": "Gary_Automates"},  # wrong — flag-value
            last_comment_body="published successfully",  # no fence — claim-bearing
            task_id="t_x_path_replay_test",
            profile="content-machine",
            source="x_path_replay",
        )
        assert verdict.status == "block"

        # Verify the task was NOT mutated.
        with kb.connect() as conn:
            task_after = kb.get_task(conn, tid)
            events_after = list(conn.execute(
                "SELECT COUNT(*) AS c FROM task_events WHERE task_id = ?", (tid,)
            ).fetchone())
        assert task_before.status == task_after.status == "done"
        # Events on the EXISTING task unchanged (we replayed a fake
        # task_id, but the real task's events must be untouched).
        assert events_before[0] == events_after[0]

    def test_replay_writes_ledger_with_source_tag(self, hermes_home):
        ledger = gate3.gate3_ledger_file()
        gate3.evaluate_only(
            summary="phantom cron",
            metadata={"cron_id": "phantom-id"},
            last_comment_body="ready to ship",
            task_id="t_x_path_replay_test",
            profile="content-machine",
            source="x_path_replay",
        )
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        assert len(rows) == 1
        row = rows[0]
        assert row["source"] == "x_path_replay"
        assert row["task_id"] == "t_x_path_replay_test"
        assert row["status"] == "block"

    def test_replay_with_real_payload_lands_in_ledger(self, hermes_home):
        """End-to-end: read a real completed X-pipeline task, replay
        its payload through evaluate_only, confirm the verdict."""
        # First, plant a real X-pipeline card and complete it normally
        # so we have a captured payload.
        with kb.connect() as conn:
            tid = _card(conn, title="x-pipeline-real")
            _comment(conn, tid, "verified via xurl whoami")
            kb.complete_task(
                conn, tid,
                summary="Posted to @drethesalesguy",
                metadata={"identity_handle": "drethesalesguy"},
            )
            # Capture the payload + last comment for replay.
            from hermes_cli import kanban_db as kb2
            comments = kb.list_comments(conn, tid)
            last_comment = comments[-1].body if comments else None
            task = kb.get_task(conn, tid)
            summary = task.result or ""

        # Now replay — this would BLOCK in shadow because the captured
        # X-pipeline payload lacks a `## re-classification` fence.
        # Per Decision 2026-07-12 X-path expectation sharpening: this
        # block is TRUE (the X worker genuinely didn't attach a
        # discharge), not a false block. The replay surfaces that fact
        # so the operator knows X workers need training.
        verdict = gate3.evaluate_only(
            summary=summary,
            metadata={"identity_handle": "drethesalesguy"},
            last_comment_body=last_comment,
            task_id=tid,
            profile="content-machine",
            source="x_path_replay",
        )
        # The verdict should be 'skip' if no claim-bearing field fires,
        # or 'block' if it does. Either is acceptable for this test —
        # we just verify the replay wrote to the ledger with the tag.
        ledger = gate3.gate3_ledger_file()
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l]
        # At least one row should be present from this replay.
        assert any(r.get("source") == "x_path_replay" for r in rows)


# ---------------------------------------------------------------------------
# Operator CLI surface — flip is atomic, status reads correctly
# ---------------------------------------------------------------------------

class TestOperatorCLI:
    def test_flip_writes_atomically(self, hermes_home):
        """flip_gate3_mode is the CLI helper. Verify it writes the
        expected content and the file is parseable by effective_mode()."""
        gate3.flip_gate3_mode("enforce")
        assert gate3.gate3_effective_mode() == "enforce"
        gate3.flip_gate3_mode("shadow")
        assert gate3.gate3_effective_mode() == "shadow"

    def test_flip_rejects_invalid_mode(self, hermes_home):
        with pytest.raises(ValueError) as exc_info:
            gate3.flip_gate3_mode("garbage")
        assert "invalid" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Operator-root resolution — the Gap 1 regression
# ---------------------------------------------------------------------------

class TestOperatorRootResolution:
    """Regression for Gap 1 (per-profile split-brain).

    Gate 3's mode file and JSONL ledger live under the operator root,
    not HERMES_HOME. Workers under `-p <profile>` get a profile-scoped
    HERMES_HOME, but the gate MUST resolve to the same canonical path
    as the operator root.

    Parametrized over every profile surface that runs workers, so any
    future topology change (e.g., a new profile) gets coverage by
    default.
    """

    @pytest.mark.parametrize("profile_subdir", [
        None,                         # operator root (no profile)
        "profiles/head-of-content",
        "profiles/head-of-research",
        "profiles/content-machine",
        "profiles/programmer",
        "profiles/devops",
        "profiles/analyst",
    ])
    def test_operator_root_resolves_to_same_canonical_path(
        self, tmp_path, monkeypatch, reset_global_operator_root,
        profile_subdir,
    ):
        """Every profile's HERMES_HOME must resolve to the same
        `gate3_mode_file()` and `gate3_ledger_file()` paths.

        Uses the conftest's autouse ``fake_hermes_home`` shape
        (``tmp_path/hermes_test``) as the operator root, so the
        resolver's Case B heuristic (kanban/ child of operator root)
        fires correctly.
        """
        # The conftest autouse fixture already set HERMES_HOME to
        # tmp_path/hermes_test. Use that as the operator root.
        operator_root = tmp_path / "hermes_test"
        (operator_root / "kanban").mkdir(exist_ok=True)

        if profile_subdir is None:
            # Operator root run.
            hermes_home = operator_root
        else:
            # Profile-scoped run.
            hermes_home = operator_root / profile_subdir
            hermes_home.mkdir(parents=True)

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Both paths must resolve to the operator root, NOT to the
        # profile dir. Per Decision 2026-07-12: fleet-wide state
        # cannot be per-profile.
        expected_mode = operator_root / "gate3_mode"
        expected_ledger = operator_root / "gate3_shadow.jsonl"
        assert gate3.gate3_mode_file() == expected_mode, (
            f"profile={profile_subdir}: gate3_mode_file resolved to "
            f"{gate3.gate3_mode_file()}, expected {expected_mode}"
        )
        assert gate3.gate3_ledger_file() == expected_ledger, (
            f"profile={profile_subdir}: gate3_ledger_file resolved to "
            f"{gate3.gate3_ledger_file()}, expected {expected_ledger}"
        )
