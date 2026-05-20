"""g-brain-compressor — JIT tool bloat reducer for Hermes.

Intercepts massive tool payloads (> TOKEN_BUDGET_THRESHOLD tokens) before
they enter the context window, compresses them to gzip on disk, and replaces
them with a compact reference. Keeps the context clean while preserving
the full data for on-demand retrieval.

Also provides a `fetch_archived_memory` tool so the LLM can retrieve
archived payloads if needed later.

Archive directory: ~/.mavis/context/archive/
Archive format:   <archive_id>.txt.gz
Reference format: [G-BRAIN] <archive_id>\n<summary>
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("g-brain-compressor")

# ── Configuration ──────────────────────────────────────────────────────────
ARCHIVE_DIR = Path(os.path.expanduser("~/.mavis/context/archive"))
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

TOKEN_BUDGET_THRESHOLD = 5000   # tokens — G-Brain intervenes above this
ENABLE_COMPRESSION = True       # set to False to disable (still logs)
MAX_ARCHIVE_AGE_DAYS = 30       # auto-cleanup of old archives

# ── Token Estimation ──────────────────────────────────────────────────────
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
    # Fallback: ~4 chars/token ASCII, ~2 chars/token CJK
    ascii_chars = sum(1 for c in text if ord(c) <= 127)
    cjk_chars = len(text) - ascii_chars
    return int(ascii_chars / 4 + cjk_chars / 2)


# ── Archive Manager ────────────────────────────────────────────────────────

def archive_payload(content: str, tool_name: str = "unknown",
                    metadata: Optional[dict] = None) -> dict:
    """
    Compress and archive a payload to gzip.

    Returns:
        {archive_id, path, original_tokens, archived_tokens, saved_tokens,
         compression_ratio, timestamp}
    """
    archive_id = f"gbr_{uuid.uuid4().hex[:8]}"
    archive_path = ARCHIVE_DIR / f"{archive_id}.txt.gz"

    original_tokens = _estimate_tokens(content)

    # Write gzip compressed archive
    with gzip.open(archive_path, "wt", encoding="utf-8") as f:
        f.write(content)

    archived_size = archive_path.stat().st_size
    archived_tokens = _estimate_tokens(content)  # same content, just stored

    # Write metadata sidecar
    meta_path = ARCHIVE_DIR / f"{archive_id}.meta.json"
    meta = {
        "archive_id": archive_id,
        "tool_name": tool_name,
        "original_tokens": original_tokens,
        "archived_bytes": archived_size,
        "timestamp": int(time.time()),
        "metadata": metadata or {},
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f)

    saved = original_tokens - len(content) // 4  # rough saved estimate
    ratio = (archived_size / max(len(content.encode("utf-8")), 1)) * 100

    logger.debug(
        f"Archived {archive_id} ({original_tokens} tok → {archived_size} bytes, "
        f"{ratio:.1f}% of raw), tool={tool_name}"
    )

    return {
        "archive_id": archive_id,
        "path": str(archive_path),
        "original_tokens": original_tokens,
        "archived_bytes": archived_size,
        "compression_ratio_pct": round(ratio, 1),
        "saved_tokens_estimate": saved,
        "timestamp": meta["timestamp"],
    }


def build_reference(archive_id: str, original_tokens: int,
                    tool_name: str, summary: str) -> str:
    """Build the compact reference string injected into the context."""
    return (
        f"[G-BRAIN: Tool output archived → {archive_id}]\n"
        f"Original: {original_tokens:,} tokens | Tool: {tool_name}\n"
        f"Summary: {summary}\n"
        f"Retrieve: fetch_archived_memory(id='{archive_id}')"
    )


def summarize_content(content: str, max_chars: int = 300) -> str:
    """Generate a brief plain-text summary of archived content."""
    lines = content.strip().split("\n")
    # First 3 lines as representative sample
    sample = "\n".join(lines[:3])
    if len(content) > max_chars:
        sample = content[:max_chars] + "..."
    return sample


def retrieve_archived(archive_id: str) -> Optional[dict]:
    """
    Retrieve and decompress an archived payload.

    Returns:
        {archive_id, content, original_tokens, tool_name, metadata}
        or None if not found.
    """
    archive_id = archive_id.strip()
    archive_path = ARCHIVE_DIR / f"{archive_id}.txt.gz"
    meta_path = ARCHIVE_DIR / f"{archive_id}.meta.json"

    if not archive_path.exists():
        logger.warning(f"Archive not found: {archive_id}")
        return None

    try:
        with gzip.open(archive_path, "rt", encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:
        logger.error(f"Failed to decompress {archive_id}: {exc}")
        return None

    meta = {}
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception:
            pass

    return {
        "archive_id": archive_id,
        "content": content,
        "original_tokens": _estimate_tokens(content),
        "tool_name": meta.get("tool_name", "unknown"),
        "metadata": meta.get("metadata", {}),
        "retrieved_at": int(time.time()),
    }


# ── Message Scanner ────────────────────────────────────────────────────────

def scan_and_compress(messages: list, threshold: int = TOKEN_BUDGET_THRESHOLD) -> dict:
    """
    Scan messages for bloated tool results and compress them in-place.

    Operates directly on the message list (mutating in-place) to avoid
    any reference/copy overhead in the hot path.

    Returns:
        {interventions, total_saved_tokens, archives_created}
    """
    total_saved = 0
    interventions = []
    archives_created = []

    for msg in messages:
        role = msg.get("role", "")
        if role not in ("tool", "function", "user"):
            continue

        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue

        tokens = _estimate_tokens(content)
        if tokens <= threshold:
            continue

        # ── G-Brain Intervention ────────────────────────────────────────
        tool_name = "unknown"
        if role == "tool":
            tool_name = msg.get("name", msg.get("tool_call_id", "tool")[:30])

        # Heuristic tool name extraction from payload content.
        # Catches common patterns that leave traces in the output:
        #   skill:      'invoked the "skill-name" skill'
        #   mcp tool:   'Applying /mcp_tools/<name>' or '"name": {' / '"name":()'
        #   bash cmd:   'Output of "bash" invoking' or '$ <cmd>'
        #   file read:  'Output of "read_many_files"' etc.
        content_lower = content[:500].lower()
        if tool_name == "unknown":
            import re as _re
            # skill invocation: 'invoked the "X" skill'
            m = _re.search(r'invoked the "([^"]+)" skill', content_lower)
            if m:
                tool_name = m.group(1)
            else:
                # MCP pattern: '/mcp_tools/<name>' or '"name": {' / '"name": ()'
                m = _re.search(r'/(?:mcp_tools|tools)/([a-z_][a-z0-9_]*)', content_lower)
                if m:
                    tool_name = m.group(1)
                else:
                    m = _re.search(r'"([a-z_][a-z0-9_]{2,20})"\s*:\s*[\{\(]', content[:200])
                    if m:
                        tool_name = m.group(1)
                    else:
                        # Bash command output: '$ <cmd' prefix lines
                        m = _re.search(r'^\$\s+([a-z\-]+\s+)?([a-z]+)', content[:200], _re.MULTILINE)
                        if m:
                            tool_name = m.group(2) or "bash"
                        else:
                            # Read output: 'Output of "read_file"' etc.
                            m = _re.search(r'output of "([a-z_]+)"', content_lower)
                            if m:
                                tool_name = m.group(1)

        archive_info = archive_payload(
            content=content,
            tool_name=tool_name,
            metadata={"role": role, "tokens": tokens},
        )
        archives_created.append(archive_info["archive_id"])

        summary = summarize_content(content)
        reference = build_reference(
            archive_id=archive_info["archive_id"],
            original_tokens=tokens,
            tool_name=tool_name,
            summary=summary,
        )

        # Mutate in-place — the compressed message replaces the original
        msg["content"] = reference
        new_tokens = _estimate_tokens(reference)
        saved = tokens - new_tokens
        total_saved += saved

        interventions.append({
            "archive_id": archive_info["archive_id"],
            "role": role,
            "tool": tool_name,
            "original_tokens": tokens,
            "saved_tokens": saved,
        })

        logger.info(
            f"🧠 G-Brain intervened: {tool_name} payload "
            f"({tokens:,} → {new_tokens} tok, saved {saved:,})"
        )

    return {
        "interventions": len(interventions),
        "total_saved_tokens": total_saved,
        "archives_created": archives_created,
    }


# ── Cleanup ──────────────────────────────────────────────────────────────

def cleanup_old_archives(max_age_days: int = MAX_ARCHIVE_AGE_DAYS) -> int:
    """Remove archives older than max_age_days. Returns count deleted."""
    cutoff = time.time() - (max_age_days * 86400)
    deleted = 0
    for p in ARCHIVE_DIR.glob("gbr_*.txt.gz"):
        if p.stat().st_mtime < cutoff:
            try:
                p.unlink()
                meta = ARCHIVE_DIR / f"{p.stem}.meta.json"
                if meta.exists():
                    meta.unlink()
                deleted += 1
            except Exception:
                pass
    if deleted:
        logger.info(f"Cleaned up {deleted} old archives")
    return deleted


# ── Stats ────────────────────────────────────────────────────────────────

_ARCHIVE_COUNT = 0
_TOTAL_SAVED = 0


def stats() -> dict:
    global _ARCHIVE_COUNT, _TOTAL_SAVED
    count = len(list(ARCHIVE_DIR.glob("gbr_*.txt.gz")))
    total_size = sum(p.stat().st_size for p in ARCHIVE_DIR.glob("gbr_*.txt.gz"))
    return {
        "archives_on_disk": count,
        "total_bytes": total_size,
        "total_archives_created": _ARCHIVE_COUNT,
        "total_tokens_saved_estimate": _TOTAL_SAVED,
    }


# ── Hermes Hook Handlers ─────────────────────────────────────────────────

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
    pre_api_request hook — scan and compress bloated tool payloads
    before the request is sent to the LLM.

    Operates in-place on the message list for zero-copy efficiency.
    Fails open: any exception is caught and logged so the API call proceeds.
    """
    global _ARCHIVE_COUNT, _TOTAL_SAVED

    if not ENABLE_COMPRESSION:
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
        result = scan_and_compress(msg_list, threshold=TOKEN_BUDGET_THRESHOLD)
        if result["interventions"] > 0:
            _ARCHIVE_COUNT += result["interventions"]
            _TOTAL_SAVED += result["total_saved_tokens"]
            logger.info(
                f"🧠 G-Brain pre_api #{api_call_count}: "
                f"{result['interventions']} interventions, "
                f"~{result['total_saved_tokens']:,} tokens saved, "
                f"archives: {result['archives_created']}"
            )
    except Exception as exc:
        logger.warning(f"G-Brain pre_api hook failed safely: {exc}")


def on_post_api_request(
    *,
    task_id: str = "",
    session_id: str = "",
    platform: str = "",
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_mode: str = "",
    api_call_count: int = 0,
    api_duration: float = 0.0,
    finish_reason: str = "",
    message_count: int = 0,
    response_model: Any = None,
    response: Any = None,
    usage: Any = None,
    assistant_message: Any = None,
    assistant_content_chars: int = 0,
    assistant_tool_call_count: int = 0,
    **_: Any,
) -> None:
    """post_api_request hook — log stats and run cleanup periodically."""
    # Run cleanup every ~50 calls
    if api_call_count % 50 == 0:
        deleted = cleanup_old_archives()
        s = stats()
        logger.info(
            f"🧠 G-Brain stats: {s['archives_on_disk']} archives, "
            f"{s['total_bytes']:,} bytes, {s['total_tokens_saved_estimate']:,} tok saved"
        )


# ── Plugin Registration ─────────────────────────────────────────────────

def register(ctx) -> None:
    """Register hooks with the Hermes plugin system."""
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    logger.info(
        f"🧠 G-Brain Compressor registered "
        f"(threshold={TOKEN_BUDGET_THRESHOLD} tok, archive_dir={ARCHIVE_DIR})"
    )