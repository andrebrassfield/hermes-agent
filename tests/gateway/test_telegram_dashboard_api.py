"""Tests for Telegram platform status integration with the dashboard API.

Verifies that the API server exposes Telegram gateway state in a way
the TelegramPage dashboard component can consume.
"""

import json
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_adapter():
    """Create a minimal APIServerAdapter for testing status endpoints."""
    config = PlatformConfig(
        enabled=True,
        extra={
            "host": "127.0.0.1",
            "port": 0,  # random port — won't be started
            "key": "test-key-12345678",
        },
    )
    adapter = APIServerAdapter(config)
    return adapter


# ---------------------------------------------------------------------------
# Telegram platform status in /health/detailed
# ---------------------------------------------------------------------------


class TestTelegramPlatformStatus:
    """The /health/detailed endpoint returns runtime platform state including
    Telegram.  The TelegramPage dashboard component reads this data to show
    connection status."""

    async def _call_health_detailed(self, adapter):
        """Build the health endpoint's response body without starting a server."""
        from aiohttp import web

        request = mock_request()
        response = await adapter._handle_health_detailed(request)
        assert response is not None
        return json.loads(response.body) if isinstance(response.body, bytes) else response.body

    @patch("gateway.status.read_runtime_status")
    def test_telegram_connected_status_in_health_detailed(
        self, mock_read_runtime_status, api_adapter
    ):
        """When telegram is connected, the health endpoint returns connected state."""

        mock_read_runtime_status.return_value = {
            "gateway_state": "running",
            "platforms": {
                "telegram": {
                    "state": "connected",
                    "error_message": None,
                    "updated_at": "2026-05-16T22:00:00+00:00",
                },
                "discord": {
                    "state": "disconnected",
                    "error_message": None,
                    "updated_at": "2026-05-16T22:00:00+00:00",
                },
            },
            "active_agents": 2,
        }

        import asyncio
        result = asyncio.run(self._call_health_detailed(api_adapter))

        assert result["status"] == "ok"
        platforms = result["platforms"]
        assert "telegram" in platforms
        assert platforms["telegram"]["state"] == "connected"

    @patch("gateway.status.read_runtime_status")
    def test_telegram_disconnected_status_in_health_detailed(
        self, mock_read_runtime_status, api_adapter
    ):
        """When telegram is disconnected, the health endpoint reflects it."""

        mock_read_runtime_status.return_value = {
            "gateway_state": "running",
            "platforms": {
                "telegram": {
                    "state": "disconnected",
                    "error_message": "Network error",
                    "updated_at": "2026-05-16T22:00:00+00:00",
                },
            },
            "active_agents": 0,
        }

        import asyncio
        result = asyncio.run(self._call_health_detailed(api_adapter))

        assert result["platforms"]["telegram"]["state"] == "disconnected"
        assert result["platforms"]["telegram"]["error_message"] == "Network error"

    @patch("gateway.status.read_runtime_status")
    def test_telegram_not_in_platforms_when_unconfigured(
        self, mock_read_runtime_status, api_adapter
    ):
        """When telegram is not configured, it's absent from the platforms dict."""

        mock_read_runtime_status.return_value = {
            "gateway_state": "running",
            "platforms": {
                "discord": {"state": "connected", "error_message": None, "updated_at": "2026-05-16T22:00:00+00:00"},
            },
            "active_agents": 1,
        }

        import asyncio
        result = asyncio.run(self._call_health_detailed(api_adapter))

        assert "telegram" not in result["platforms"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def mock_request():
    """Build a minimal aiohttp Request stub for handler calls."""
    from aiohttp import web
    from unittest.mock import MagicMock

    request = MagicMock(spec=web.Request)
    request.headers = {}
    request.query = {}
    request.method = "GET"
    request.path = "/health/detailed"
    request.app = {}
    return request
