"""Tests that write_runtime_status(reset_platforms=True) purges stale platform
entries from gateway_state.json.

Bug: when a gateway restarts with fewer platforms than its previous run (e.g.
telegram disabled in config), the old platform entries persisted in the
on-disk state file forever.  The fix adds a ``reset_platforms`` parameter that
clears the platforms dict at startup so only currently-active adapters are
represented.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    """Return a temp gateway_state.json path."""
    return tmp_path / "gateway_state.json"


def _seed_state(path: Path, platforms: dict) -> None:
    """Write a gateway_state.json with the given platforms dict."""
    payload = {
        "pid": 99999,
        "kind": "hermes-gateway",
        "argv": ["python", "-m", "hermes_cli.main", "gateway", "run"],
        "start_time": 170000000000,
        "gateway_state": "running",
        "exit_reason": None,
        "restart_requested": False,
        "active_agents": 0,
        "platforms": platforms,
        "updated_at": "2026-07-03T03:45:10.636472+00:00",
    }
    path.write_text(json.dumps(payload))


def test_reset_platforms_clears_stale_entries(state_file: Path):
    """reset_platforms=True removes all old platform entries."""
    from gateway.status import write_runtime_status

    # Seed with stale platforms from a previous run
    _seed_state(state_file, {
        "telegram": {"state": "connected", "error_code": None, "error_message": None, "updated_at": "old"},
        "photon": {"state": "retrying", "error_code": "SIDECAR_CRASHED", "error_message": "crashed", "updated_at": "old"},
    })

    with patch("gateway.status._get_runtime_status_path", return_value=state_file):
        write_runtime_status(gateway_state="starting", exit_reason=None, reset_platforms=True)

    data = json.loads(state_file.read_text())
    assert data["platforms"] == {}, f"Expected empty platforms, got {data['platforms']}"


def test_without_reset_platforms_preserves_stale_entries(state_file: Path):
    """Without reset_platforms, stale entries persist (proves the bug existed)."""
    from gateway.status import write_runtime_status

    stale = {
        "telegram": {"state": "connected", "error_code": None, "error_message": None, "updated_at": "old"},
        "photon": {"state": "retrying", "error_code": "SIDECAR_CRASHED", "error_message": "crashed", "updated_at": "old"},
    }
    _seed_state(state_file, stale)

    with patch("gateway.status._get_runtime_status_path", return_value=state_file):
        write_runtime_status(gateway_state="starting", exit_reason=None)

    data = json.loads(state_file.read_text())
    assert "telegram" in data["platforms"], "Without reset, stale telegram should persist"
    assert "photon" in data["platforms"], "Without reset, stale photon should persist"


def test_reset_then_add_new_platform_only(state_file: Path):
    """After reset, only newly-written platform entries appear."""
    from gateway.status import write_runtime_status

    _seed_state(state_file, {
        "telegram": {"state": "connected", "error_code": None, "error_message": None, "updated_at": "old"},
        "photon": {"state": "retrying", "error_code": "SIDECAR_CRASHED", "error_message": "crashed", "updated_at": "old"},
    })

    with patch("gateway.status._get_runtime_status_path", return_value=state_file):
        # Reset at startup
        write_runtime_status(gateway_state="starting", exit_reason=None, reset_platforms=True)
        # Then a platform adapter writes its own state
        write_runtime_status(platform="api_server", platform_state="connected", error_code=None, error_message=None)

    data = json.loads(state_file.read_text())
    assert "api_server" in data["platforms"], "New platform should be present after write"
    assert "telegram" not in data["platforms"], "Stale telegram should be gone after reset"
    assert "photon" not in data["platforms"], "Stale photon should be gone after reset"