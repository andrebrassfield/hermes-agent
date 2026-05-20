"""attention-pruner — "lost in the middle" eviction engine for Hermes.

Sits *after* g-brain-compressor in the plugin chain. Once the compressor
has done its pass on bloated tool payloads, the pruner targets the other
source of context bloat: long stretches of conversational/reasoning turns
that dilute the LLM's attention without carrying decisions.

Eviction rules
──────────────
1. messages[0] (system prompt) is ALWAYS pinned — KV cache prefix lock.
2. The last 15 messages are ALWAYS pinned — recency buffer.
3. Everything between the pinned head and pinned tail is the *evictable
   middle*. The pruner scans it for tool-pairs and decision nodes; the
   rest gets compressed and replaced with a stub.
4. Tool-pair integrity: a tool_call message and its tool result are
   atomic — neither may be split across the eviction boundary. If either
   falls on the boundary, the pruner extends the window to capture both.
5. Decision nodes (messages containing "decide", "approve", "reject",
   "summarize", "final") are preserved — no silent eviction of consequential
   turns.

Rolling summary
───────────────
Each evicted batch is narrated with a 2-sentence summary and gzip-compressed
to ~/.mavis/context/archive/gbr_<id>.txt.gz alongside the compressor's own
archives. The stub in the message list preserves the archive_id so the full
log is always retrievable.

Activation
──────────
The pruner activates when BOTH conditions are met:
  - post-compressor token count > TOKEN_BUDGET_THRESHOLD (80 K tokens)
  - message count ≥ MIN_MESSAGE_COUNT (20 messages)
This dual trigger avoids false positives on short sessions.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger("attention-pruner")

# ── Configuration ──────────────────────────────────────────────────────────
ARCHIVE_DIR = Path(os.path.expanduser("~/.mavis/context/archive"))
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

ENABLE_PRUNING = True          # set to False to disable (still logs)
TOKEN_BUDGET_THRESHOLD = 80_000   # post-compressor tokens before pruning activates
MIN_MESSAGE_COUNT = 20           # also requires this many messages
PINNED_TAIL = 15                 # last N messages that are always safe
ROLLING_SUMMARY_MAX_CHARS = 500   # max characters in the on-disk summary

# Patterns that mark a message as a "decision" — never silently evict
DECISION_PATTERNS = re.compile(
    r"\b(decide|approved?|rejected?|summarize|conclude|final"
    r"|commit|confirm|assert|梗|决策|结论|批准|拒绝)\b",
    re.IGNORECASE,
)

# ── Token Estimation (mirrors g-brain-compressor) ──────────────────────────
_ENCODER = None


def _get_encoder():
    global _ENCODER
    if _ENCODER is None:
        try:
            import tiktoken
            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODER = None
    return _ENCODER


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    ascii_chars = sum(1 for c in text if ord(c) <= 127)
    cjk_chars = len(text) - ascii_chars
    return int(ascii_chars / 4 + cjk_chars / 2)


def _messages_token_count(messages: list) -> int:
    """Fast estimate of total token count for a message list."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content)
        # rough overhead for role + name + tool_calls JSON
        total += 30
    return total


# ── Archive Helpers ────────────────────────────────────────────────────────

def archive_batch(
    messages: list,
    batch_id: str,
    summary: str,
    metadata: Optional[dict] = None,
) -> str:
    """
    Compress a list of messages to gzip on disk.

    Returns the archive_id (gbr_XXXXXXXX format, shared with compressor
    so the retrieval interface is identical).
    """
    archive_id = batch_id or f"gbr_{uuid.uuid4().hex[:8]}"
    archive_path = ARCHIVE_DIR / f"{archive_id}.txt.gz"
    meta_path = ARCHIVE_DIR / f"{archive_id}.meta.json"

    content = json.dumps(messages, ensure_ascii=False, indent=2)

    with gzip.open(archive_path, "wt", encoding="utf-8") as f:
        f.write(content)

    meta = {
        "archive_id": archive_id,
        "type": "pruned_attention_batch",
        "summary": summary,
        "message_count": len(messages),
        "timestamp": int(time.time()),
        "metadata": metadata or {},
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f)

    logger.debug(
        f"Attention Pruner archived {archive_id} "
        f"({len(messages)} msgs, {len(content.encode('utf-8')):,} bytes)"
    )
    return archive_id


# ── Core Eviction Logic ────────────────────────────────────────────────────

def _is_decision(msg: dict) -> bool:
    """Return True if a message looks like a decision node — never evict."""
    content = msg.get("content", "")
    if isinstance(content, str) and DECISION_PATTERNS.search(content):
        return True
    # Also check tool calls — invoking a decisive tool is itself a decision
    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            if any(kw in name.lower() for kw in ("decide", "approve", "reject", "commit")):
                return True
    return False


def _build_stub(archive_id: str, turns_evicted: int, summary: str) -> str:
    """Compact placeholder injected where evicted messages lived."""
    return (
        f"[G-BRAIN: {turns_evicted} conversation turns archived → {archive_id}]\n"
        f"Summary: {summary}\n"
        f"Retrieve: fetch_archived_memory(id='{archive_id}')"
    )


def _make_rolling_summary(messages: list) -> str:
    """
    Build a 2-sentence narrative summary of a batch of messages.
    Preserves the "plot" of the conversation so the LLM can reconstruct
    context after eviction without seeing a meaningless stub.
    """
    if not messages:
        return "(no messages)"

    contents = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            # Strip tool-call JSON noise for cleaner summary
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                try:
                    parts = []
                    for tc in msg["tool_calls"]:
                        fn = tc.get("function", {})
                        parts.append(f"[calls {fn.get('name','?')} with args]")
                    content = " ".join(parts)
                except Exception:
                    pass
            contents.append(content.strip())

    if not contents:
        return "(opaque tool/results only)"

    # First meaningful line as anchor
    first = contents[0][:200]
    # Last meaningful line before the recent buffer
    last = contents[-1][:200] if len(contents) > 1 else ""

    turns = len(contents)
    if last and last != first:
        return f"Session discussed: {first[:120]}... ({turns} turns, last: {last[:80]})"
    return f"Session had {turns} turns: {first[:150]}"


def _find_tool_pair_extent(messages: list, start: int, end: int) -> tuple[int, int]:
    """
    Scan messages[start:end] for tool_calls/tool results and extend the
    eviction window so tool pairs are never split.

    Returns (actual_start, actual_end) — may be wider than the input range
    if tool messages straddle the boundary.
    """
    actual_start = start
    actual_end = end

    # Extend end to include the first tool result AFTER the boundary
    # (tool result = role=tool, or a tool result block in assistant message)
    for i in range(end, min(end + 5, len(messages))):
        msg = messages[i]
        role = msg.get("role", "")
        if role == "tool":
            actual_end = i + 1
            break
        # Assistant message with inline tool result content
        content = msg.get("content", "")
        if isinstance(content, str) and "tool_use" in content[:500]:
            actual_end = i + 1
            break

    # Extend start to include the last tool_call BEFORE the boundary
    for i in range(start - 1, max(start - 6, -1), -1):
        msg = messages[i]
        if msg.get("tool_calls") or msg.get("role") == "tool":
            actual_start = i
            break

    return actual_start, actual_end


def _scan_eviction_candidates(messages: list, pivot: int) -> tuple[int, int, list]:
    """
    Scan from pivot (outside the protected window) inward and find the
    contiguous evictable region, accounting for tool pairs and decision nodes.

    Returns (ev_start, ev_end, preserved_indices).
    """
    protected_tail_start = len(messages) - PINNED_TAIL

    if pivot >= protected_tail_start:
        # Not enough messages to evict
        return -1, -1, []

    # The evictable region: [pivot, protected_tail_start)
    ev_end = protected_tail_start
    ev_start = pivot
    preserved: list[int] = []

    i = ev_end - 1
    while i >= ev_start:
        msg = messages[i]

        # Never evict a decision node
        if _is_decision(msg):
            preserved.append(i)
            ev_end = i
        # Never split a tool pair — extend backward to capture both halves
        elif msg.get("role") == "tool" or msg.get("tool_calls"):
            # Find the full extent of this tool pair
            pair_start, pair_end = _find_tool_pair_extent(messages, ev_start, i + 1)
            ev_start = pair_start
            ev_end = max(ev_end, pair_end)
            i = pair_start - 1
            continue

        i -= 1

    return ev_start, ev_end, preserved


def prune(messages: list) -> dict:
    """
    Main entry point. Inspects the message list, evicts the "evictable middle",
    archives the evicted turns to gzip, and replaces them with a stub.

    Returns:
        {
            "did_prune": bool,
            "turns_evicted": int,
            "archive_id": str or None,
            "stub": str or None,
            "summary": str or None,
            "post_prune_token_count": int,
            "preserved_indices": list,
        }
    """
    if not ENABLE_PRUNING:
        return {"did_prune": False, "reason": "disabled"}

    total_count = len(messages)
    token_count = _messages_token_count(messages)

    # Dual trigger: need BOTH token pressure AND message volume
    if token_count < TOKEN_BUDGET_THRESHOLD or total_count < MIN_MESSAGE_COUNT:
        return {
            "did_prune": False,
            "reason": f"thresholds not met ({token_count:,}/{TOKEN_BUDGET_THRESHOLD:,} tok, "
                      f"{total_count}/{MIN_MESSAGE_COUNT} msgs)",
        }

    # Protected zones
    if total_count <= PINNED_TAIL + 3:
        return {"did_prune": False, "reason": "session too short to evict"}

    # The pivot: start evicting from PINNED_TAIL positions before the tail
    # (i.e. we keep pivot+pinned_tail messages at the end)
    pivot = PINNED_TAIL + 3

    ev_start, ev_end, preserved = _scan_eviction_candidates(messages, pivot)

    if ev_start < 0 or ev_end <= ev_start:
        return {"did_prune": False, "reason": "no viable eviction window found"}

    turns_evicted = ev_end - ev_start
    if turns_evicted < 3:
        return {"did_prune": False, "reason": "fewer than 3 messages to evict"}

    # Build summary BEFORE mutating the list
    evicted_slice = messages[ev_start:ev_end]
    summary = _make_rolling_summary(evicted_slice)

    # Archive the batch
    archive_id = archive_batch(
        messages=evicted_slice,
        batch_id=f"gbr_{uuid.uuid4().hex[:8]}",
        summary=summary,
        metadata={
            "evicted_range": [ev_start, ev_end],
            "preserved_at_indices": preserved,
            "token_count_before_archive": token_count,
        },
    )

    # Build stub
    stub = _build_stub(archive_id, turns_evicted, summary)

    # Mutate in-place: replace the evictable middle with the stub
    messages[ev_start:ev_end] = [{"role": "system", "content": stub}]

    post_token_count = _messages_token_count(messages)

    logger.info(
        f"✂️  Attention Pruner: evicted {turns_evicted} msgs "
        f"(tokens {token_count:,} → {post_token_count:,}, "
        f"saved ~{token_count - post_token_count:,}), "
        f"archive={archive_id}"
    )

    return {
        "did_prune": True,
        "turns_evicted": turns_evicted,
        "archive_id": archive_id,
        "stub": stub,
        "summary": summary,
        "post_prune_token_count": post_token_count,
        "preserved_indices": preserved,
        "evicted_range": [ev_start, ev_end],
    }


# ── Hermes Hook Handlers ──────────────────────────────────────────────────

def on_pre_api_request(
    *,
    task_id: str = "",
    session_id: str = "",
    platform: str = "",
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_mode: str = "",
    api_call_count: int = 0,
    request_messages: Any = None,
    messages: Any = None,
    turn_type: str = "user",
    message_count: int = 0,
    tool_count: int = 0,
    approx_input_tokens: int = 0,
    request_char_count: int = 0,
    max_tokens: Any = None,
    conversation_history: Any = None,
    user_message: Any = None,
    **_: Any,
) -> None:
    """
    pre_api_request hook — runs AFTER g-brain-compressor.

    The message list at this point has already been processed by the
    compressor, so token counts reflect compressed payloads. The pruner
    makes its eviction decision on that post-compressor state.
    """
    if not ENABLE_PRUNING:
        return

    # Resolve the actual messages list
    msg_list = None
    for candidate in (request_messages, messages, conversation_history):
        if isinstance(candidate, list):
            msg_list = candidate
            break

    if msg_list is None:
        return

    try:
        result = prune(msg_list)
        if result["did_prune"]:
            logger.info(
                f"✂️  Attention Pruner pre_api #{api_call_count}: "
                f"evicted {result['turns_evicted']} msgs, "
                f"archive={result['archive_id']}"
            )
    except Exception as exc:
        logger.warning(f"Attention Pruner pre_api hook failed safely: {exc}")


# ── Plugin Registration ─────────────────────────────────────────────────

def register(ctx) -> None:
    """Register hooks with the Hermes plugin system."""
    ctx.register_hook("pre_api_request", on_pre_api_request)
    logger.info(
        f"✂️  Attention Pruner registered "
        f"(threshold={TOKEN_BUDGET_THRESHOLD:,} tok, "
        f"min_msgs={MIN_MESSAGE_COUNT}, "
        f"pinned_tail={PINNED_TAIL}, "
        f"archive_dir={ARCHIVE_DIR})"
    )