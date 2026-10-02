"""Plugin discovery: built-ins plus anything installed under the `acm.plugins` entry-point group.
Plugins are operator-installed Python packages; the hub never loads code from the database or the UI."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from .base import BasePlugin

log = logging.getLogger("acm_hub.plugins")
_registry: dict[str, BasePlugin] = {}
_loaded = False


def register(plugin: BasePlugin) -> None:
    _registry[plugin.info.key] = plugin


def unregister(key: str) -> None:
    _registry.pop(key, None)


def load_all(*, force: bool = False) -> dict[str, BasePlugin]:
    global _loaded
    if _loaded and not force:
        return _registry
    from .builtin import BUILTINS

    for p in BUILTINS:
        register(p)
    for ep in entry_points(group="acm.plugins"):
        try:
            obj = ep.load()
            plugin = obj() if isinstance(obj, type) else obj
            if not isinstance(plugin, BasePlugin):
                raise TypeError("entry point must provide a BasePlugin")
            register(plugin)
        except Exception:  # noqa: BLE001 — one broken plugin must not stop the hub
            log.exception("could not load plugin entry point %s", ep.name)
    _loaded = True
    return _registry


def get(key: str) -> BasePlugin | None:
    return load_all().get(key)


def all_plugins() -> list[BasePlugin]:
    return sorted(load_all().values(), key=lambda p: p.info.key)
