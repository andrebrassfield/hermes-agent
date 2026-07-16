"""Regression test for Bug #6 — openrouter-fallback missing `model` field.

The openrouter-fallback path (``_resolve_openrouter_runtime``) fires when
callers pass ``explicit_base_url`` + ``explicit_api_key`` to bypass the
credential pool. This path did NOT propagate ``target_model`` to the returned
dict's ``model`` field, causing aux slots to boot with ``model: None``.

This test forces the fallback by passing explicit credentials and verifies
that ``model`` IS present in the result — a test that fails on pre-patch code.

Ref: Decision 2026-07-16 (Decision 1 = OPTION 2).
"""

from __future__ import annotations

import pytest

from hermes_cli import runtime_provider as rp


class TestOpenRouterFallbackModelField:
    """Regression suite for runtime_provider model-field propagation.

    Decision 2026-07-16: patch only the openrouter-fallback return path
    (~line 2060). Do NOT sweep the other 13 return paths here — those are
    tracked in the backlog card ``runtime_provider: populate model on
    remaining 13 return paths``.
    """

    def test_openrouter_fallback_includes_model_field(self) -> None:
        """``resolve_runtime_provider`` with explicit creds must include ``model`` in result.

        The openrouter-fallback path is taken when ``explicit_base_url`` and
        ``explicit_api_key`` are both provided (bypassing the credential pool).
        Before the fix this returned a dict WITHOUT the ``model`` key, causing
        aux providers to boot with ``model: None``.
        """
        result = rp.resolve_runtime_provider(
            requested="openrouter",
            target_model="openrouter/test-model",
            explicit_base_url="https://openrouter.ai/api/v1",
            explicit_api_key="sk-test-fake-key-for-regression",
        )

        # Must have a model key — the aux slots thread this through and
        # will crash at boot if it is absent.
        assert "model" in result, (
            f"model key missing from fallback result. "
            f"keys={list(result.keys())}"
        )
        # Value must be the requested model
        assert result["model"] == "openrouter/test-model"

    def test_openrouter_fallback_model_key_present_without_target_model(self) -> None:
        """Without target_model, model key must still be present (value may be None).

        Mirrors the pool-entry behavior: when neither target_model nor
        model_cfg.get("default") is set, model=None is the correct answer.
        The gate is the key's PRESENCE, not its value.
        """
        result = rp.resolve_runtime_provider(
            requested="openrouter",
            explicit_base_url="https://openrouter.ai/api/v1",
            explicit_api_key="sk-test-fake-key",
            # no target_model
        )
        assert "model" in result, (
            f"model key missing from fallback result. "
            f"keys={list(result.keys())}"
        )

    def test_openrouter_fallback_preserves_other_keys(self) -> None:
        """Patch must not drop any existing keys from the result dict."""
        result = rp.resolve_runtime_provider(
            requested="openrouter",
            target_model="anthropic/claude-3-haiku",
            explicit_base_url="https://openrouter.ai/api/v1",
            explicit_api_key="sk-test",
        )
        # These keys were present before the patch and must remain.
        assert "provider" in result
        assert "api_mode" in result
        assert "base_url" in result
        assert "api_key" in result
        assert "requested_provider" in result
        assert result["provider"] == "openrouter"
