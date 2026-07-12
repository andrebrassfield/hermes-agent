"""Gate 3 — claim_reframing_required.

Structural (not vocabulary-based) gate that fires on every ``kanban_complete``
call and blocks completion when a worker asserts external state (a file path,
cron/job reference, or flag/config value) without re-classifying the claim
against an authority external to itself.

Trigger design (PROTOCOL §3 — kept deliberately structural, see Decision
2026-07-12-verify-before-claim-rd-killed for why vocabulary gates are
self-certifying):

  1. ``extract_structural_claims`` walks the completion payload (summary +
     structured metadata fields only — never comment prose) and returns
     a list of typed Claim records for anything that asserts external state.
  2. If no claims → GATE3_SKIP (zero-cost no-op; honest completions pass).
  3. For each claim, the worker's most recent ``kanban_comment`` must carry
     a ``## re-classification`` fence whose body, within 5 lines, cites a
     check command paired with ``exit_code: 0`` (or a resolver-verified
     tool-call echo).
  4. The cited check command must query an authority external to the worker
     (primary discriminator — see ``AUTHORITY_ALLOWLIST``).

Failure to satisfy (3) or (4) for ANY claim → ``Gate3BlockError`` raised
before the completion write txn, mirroring ``HallucinatedCardsError``.

NOTE — file-claim weakness (2026-07-12): for file/code claims the worker
IS the author of the cited file, so the check is PROCEDURAL proof
(command ran with exit_code: 0) rather than authority-independence.
cron and identity claims get true authority-independence (external
daemon / API). True file-claim correctness is Gate 3b (AST/behavioral),
explicitly descoped from this gate.

LOC: ~110 (gate + extractor + comment-shape parser).
"""

from __future__ import annotations

import re
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Authority allowlist — a paired check discharges a claim only if it queries
# one of these sources. A read or stat of a working-tree file (cat, [ -f ],
# head, grep <file>) does NOT discharge, even if the path is absent from
# changed_files. The worker controls changed_files and can author the file
# it is reading — exactly the failure mode that killed the
# verify-before-claim primitive on 2026-07-12.
AUTHORITY_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "cron": (
        r"\bcrontab\s+-l\b",
        # Hermes cron CLI subcommands (show/list/status). The cronjob
        # subcommand is the operator-facing name; the cron CLI is the
        # dispatcher-facing one. Both are external authority sources.
        r"\bhermes\s+(-p\s+\S+\s+)?cron\s+(show|list|status)\b",
        r"\bhermes\s+(-p\s+\S+\s+)?cronjob\s+(show|list|status)\b",
        r"\blaunchctl\s+list\b",
    ),
    "file_path": (
        # git object store reads only — never working-tree reads.
        # Allow `git -C <repo> <subcommand>` flag combos too; real
        # workers use `git -C <path>` to operate on a specific repo.
        # Regression 2026-07-12: original regex missed `git -C <path>`
        # forms, false-blocking legitimate git-store reads.
        r"\bgit\s+(-C\s+\S+\s+)?show\s+",
        r"\bgit\s+(-C\s+\S+\s+)?log\b.*--stat",
        r"\bgit\s+(-C\s+\S+\s+)?diff\s+HEAD",
        r"\bgit\s+(-C\s+\S+\s+)?cat-file\s+",
    ),
    "flag_value": (
        # Live service API/CLI echo. The claimed flag must be queried via
        # a CLI that resolves live, not a config-file read.
        r"\bxurl\s+whoami\b",
        r"\bxurl\s+auth\s+status\b",
        r"\bhermes\s+config\s+get\b",
    ),
}

# Structured claim patterns — extracted from summary + metadata fields.
# Each pattern is anchored to a claim kind that maps to the allowlist above.
_CLAIM_PATTERNS: dict[str, re.Pattern[str]] = {
    "file_path": re.compile(
        r"(?:^|[\s\"'`])(?P<path>"
        r"(?:~/[^\s\"'`]+|/Users/[^\s\"'`]+|/tmp/[^\s\"'`]+\.\w+|"
        r"[^\s\"'`]+\.(?:py|md|json|yaml|yml|toml|sh|sql|db)\b)"
        r")",
        re.MULTILINE,
    ),
    "cron": re.compile(
        r"(?P<cron>"
        r"\b(?:cron_id|job_id)\s*[=:]\s*[A-Za-z0-9_-]+"
        r"|\bhermes\s+cron\s+(?:show|list|status)\b"
        r"|\bschedule\s*:\s*"
        r"|\blast_run_at\s*[=:]"
        r")",
        re.IGNORECASE,
    ),
    "flag_value": re.compile(
        r"(?P<flag>"
        r"\b(?:enabled|state|status|ready)\s*:\s*(?:true|false|enabled|disabled|paused|running|scheduled|ok|error)\b"
        r"|\b[A-Za-z_][A-Za-z0-9_-]*\s*=\s*(?:true|false|[0-9]+|\"[^\"]+\")"
        r")",
        re.IGNORECASE,
    ),
}

# Comment-shape parser. The fence must be at the start of a line; the paired
# exit code line must appear within FENCE_WINDOW lines below it.
_FENCE_PATTERN = re.compile(r"(?m)^[ \t]*##\s+re-classification\b")
_FENCE_WINDOW = 10

# Exit-code line: matches "exit_code: 0" or "exit 0" or "rc=0" — paired with
# the check command in the same fence window.
_EXIT_PATTERN = re.compile(
    r"(?m)^[ \t]*(?:exit_code|exit|rc)\s*[=:]\s*0\b"
)

# (See _FENCE_WINDOW above for the rationale on 10 lines.)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Claim:
    """A structural claim extracted from the completion payload."""

    kind: str            # 'file_path' | 'cron' | 'flag_value'
    raw: str             # the matched substring
    target: str          # parsed path / cron id / key=value

    def __repr__(self) -> str:  # pragma: no cover
        return f"Claim({self.kind}, {self.target!r})"


@dataclass
class Gate3Verdict:
    """Outcome of the gate check."""

    status: str          # 'skip' | 'pass' | 'block'
    claims: list[Claim]
    reason: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status in ("skip", "pass")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_structural_claims(
    *,
    summary: Optional[str],
    metadata: Optional[dict],
) -> list[Claim]:
    """Walk summary + structured metadata fields for structural claims.

    Never walks comment prose (too easy to stuff claims into a comment to
    evade). Walks summary as a literal substring scan and metadata's
    structured fields directly.
    """
    claims: list[Claim] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: str, raw: str, target: str) -> None:
        key = (kind, target)
        if key in seen:
            return
        seen.add(key)
        claims.append(Claim(kind=kind, raw=raw, target=target))

    # 1. Walk summary (literal path-shaped substrings + cron/flag regexes).
    if summary:
        for kind, pattern in _CLAIM_PATTERNS.items():
            # Map the dict key to the regex's named-group name. The cron
            # and flag_value patterns use 'cron' and 'flag' as their named
            # groups (file_path uses 'path').
            group_name = {"file_path": "path", "cron": "cron", "flag_value": "flag"}[kind]
            for m in pattern.finditer(summary):
                target = m.group(group_name) or m.group(0)
                _add(kind, m.group(0).strip(), target.strip())

    # 2. Walk structured metadata fields directly. These are typed, not
    #    prose, so the regex match is reliable.
    if isinstance(metadata, dict):
        # metadata.cron_id — typed cron reference
        cid = metadata.get("cron_id") or metadata.get("job_id")
        if isinstance(cid, str) and cid.strip():
            _add("cron", f"cron_id={cid}", cid.strip())

        # metadata.flag — typed flag/config claim
        flag = metadata.get("flag")
        if isinstance(flag, str) and flag.strip():
            _add("flag_value", f"flag={flag}", flag.strip())

        # metadata.identity_handle — identity claim, falls under flag_value
        handle = metadata.get("identity_handle")
        if isinstance(handle, str) and handle.strip():
            _add("flag_value", f"identity_handle={handle}", handle.strip())

        # Normalize metadata values that may have been auto-stringified
        # by the worker tool layer (single-item lists can come through
        # as `{"item": "value"}` dicts). Without this, the extractor
        # would silently miss claim-bearing payloads — a false-pass
        # gate failure mode, worse than false-block. The Dispatch
        # 2026-07-12 worker hit this exact shape on metadata.changed_files
        # = `{"item": "01-Inbox/.../note.md"}` and the gate returned zero
        # claims on an obviously claim-bearing payload.
        def _normalize_list_like(v):
            """Coerce dict-wrapped single values back to list."""
            if isinstance(v, dict):
                # Common wrappers: {"item": x}, {"value": x}, {"path": x}
                # Heuristic: if any value is a string, treat the dict as
                # a single-element list. Conservative — if multiple
                # string values, fall through to the value list.
                strs = [val for val in v.values() if isinstance(val, str)]
                if len(strs) == len(v) and len(v) >= 1:
                    return strs
            return v

        cf = metadata.get("changed_files")
        cf = _normalize_list_like(cf)
        if isinstance(cf, (list, tuple)):
            for p in cf:
                if isinstance(p, str) and p.strip():
                    _add("file_path", p.strip(), p.strip())

        # metadata.artifacts is a STRUCTURAL CLAIM about deliverable
        # file state. Unlike changed_files (audit), artifacts are the
        # files the worker is asserting are written and ready to ship.
        # Gate 3 fires on each.
        art = metadata.get("artifacts")
        art = _normalize_list_like(art)
        if isinstance(art, (list, tuple)):
            for p in art:
                if isinstance(p, str) and p.strip():
                    _add("file_path", p.strip(), p.strip())

    return claims


# ---------------------------------------------------------------------------
# Comment-shape parsing
# ---------------------------------------------------------------------------

def find_reclassification_block(comment_body: Optional[str]) -> Optional[str]:
    """Return the body text under the most recent ``## re-classification``
    fence (up to FENCE_WINDOW lines), or None if the fence is absent.

    Distinct from ``## self-classification`` — the worker must re-derive
    the problem from a fresh check, not restate its prior conclusion.
    """
    if not comment_body:
        return None
    fence = _FENCE_PATTERN.search(comment_body)
    if not fence:
        return None
    body = comment_body[fence.end():]
    lines = body.splitlines()[:_FENCE_WINDOW]
    return "\n".join(lines)


def find_paired_check_command(reclass_body: str) -> Optional[str]:
    r"""Return the cited check command from the re-classification body.

    Looks for the first command-shaped line (not a comment, not blank, not
    the ``exit_code:`` status line) AND has an ``exit_code: 0`` / ``exit 0``
    / ``rc=0`` line within the same fence window.

    Returns ANY command-shaped line — the gate's authority allowlist check
    is responsible for rejecting non-authority commands (``cat``, ``[ -f ]``,
    ``head``, ``grep <file>``) with the proper reason text. Splitting the
    parser (return any command) from the authority check (reject non-
    authority) lets the rejection reason name the real failure mode.

    Tolerates common prompt prefixes workers emit: ``$ ``, ``# ``, and
    leading fenced-code-block wrapper lines.
    """
    if not reclass_body:
        return None
    if not _EXIT_PATTERN.search(reclass_body):
        return None
    # Walk lines for the first command-shaped line. Strip common shell
    # prompt markers (``$ `` for user, ``# `` and ``> `` for root/continuation)
    # and markdown fenced-code wrapper lines (``\`\`\``). We deliberately
    # do NOT include ``#`` as a leading-character strip — ``#`` is the
    # start of every markdown heading AND the fence itself; stripping it
    # would cause the parser to treat "## re-classification" or
    # "# Heading" as a command.
    prompt_strip = re.compile(r"^[$>]+\s*")
    for raw in reclass_body.splitlines():
        line = prompt_strip.sub("", raw.strip())
        if not line:
            continue
        # Skip fenced-code-block wrapper lines.
        if line.startswith("```"):
            continue
        # Skip comment lines.
        if line.startswith("#"):
            continue
        # Skip the exit-code status line itself.
        if _EXIT_PATTERN.match(line):
            continue
        return line
    return None


# ---------------------------------------------------------------------------
# Independence check
# ---------------------------------------------------------------------------
# NOTE: The secondary "changed_files substring intersect" check was removed
# on 2026-07-12. Rationale: it only fired when the primary allowlist
# already matched (the PASS case) — pure over-block. `git show HEAD:<path>`
# against changed_files=[<path>] was flagged even though `git show` reads
# the git object store, not the worker's working-tree write. The primary
# source-KIND allowlist is the sufficient mechanical discriminator.

def _split_compound_command(check_cmd: str) -> list[str]:
    """Split a compound shell command into individual segments.

    A check command may chain multiple commands via ``&&``, ``||``, ``;``.
    These are sequential operators — each segment runs and the next runs
    based on the previous result. If ANY segment is a working-tree read,
    the check fails closed (the worker can be verifying the working tree
    regardless of what the authority segment does).

    Pipes (``|``) are NOT split. ``crontab -l | grep research-url-gate-a1``
    is one check: the crontab output (authority) is filtered by grep. The
    grep is filtering authority output, not reading a file. Splitting on
    pipes would break the canonical cron-existence check.

    This is the bypass-by-concatenation defense added on 2026-07-12 after
    the X3 reframing exposed a live parsing hole: the previous code did
    ``re.search(p, check_cmd)`` which matched the ``git show`` substring
    inside a compound command and missed the trailing ``cat foo.py``.
    """
    parts = re.split(r"\s*(?:&&|\|\||;)\s*", check_cmd)
    return [p.strip() for p in parts if p.strip()]


def _check_command_uses_authority(check_cmd: str, kind: str) -> bool:
    """Primary discriminator: does the cited check query an external authority?

    Allowlist maps claim kind → regex set. If ANY segment of a compound
    command matches an authority pattern AND NO segment is a working-tree
    read, the check is on an external authority (scheduler, git store,
    live service). If any segment is a working-tree read (cat, [ -f ],
    head, grep <file>), the check fails closed — the worker's write is
    potentially being verified by reading the worker's write.

    Working-tree reads NEVER discharge a claim, regardless of position in
    the compound chain.

    Note on pipes: a single segment after splitting on ``&&``/``||``/``;``
    may itself contain pipes (``crontab -l | grep x``). We only check
    the FIRST token of the segment (the executable that runs), not the
    pipe-targets — pipe-targets filter authority output, they don't
    re-introduce a working-tree read.
    """
    patterns = AUTHORITY_ALLOWLIST.get(kind, ())
    # Working-tree read patterns — if the head of ANY segment matches,
    # the check fails. We look at the head only because pipes within a
    # segment feed authority output to filters.
    working_tree_head = (
        r"^cat\s+\S",
        r"^head\s+\S",
        r"^tail\s+\S",
        r"^grep\s+\S",
        r"^stat\s+\S",
        r"^\[\s*-f\s+\S",
        r"^\[\s*-e\s+\S",
    )
    segments = _split_compound_command(check_cmd)
    if not segments:
        return False
    # Check each segment's head. If any segment head is a working-tree
    # read, fail closed.
    for seg in segments:
        if any(re.search(p, seg) for p in working_tree_head):
            return False
    # Any segment head matches an authority pattern → check passes.
    return any(
        any(re.search(p, seg) for p in patterns) for seg in segments
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def gate3_claim_reframing_required(
    *,
    summary: Optional[str],
    metadata: Optional[dict],
    last_comment_body: Optional[str],
) -> Gate3Verdict:
    """The gate. Returns a Gate3Verdict; callers MUST raise on status=='block'.

    Fail-closed: any ambiguity → block. Missing paired evidence OR a check
    that can't be parsed OR a check that doesn't query external authority OR
    a check that reads a worker-authored file → GATE3_BLOCK.
    """
    claims = extract_structural_claims(summary=summary, metadata=metadata)
    if not claims:
        return Gate3Verdict(status="skip", claims=[])

    reclass = find_reclassification_block(last_comment_body)
    if reclass is None:
        return Gate3Verdict(
            status="block",
            claims=claims,
            reason=(
                "GATE3_BLOCK: no `## re-classification` fence in latest "
                "comment. Worker must re-derive the problem from a fresh "
                "check (NOT restate prior conclusion) within 5 lines under "
                "the fence, with a paired exit_code: 0."
            ),
        )

    check_cmd = find_paired_check_command(reclass)
    if check_cmd is None:
        return Gate3Verdict(
            status="block",
            claims=claims,
            reason=(
                "GATE3_BLOCK: re-classification fence present but no paired "
                "check command with exit_code: 0 within the 5-line window. "
                "Cite the actual command run + its exit status."
            ),
        )

    changed_files = (
        list(metadata.get("changed_files", []))  # type: ignore[union-attr]
        if isinstance(metadata, dict) else []
    )

    # Each claim must be discharged by at least one check command whose
    # source is independent. The primary source-KIND allowlist is the
    # sufficient mechanical discriminator: a command that matches an
    # authority pattern (crontab / git store / live service) reads from
    # outside the worker's control. The old secondary "changed_files
    # substring intersect" check was over-broad — it flagged `git show
    # HEAD:foo.py` against changed_files=[foo.py] even though `git show`
    # reads the git object store (independent of the worker's write).
    # Per Decision 2026-07-12, file claims get PROCEDURAL proof (a check
    # ran with exit_code: 0) rather than authority-independence — the
    # worker IS the author. True file-claim correctness is Gate 3b
    # (AST/behavioral), explicitly descoped. cron and identity claims
    # get true authority-independence (external daemon / API).
    for claim in claims:
        if not _check_command_uses_authority(check_cmd, claim.kind):
            return Gate3Verdict(
                status="block",
                claims=[claim],
                reason=(
                    f"GATE3_BLOCK: paired check `{check_cmd[:80]}` does not "
                    f"query an external authority for claim kind "
                    f"'{claim.kind}' (target={claim.target!r}). Working-"
                    f"tree reads (cat, [ -f ], head, grep) do NOT discharge "
                    f"a claim even if the path is absent from changed_files."
                ),
            )

    return Gate3Verdict(status="pass", claims=claims)


# ---------------------------------------------------------------------------
# Exception — raise from complete_task before the write txn
# ---------------------------------------------------------------------------

class Gate3BlockError(ValueError):
    """Raised by ``complete_task`` when Gate 3 finds an under-discharged claim.

    Mirrors ``HallucinatedCardsError`` so the tool handler can render the
    same structured rejection path. The structured fields let the worker
    fix the specific claim without guessing.
    """

    def __init__(
        self,
        verdict: Gate3Verdict,
        completing_task_id: str,
    ) -> None:
        self.verdict = verdict
        self.completing_task_id = completing_task_id
        claims_str = ", ".join(
            f"{c.kind}:{c.target}" for c in verdict.claims
        ) or "<none>"
        super().__init__(
            f"completion blocked by Gate 3 (claim_reframing_required) on "
            f"task {completing_task_id}: claims=[{claims_str}]; "
            f"reason={verdict.reason}"
        )


class Gate3ConfigError(RuntimeError):
    """Raised when the gate cannot read its mode file (``~/.hermes/gate3_mode``).

    Distinct from Gate3BlockError so the operator can tell
    "gate can't read its setting" from "worker under-discharged". The
    integrity rule: a missing/unreadable mode file is NOT a silent
    shadow default — that's the deletion-regression hole. It is a
    fail-closed BLOCK with a distinct event kind.
    """


# ---------------------------------------------------------------------------
# Mode file — fleet-wide authoritative source (Option B per Decision 2026-07-12)
# ---------------------------------------------------------------------------

_VALID_MODES = ("shadow", "enforce")


# Fleet-wide paths — INDEPENDENT of HERMES_HOME.
#
# Critical (2026-07-12): the mode file and ledger are FLEET-WIDE state.
# They must NOT live under HERMES_HOME because HERMES_HOME is per-profile
# under the worker process model (when a worker runs under
# `-p head-of-content`, its HERMES_HOME resolves to the profile-scoped
# home, not the operator root). Per-profile mode files would reopen the
# split-brain hole this whole design exists to close. Every profile's
# gate reads from the SAME file at the SAME absolute path.
#
# Layout:
#   ~/.hermes/gate3_mode           — operator root (canonical, atomic)
#   ~/.hermes/gate3_shadow.jsonl   — operator root (canonical, append-only)
#
# HERMES_HOME is only resolved once at first call and cached. If
# HERMES_HOME changes mid-process (rare), the operator runs
# `hermes gate3 flip-enforce` which writes to the canonical absolute
# path — same file, same mode, fleet-wide.
_OPERATOR_ROOT: Optional[Path] = None


def _operator_root() -> Path:
    """Resolve the operator root once per process.

    Operator root = the dir that *contains* the `profiles/` child.
    In a normal install, HERMES_HOME IS the operator root, so the
    candidate itself is the answer. In a profile-scoped run (worker
    under `-p <name>`), HERMES_HOME is `~/.hermes/profiles/<name>`
    and the operator root is the grandparent of that — i.e. the
    parent of the `profiles/` directory.

    The walk is bounded: at most 3 parents up from HERMES_HOME. The
    fallback (when the candidate doesn't match any heuristic) is the
    candidate itself, which preserves backward compatibility for
    unusual install shapes but produces per-profile paths — exactly
    the split-brain hole this exists to close.
    """
    global _OPERATOR_ROOT
    if _OPERATOR_ROOT is not None:
        return _OPERATOR_ROOT
    from hermes_constants import get_hermes_home
    candidate = get_hermes_home()
    # Case A: HERMES_HOME is the operator root — it has `profiles/` as
    # a child.
    if (candidate / "profiles").is_dir():
        _OPERATOR_ROOT = candidate
        return _OPERATOR_ROOT
    # Case B: HERMES_HOME is `~/.hermes/profiles/<name>` — operator
    # root is two parents up (HERMES_HOME.parent.parent = `~/.hermes`).
    operator_candidate = candidate.parent.parent
    if (
        (operator_candidate / "profiles").is_dir()
        and (operator_candidate / "kanban").is_dir()
    ):
        _OPERATOR_ROOT = operator_candidate
        return _OPERATOR_ROOT
    # Case C: fallback. Warn via stderr once; operator should set
    # HERMES_HOME to the operator root or symlink it.
    import sys
    print(
        f"[gate3] WARN: cannot resolve operator root from "
        f"HERMES_HOME={candidate}. Falling back to HERMES_HOME "
        f"itself — this will create per-profile paths and reopen "
        f"the split-brain hole. Set HERMES_HOME to the operator "
        f"root (the dir containing profiles/ and kanban/).",
        file=sys.stderr,
    )
    _OPERATOR_ROOT = candidate
    return _OPERATOR_ROOT


def gate3_mode_file() -> Path:
    """Path to the fleet-wide mode file.

    Fleet-wide (operator root), NOT profile-scoped. The same file is
    read by every process regardless of which profile dispatched it.
    Atomic write on flip; fresh read per eval; no caching.
    """
    return _operator_root() / "gate3_mode"


def gate3_ledger_file() -> Path:
    """JSONL ledger at the operator root.

    Fleet-wide — every process appends to the same ledger.
    """
    return _operator_root() / "gate3_shadow.jsonl"


def gate3_effective_mode(*, at_eval: bool = True) -> str:
    """Read the fleet-wide mode fresh on every call.

    ``at_eval=True`` (default, used during complete_task gate eval):
    missing/unreadable/invalid file → raise Gate3ConfigError. Fail-closed.
    Do NOT silently default to shadow — that's the deletion-regression
    hole (an operator deleting the file should not silently ungate).

    ``at_eval=False`` (used by the CLI helper ``gate3 status``):
    missing file is treated as 'shadow' for display purposes only —
    the file is created lazily on first eval by ``init_db`` or on the
    next ``gate3 flip-enforce`` call.
    """
    path = gate3_mode_file()
    try:
        text = path.read_text().strip()
    except FileNotFoundError as e:
        if at_eval:
            raise Gate3ConfigError(
                f"gate3 mode file missing at {path} — operator must "
                f"run `hermes gate3 flip-enforce` or `hermes gate3 "
                f"flip-shadow` to create it. Failing closed."
            ) from e
        return "shadow"  # display-only path
    except OSError as e:
        if at_eval:
            raise Gate3ConfigError(
                f"gate3 mode file unreadable at {path}: {e}. "
                f"Failing closed."
            ) from e
        return "shadow"  # display-only path
    if text not in _VALID_MODES:
        if at_eval:
            raise Gate3ConfigError(
                f"gate3 mode file at {path} contains {text!r}, must be "
                f"one of {_VALID_MODES}. Failing closed."
            )
        return "shadow"  # display-only path
    return text


def ensure_gate3_mode_file() -> None:
    """Create the mode file with default 'shadow' if absent.

    Called by ``init_db`` (and by the operator CLI on first flip). Not
    silent at eval — ``gate3_effective_mode(at_eval=True)`` raises if
    the file is missing after init has had a chance to run.
    """
    path = gate3_mode_file()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_mode_file(path, "shadow")


def _atomic_write_mode_file(path: Path, content: str) -> None:
    """Atomic write: temp in SAME dir as target (never /tmp), then rename.

    POSIX rename(2) is atomic — a mid-eval reader sees either the old
    content or the new, never a torn file.
    """
    import os
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def flip_gate3_mode(new_mode: str) -> None:
    """Atomic flip of the fleet-wide mode. Used by ``hermes gate3 flip-*`` CLI.

    Validates ``new_mode`` is in {_VALID_MODES}, then atomic-writes.
    Logs to stderr so the operator sees the result.
    """
    if new_mode not in _VALID_MODES:
        raise ValueError(
            f"invalid mode {new_mode!r}; must be one of {_VALID_MODES}"
        )
    path = gate3_mode_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_mode_file(path, new_mode)
    import sys
    print(f"gate3 mode flipped to {new_mode!r} at {path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# JSONL ledger — every non-skip eval, atomic-append
# ---------------------------------------------------------------------------

def _ledger_lock_path() -> Path:
    """Sidecar lockfile for ledger writes. Same dir as the ledger."""
    return gate3_ledger_file().with_suffix(".jsonl.lock")


def _append_ledger_row(row: dict) -> None:
    """Append a JSON-encoded row to the ledger. Atomic per-row via temp+rename.

    The ledger is the flip-receipt surface — the operator reads it to
    decide the flip. Every non-skip eval (pass + block + fail-closed)
    writes one row. Skips do NOT write (no eval work to log).
    """
    import json
    import os
    import time

    path = gate3_ledger_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Stamp the timestamp if not already set
    row.setdefault("timestamp", int(time.time()))
    line = json.dumps(row, sort_keys=True, separators=(",", ":"))
    # Append under a sidecar lockfile. The lock is best-effort; two
    # concurrent writers serialize via OS-level rename + append atomicity
    # on POSIX. We do NOT use fcntl/flock to avoid a hard dep on the
    # lock module being importable in every worker.
    lock = _ledger_lock_path()
    if lock.exists():
        # Another writer is in flight; the rename below is atomic per
        # row but the read-modify-rename cycle is not. For the volumes
        # we expect (a few hundred evals per minute), this is fine.
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Read existing (if any), append, write atomically
    existing = path.read_text() if path.exists() else ""
    tmp.write_text(existing + line + "\n")
    os.replace(tmp, path)


# In-process skip counter — flushed to JSONL every N evals + at exit.
# Per Decision 2026-07-12 #2 (skip-count canary).
_SKIP_COUNT_BETWEEN_FLUSH = 100
_skip_counter = {"count": 0}
_skip_counter_registered = False


def _register_skip_counter_exit_hook() -> None:
    """Register an atexit hook to flush the partial skip counter on exit."""
    global _skip_counter_registered
    if _skip_counter_registered:
        return
    _skip_counter_registered = True
    import atexit

    def _flush():
        if _skip_counter["count"] > 0:
            _append_ledger_row({
                "event": "gate3_skip_summary",
                "skip_count": _skip_counter["count"],
                "flush_reason": "process_exit",
            })
            _skip_counter["count"] = 0

    atexit.register(_flush)


def _record_skip() -> None:
    """Increment the in-process skip counter; emit summary every N."""
    _register_skip_counter_exit_hook()
    _skip_counter["count"] += 1
    if _skip_counter["count"] >= _SKIP_COUNT_BETWEEN_FLUSH:
        _append_ledger_row({
            "event": "gate3_skip_summary",
            "skip_count": _skip_counter["count"],
            "flush_reason": "threshold",
        })
        _skip_counter["count"] = 0


def _record_eval(
    *,
    effective_mode: str,
    verdict: "Gate3Verdict",
    task_id: str,
    profile: Optional[str],
    reclassification_found: bool,
    paired_check_command: Optional[str],
    comment_excerpt: Optional[str],
    source: Optional[str] = None,
) -> None:
    """Emit a JSONL ledger row for one gate eval (pass or block).

    The flip receipt reads this ledger to verify:
      (i) effective_mode=shadow for the entire shadow window
      (ii) zero FALSE X-path blocks (blocks where the parser missed a
           fence that was actually present)
      (iii) would-block rate is low or declining (worker-adoption signal)
    """
    # Comment excerpt is truncated to keep ledger rows readable. The
    # full body is on the task's comment row in the DB if needed.
    excerpt = None
    if comment_excerpt:
        excerpt = comment_excerpt[:500]
    row = {
        "event": "completion_would_block_gate3",
        "effective_mode": effective_mode,
        "task_id": task_id,
        "profile": profile,
        "claims": [
            {"kind": c.kind, "target": c.target, "raw": c.raw}
            for c in verdict.claims
        ],
        "reason": verdict.reason,
        "status": verdict.status,
        "reclassification_found": reclassification_found,
        "paired_check_command": paired_check_command,
        "comment_excerpt": excerpt,
    }
    if source is not None:
        row["source"] = source
    _append_ledger_row(row)


# ---------------------------------------------------------------------------
# Non-mutating replay — for X-path verification (Decision 2026-07-12 #3)
# ---------------------------------------------------------------------------

def evaluate_only(
    *,
    summary: Optional[str],
    metadata: Optional[dict],
    last_comment_body: Optional[str],
    task_id: str,
    profile: Optional[str],
    source: str = "x_path_replay",
) -> Gate3Verdict:
    """Non-mutating gate eval. Computes the SAME verdict enforce uses,
    logs to JSONL with the source tag, returns the verdict.

    Does NOT call complete_task. Does NOT emit task_events. Does NOT
    mutate the board. Used by ``hermes gate3 replay-x-path --task-id T``
    to verify what a real captured payload WOULD have done in shadow
    without committing the closure.

    Integrity rule (Decision 2026-07-12): same verdict function, same
    extraction/parse pipeline, no forked pre-processing.
    """
    claims = extract_structural_claims(summary=summary, metadata=metadata)
    if not claims:
        # Skip case — non-mutating replay of a non-claim payload just
        # returns skip without logging (matches the production skip path).
        return Gate3Verdict(status="skip", claims=[])

    reclass = find_reclassification_block(last_comment_body)
    check_cmd = find_paired_check_command(reclass) if reclass else None

    # Compute the verdict via the same gate function enforce uses.
    verdict = gate3_claim_reframing_required(
        summary=summary,
        metadata=metadata,
        last_comment_body=last_comment_body,
    )

    # Try to read the mode file (display-only at_eval=False). If it
    # raises, fall back to "shadow" for the ledger row — the real eval
    # path will hard-block via Gate3ConfigError.
    try:
        mode = gate3_effective_mode(at_eval=False)
    except Gate3ConfigError:
        mode = "shadow"

    _record_eval(
        effective_mode=mode,
        verdict=verdict,
        task_id=task_id,
        profile=profile,
        reclassification_found=reclass is not None,
        paired_check_command=check_cmd,
        comment_excerpt=last_comment_body,
        source=source,
    )
    return verdict
