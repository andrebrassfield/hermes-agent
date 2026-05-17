"""Obsidian vault integration plugin.

Entry point for the Hermes plugin loader. The real implementation lives in
plugin_api.py so that the manifest requirement (``plugin_api.py``) is met
while the loader's convention (``__init__.py`` with ``register``) is also
satisfied.
"""

from .plugin_api import register  # noqa: F401
