"""
Tests for PlatformRegistry.plugin_entries() memoization.

Acceptance criteria:
  5/5 unit tests PASS for the lazy loader
  Live benchmark shows >500ms reduction on Telegram-only delivery
  No regression on Feishu delivery
  Clear go/no-go with measured numbers

The memo keeps lark_oapi (~1.5s import) out of Telegram-only cron workers:
every load_gateway_config() call that iterates plugin_entries() now pays
~0ms on the 2nd+ call instead of ~1.5s per call.
"""

from unittest.mock import MagicMock


def test_plugin_entries_returns_list():
    """Sanity: plugin_entries() returns a list (not None, not a generator)."""
    from gateway.platform_registry import PlatformRegistry

    registry = PlatformRegistry()
    result = registry.plugin_entries()
    assert isinstance(result, list)


def test_plugin_entries_memoizes_subsequent_calls():
    """
    After the first call, subsequent plugin_entries() calls return the
    cached list WITHOUT calling _resolve_all() again.

    This is the core behavior: multiple load_gateway_config() calls in a
    single cron worker process must not re-import platform SDKs.
    """
    from gateway.platform_registry import PlatformRegistry

    registry = PlatformRegistry()

    resolve_count = [0]
    original_resolve_all = registry._resolve_all

    def counting_resolve_all():
        resolve_count[0] += 1
        original_resolve_all()

    registry._resolve_all = counting_resolve_all

    # First call populates the cache
    result1 = registry.plugin_entries()
    assert resolve_count[0] == 1, "First call must resolve"

    # Second call must NOT resolve again
    result2 = registry.plugin_entries()
    assert resolve_count[0] == 1, "Second call must use cache (no additional resolve)"

    # Results must be equivalent lists
    assert list(result1) == list(result2)


def test_register_invalidates_memo():
    """
    Registering a new platform entry invalidates the memo so the next
    plugin_entries() call picks up the new entry.

    Without invalidation, a fresh discovery (force=True) would return stale
    cached entries from before the new plugin registered.
    """
    from gateway.platform_registry import PlatformEntry, PlatformRegistry

    registry = PlatformRegistry()

    # Prime the cache
    _ = registry.plugin_entries()
    assert registry._plugin_entries_cache is not None

    # Register a new entry (simulates a new platform being added)
    entry = PlatformEntry(
        name="test-platform",
        label="Test Platform",
        source="plugin",
        adapter_factory=lambda cfg: None,
        check_fn=lambda: True,
    )
    registry.register(entry)

    # Cache must be invalidated
    assert registry._plugin_entries_cache is None

    # Next call must re-iterate and include the new entry
    entries = registry.plugin_entries()
    names = [e.name for e in entries]
    assert "test-platform" in names


def test_unregister_invalidates_memo():
    """Unregistering a platform invalidates the memo."""
    from gateway.platform_registry import PlatformEntry, PlatformRegistry

    registry = PlatformRegistry()

    # Prime the cache
    _ = registry.plugin_entries()
    assert registry._plugin_entries_cache is not None

    # Unregister (no-op if nothing is registered — still invalidates as a no-op)
    registry.unregister("nonexistent-platform")

    # Cache must be invalidated regardless of whether anything was removed
    assert registry._plugin_entries_cache is None


def test_plugin_entries_returns_snapshot_not_live_view():
    """
    Each plugin_entries() call returns a fresh list (snapshot), so callers
    can mutate the returned list without affecting the cache.

    The memo itself is never exposed — only a copy of it.
    """
    from gateway.platform_registry import PlatformRegistry

    registry = PlatformRegistry()

    result1 = registry.plugin_entries()
    original_len = len(result1)
    result1.clear()  # Mutate the returned list

    # A second call must return the original content, not the mutated copy
    result2 = registry.plugin_entries()
    assert len(result2) == original_len, "Returned list must be a snapshot, not the memo"
