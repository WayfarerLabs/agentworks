"""``register_plugin`` and the ``seated_plugin`` test helper (R2, R5, R6).

``register_plugin`` runs at import time (invoked by the installed index),
once per shipped plugin. It validates the WHOLE descriptor, prechecks every
implementation for a name collision, then seats the implementation classes.
No capability registry is mutated until validation and collision checking
have completed for the full contribution, so seating is all-or-nothing by
construction.

This seating guard, NOT ``_check_collision``, is the enforcement point for
every capability name-clash (built-in/plugin and plugin/plugin alike): a
capability's published row name IS its impl's registry key, so a clash is
caught here at registration, before any capability row is ever built.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from agentworks.capabilities.conformance import conformance_error
from agentworks.capabilities.descriptor import capability_descriptors, descriptor_for
from agentworks.plugins.adapters import capability_adapters
from agentworks.plugins.base import Plugin, PluginError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.plugins.adapters import CapabilityAdapter


# Provenance: which plugin seated each ``(kind, name)`` capability impl. A
# ``(kind, name)`` present in a capability registry but ABSENT here is a
# core built-in (seated at import by the core capability modules). This is
# the "a plugin's seated impls are its own known set" the LLD relies on to
# name a collision's occupant as a core built-in vs another plugin, without
# the descriptor self-declaring its origin.
_PLUGIN_SEATED: dict[tuple[str, str], str] = {}
_PLUGIN_NAME_RE = re.compile(r"[a-z](?:[a-z0-9-]*[a-z0-9])?")


def register_plugin(plugin: Plugin) -> None:
    """Validate ``plugin`` wholesale, then seat its capability impls
    atomically. Idempotent per impl name FOR THIS PLUGIN; every other
    occupant of a name it declares (a core built-in or another plugin, and
    whether or not the impl class is the same one) is a ``PluginError``
    naming the plugin and the occupant's actual origin. Returns nothing and
    does not touch the index or publish rows; it only seats impls into the
    four code registries, exactly as core impls populate them at import.

    Returning therefore means every name in the descriptor is attributed to
    this plugin in :data:`_PLUGIN_SEATED`, which is what lets
    :func:`plugin_seated_names` decide who publishes each row.
    """
    planned = _validate_descriptor(plugin)
    to_seat = _precheck_and_prepare(plugin, planned)
    # Pass 3: seat all. Preparation is a class pass-through, so this loop is
    # pure dict writes that cannot fail partway.
    for adapter, name, payload in to_seat:
        adapter.seat(name, payload)
        _PLUGIN_SEATED[(adapter.kind, name)] = plugin.name


def _validate_descriptor(plugin: Plugin) -> list[tuple[CapabilityAdapter, str, type]]:
    """Pass 1: validate the whole descriptor before touching any registry.

    Returns the ``(adapter, capability_name, impl_cls)`` plan for the seat
    passes. Each failure is a ``PluginError`` naming the plugin.

    Contract conformance (``conformance_error``, derived from the kind's
    descriptor) runs here, in the validating pass, so a non-conforming impl
    is refused BEFORE any registry is mutated and atomic seating is
    preserved. The bare ``isinstance(impl, type)`` and ``name`` checks stay
    ahead of it: they are the shape the conformance check itself assumes,
    and their messages name the specific mistake an author is most likely to
    have made.
    """
    if not isinstance(plugin.name, str) or _PLUGIN_NAME_RE.fullmatch(plugin.name) is None:
        raise PluginError(
            f"system plugin name {plugin.name!r} must use lowercase ASCII letters, digits, and "
            "hyphens, must start with a letter, and must end with a letter or digit"
        )

    adapters = capability_adapters()
    planned: list[tuple[CapabilityAdapter, str, type]] = []
    seen: set[tuple[str, str]] = set()
    for kind, impls in plugin.capabilities.items():
        adapter = adapters.get(kind)
        if adapter is None:
            known = ", ".join(sorted(adapters))
            raise PluginError(
                f"system plugin {plugin.name!r} declares capability kind {kind!r}, "
                f"which has no adapter (known capability kinds: {known})"
            )
        for impl in impls:
            if not isinstance(impl, type):
                raise PluginError(
                    f"system plugin {plugin.name!r} {kind} capability impl {impl!r} is not a class "
                    f"(pass the impl class itself, not an instance)"
                )
            name = getattr(impl, "name", None)
            if not isinstance(name, str) or not name or "/" in name:
                raise PluginError(
                    f"system plugin {plugin.name!r} {kind} impl {impl.__name__!r} has an invalid "
                    f"capability name {name!r} (must be a non-empty, '/'-free 'name' class attribute)"
                )
            reason = conformance_error(descriptor_for(kind), impl)
            if reason is not None:
                raise PluginError(
                    f"system plugin {plugin.name!r} {kind} impl {impl.__name__!r} does not satisfy "
                    f"the {kind} capability contract: {reason}"
                )
            key = (kind, name)
            if key in seen:
                raise PluginError(
                    f"system plugin {plugin.name!r} declares {kind} {name!r} twice (intra-descriptor collision)"
                )
            seen.add(key)
            planned.append((adapter, name, impl))
    return planned


def _precheck_and_prepare(
    plugin: Plugin,
    planned: list[tuple[CapabilityAdapter, str, type]],
) -> list[tuple[CapabilityAdapter, str, object]]:
    """Pass 2: prove every impl seatable AND build its payload, with NO
    registry mutation. Returns the ``(adapter, name, payload)`` items that
    actually need seating (idempotent no-op matches are dropped).

    A ``None`` occupant needs seating: its class payload is prepared here,
    so pass 3 is a pure write. An occupant THIS PLUGIN seated is an
    idempotent no-op. Any other occupant raises here, before any impl is
    seated.

    Idempotency is a property of the SEATER, not of the class, and reading
    it off ``_PLUGIN_SEATED`` rather than off ``adapter.matches`` alone is
    what makes that true. A plugin declaring a core built-in's own impl
    class matches by identity, so it used to skip seating and, because
    only the seat loop records provenance, never appear in
    ``plugin_seated_names``. The built-in publisher then published the row
    it always publishes AND ``publish_plugins`` published the plugin's, and
    the operator got ``Registry.add``'s collision, which names the kind and
    the name but not the plugin that caused it. Same collision, one bootstrap
    phase later, with the one fact they needed removed.
    """
    to_seat: list[tuple[CapabilityAdapter, str, object]] = []
    for adapter, name, impl in planned:
        occupant = adapter.peek(name)
        if occupant is not None:
            if _PLUGIN_SEATED.get((adapter.kind, name)) == plugin.name and adapter.matches(occupant, impl):
                continue  # this plugin re-registering its own impl: a true no-op
            raise PluginError(
                f"system plugin {plugin.name!r} cannot seat {adapter.kind} {name!r}: "
                f"{_occupant_origin(adapter.kind, name)}"
            )
        to_seat.append((adapter, name, adapter.prepare(impl)))
    return to_seat


def plugin_seated_names(kind: str) -> frozenset[str]:
    """The ``kind`` capability names a system plugin seated into the core
    code registry (as opposed to the core built-ins).

    A migrated capability's impl still lives in the core registry (the plugin's
    adapter seats it there at import, and ``_impl_for`` stamps it onto the graph
    node from there), but its RESOURCE ROW must be published exactly once, by
    ``publish_plugins`` with a ``system-plugin`` origin. So each core capability
    built-in capability publisher skips the names reported here, leaving the
    plugin as the sole publisher of its row; publishing it there too would
    collide (built-in vs system-plugin) at ``Registry.add``. This reuses the same provenance the
    collision-message path uses (``_PLUGIN_SEATED``), so "seated by a plugin" has
    one source of truth."""
    return frozenset(name for (seated_kind, name) in _PLUGIN_SEATED if seated_kind == kind)


def seat_installed_plugins() -> None:
    """Make sure every INSTALLED plugin's capability implementations are in
    the core code registries.

    Seating is an import side effect: ``agentworks.plugins`` builds its
    installed index at import and registers each shipped plugin, so anything
    that has reached the resource machinery through ``build_registry`` has
    already paid for it. The surfaces that describe a host WITHOUT building a
    registry (schema emission, the rendered sample, the field reference) had
    not, and silently described a host missing every plugin-contributed
    capability: ``agw resource schema vm-site`` emitted lima and wsl2 while
    three platform plugins ship in-tree.

    So those surfaces call this instead of importing the package with a
    comment explaining why, and the requirement has a name.
    Enablement-independent: a plugin's implementations seat whether or not
    config opts into the plugin, because enablement is a property of the
    published ROW, not of the registry.

    **Read the body before relying on this.** The import IS the seating, so
    a CALLER that imports this function has already seated by the time it
    calls it, and this function body is a module-table hit. Two consequences
    worth stating rather than discovering:

    - it cannot RE-seat. If a registry is ever cleared (nothing does today;
      ``seated_plugin`` restores what it took), this has to become a real
      operation over ``SYSTEM_PLUGINS`` rather than an import.
    - it cannot be pinned in-process. A test that neuters it still passes
      as soon as anything in the same interpreter imports
      ``agentworks.plugins`` for ``Plugin`` or ``seated_plugin``, which
      three test modules do at module scope. The pin that means something
      is ``tests/manifests/test_spec_model.py``'s fresh interpreter.
    """
    import agentworks.plugins  # noqa: F401  (importing IS the seating)


def _occupant_origin(kind: str, name: str) -> str:
    """Describe the current occupant of ``(kind, name)`` for a collision
    message, distinguishing a core built-in from another system plugin."""
    other = _PLUGIN_SEATED.get((kind, name))
    if other is not None:
        return f"it is already published by system plugin {other!r}"
    return f"it collides with a core built-in {kind} of the same name"


@contextmanager
def seated_plugin(plugin: Plugin) -> Iterator[None]:
    """Seat ``plugin`` for the duration of the context, then restore the
    four capability registries (and the seating provenance) to their
    pre-context state, including on exception.

    Tests seat a fixture plugin through this helper without hand-snapshotting
    the global registries and without polluting later tests. It snapshots
    against a test-local index, never the shipped ``SYSTEM_PLUGINS``.
    """
    registries = _capability_registries()
    snapshots = [dict(registry) for registry in registries]
    provenance = dict(_PLUGIN_SEATED)
    try:
        register_plugin(plugin)
        yield
    finally:
        for registry, snapshot in zip(registries, snapshots, strict=True):
            registry.clear()
            registry.update(snapshot)
        _PLUGIN_SEATED.clear()
        _PLUGIN_SEATED.update(provenance)


def _capability_registries() -> tuple[dict[str, Any], ...]:
    """Every live capability registry, in descriptor-table order, for the
    snapshot/restore helper. Restore mutates these dicts IN PLACE (clear +
    update) because other modules hold references to them.

    Derived from the descriptor table rather than enumerated here:
    participation in snapshot/restore IS membership in the table, so a
    capability kind cannot be added without its registry being snapshotted.
    Called inside the function (not bound at import) for the table's cycle
    discipline."""
    return tuple(descriptor.registry() for descriptor in capability_descriptors())
