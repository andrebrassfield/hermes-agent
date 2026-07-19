"""Regression tests for cron routing_chain fallback (t_5102d907).

Bug fix from 2026-07-19 dream-cycle-brain 04:01 CDT incident: jobs.json
declared a `routing_chain` (3 providers) but the dispatch hot path ignored
it — a `RuntimeError: Connection error.` from the primary did NOT escalate
to any fallback.

These tests lock the new behaviour:

  - `_should_escalate_via_routing_chain` accepts only jobs that declare a
    non-empty routing_chain with escalation_on != "never", AND whose error
    matches one of the retry-eligible patterns (connection errors, timeouts,
    rate limits, 5xx, auth/quota).
  - `_routing_chain_retry` retries the primary once, then walks the declared
    chain. Returns the first successful result, or the primary error + a
    full attempts_log when the chain is exhausted.
  - `run_one_job` consults the routing_chain on primary failure, swaps in
    a fallback result if one succeeds, and fires a Telegram exhaustion
    alert via `_fire_chain_exhaustion_alert` when the chain runs out.

All tests patch `cron.scheduler.run_job` (the shared body called by both
the built-in ticker and any external scheduler provider) so the chain
replay exercises the real retry/orchestration code without burning real
inference credits.
"""
from __future__ import annotations

import cron.scheduler as s


# ---------------------------------------------------------------------------
# _should_escalate_via_routing_chain
# ---------------------------------------------------------------------------


def test_escalate_gate_accepts_chain_with_eligible_error():
    """Connection-error fails primary; job declares a 2-step chain;
    escalation_on = failure_or_low_quality → escalate."""
    job = {
        "id": "j1",
        "name": "nightly",
        "provider": "minimax",
        "model": "MiniMax-M3",
        "routing_chain": [
            {"provider": "opencode-go", "model": "mimo-v2.5"},
            {"provider": "minimaxai", "model": "minimax-m3"},
        ],
        "escalation_on": "failure_or_low_quality",
    }
    assert s._should_escalate_via_routing_chain(
        job, "RuntimeError: Connection error."
    ) is True


def test_escalate_gate_rejects_when_escalation_on_is_never():
    """Operator chose to NOT escalate; routing_chain declared but inert."""
    job = {
        "id": "j2",
        "routing_chain": [{"provider": "opencode-go", "model": "mimo-v2.5"}],
        "escalation_on": "never",
    }
    assert s._should_escalate_via_routing_chain(
        job, "RuntimeError: Connection error."
    ) is False


def test_escalate_gate_rejects_when_chain_is_missing():
    """Legacy job without routing_chain → no escalation, regardless of error."""
    job = {"id": "j3", "escalation_on": "failure_or_low_quality"}
    assert s._should_escalate_via_routing_chain(
        job, "RuntimeError: Connection error."
    ) is False


def test_escalate_gate_rejects_permanent_error_patterns():
    """Permanent prompt/skill errors must NOT trigger a retry — re-running
    against a different provider just spends money to fail again."""
    job = {
        "id": "j4",
        "routing_chain": [{"provider": "opencode-go", "model": "mimo-v2.5"}],
        "escalation_on": "failure_or_low_quality",
    }
    # CronPromptInjectionBlocked-style errors are NOT in the eligible list
    assert s._should_escalate_via_routing_chain(
        job, "CronPromptInjectionBlocked: matched skill-injection pattern"
    ) is False
    # Empty error → no escalation
    assert s._should_escalate_via_routing_chain(job, "") is False
    assert s._should_escalate_via_routing_chain(job, "") is False


def test_escalate_gate_matches_rate_limit_and_5xx():
    """The eligible-error list covers rate limits, 429, and 5xx — these are
    the patterns a different provider can plausibly recover from."""
    job = {
        "id": "j5",
        "routing_chain": [{"provider": "opencode-go", "model": "mimo-v2.5"}],
        "escalation_on": "failure",
    }
    for err in [
        " 429 Too Many Requests",
        " 503 Service Unavailable",
        "weekly usage limit exceeded",
        "ReadTimeout: HTTPSConnectionPool",
        "BadGateway: upstream returned 502",
    ]:
        assert s._should_escalate_via_routing_chain(job, err) is True, f"expected escalate for {err!r}"


# ---------------------------------------------------------------------------
# _routing_chain_retry
# ---------------------------------------------------------------------------


def test_routing_chain_retry_succeeds_on_primary_retry(monkeypatch):
    """Bug-class scenario: primary failed at run_one_job's outer call,
    primary RETRY inside _routing_chain_retry then succeeds (transient
    hiccup clears). No chain entry should be tried."""
    calls = []

    def fake_run_job(j, *, defer_agent_teardown=None):
        calls.append(dict(j))
        n = len(calls)
        if n == 1:
            # The primary RETRY inside _routing_chain_retry succeeds
            # on the first attempt — a transient hiccup cleared.
            return True, "out-recovered", "ok", None
        # Defensive: chain must not be consulted on this path.
        raise AssertionError(f"unexpected run_job call #{n}")

    monkeypatch.setattr(s, "run_job", fake_run_job)
    job = {
        "id": "rt1",
        "name": "rt1",
        "provider": "minimax",
        "model": "MiniMax-M3",
        "routing_chain": [
            {"provider": "opencode-go", "model": "mimo-v2.5"},
        ],
        "escalation_on": "failure_or_low_quality",
    }
    ok, out, final, err, attempts = s._routing_chain_retry(
        job, primary_error="RuntimeError: Connection error.", defer_agent_teardown=[]
    )
    assert ok is True
    assert final == "ok"
    # Exactly one attempt recorded (the primary retry that succeeded);
    # chain was not consulted.
    assert len(attempts) == 1
    assert attempts[0]["step"] == "primary_retry"
    assert attempts[0]["ok"] is True
    assert len(calls) == 1
    # Original job passed unchanged on the retry (no chain entry inserted)
    assert calls[0]["provider"] == "minimax"


def test_routing_chain_retry_walks_chain_in_order(monkeypatch):
    """Primary fails twice (retry + first chain), second chain entry
    succeeds. Verify: attempts log records every step, providers visited
    in declared order, final tuple reflects the success."""
    calls = []

    def fake_run_job(j, *, defer_agent_teardown=None):
        calls.append(dict(j))
        n = len(calls)
        if n == 1:
            # Primary retry fails
            return False, "", "", "RuntimeError: Connection error."
        if n == 2:
            # First chain entry fails
            return False, "", "", "RuntimeError: timeout"
        # Second chain entry succeeds
        return True, "out-chain2", "chain-2 final", None

    monkeypatch.setattr(s, "run_job", fake_run_job)
    job = {
        "id": "rt2",
        "name": "rt2",
        "provider": "minimax",
        "model": "MiniMax-M3",
        "routing_chain": [
            {"provider": "opencode-go", "model": "mimo-v2.5"},
            {"provider": "minimaxai", "model": "minimax-m3"},
        ],
        "escalation_on": "failure_or_low_quality",
    }
    ok, out, final, err, attempts = s._routing_chain_retry(
        job, primary_error="RuntimeError: Connection error.", defer_agent_teardown=[]
    )
    assert ok is True
    assert final == "chain-2 final"
    assert len(attempts) == 3
    assert attempts[0]["step"] == "primary_retry"
    assert attempts[0]["ok"] is False
    assert attempts[1]["step"] == "chain:0"
    assert attempts[1]["provider"] == "opencode-go"
    assert attempts[1]["ok"] is False
    assert attempts[2]["step"] == "chain:1"
    assert attempts[2]["provider"] == "minimaxai"
    assert attempts[2]["ok"] is True
    # Providers visited in declared order
    assert [c["provider"] for c in calls] == [
        "minimax",  # primary retry
        "opencode-go",  # chain entry 0
        "minimaxai",  # chain entry 1
    ]
    # Chain retry must NOT carry the chain (no infinite recursion)
    assert "routing_chain" not in calls[1]
    assert "routing_chain" not in calls[2]


def test_routing_chain_retry_exhaustion_returns_primary_error(monkeypatch):
    """Primary retry fails, every chain entry fails → result is the
    primary error and the attempts log has full history."""
    def fake_run_job(j, *, defer_agent_teardown=None):
        return False, "", "", "RuntimeError: Connection error."

    monkeypatch.setattr(s, "run_job", fake_run_job)
    job = {
        "id": "rt3",
        "name": "rt3",
        "provider": "minimax",
        "model": "MiniMax-M3",
        "routing_chain": [
            {"provider": "opencode-go", "model": "mimo-v2.5"},
            {"provider": "minimax-oauth", "model": "MiniMax-M2.5"},
        ],
        "escalation_on": "failure_or_low_quality",
    }
    ok, out, final, err, attempts = s._routing_chain_retry(
        job, primary_error="RuntimeError: Connection error.", defer_agent_teardown=[]
    )
    assert ok is False
    assert err == "RuntimeError: Connection error."
    assert len(attempts) == 3
    assert all(a["ok"] is False for a in attempts)


def test_routing_chain_retry_swallows_run_job_exceptions(monkeypatch):
    """If run_job raises (rather than returning a failure tuple), the
    helper must convert that into a failed attempt and continue. Here:
    primary retry raises (counted as a failed attempt), then the chain
    entry succeeds."""
    call_count = {"n": 0}

    def fake_run_job(j, *, defer_agent_teardown=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom-from-primary")
        return True, "ok-out", "ok-final", None

    monkeypatch.setattr(s, "run_job", fake_run_job)
    job = {
        "id": "rt4",
        "name": "rt4",
        "provider": "minimax",
        "model": "MiniMax-M3",
        "routing_chain": [{"provider": "minimaxai", "model": "minimax-m3"}],
        "escalation_on": "failure_or_low_quality",
    }
    ok, out, final, err, attempts = s._routing_chain_retry(
        job, primary_error="", defer_agent_teardown=[]
    )
    assert ok is True
    # Primary retry raised → recorded as failed attempt 0; chain entry
    # succeeded → recorded as successful attempt 1. Helper must not
    # propagate the original exception.
    assert len(attempts) == 2
    assert attempts[0]["step"] == "primary_retry"
    assert attempts[0]["ok"] is False
    assert "boom-from-primary" in (attempts[0]["error"] or "")
    assert attempts[1]["step"] == "chain:0"
    assert attempts[1]["ok"] is True


# ---------------------------------------------------------------------------
# _fire_chain_exhaustion_alert
# ---------------------------------------------------------------------------


def test_chain_exhaustion_alert_routes_to_telegram_dm(monkeypatch):
    """On chain exhaustion, alert must target Telegram with the job's
    origin chat_id (falling back to Dre's main DM 6598264778)."""
    captured = {}

    from enum import Enum

    class _PlatformEnum(Enum):
        TELEGRAM = "telegram"

    class _FakePconfig:
        enabled = True

    class _FakeConfig:
        platforms = {_PlatformEnum.TELEGRAM: _FakePconfig()}

    import gateway.config as gwc
    monkeypatch.setattr(gwc, "Platform", _PlatformEnum)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: _FakeConfig())

    async def fake_send(platform, pconfig, chat_id, message, **kwargs):
        captured["platform"] = platform
        captured["chat_id"] = chat_id
        captured["message"] = message
        return None

    import tools.send_message_tool as smt
    monkeypatch.setattr(smt, "_send_to_platform", fake_send)

    job = {
        "id": "dream-cycle-brain",
        "name": "dream-cycle-brain",
        "origin": {"platform": "telegram", "chat_id": "6598264778"},
        "schedule_display": "0 4 * * *",
        "routing_chain": [
            {"provider": "opencode-go", "model": "mimo-v2.5"},
            {"provider": "minimax-oauth", "model": "MiniMax-M2.5"},
            {"provider": "minimaxai", "model": "minimax-m3"},
        ],
    }
    attempts = [
        {"step": "primary_retry", "provider": "minimax", "model": "MiniMax-M3",
         "ok": False, "error": "RuntimeError: Connection error.", "elapsed_ms": 1500},
        {"step": "chain:0", "provider": "opencode-go", "model": "mimo-v2.5",
         "ok": False, "error": "RuntimeError: Connection error.", "elapsed_ms": 2100},
        {"step": "chain:1", "provider": "minimax-oauth", "model": "MiniMax-M2.5",
         "ok": False, "error": "RuntimeError: timeout", "elapsed_ms": 30000},
        {"step": "chain:2", "provider": "minimaxai", "model": "minimax-m3",
         "ok": False, "error": "RuntimeError: Connection error.", "elapsed_ms": 1800},
    ]
    result = s._fire_chain_exhaustion_alert(job, attempts)
    assert result is None, f"expected None (success), got {result!r}"
    assert captured["chat_id"] == "6598264778"
    assert "dream-cycle-brain" in captured["message"]
    assert "EXHAUSTED" in captured["message"] or "exhausted" in captured["message"]
    assert "opencode-go" in captured["message"]
    assert "minimax-m3" in captured["message"]


def test_chain_exhaustion_alert_falls_back_to_dre_dm_when_origin_missing(monkeypatch):
    """CLI-created jobs without an origin must still alert — falling back
    to Dre's main DM 6598264778."""
    captured = {}

    from enum import Enum

    class _PlatformEnum(Enum):
        TELEGRAM = "telegram"

    class _FakePconfig:
        enabled = True

    class _FakeConfig:
        platforms = {_PlatformEnum.TELEGRAM: _FakePconfig()}

    import gateway.config as gwc
    monkeypatch.setattr(gwc, "Platform", _PlatformEnum)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: _FakeConfig())

    async def fake_send(platform, pconfig, chat_id, message, **kwargs):
        captured["chat_id"] = chat_id
        return None

    import tools.send_message_tool as smt
    monkeypatch.setattr(smt, "_send_to_platform", fake_send)

    job = {"id": "orphan", "name": "orphan"}  # no origin
    attempts = [{"step": "primary_retry", "provider": "p", "model": "m",
                 "ok": False, "error": "x", "elapsed_ms": 1}]
    result = s._fire_chain_exhaustion_alert(job, attempts)
    assert result is None
    assert captured["chat_id"] == "6598264778"


def test_chain_exhaustion_alert_handles_send_failure(monkeypatch):
    """If the Telegram send fails, the helper returns a short error string
    instead of raising — the caller's run_one_job records it without
    crashing the cron pipeline."""

    class _PEnum:
        TELEGRAM = "telegram"

        def __call__(self, value):
            raise ValueError("bad platform")

    import gateway.config as gwc
    monkeypatch.setattr(gwc, "Platform", _PEnum)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: object())

    job = {
        "id": "x",
        "name": "x",
        "origin": {"platform": "telegram", "chat_id": "6598264778"},
    }
    attempts = []
    result = s._fire_chain_exhaustion_alert(job, attempts)
    # Returns a string error; never raises.
    assert isinstance(result, str)
    assert "alert failed" in result or "platform" in result


# ---------------------------------------------------------------------------
# run_one_job wiring — confirm the dispatcher consults the routing_chain
# ---------------------------------------------------------------------------


def test_run_one_job_invokes_chain_when_primary_fails(monkeypatch):
    """End-to-end through run_one_job: primary fails, primary retry inside
    _routing_chain_retry also fails, then chain entry succeeds → run_one_job
    returns True with the chain's output. Verify the chain call's provider
    was overridden to the chain entry's provider."""
    rc_calls = []

    def fake_run_job(j, *, defer_agent_teardown=None):
        rc_calls.append(dict(j))
        n = len(rc_calls)
        if n == 1:
            # Primary run fails
            return False, "primary-out", "", "RuntimeError: Connection error."
        if n == 2:
            # Primary retry inside _routing_chain_retry also fails
            return False, "primary-retry-out", "", "RuntimeError: Connection error."
        # Chain entry succeeds — provider should be the chain entry's
        return True, "chain-out", "chain-final-response", None

    def fake_save(jid, out):
        return f"/tmp/{jid}.txt"

    def fake_deliver(job, content, adapters=None, loop=None):
        return None

    mark_log: list[tuple] = []

    def fake_mark(jid, ok, err=None, delivery_error=None):
        mark_log.append((jid, ok, err))

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    # No-op the alert so the test doesn't depend on Telegram being up.
    monkeypatch.setattr(
        s, "_fire_chain_exhaustion_alert", lambda job, attempts: None
    )

    job = {
        "id": "rt_end_to_end",
        "name": "rt_e2e",
        "provider": "minimax",
        "model": "MiniMax-M3",
        "origin": {"platform": "telegram", "chat_id": "6598264778"},
        "routing_chain": [
            {"provider": "minimaxai", "model": "minimax-m3"},
        ],
        "escalation_on": "failure_or_low_quality",
        "schedule_display": "0 4 * * *",
    }
    ok = s.run_one_job(job)
    assert ok is True
    # mark_job_run was called with ok=True (the chain recovered)
    assert len(mark_log) == 1
    assert mark_log[0][1] is True
    assert mark_log[0][2] is None
    # 3 run_job calls: primary failure + primary-retry failure + chain success
    assert len(rc_calls) == 3
    # The chain call's provider is the chain entry's provider (minimaxai),
    # not the original job's provider (minimax).
    assert rc_calls[1]["provider"] == "minimax"  # primary retry, no override
    assert rc_calls[2]["provider"] == "minimaxai"  # chain entry, overridden
    # Chain retry must NOT carry the chain (no infinite recursion)
    assert "routing_chain" not in rc_calls[2]


def test_run_one_job_skips_chain_for_no_agent_jobs(monkeypatch):
    """no_agent=True jobs (script-only watchdogs) must NOT trigger the
    routing_chain retry — they never had an LLM call to retry."""
    calls = []

    def fake_run_job(j, *, defer_agent_teardown=None):
        calls.append(j.get("id"))
        return False, "out", "", "script failed"

    def fake_save(jid, out):
        return f"/tmp/{jid}.txt"

    mark_log: list[tuple] = []

    def fake_mark(jid, ok, err=None, delivery_error=None):
        mark_log.append((jid, ok, err))

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "_deliver_result", lambda *a, **kw: None)
    monkeypatch.setattr(s, "mark_job_run", fake_mark)

    job = {
        "id": "watchdog",
        "name": "watchdog",
        "no_agent": True,
        "script": "/tmp/never-runs.sh",
        "routing_chain": [{"provider": "minimaxai", "model": "minimax-m3"}],
        "escalation_on": "failure_or_low_quality",
    }
    s.run_one_job(job)
    # Only the original run_job call — no chain retry
    assert len(calls) == 1
    # mark_job_run was called with ok=False (script failed)
    assert len(mark_log) == 1
    assert mark_log[0][1] is False


def test_run_one_job_does_not_escalate_when_escalation_on_is_never(monkeypatch):
    """escalation_on='never' means: even if a chain is declared, do not
    retry. Verifies the gate at the run_one_job level."""
    calls = []

    def fake_run_job(j, *, defer_agent_teardown=None):
        calls.append(j.get("id"))
        return False, "out", "", "RuntimeError: Connection error."

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", lambda *a, **kw: None)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **kw: None)
    monkeypatch.setattr(
        s, "_fire_chain_exhaustion_alert", lambda *a, **kw: None
    )

    job = {
        "id": "never-escalate",
        "name": "never-escalate",
        "provider": "minimax",
        "model": "MiniMax-M3",
        "routing_chain": [{"provider": "minimaxai", "model": "minimax-m3"}],
        "escalation_on": "never",
    }
    s.run_one_job(job)
    # Single run_job call; chain NOT consulted.
    assert len(calls) == 1
