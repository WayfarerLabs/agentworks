"""The generic built-in capability publisher, derived from the descriptor
table.

Every capability kind mirrors its code registry into the resource Registry
as read-only rows, so references to a capability (a ``vm-site``'s
``spec.platform``, the ``[secret_config].backends`` chain) resolve through
the framework's uniform miss policy and the capabilities list and describe
like every other resource. That was four hand-written copies of one idiom,
differing only in which registry they read, which row type they built, and
which source label they stamped: exactly the three things a descriptor
carries. :func:`publish_capability_rows` is the idiom, once.

The kind's ``publisher_source`` is preserved per kind rather than derived
from a module path, because the labels are operator-visible provenance
(``secret-backend`` publishes as ``agentworks.secrets``, the package, not
the ``backends`` module that used to front it).
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
