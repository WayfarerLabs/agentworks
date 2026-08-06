"""``register_plugin`` and the ``seated_plugin`` test helper (R2, R5, R6).

``register_plugin`` runs at import time (invoked by the installed index),
once per shipped plugin. It validates the WHOLE descriptor, then prechecks
every impl for a name collision AND prepares its registry payload (the
fallible ``secret-backend`` construction included), then seats the prepared
payloads. No capability registry is mutated until every impl across the
whole descriptor is known seatable AND its payload is built, so the seat
phase is pure dict writes that cannot fail partway: seating is
all-or-nothing by construction (no rollback path, so a mid-descriptor
failure leaving orphaned impls is unrepresentable).

This seating guard, NOT ``_check_collision``, is the enforcement point for
every capability name-clash (built-in/plugin and plugin/plugin alike): a
capability's published row name IS its impl's registry key, so a clash is
caught here at registration, before any capability row is ever built.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from agentworks.capabilities.conformance import conformance_error
from agentworks.capabilities.descriptor import descriptor_for
from agentworks.plugins.adapters import CAPABILITY_ADAPTERS
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


def register_plugin(plugin: Plugin) -> None:
    """Validate ``plugin`` wholesale, then seat its capability impls
    atomically. Idempotent per impl name; a cross-origin impl-name
    collision (built-in or another plugin) is a ``PluginError`` naming the
    plugin and the occupant's actual origin. Returns nothing and does not
    touch the index or publish rows; it only seats impls into the four code
    registries, exactly as core impls populate them at import.
    """
    planned = _validate_descriptor(plugin)
    to_seat = _precheck_and_prepare(plugin, planned)
    # Pass 3: seat all. Every payload was prepared (including the fallible
    # secret-backend construction) during the precheck with no registry
    # mutation, so this loop is pure dict writes that cannot fail partway.
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
    if not plugin.name or "/" in plugin.name:
        raise PluginError(f"system plugin name {plugin.name!r} must be non-empty and '/'-free")

    planned: list[tuple[CapabilityAdapter, str, type]] = []
    seen: set[tuple[str, str]] = set()
    for kind, impls in plugin.capabilities.items():
        adapter = CAPABILITY_ADAPTERS.get(kind)
        if adapter is None:
            known = ", ".join(sorted(CAPABILITY_ADAPTERS))
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

    A ``None`` occupant needs seating: its payload is prepared here (the
    fallible ``secret-backend`` construction included), so pass 3 is a pure
    write. A same-impl occupant is an idempotent no-op, skipped (not
    re-prepared, so it is never re-instantiated). A different occupant
    raises here, before any impl is seated.
    """
    to_seat: list[tuple[CapabilityAdapter, str, object]] = []
    for adapter, name, impl in planned:
        occupant = adapter.peek(name)
        if occupant is not None:
            if adapter.matches(occupant, impl):
                continue  # idempotent re-registration: a true no-op
            raise PluginError(
                f"system plugin {plugin.name!r} cannot seat {adapter.kind} {name!r}: "
                f"{_occupant_origin(adapter.kind, name)}"
            )
        try:
            payload = adapter.prepare(impl)
        except Exception as exc:  # noqa: BLE001 -- any impl constructor failure is a curation bug, re-typed here
            raise PluginError(
                f"system plugin {plugin.name!r} could not construct {adapter.kind} {name!r}: {exc}"
            ) from exc
        to_seat.append((adapter, name, payload))
    return to_seat


def plugin_seated_names(kind: str) -> frozenset[str]:
    """The ``kind`` capability names a system plugin seated into the core
    code registry (as opposed to the core built-ins).

    A migrated capability's impl still lives in the core registry (the plugin's
    adapter seats it there at import, and ``_impl_for`` stamps it onto the graph
    node from there), but its RESOURCE ROW must be published exactly once, by
    ``publish_plugins`` with a ``system-plugin`` origin. So each core capability
    ``publish_to`` skips the names reported here, leaving the plugin as the sole
    publisher of its row; publishing it here too would collide (built-in vs
    system-plugin) at ``Registry.add``. This reuses the same provenance the
    collision-message path uses (``_PLUGIN_SEATED``), so "seated by a plugin" has
    one source of truth."""
    return frozenset(name for (seated_kind, name) in _PLUGIN_SEATED if seated_kind == kind)


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
    """The four live capability registries, in a stable order, for the
    snapshot/restore helper. Restore mutates these dicts IN PLACE (clear +
    update) because other modules hold references to them."""
    from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY
    from agentworks.capabilities.harness_integration import HARNESS_INTEGRATION_REGISTRY
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

    return (
        VM_PLATFORM_REGISTRY,
        HARNESS_INTEGRATION_REGISTRY,
        GIT_CREDENTIAL_PROVIDER_REGISTRY,
        SECRET_BACKEND_REGISTRY,
    )
