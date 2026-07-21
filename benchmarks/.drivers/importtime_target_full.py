#!/usr/bin/env python3
"""Realistic Telegram-only cron delivery import-time path.

Reproduces what a cron tick does:
  1. discover_plugins() registers deferred Feishu/Teams/WhatsApp loaders
     on the platform_registry singleton.
  2. load_gateway_config() iterates get_connected_platforms() which calls
     plugin_entries() -> _resolve_all() on the registry, triggering lark_oapi
     + Teams + WhatsApp SDK imports.
"""
import os
import sys

REPO = "/Users/brassfieldventuresllc/.hermes/hermes-agent"
sys.path.insert(0, REPO)

# Step 1: discover_plugins (registers deferred loaders, NOT resolving them)
try:
    from hermes_cli.plugins import PluginManager
except Exception:
    # Older import path
    from hermes_cli.plugin_manager import PluginManager  # type: ignore

pm = PluginManager()
plugins = pm.discover_and_load(force=False)

# Step 2: load_gateway_config — touches plugin_entries which triggers
# _resolve_all() -> lark_oapi + Teams + WhatsApp SDK imports.
from gateway.config import load_gateway_config
cfg = load_gateway_config()
connected = cfg.get_connected_platforms() if cfg is not None else []

print(f"OK: discovered {plugins if isinstance(plugins, list) else 'n'} plugins, "
      f"connected platforms: {len(connected)}",
      file=sys.stderr)
