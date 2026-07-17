"""Parametrised invariant test for the model-field sweep (Bug #6 bug class).

Bug class mirror of test_resolve_runtime_provider_pool_entry_includes_model
(2026-07-12, line 48 of test_runtime_provider_resolution.py) and
test_resolve_openrouter_runtime_surfaces_model (2026-07-16, line 3416).
The pool path was fixed at 0bf17dfd7; the openrouter-fallback resolver
was fixed at 05e95e0b1. The remaining inline-fallback return-dicts in
resolve_runtime_provider() also drop the "model" key from their returned
dict, so callers using the singleton-fallback paths for the OAuth and
api_key providers hit the upstream API with an empty model and get HTTP
401 / missing-model back.

This test pins the invariant for every remaining path: every
resolve_runtime_provider() return that surfaces a usable runtime MUST
include a non-empty "model" field. The test contract is invariant
assertion (NOT snapshot of the model name) — the test is allowed to be
parameterised over any target_model value.

Decision 2026-07-17 (sweep shard): all paths are tested here so the
single-path test from 05e95e0b1 is replaced by this parametrised
sweep. The earlier single-path test stays in the file as historical
context — it covers the openrouter-fallback resolver directly, which
this sweep tests indirectly through resolve_runtime_provider().

Stubbing note: every path is forced through its singleton-fallback
inline return by stubbing load_pool() to return None. Without that,
providers with a credential pool (anthropic, nous, openai-codex,
minimax-oauth) get routed through the already-fixed
_resolve_runtime_from_pool_entry, which would mask the inline-return
bug. Stubbing surfaces the actual bug we're fixing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hermes_cli import runtime_provider as rp


# Every path tested asserts: returned["model"] must equal target_model.
TARGET_MODEL = "MiniMax-M3"


def _empty_pool(provider):
    """Force every provider through its singleton-fallback inline return."""
    return None


@pytest.fixture(autouse=True)
def _no_pool(monkeypatch):
    """The pool path is already fixed at 0bf17dfd7 and would otherwise mask
    the inline-return bug. Autouse so every parametrised case exercises the
    broken code path, not the fixed one.
    """
    monkeypatch.setattr(rp, "load_pool", _empty_pool)


# ---------------------------------------------------------------------------
# Singleton-fallback inline returns (oauth + bedrock + api_key + copilot-acp).
# Each case stubs the credential resolver for that provider to a deterministic
# shape, then calls resolve_runtime_provider(requested=..., target_model=...)
# and asserts model == target_model.
# ---------------------------------------------------------------------------

_OAUTH_FAKE_CREDS = {
    "nous": {
        "base_url": "https://inference.nous.research/hermes",
        "api_key": "fake-nous-key",
        "source": "portal",
        "expires_at": None,
    },
    "openai-codex": {
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_key": "fake-codex-key",
        "source": "hermes-auth-store",
        "last_refresh": None,
    },
    "xai-oauth": {
        "base_url": "https://api.x.ai/v1",
        "api_key": "fake-xai-key",
        "source": "hermes-auth-store",
        "last_refresh": None,
    },
    "qwen-oauth": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "fake-qwen-key",
        "source": "qwen-cli",
        "expires_at_ms": None,
    },
}


def _fake_nous(*, timeout_seconds=15.0):
    return _OAUTH_FAKE_CREDS["nous"]


def _fake_codex():
    return _OAUTH_FAKE_CREDS["openai-codex"]


def _fake_xai():
    return _OAUTH_FAKE_CREDS["xai-oauth"]


def _fake_qwen():
    return _OAUTH_FAKE_CREDS["qwen-oauth"]


@pytest.mark.parametrize(
    "provider,resolver_attr,patch_value",
    [
        # nous singleton fallback (line ~1758 on HEAD 05e95e0b1)
        ("nous", "resolve_nous_runtime_credentials", _fake_nous),
        # openai-codex singleton fallback (line ~1785)
        ("openai-codex", "resolve_codex_runtime_credentials", _fake_codex),
        # xai-oauth singleton fallback (line ~1811)
        ("xai-oauth", "resolve_xai_oauth_runtime_credentials", _fake_xai),
        # qwen-oauth singleton fallback (line ~1831)
        ("qwen-oauth", "resolve_qwen_runtime_credentials", _fake_qwen),
    ],
)
def test_singleton_oauth_path_surfaces_model(monkeypatch, provider, resolver_attr, patch_value):
    """Singleton-fallback returns for OAuth providers must surface `model`.

    Each of these paths lives in resolve_runtime_provider() and returns
    a fresh dict literal without the "model" key — the resolved runtime
    has no `model` and downstream aux slots (curator / compression /
    vision / web_extract / approval / skills_hub) then crash with an
    empty model.
    """
    monkeypatch.setattr(rp, resolver_attr, patch_value)

    resolved = rp.resolve_runtime_provider(
        requested=provider, target_model=TARGET_MODEL,
    )
    assert "model" in resolved, (
        f"singleton-fallback for {provider!r} dropped the model key "
        f"(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == TARGET_MODEL, (
        f"singleton-fallback for {provider!r} returned the wrong model: "
        f"expected {TARGET_MODEL!r}, got {resolved.get('model')!r}"
    )


def test_minimax_oauth_singleton_surfaces_model(monkeypatch):
    """minimax-oauth singleton fallback (line ~1851) drops the model key.

    Uses a different credential-resolver call (lazy import inside
    hermes_cli.auth) so it can't share the parametrised fixture above.
    """
    def fake_minimax():
        return {
            "base_url": "https://api.minimax.io/anthropic",
            "api_key": "fake-minimax-key",
            "source": "oauth",
        }
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_minimax_oauth_runtime_credentials", fake_minimax,
    )
    resolved = rp.resolve_runtime_provider(
        requested="minimax-oauth", target_model=TARGET_MODEL,
    )
    assert "model" in resolved, (
        "minimax-oauth singleton-fallback dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == TARGET_MODEL


def test_anthropic_singleton_surfaces_model(monkeypatch):
    """anthropic singleton fallback (line ~1967) drops the model key."""
    from agent import anthropic_adapter
    monkeypatch.setattr(anthropic_adapter, "resolve_anthropic_token", lambda: "sk-test-fake")

    resolved = rp.resolve_runtime_provider(
        requested="anthropic", target_model=TARGET_MODEL,
    )
    assert "model" in resolved, (
        "anthropic singleton-fallback dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == TARGET_MODEL


def test_bedrock_claude_surfaces_model(monkeypatch):
    """bedrock Claude branch (line ~2021) drops the model key."""
    from agent import bedrock_adapter
    monkeypatch.setattr(bedrock_adapter, "has_aws_credentials", lambda: True)
    monkeypatch.setattr(bedrock_adapter, "resolve_bedrock_region", lambda: "us-east-1")
    monkeypatch.setattr(bedrock_adapter, "resolve_aws_auth_env_var", lambda: "env")
    monkeypatch.setattr(bedrock_adapter, "is_anthropic_bedrock_model", lambda m: True)
    monkeypatch.setattr(rp, "load_config", lambda: {})

    resolved = rp.resolve_runtime_provider(
        requested="bedrock", target_model="anthropic.claude-opus-4-20250514",
    )
    assert "model" in resolved, (
        "bedrock (Claude) singleton-fallback dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == "anthropic.claude-opus-4-20250514"


def test_bedrock_non_claude_surfaces_model(monkeypatch):
    """bedrock non-Claude branch (line ~2037) drops the model key."""
    from agent import bedrock_adapter
    monkeypatch.setattr(bedrock_adapter, "has_aws_credentials", lambda: True)
    monkeypatch.setattr(bedrock_adapter, "resolve_bedrock_region", lambda: "us-east-1")
    monkeypatch.setattr(bedrock_adapter, "resolve_aws_auth_env_var", lambda: "env")
    monkeypatch.setattr(bedrock_adapter, "is_anthropic_bedrock_model", lambda m: False)
    monkeypatch.setattr(rp, "load_config", lambda: {})

    resolved = rp.resolve_runtime_provider(
        requested="bedrock", target_model="amazon.nova-pro-v1:0",
    )
    assert "model" in resolved, (
        "bedrock (non-Claude) singleton-fallback dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == "amazon.nova-pro-v1:0"


def test_api_key_provider_surfaces_model(monkeypatch):
    """api_key provider branch (line ~2096) drops the model key.

    Covers z.ai/GLM, Kimi, MiniMax, MiniMax-CN — any provider with
    PROVIDER_REGISTRY auth_type == "api_key".
    """
    def fake_apikey(provider):
        return {
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-fake",
            "source": "env",
        }
    monkeypatch.setattr(rp, "resolve_api_key_provider_credentials", fake_apikey)

    resolved = rp.resolve_runtime_provider(
        requested="z-ai", target_model=TARGET_MODEL,
    )
    assert "model" in resolved, (
        "api_key-provider singleton-fallback dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == TARGET_MODEL


def test_copilot_acp_surfaces_model(monkeypatch):
    """copilot-acp singleton fallback (line ~1867) drops the model key."""
    def fake_copilot(provider):
        return {
            "base_url": "https://copilot.example.com/v1",
            "api_key": "fake-copilot-key",
            "command": "copilot",
            "args": [],
            "source": "process",
        }
    monkeypatch.setattr(rp, "resolve_external_process_provider_credentials", fake_copilot)

    resolved = rp.resolve_runtime_provider(
        requested="copilot-acp", target_model=TARGET_MODEL,
    )
    assert "model" in resolved, (
        "copilot-acp singleton-fallback dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == TARGET_MODEL


# ---------------------------------------------------------------------------
# Short-circuit and helper paths (lines 1546–1670 on HEAD 05e95e0b1).
# These fire BEFORE the pool-load stage and never go through the pool path,
# so they are not masked by the _no_pool autouse fixture.
# ---------------------------------------------------------------------------

def test_moa_short_circuit_surfaces_model():
    """moa virtual provider short-circuit (line ~1546) drops the model key."""
    resolved = rp.resolve_runtime_provider(
        requested="moa", target_model=TARGET_MODEL,
    )
    assert "model" in resolved, (
        "moa short-circuit dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == TARGET_MODEL


def test_anthropic_azure_short_circuit_surfaces_model():
    """anthropic Azure short-circuit (line ~1562) drops the model key."""
    resolved = rp.resolve_runtime_provider(
        requested="anthropic",
        explicit_base_url="https://example.azure.com",
        explicit_api_key="sk-test-fake",
        target_model=TARGET_MODEL,
    )
    assert "model" in resolved, (
        "anthropic-azure short-circuit dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == TARGET_MODEL


def test_azure_foundry_helper_surfaces_model():
    """_resolve_azure_foundry_runtime (line ~1202) drops the model key."""
    resolved = rp._resolve_azure_foundry_runtime(
        requested_provider="azure-foundry",
        model_cfg={
            "provider": "azure-foundry",
            "base_url": "https://example.azure.com",
            "api_mode": "chat_completions",
        },
        explicit_api_key="sk-test-fake",
        target_model=TARGET_MODEL,
    )
    assert "model" in resolved, (
        "_resolve_azure_foundry_runtime dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == TARGET_MODEL


def test_vertex_surfaces_model():
    """vertex inline return (line ~1599) drops the model key."""
    def fake_vertex():
        return ("fake-vertex-token", "https://vertex.example.com/v1")
    with patch("agent.vertex_adapter.get_vertex_config", fake_vertex):
        resolved = rp.resolve_runtime_provider(
            requested="vertex", target_model=TARGET_MODEL,
        )
    assert "model" in resolved, (
        "vertex inline return dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == TARGET_MODEL


def test_named_custom_runtime_surfaces_model():
    """_resolve_named_custom_runtime (line ~919) drops the model key."""
    resolved = rp.resolve_runtime_provider(
        requested="ollama",
        explicit_base_url="http://localhost:11434/v1",
        explicit_api_key="no-key-required",
        target_model=TARGET_MODEL,
    )
    assert "model" in resolved, (
        "_resolve_named_custom_runtime dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == TARGET_MODEL


def test_explicit_runtime_anthropic_surfaces_model():
    """_resolve_explicit_runtime anthropic branch (line ~1673) drops model."""
    from agent import anthropic_adapter
    with patch.object(anthropic_adapter, "resolve_anthropic_token", lambda: "sk-test-fake"):
        resolved = rp.resolve_runtime_provider(
            requested="anthropic",
            explicit_base_url="https://api.anthropic.com",
            explicit_api_key="sk-test-fake",
            target_model=TARGET_MODEL,
        )
    assert "model" in resolved, (
        "_resolve_explicit_runtime (anthropic) dropped the model key "
        "(Bug #6 bug class, sweep shard 2026-07-17)"
    )
    assert resolved["model"] == TARGET_MODEL
