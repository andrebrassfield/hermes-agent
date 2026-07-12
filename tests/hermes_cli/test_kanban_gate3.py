"""Tests for Gate 3 — claim_reframing_required.

Reference: 06-Decisions/Decision-2026-07-12-verify-before-claim-rd-killed.md
(operationally: catches phantom cron / R&D wrong-verdict / labeled-but-
unverified-token at structural layer, NOT vocabulary).

Matrix (hard receipt for ship):
  R1 — phantom cron             → GATE3_BLOCK
  R2 — R&D wrong-verdict        → GATE3_BLOCK
  R3 — labeled-but-unverified   → GATE3_BLOCK
  P1 — cron claim + crontab echo           → GATE3_PASS
  P2 — file claim + git show echo          → GATE3_PASS
  P3 — identity claim + xurl whoami echo   → GATE3_PASS
  X1 — fence but no paired command         → GATE3_BLOCK
  X2 — claim in summary only (not meta)    → GATE3_BLOCK
  X3 — check reads a changed_files path    → GATE3_BLOCK
  X4 — check is cat/[ -f ] of self-written  → GATE3_BLOCK

Plus unit-level coverage of the extractor, fence parser, paired-check
finder, and authority allowlist.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_gate3 as gate3


# ---------------------------------------------------------------------------
# Fixtures — same isolated HERMES_HOME as test_kanban_db.py
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB.

    Default mode is ``shadow`` (per Decision 2026-07-12 #1 — fresh
    installs default to shadow, the operator must explicitly flip to
    enforce). Tests that need the BLOCK path to actually raise must
    use the ``enforce_mode`` fixture (which flips the mode file to
    ``enforce``) or the ``shadow_mode`` fixture (explicit, for clarity).
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def enforce_mode(kanban_home):
    """Flip the fleet-wide Gate 3 mode to ``enforce`` for this test.

    Tests that assert a Gate3BlockError raise must use this fixture —
    shadow mode (default) commits through and logs a would-block
    event without raising (Decision 2026-07-12 #1, option (a)).
    """
    gate3.flip_gate3_mode("enforce")
    yield "enforce"
    # Restore shadow for any subsequent test in the same session.
    gate3.flip_gate3_mode("shadow")


@pytest.fixture
def shadow_mode(kanban_home):
    """Explicit shadow mode (default after init, but named for clarity)."""
    gate3.flip_gate3_mode("shadow")
    yield "shadow"


def _make_card(conn, *, title: str = "test", assignee: str = "alice") -> str:
    """Create + claim a card so complete_task can run. Returns task_id."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    kb.claim_task(conn, tid)
    return tid


def _comment(conn, tid: str, body: str) -> None:
    """Add a kanban_comment to the card (last one is what Gate 3 inspects)."""
    kb.add_comment(conn, tid, author="alice", body=body)


# Reusable re-classification bodies for positive controls. Minimal
# realistic worker output: fence + blank + ``` + $ cmd + exit_code.
RECLASS_GIT = textwrap.dedent(
    """\
    ## re-classification

    ```
    $ git show HEAD:verify_claim.py
    exit_code: 0
    ```
    """
)

RECLASS_CRON = textwrap.dedent(
    """\
    ## re-classification

    ```
    $ crontab -l | grep research-url-gate-a1
    exit_code: 0
    ```
    """
)

RECLASS_XURL = textwrap.dedent(
    """\
    ## re-classification

    ```
    $ xurl whoami
    exit_code: 0
    ```
    """
)


# ---------------------------------------------------------------------------
# Matrix tests — the ship gate
# ---------------------------------------------------------------------------

class TestRetroFiresBlock:
    """3 retro-fires from 2026-07-12 — all must GATE3_BLOCK."""

    def test_r1_phantom_cron_blocks(self, kanban_home, enforce_mode):
        with kb.connect() as conn:
            tid = _make_card(conn, title="setup url-gate cron")
            _comment(conn, tid, "ready to ship")  # no re-classification fence
            with pytest.raises(kb.Gate3BlockError) as exc_info:
                kb.complete_task(
                    conn, tid,
                    summary="Cron scheduled: research-url-gate-a1 will run hourly",
                    metadata={
                        "cron_id": "research-url-gate-a1",
                        "jobs_path": "/Users/brassfieldventuresllc/.hermes/profiles/head-of-research/cron/jobs.json",
                    },
                )
            assert exc_info.value.verdict.status == "block"
            assert any(
                c.kind == "cron" for c in exc_info.value.verdict.claims
            ), f"expected cron claim, got {exc_info.value.verdict.claims!r}"
            # Task MUST remain in-flight, not flipped to done
            task = kb.get_task(conn, tid)
            assert task.status != "done", (
                f"task flipped to {task.status} on Gate3_BLOCK — gate must NOT "
                f"mutate state"
            )

    def test_r2_rd_wrong_verdict_blocks(self, kanban_home, enforce_mode):
        with kb.connect() as conn:
            tid = _make_card(conn, title="verify-before-claim R&D")
            # Worker writes its own trace file and reads it back — exactly
            # the self-certifying pattern that killed the primitive.
            _comment(conn, tid, "test_morning_failures passes — SHIPS")
            with pytest.raises(kb.Gate3BlockError) as exc_info:
                kb.complete_task(
                    conn, tid,
                    summary="SHIPS — caught 4/4 morning failures",
                    metadata={
                        "changed_files": [
                            "verify_claim.py",
                            "traces/claim_hashes.txt",
                            "traces/<hash>.json",
                        ],
                        "tests_run": 4,
                    },
                )
            assert exc_info.value.verdict.status == "block"
            # file_path claim should be present (the changed_files list)
            assert any(
                c.kind == "file_path" for c in exc_info.value.verdict.claims
            ), f"expected file_path claim, got {exc_info.value.verdict.claims!r}"

    def test_r3_labeled_but_unverified_token_blocks(self, kanban_home, enforce_mode):
        with kb.connect() as conn:
            tid = _make_card(conn, title="publish to @drethesalesguy")
            # Worker trusts the status screen label without calling whoami.
            _comment(conn, tid, "identity confirmed via status screen")
            with pytest.raises(kb.Gate3BlockError) as exc_info:
                kb.complete_task(
                    conn, tid,
                    summary="Post scheduled to @drethesalesguy",
                    metadata={
                        "identity_handle": "drethesalesguy",
                        "auth_status": "hermes-x",
                    },
                )
            assert exc_info.value.verdict.status == "block"
            assert any(
                c.kind == "flag_value" for c in exc_info.value.verdict.claims
            ), f"expected flag_value claim, got {exc_info.value.verdict.claims!r}"


class TestPositiveControlsPass:
    """3 positive controls — proper re-classification with external authority."""

    def test_p1_cron_with_crontab_echo_passes(self, kanban_home):
        with kb.connect() as conn:
            tid = _make_card(conn, title="setup url-gate cron")
            _comment(conn, tid, RECLASS_CRON)
            ok = kb.complete_task(
                conn, tid,
                summary="Cron scheduled: research-url-gate-a1 will run hourly",
                metadata={"cron_id": "research-url-gate-a1"},
            )
            assert ok, "valid cron claim with crontab echo should pass"
            task = kb.get_task(conn, tid)
            assert task.status == "done"

    def test_p2_file_with_git_show_passes(self, kanban_home):
        with kb.connect() as conn:
            tid = _make_card(conn, title="verify claim primitive")
            _comment(conn, tid, RECLASS_GIT)
            # Note: changed_files intentionally does NOT include
            # verify_claim.py — otherwise the secondary independence
            # check fires (X3 class). A real worker would claim the
            # file state via `git show HEAD:verify_claim.py` reading
            # from the object store, not from the working tree they
            # just wrote.
            ok = kb.complete_task(
                conn, tid,
                summary="Verified verify_claim.py source via git show",
                metadata={"changed_files": ["some_other_file.py"]},
            )
            assert ok, "file claim with git show echo should pass"
            task = kb.get_task(conn, tid)
            assert task.status == "done"

    def test_p3_identity_with_xurl_whoami_passes(self, kanban_home):
        with kb.connect() as conn:
            tid = _make_card(conn, title="publish to @drethesalesguy")
            _comment(conn, tid, RECLASS_XURL)
            ok = kb.complete_task(
                conn, tid,
                summary="Identity confirmed via xurl whoami echo",
                metadata={"identity_handle": "drethesalesguy"},
            )
            assert ok, "identity claim with xurl whoami echo should pass"
            task = kb.get_task(conn, tid)
            assert task.status == "done"


class TestBypassAttemptsBlock:
    """4 bypass attempts — all must GATE3_BLOCK (matrix add: X4)."""

    def test_x1_fence_without_paired_command_blocks(self, kanban_home, enforce_mode):
        """Fence is present but no paired exit_code: 0 in the window."""
        with kb.connect() as conn:
            tid = _make_card(conn, title="setup cron")
            _comment(conn, tid, "## re-classification\n\nVerified — looking good!")
            with pytest.raises(kb.Gate3BlockError) as exc_info:
                kb.complete_task(
                    conn, tid,
                    summary="cron scheduled",
                    metadata={"cron_id": "phantom-cron-id"},
                )
            assert "paired" in exc_info.value.verdict.reason.lower()

    def test_x2_claim_in_summary_only_still_blocks(self, kanban_home, enforce_mode):
        """Worker tries to evade by putting the claim only in summary prose,
        not in metadata. Gate 3 walks BOTH — must still fire."""
        with kb.connect() as conn:
            tid = _make_card(conn, title="setup cron")
            _comment(conn, tid, "## re-classification\n\nVerified via crontab.\n\nexit_code: 0")
            with pytest.raises(kb.Gate3BlockError) as exc_info:
                kb.complete_task(
                    conn, tid,
                    summary="Cron scheduled at /Users/brassfieldventuresllc/.hermes/profiles/research/cron/jobs.json",
                    metadata={"tests_run": 1},  # no claim-shaped keys
                )
            assert any(
                c.kind == "file_path" for c in exc_info.value.verdict.claims
            ), "claim in summary must still be extracted"

    def test_x3_compound_command_with_working_tree_read_blocks(self, kanban_home, enforce_mode):
        """Compound command `git show HEAD:foo.py && cat foo.py` against
        changed_files=[foo.py]. The check CITES an authority command (git
        show) but the OPERATIVE part is `cat foo.py` — a working-tree read
        of a worker-authored file. The parser must identify the working-
        tree read, not be fooled by the authority prefix.

        This is the bypass-by-concatenation case: a worker writes a check
        that LOOKS like a git command but actually does a `cat`. If the
        parser just regex-matches `git show` and returns PASS, X4's
        working-tree block is bypassed.
        """
        with kb.connect() as conn:
            tid = _make_card(conn, title="verify code")
            body = textwrap.dedent(
                """\
                ## re-classification

                ```
                $ git show HEAD:verify_claim.py && cat verify_claim.py
                exit_code: 0
                ```
                """
            )
            _comment(conn, tid, body)
            with pytest.raises(kb.Gate3BlockError) as exc_info:
                kb.complete_task(
                    conn, tid,
                    summary="Verified verify_claim.py",
                    metadata={"changed_files": ["verify_claim.py"]},
                )
            # Reason must name the working-tree read / authority failure,
            # not "changed_files" (that secondary check was removed).
            reason = exc_info.value.verdict.reason.lower()
            assert "authority" in reason or "working-tree" in reason, (
                f"reason should name authority/working-tree read failure, "
                f"got: {exc_info.value.verdict.reason}"
            )

    def test_x4_cat_or_stat_of_self_written_file_blocks(self, kanban_home, enforce_mode):
        """Worker cites `cat` / `stat` / `[ -f ]` / `head` / `grep <file>` of a
        working-tree path with empty changed_files. Primary discriminator:
        no authority allowlist match → BLOCK."""
        with kb.connect() as conn:
            tid = _make_card(conn, title="verify state")
            # Try each of the four disallowed command shapes
            for bad_cmd in (
                "cat verify_claim.py",
                "[ -f jobs.json ] && echo OK",
                "head -1 jobs.json",
                "grep cron_id jobs.json",
            ):
                body = textwrap.dedent(
                    f"""\
                    ## re-classification

                    ```
                    $ {bad_cmd}
                    exit_code: 0
                    ```
                    """
                )
                kb.add_comment(conn, tid, author="alice", body=body)
            # No re-classification paired with cron claim → BLOCK
            # The latest comment IS a re-classification fence with a paired
            # exit_code: 0, but the cited command doesn't match the
            # authority allowlist for cron claims.
            with pytest.raises(kb.Gate3BlockError) as exc_info:
                kb.complete_task(
                    conn, tid,
                    summary="cron scheduled",
                    metadata={"cron_id": "phantom-cron-id"},
                )
            assert "authority" in exc_info.value.verdict.reason.lower()


# ---------------------------------------------------------------------------
# Unit tests — extractor, fence parser, paired-check finder, allowlist
# ---------------------------------------------------------------------------

class TestExtractStructuralClaims:
    def test_zero_claim_summary_returns_empty(self):
        claims = gate3.extract_structural_claims(
            summary="drafted posts", metadata={"tests_run": 3}
        )
        assert claims == []

    def test_summary_path_claim_extracted(self):
        claims = gate3.extract_structural_claims(
            summary="wrote /tmp/output.md",
            metadata={},
        )
        assert any(c.kind == "file_path" for c in claims)
        assert any("/tmp/output.md" in c.target for c in claims)

    def test_metadata_cron_id_extracted(self):
        claims = gate3.extract_structural_claims(
            summary="setup", metadata={"cron_id": "abc-123"}
        )
        assert any(c.kind == "cron" and c.target == "abc-123" for c in claims)

    def test_metadata_flag_extracted(self):
        claims = gate3.extract_structural_claims(
            summary="setup", metadata={"flag": "ready=true"}
        )
        assert any(c.kind == "flag_value" for c in claims)

    def test_metadata_identity_handle_extracted(self):
        claims = gate3.extract_structural_claims(
            summary="setup", metadata={"identity_handle": "drethesalesguy"}
        )
        assert any(c.kind == "flag_value" for c in claims)

    def test_metadata_changed_files_each_becomes_claim(self):
        claims = gate3.extract_structural_claims(
            summary="wrote stuff",
            metadata={"changed_files": ["a.py", "b.md"]},
        )
        targets = {c.target for c in claims if c.kind == "file_path"}
        assert "a.py" in targets and "b.md" in targets

    def test_metadata_changed_files_dict_normalized(self):
        """Worker tool layer can serialize single-item lists as
        `{"item": "value"}` dicts. The extractor must normalize
        these or claim-bearing payloads silently skip the gate
        (false-pass). Regression 2026-07-12.
        """
        claims = gate3.extract_structural_claims(
            summary="wrote stuff",
            metadata={"changed_files": {"item": "a.py"}},
        )
        targets = {c.target for c in claims if c.kind == "file_path"}
        assert "a.py" in targets, (
            f"dict-wrapped changed_files not normalized; "
            f"got targets={targets}"
        )

    def test_t45e198b8_cron_id_does_not_phantom_flag(self):
        """Bug-A regression (2026-07-12, surface evidence t_45e198b8):

        The summary ``cron_id=3dade63a4609 verified`` previously extracted
        TWO claims — one ``cron`` (correct) and one phantom ``flag_value``
        with target ``3dade63a4609`` (the value side of ``cron_id=X`` was
        matching the flag_value regex's ``[A-Za-z_]\\w* = [0-9]+`` shape).

        Under the amended rubric the gate's block on the phantom was
        correct (no authority for flag_value), but the phantom fills the
        soak's presumed-true list with known-cause noise and burns worker
        retries on a claim that doesn't actually exist. Suppress the
        phantom by tracking cron spans and skipping overlapping flag_value
        matches.
        """
        claims = gate3.extract_structural_claims(
            summary="cron_id=3dade63a4609 verified",
            metadata={},
        )
        cron_claims = [c for c in claims if c.kind == "cron"]
        flag_claims = [c for c in claims if c.kind == "flag_value"]
        assert len(cron_claims) == 1, (
            f"expected exactly 1 cron claim, got {len(cron_claims)}: "
            f"{[(c.kind, c.target) for c in claims]}"
        )
        assert len(flag_claims) == 0, (
            f"phantom flag_value should be suppressed (Bug A regression); "
            f"got {[(c.kind, c.target) for c in flag_claims]}"
        )

    def test_independent_flag_still_extracted(self):
        """Bug-A negative case: a flag_value claim that does NOT overlap
        a cron span must still be extracted. The fix is span-scoped, not
        blanket-suppression."""
        claims = gate3.extract_structural_claims(
            summary="enabled=true then cron_id=abc scheduled",
            metadata={},
        )
        cron_claims = [c for c in claims if c.kind == "cron"]
        flag_claims = [c for c in claims if c.kind == "flag_value"]
        assert len(cron_claims) == 1
        assert len(flag_claims) == 1
        assert "true" in flag_claims[0].target


class TestFindReclassificationBlock:
    def test_no_fence_returns_none(self):
        assert gate3.find_reclassification_block("just a regular comment") is None

    def test_self_classification_fence_does_not_match(self):
        """## self-classification must NOT trigger the re-class parser —
        that's the discriminator against the worker faking a re-derivation
        by renaming the heading."""
        body = "## self-classification\n\nVerified — all good.\n"
        assert gate3.find_reclassification_block(body) is None

    def test_re_classification_fence_captures_window(self):
        body = textwrap.dedent(
            """\
            some preamble

            ## re-classification

            line 1
            line 2
            line 3
            line 4
            line 5
            line 6
            line 7
            line 8
            line 9
            line 10
            line 11 (outside window)
            """
        )
        window = gate3.find_reclassification_block(body)
        assert window is not None
        # _FENCE_WINDOW=10 splitlines entries under the fence. The body
        # has 2 leading blanks after the fence heading (one for line-end,
        # one for content separation), leaving 8 content-line slots.
        # With the leading blanks, lines 1-8 fit; line 9+ are outside.
        for i in range(1, 9):
            assert f"line {i}" in window, f"line {i} missing from window"
        assert "line 9" not in window
        assert "line 11" not in window
        assert "(outside window)" not in window


class TestFindPairedCheckCommand:
    def test_exit_code_zero_required(self):
        body = "## re-classification\n\n$ crontab -l\n"
        assert gate3.find_paired_check_command(body) is None

    def test_authority_command_with_exit_code_found(self):
        body = textwrap.dedent(
            """\
            ## re-classification

            $ git log --stat -- verify_claim.py
            exit_code: 0
            """
        )
        cmd = gate3.find_paired_check_command(body)
        assert cmd is not None
        assert "git" in cmd

    def test_cat_command_not_a_discharge(self):
        """Even with exit_code: 0, `cat` is not in the authority allowlist."""
        body = textwrap.dedent(
            """\
            ## re-classification

            $ cat verify_claim.py
            exit_code: 0
            """
        )
        cmd = gate3.find_paired_check_command(body)
        # The command IS extracted by the regex, but the authority check
        # below will fail it. This test verifies the parser surface only.
        assert cmd is not None and "cat" in cmd

    def test_command_inside_fenced_block_extracted(self):
        """Bug-B regression (2026-07-12, surface evidence t_98df4108):

        The previous parser skipped lines starting with ``` ` ``` as
        fence wrappers and never scanned inside fenced code blocks. A
        worker who wraps the check command + exit_code in a markdown
        code fence (the natural hygiene shape) would have the parser
        miss both. The fix scans INSIDE the fence.
        """
        body = textwrap.dedent(
            """\
            ## re-classification

            Verified file state via git object store.

            ```
            $ git -C ~/The-Brain rev-parse --is-inside-work-tree
            true
            exit_code: 0
            ```
            """
        )
        cmd = gate3.find_paired_check_command(body)
        assert cmd is not None, (
            "parser missed a fenced-block command+exit_code pair "
            "(Bug B regression)"
        )
        assert "git" in cmd and "rev-parse" in cmd

    def test_t98df4108_excerpt_parses_correctly(self):
        """Bug-B regression — verbatim shape from shadow ledger task t_98df4108.

        The original excerpt had an unclosed fence (worker error), but the
        test pinpoints the canonicalized form: command + exit_code inside
        a properly-closed fence, with surrounding markdown commentary that
        includes other punctuation. Pre-fix parser would return None
        (skipping the fence); post-fix returns the command.
        """
        body = textwrap.dedent(
            """\
            ## re-classification

            Path note: the spec named `01-Inbox/agents/programmer/README.md`
            but that path does NOT exist in HEAD.

            git binary: /usr/bin/git (2.50.1 Apple Git-155)
            repo check: `git -C ~/The-Brain rev-parse --is-inside-work-tree`

            ```
            $ git -C ~/The-Brain rev-parse --is-inside-work-tree
            true
            exit_code: 0
            ```

            ```
            $ git -C ~/The-Brain show HEAD:01-Inbox/agents/programmer/post-flip-test-cron-reclassification.md
            gate3 adoption v3 — file claim dict-normalized
            exit_code: 0
            ```
            """
        )
        cmd = gate3.find_paired_check_command(body)
        assert cmd is not None
        assert "git" in cmd, f"expected git command, got: {cmd!r}"

    def test_fenced_block_without_exit_code_returns_none(self):
        """Bug-B negative case: a fenced block with no exit_code: 0 must
        still return None — the parser must not invent authority."""
        body = textwrap.dedent(
            """\
            ## re-classification

            ```
            $ git show HEAD:foo.py
            ```
            """
        )
        assert gate3.find_paired_check_command(body) is None


class TestAuthorityAllowlist:
    def test_crontab_matches_cron_claims(self):
        assert gate3._check_command_uses_authority("crontab -l | grep x", "cron")

    def test_hermes_cron_show_matches_cron_claims(self):
        assert gate3._check_command_uses_authority(
            "hermes -p foo cron show abc", "cron"
        )

    def test_hermes_cronjob_show_matches_cron_claims(self):
        # Regression for 2026-07-12 parser gap: Hermes-internal cron
        # jobs use `hermes cronjob show <id>` (the cronjob subcommand,
        # not the cron subcommand). The original allowlist only had
        # `\bhermes\s+cron\b` which doesn't match `cronjob`. Fixed
        # 2026-07-12 — without this, all Hermes-cron-job claims would
        # false-block.
        assert gate3._check_command_uses_authority(
            "hermes cronjob show 3dade63a4609", "cron"
        )

    def test_git_show_matches_file_claims(self):
        assert gate3._check_command_uses_authority(
            "git show HEAD:foo.py", "file_path"
        )

    def test_git_minus_C_show_matches_file_claims(self):
        # Regression 2026-07-12: real workers use `git -C <repo> show`
        # to operate on a specific repo. The original regex missed the
        # `-C <path>` flag form and false-blocked legitimate git-store
        # reads.
        assert gate3._check_command_uses_authority(
            "git -C ~/The-Brain show HEAD:foo.py", "file_path"
        )

    def test_xurl_whoami_matches_flag_claims(self):
        assert gate3._check_command_uses_authority(
            "xurl whoami", "flag_value"
        )

    def test_cat_does_not_match_any_kind(self):
        """Primary discriminator: working-tree reads are NEVER an authority."""
        for kind in ("cron", "file_path", "flag_value"):
            assert not gate3._check_command_uses_authority(
                "cat foo.py", kind
            ), f"cat unexpectedly matched kind={kind}"

    def test_stat_does_not_match(self):
        for kind in ("cron", "file_path", "flag_value"):
            assert not gate3._check_command_uses_authority(
                "stat foo.py", kind
            )

    def test_grep_does_not_match(self):
        for kind in ("cron", "file_path", "flag_value"):
            assert not gate3._check_command_uses_authority(
                "grep cron_id jobs.json", kind
            )


class TestCompoundCommand:
    """Compound-command parsing — the bypass-by-concatenation defense.

    Added 2026-07-12 after the X3 reframing exposed a live parsing hole:
    `git show HEAD:foo.py && cat foo.py` was matching the authority
    regex on the first half and passing the check, even though the
    second half is a working-tree read.
    """

    def test_split_sequential_operators(self):
        segs = gate3._split_compound_command(
            "git show HEAD:foo.py && cat foo.py"
        )
        assert segs == ["git show HEAD:foo.py", "cat foo.py"]

    def test_pipe_not_split(self):
        """Pipes filter authority output; don't break `crontab -l | grep x`."""
        segs = gate3._split_compound_command("crontab -l | grep x")
        assert segs == ["crontab -l | grep x"]

    def test_compound_with_working_tree_fails_closed(self):
        """Authority + cat = BLOCK (cat is the operative second segment)."""
        assert not gate3._check_command_uses_authority(
            "git show HEAD:foo.py && cat foo.py", "file_path"
        )

    def test_crontab_pipe_grep_is_authority(self):
        """The canonical cron-existence check must pass."""
        assert gate3._check_command_uses_authority(
            "crontab -l | grep research-url-gate-a1", "cron"
        )

    def test_semicolon_compound_with_working_tree_fails_closed(self):
        """Semicolon also counts as sequential — `cat foo.py; git show ...` BLOCK."""
        assert not gate3._check_command_uses_authority(
            "cat foo.py; git show HEAD:foo.py", "file_path"
        )

    def test_or_compound_with_working_tree_fails_closed(self):
        """`||` is also sequential."""
        assert not gate3._check_command_uses_authority(
            "cat foo.py || git show HEAD:foo.py", "file_path"
        )


class TestGateEndToEnd:
    def test_skip_on_no_claims(self):
        verdict = gate3.gate3_claim_reframing_required(
            summary="drafted three posts", metadata={"count": 3},
            last_comment_body=None,
        )
        assert verdict.status == "skip"

    def test_pass_on_valid_reclassification(self):
        verdict = gate3.gate3_claim_reframing_required(
            summary="publish to drethesalesguy",
            metadata={"identity_handle": "drethesalesguy"},
            last_comment_body=RECLASS_XURL,
        )
        assert verdict.status == "pass"

    def test_block_when_authority_fails(self):
        verdict = gate3.gate3_claim_reframing_required(
            summary="cron scheduled",
            metadata={"cron_id": "x"},
            last_comment_body=textwrap.dedent(
                """\
                ## re-classification

                ```
                $ cat jobs.json
                exit_code: 0
                ```
                """
            ),
        )
        assert verdict.status == "block"
        assert "authority" in verdict.reason.lower()
