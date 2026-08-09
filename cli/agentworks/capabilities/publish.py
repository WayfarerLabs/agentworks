"""Publish built-in capability implementations as read-only resource rows.

Each kind's descriptor supplies its registry, row type, and operator-visible
provenance label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.capabilities.descriptor import CapabilityKindDescriptor
    from agentworks.resources.registry import Registry


def publish_capability_rows(registry: Registry, descriptor: CapabilityKindDescriptor) -> None:
    """Publish one built-in row per registered implementation of
    ``descriptor``'s kind, in name order.

    Publication is UNCONDITIONAL for the impls this build ships (R13): host
    support is the row's folded READINESS, not its absence, so an installed
    but host-unsupported platform (wsl2 off Windows) is a present, not-ready
    node rather than a dangling reference.

    Impls seated by a system plugin are skipped. Such an impl lives in the
    same code registry as a built-in (the plugin's adapter seats it there at
    import, and the graph stamps it onto the node from there), but its ROW is
    published by ``plugins.publish_plugins`` with a ``system-plugin`` origin;
    publishing it here too would collide (built-in vs system-plugin) at
    ``Registry.add``.
    """
    from agentworks.origin import Origin
    from agentworks.plugins.registration import plugin_seated_names

    seated_by_plugin = plugin_seated_names(descriptor.kind)
    origin = Origin.built_in(source=descriptor.publisher_source)
    impls = descriptor.registry()
    for name in sorted(impls):
        if name in seated_by_plugin:
            continue
        registry.add(descriptor.kind, name, descriptor.entry_factory(name, impls[name], None), origin)
