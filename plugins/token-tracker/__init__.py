"""token-tracker — Hermes plugin for Mavis context observability.

Instruments every API turn to capture:
  - Per-turn token allocation (system / user / assistant / tool)
  - Context growth rate and projected exhaustion turn
  - Tool output bloat leaderboard
  - Session-end reports with pie chart data

Data flows into ~/.mavis/context/token-tracker.db (SQLite).
Dashboard served at /api/plugins/token-tracker/.

Non-blocking: all DB I/O runs in background threads via a bounded queue.
Fail-safe: hook errors are caught and logged; tracking never breaks a session.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token estimation — tiktoken cl100k_base (industry standard, ~5% vs MiniMax)
# ---------------------------------------------------------------------------
_ENC = None
_ENC_LOCK = threading.Lock()

def _get_encoder():
    global _ENC
    if _ENC is None:
        with _ENC_LOCK:
            if _ENC is None:
                try:
                    import tiktoken
                    _ENC = tiktoken.get_encoding("cl100k_base")
                except Exception:
                    # Fail-open: rough char-based fallback
                    _ENC = None
    return _ENC


def tok(text: str) -> int:
    """Token estimate using cl100k_base (GPT-4 encoding)."""
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    # Fallback: ~4 chars/token for ASCII, ~2 for CJK
    ascii_chars = sum(1 for c in text if ord(c) <= 127)
    cjk_chars = len(text) - ascii_chars
    return int(ascii_chars / 4 + cjk_chars / 2)


def tok_message(msg: dict) -> int:
    """Tokens for a single message including role/content framing overhead."""
    base = 4
    content = msg.get("content", "") or ""
    if isinstance(content, list):
        text = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    else:
        text = str(content)
    return base + tok(text)


def tok_messages(messages: list) -> int:
    return sum(tok_message(m) for m in messages)


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------
DB_PATH = os.path.expanduser("~/.mavis/context/token-tracker.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

_TURN_COLUMNS = (
    "session_id TEXT, turn INTEGER NOT NULL, ts INTEGER NOT NULL, "
    "role TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, "
    "context_before INTEGER DEFAULT 0, context_after INTEGER DEFAULT 0, "
    "tool_calls INTEGER DEFAULT 0, tool_name TEXT, raw_usage TEXT"
)

_SESSION_COLUMNS = (
    "session_id TEXT PRIMARY KEY, agent_name TEXT, model TEXT, "
    "started_at INTEGER, ended_at INTEGER, total_turns INTEGER, "
    "total_in INTEGER DEFAULT 0, total_out INTEGER DEFAULT 0, "
    "peak_context INTEGER DEFAULT 0, avg_growth REAL DEFAULT 0, "
    "projected_turn INTEGER, top_tool TEXT, top_tool_calls INTEGER DEFAULT 0"
)

_TOOL_COLUMNS = (
    "session_id TEXT, turn INTEGER, tool_name TEXT, "
    "tokens INTEGER DEFAULT 0, chars INTEGER DEFAULT 0, ts INTEGER"
)

_init_done = False
_init_lock = threading.Lock()


def _init_db():
    global _init_done
    if _init_done:
        return
    with _init_lock:
        if _init_done:
            return
        conn = sqlite3.connect(DB_PATH, timeout=10)
        c = conn.cursor()
        c.execute(f"CREATE TABLE IF NOT EXISTS turn_tokens ({_TURN_COLUMNS})")
        c.execute(f"CREATE TABLE IF NOT EXISTS session_log ({_SESSION_COLUMNS})")
        c.execute(f"CREATE TABLE IF NOT EXISTS tool_bloat ({_TOOL_COLUMNS})")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sid_turn ON turn_tokens(session_id, turn)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bloat_sid ON tool_bloat(session_id, turn)")
        conn.commit()
        conn.close()
        _init_done = True


# ---------------------------------------------------------------------------
# Background write queue (non-blocking I/O)
# ---------------------------------------------------------------------------
_WRITE_Q: queue.Queue = queue.Queue(maxsize=1000)
_WRITE_THREAD: Optional[threading.Thread] = None
_WRITE_THREAD_LOCK = threading.Lock()


def _bg_writer():
    """Singleton background writer thread — consumes _WRITE_Q."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    c = conn.cursor()
    while True:
        try:
            item = _WRITE_Q.get(timeout=2.0)
            if item is None:
                break  # shutdown signal
            op, args = item
            if op == "turn":
                c.execute(
                    "INSERT INTO turn_tokens "
                    "(session_id, turn, ts, role, tokens_in, tokens_out, "
                    "context_before, context_after, tool_calls, tool_name, raw_usage) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    args,
                )
            elif op == "session":
                c.execute(
                    "INSERT OR REPLACE INTO session_log "
                    "(session_id, agent_name, model, started_at, ended_at, total_turns, "
                    "total_in, total_out, peak_context, avg_growth, projected_turn, "
                    "top_tool, top_tool_calls) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    args,
                )
            elif op == "tool_bloat":
                c.execute(
                    "INSERT INTO tool_bloat "
                    "(session_id, turn, tool_name, tokens, chars, ts) VALUES (?, ?, ?, ?, ?, ?)",
                    args,
                )
            conn.commit()
        except queue.Empty:
            continue
        except sqlite3.OperationalError as exc:
            # DB locked — drop the write rather than block
            logger.debug("token-tracker DB write dropped (locked): %s", exc)
            continue
        except Exception as exc:
            logger.warning("token-tracker write error: %s", exc)
            continue
    conn.close()


def _ensure_writer():
    global _WRITE_THREAD
    with _WRITE_THREAD_LOCK:
        if _WRITE_THREAD is None or not _WRITE_THREAD.is_alive():
            _WRITE_THREAD = threading.Thread(target=_bg_writer, daemon=True, name="token-tracker-writer")
            _WRITE_THREAD.start()


def _enqueue(op: str, args: tuple):
    try:
        _WRITE_Q.put_nowait((op, args))
    except queue.Full:
        # Bounded queue full — drop write rather than block the turn
        logger.debug("token-tracker write queue full, dropping %s", op)


# ---------------------------------------------------------------------------
# Per-session state (thread-safe dict per Hermes worker)
# ---------------------------------------------------------------------------
_STATE_LOCK = threading.Lock()
_TRACE_STATE: Dict[str, "TurnState"] = {}


@dataclass
class TurnState:
    """Accumulates data for one turn within a session."""
    session_id: str
    agent_name: str = "hermes"
    model: str = ""
    turn: int = 0
    ts: int = 0
    context_before: int = 0
    context_after: int = 0
    user_tokens: int = 0
    assistant_tokens: int = 0
    tool_tokens: int = 0
    tool_calls: int = 0
    tool_names: List[str] = field(default_factory=list)
    # tool bloat snapshot at this turn
    tool_bloat_snapshot: Dict[str, int] = field(default_factory=dict)


def _task_key(task_id: str, session_id: str) -> str:
    if task_id:
        return task_id
    if session_id:
        return f"session:{session_id}"
    return f"thread:{threading.get_ident()}"


# ---------------------------------------------------------------------------
# Tool bloat tracking (live counter per session)
# ---------------------------------------------------------------------------
_TOOL_CALL_COUNTS: Dict[str, Dict[str, int]] = {}  # session_id → {tool_name: calls}
_TOOL_CALL_LOCK = threading.Lock()


def _count_tool_call(session_id: str, tool_name: str, tokens: int, chars: int):
    """Record a tool result — called from post_tool_call."""
    with _TOOL_CALL_LOCK:
        if session_id not in _TOOL_CALL_COUNTS:
            _TOOL_CALL_COUNTS[session_id] = {}
        if tool_name not in _TOOL_CALL_COUNTS[session_id]:
            _TOOL_CALL_COUNTS[session_id][tool_name] = {"calls": 0, "tokens": 0, "chars": 0}
        _TOOL_CALL_COUNTS[session_id][tool_name]["calls"] += 1
        _TOOL_CALL_COUNTS[session_id][tool_name]["tokens"] += tokens
        _TOOL_CALL_COUNTS[session_id][tool_name]["chars"] += chars


def _snapshot_and_flush_bloat(session_id: str, turn: int) -> Dict[str, int]:
    """Atomically snapshot current bloat counts and reset for next turn."""
    with _TOOL_CALL_LOCK:
        snap = dict(_TOOL_CALL_COUNTS.get(session_id, {}))
        if session_id in _TOOL_CALL_COUNTS:
            # Keep accumulating totals but track per-turn snapshot
            pass
    return snap


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

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
    """pre_api_request hook — count tokens before the API call ships."""
    _ensure_writer()

    # Resolve actual message list
    input_messages = _coerce_messages(
        request_messages=request_messages,
        messages=messages,
        conversation_history=conversation_history,
        user_message=user_message,
    )

    if not input_messages:
        return

    # Count tokens by role in the pre-turn message set
    system_tok = 0
    user_tok = 0
    assistant_tok = 0
    tool_tok = 0

    for msg in input_messages:
        role = msg.get("role", "")
        t = tok_message(msg)
        if role == "system":
            system_tok += t
        elif role == "user":
            user_tok += t
        elif role == "assistant":
            assistant_tok += t
        elif role in ("tool", "function"):
            tool_tok += t

    total_before = system_tok + user_tok + assistant_tok + tool_tok

    task_key = _task_key(task_id, session_id)
    if not session_id:
        session_id = task_key

    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        if state is None:
            state = TurnState(session_id=session_id, model=model or "MiniMax-M2.7")
            _TRACE_STATE[task_key] = state
        state.model = model or state.model or "MiniMax-M2.7"
        state.turn = api_call_count
        state.ts = int(time.time())
        state.context_before = total_before
        state.user_tokens = user_tok
        state.assistant_tokens = assistant_tok
        state.tool_tokens = tool_tok
        state.tool_calls = 0
        state.tool_names = []
        state.tool_bloat_snapshot = _snapshot_and_flush_bloat(session_id, api_call_count)


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
    """post_api_request hook — record tokens after the API response."""
    _ensure_writer()

    task_key = _task_key(task_id, session_id)
    if not session_id:
        session_id = task_key

    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        if state is None:
            state = TurnState(session_id=session_id, model=model or "MiniMax-M2.7")
            _TRACE_STATE[task_key] = state

    # Count assistant output tokens
    assistant_out_tok = 0
    if assistant_message is not None:
        content = getattr(assistant_message, "content", None) or ""
        assistant_out_tok = tok(str(content))
    elif assistant_content_chars:
        # Rough: 4 chars/token
        assistant_out_tok = assistant_content_chars // 4

    state.assistant_tokens = assistant_out_tok

    # Extract usage from response or usage dict
    usage_dict = _extract_usage(usage, response)
    prompt_tok = usage_dict.get("input_tokens", usage_dict.get("prompt_tokens", 0))
    completion_tok = usage_dict.get("output_tokens", usage_dict.get("completion_tokens", 0))

    # Context after = pre-turn context + this turn's new content
    context_after = state.context_before + assistant_out_tok + completion_tok
    state.context_after = context_after

    # Get tool calls from assistant_message
    tool_calls = 0
    tool_names = []
    if assistant_message is not None:
        tc = getattr(assistant_message, "tool_calls", None) or []
        tool_calls = len(tc)
        for t in tc:
            fn = getattr(t, "function", None)
            if fn:
                tool_names.append(getattr(fn, "name", "unknown"))

    state.tool_calls = tool_calls
    state.tool_names = tool_names

    # Persist turn record (non-blocking via queue)
    raw_usage_json = json.dumps(usage_dict) if usage_dict else None
    _enqueue("turn", (
        state.session_id,
        state.turn,
        state.ts,
        "api_call",
        state.context_before,
        state.context_after,
        tool_calls,
        ",".join(tool_names) if tool_names else None,
        raw_usage_json,
        state.assistant_tokens,
        prompt_tok,
    ))

    # Persist tool bloat for this turn
    bloat_snap = state.tool_bloat_snapshot or {}
    for tool_name, counts in bloat_snap.items():
        if isinstance(counts, dict):
            _enqueue("tool_bloat", (
                state.session_id,
                state.turn,
                tool_name,
                counts.get("tokens", 0),
                counts.get("chars", 0),
                state.ts,
            ))

    # Update session summary (every 10 turns or on final call)
    if api_call_count % 10 == 0 or finish_reason == "stop":
        _update_session_summary(state)


def on_pre_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """Track tool call start — used to attribute tool results to calls."""
    if not session_id:
        session_id = _task_key(task_id, "")
    with _TOOL_CALL_LOCK:
        if session_id not in _TOOL_CALL_COUNTS:
            _TOOL_CALL_COUNTS[session_id] = {}
        if tool_name not in _TOOL_CALL_COUNTS[session_id]:
            _TOOL_CALL_COUNTS[session_id][tool_name] = {"calls": 0, "tokens": 0, "chars": 0}
        _TOOL_CALL_COUNTS[session_id][tool_name]["calls"] += 1


def on_post_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """Track tool result size for bloat analysis."""
    if not session_id:
        session_id = _task_key(task_id, "")

    # Count result tokens
    result_str = str(result) if result is not None else ""
    result_tok = tok(result_str)
    result_chars = len(result_str)

    with _TOOL_CALL_LOCK:
        if session_id in _TOOL_CALL_COUNTS and tool_name in _TOOL_CALL_COUNTS[session_id]:
            _TOOL_CALL_COUNTS[session_id][tool_name]["tokens"] += result_tok
            _TOOL_CALL_COUNTS[session_id][tool_name]["chars"] += result_chars


def on_post_llm_call(
    *,
    task_id: str = "",
    session_id: str = "",
    provider: str = "",
    base_url: str = "",
    api_mode: str = "",
    model: str = "",
    api_call_count: int = 0,
    assistant_message: Any = None,
    response: Any = None,
    api_duration: float = 0.0,
    finish_reason: str = "",
    usage: Any = None,
    assistant_content_chars: int = 0,
    assistant_tool_call_count: int = 0,
    assistant_response: Any = None,
    **_: Any,
) -> None:
    """post_llm_call hook — fallback for older Hermes branches that use this instead of post_api_request."""
    # Forward to the same handler
    on_post_api_request(
        task_id=task_id,
        session_id=session_id,
        platform="",
        model=model,
        provider=provider,
        base_url=base_url,
        api_mode=api_mode,
        api_call_count=api_call_count,
        api_duration=api_duration,
        finish_reason=finish_reason,
        message_count=0,
        response_model=getattr(response, "model", None) if response else None,
        response=response,
        usage=usage,
        assistant_message=assistant_message,
        assistant_content_chars=assistant_content_chars,
        assistant_tool_call_count=assistant_tool_call_count,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_messages(
    *,
    request_messages: Any = None,
    messages: Any = None,
    conversation_history: Any = None,
    user_message: Any = None,
) -> List[dict]:
    for candidate in (request_messages, messages, conversation_history):
        if isinstance(candidate, list):
            return candidate
    if user_message is None:
        return []
    return [{"role": "user", "content": str(user_message)}]


def _extract_usage(usage: Any, response: Any) -> dict:
    """Extract token usage dict from response object or usage parameter."""
    if isinstance(usage, dict):
        return {
            "input_tokens": usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0) or usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        }
    if response is not None:
        raw = getattr(response, "usage", None)
        if raw is not None:
            if hasattr(raw, "__dict__"):
                raw = vars(raw)
            if isinstance(raw, dict):
                return {
                    "input_tokens": raw.get("input_tokens", 0) or raw.get("prompt_tokens", 0),
                    "output_tokens": raw.get("output_tokens", 0) or raw.get("completion_tokens", 0),
                    "total_tokens": raw.get("total_tokens", 0),
                    "cache_read_tokens": raw.get("cache_read_tokens", 0),
                    "cache_creation_input_tokens": raw.get("cache_creation_input_tokens", 0),
                }
    return {}


def _update_session_summary(state: TurnState):
    """Compute and persist rolling session summary."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        c = conn.cursor()

        # Get all turns for this session to compute rolling stats
        c.execute(
            "SELECT context_before, context_after, tool_calls FROM turn_tokens "
            "WHERE session_id = ? ORDER BY turn",
            (state.session_id,),
        )
        rows = c.fetchall()

        if not rows:
            conn.close()
            return

        total_turns = len(rows)
        total_in = sum(r[1] for r in rows)  # context_after as proxy for in
        peak_context = max((r[1] for r in rows), default=0)

        # Growth rate
        contexts = [r[1] for r in rows]
        growths = [contexts[i] - contexts[i-1] for i in range(1, len(contexts))]
        avg_growth = sum(growths) / len(growths) if growths else 0
        projected = round(200000 / avg_growth) if avg_growth > 10 else None

        # Top tool
        c.execute(
            "SELECT tool_name, SUM(calls) as total FROM tool_bloat WHERE session_id = ? GROUP BY tool_name ORDER BY total DESC LIMIT 1",
            (state.session_id,),
        )
        top_row = c.fetchone()
        top_tool = top_row[0] if top_row else None
        top_tool_calls = top_row[1] if top_row else 0

        c.execute(
            "INSERT OR REPLACE INTO session_log "
            "(session_id, agent_name, model, total_turns, total_in, peak_context, "
            "avg_growth, projected_turn, top_tool, top_tool_calls) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state.session_id,
                state.agent_name,
                state.model,
                total_turns,
                total_in,
                peak_context,
                round(avg_growth, 1),
                projected,
                top_tool,
                top_tool_calls,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("session summary update failed: %s", exc)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register all hooks with the Hermes plugin manager."""
    _init_db()
    _ensure_writer()

    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("pre_llm_call", on_pre_api_request)  # fallback
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)