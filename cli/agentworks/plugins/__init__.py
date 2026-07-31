"""The system-plugin framework: the ``Plugin`` descriptor, the atomic
``register_plugin``, the per-kind capability adapters, and the installed
index ``SYSTEM_PLUGINS`` (R3).

The index uses INVERTED registration: it imports each shipped plugin module
and calls ``register_plugin(module.PLUGIN)`` itself, rather than
registration being a plugin-module import side effect. That buys three
things (R3): a registration failure is wrapped with plugin attribution
rather than an opaque traceback that kills the whole CLI; provenance is
derived from the real module (``_module.__name__``), not a self-declared
name a descriptor could spoof; and external loading later becomes "another
way to obtain a ``module.PLUGIN``", not a new authoring contract.

``_INSTALLED_MODULES`` ships the migrated system plugins (``onepassword``,
``claude``, ``proxmox``, ``azure``, R11); importing this package registers
each, seating its capability impls into the core code registries, and indexes
it into ``SYSTEM_PLUGINS``. A shipped plugin's rows publish present-but-disabled
until an operator opts in via ``[plugins].system``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agentworks.plugins import azure as _azure
from agentworks.plugins import claude as _claude
from agentworks.plugins import onepassword as _onepassword
from agentworks.plugins import proxmox as _proxmox
from agentworks.plugins.adapters import CAPABILITY_ADAPTERS, CapabilityAdapter
from agentworks.plugins.base import Plugin, PluginCommand, PluginError
from agentworks.plugins.enablement import plugin_enablement_source
from agentworks.plugins.publish import publish_plugins
from agentworks.plugins.registration import register_plugin, seated_plugin

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CAPABILITY_ADAPTERS",
    "SYSTEM_PLUGINS",
    "CapabilityAdapter",
    "Plugin",
    "PluginCommand",
    "PluginError",
    "plugin_enablement_source",
    "publish_plugins",
    "register_plugin",
    "seated_plugin",
]


class _PluginModule(Protocol):
    """The structural shape a shipped plugin module exposes: a
    module-level ``PLUGIN`` descriptor. ``__name__`` (every module carries
    it) is the attribution source for wrapped errors."""

    __name__: str
    PLUGIN: Plugin


def _build_installed_index(modules: Sequence[_PluginModule]) -> dict[str, Plugin]:
    """Import-time index build (R3): register each shipped module's
    ``PLUGIN`` itself, wrapping any failure with the real module name, and
    reject a duplicate plugin name as a typed ``PluginError`` (not
    last-writer-wins). Provenance is the real ``module.__name__``, never a
    descriptor-declared name a plugin could spoof."""
    index: dict[str, Plugin] = {}
    for module in modules:
        plugin = module.PLUGIN
        try:
            register_plugin(plugin)
        except PluginError as exc:
            raise PluginError(f"system plugin {module.__name__!r} failed to register: {exc}") from exc
        if plugin.name in index:
            raise PluginError(f"duplicate system plugin name {plugin.name!r} (from {module.__name__!r})")
        index[plugin.name] = plugin
    return index


# Add a module here to ship a plugin. Each shipped module's ``PLUGIN`` is
# registered (seating its capability impls) and indexed at import.
_INSTALLED_MODULES: tuple[_PluginModule, ...] = (_onepassword, _claude, _proxmox, _azure)

SYSTEM_PLUGINS: dict[str, Plugin] = _build_installed_index(_INSTALLED_MODULES)
