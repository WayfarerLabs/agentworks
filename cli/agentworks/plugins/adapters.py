"""The per-capability-kind ``CapabilityAdapter`` table (R5, R6).

One adapter per core capability kind reconciles the four registries behind
a uniform peek / match / prepare / seat / build-row contract. Every registry
stores the exact implementation class. ``matches`` therefore uses identity,
``prepare`` is a non-constructing pass-through, and ``build_row`` reads only
the seated class from the live registry.

Reaching the registries through ``descriptor.registry()`` means this module
names none of them: the sanctioned registry read moved to the four
capability ``kinds.py`` modules that own the descriptors, and the graph
guard's allow-list no longer exempts this file.
"""

from __future__ import annotations

from functools import cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from agentworks.capabilities.descriptor import capability_descriptors
from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.descriptor import CapabilityKindDescriptor
    from agentworks.origin import Origin


class CapabilityAdapter(Protocol):
    """The uniform contract each capability kind implements.

    Seating is split into ``prepare`` (build the registry payload, no
    mutation) and ``seat`` (write the prepared payload), so registration
    finishes every conformance and collision check before it touches a
    registry. ``matches`` is the exact-class idempotency check.

    Neither half can fail today: every registry stores the class itself, so
    ``prepare`` is a pass-through. The split is what ``register_plugin``
    orders its work around, not a claim about where errors come from.
    """

    kind: str

    def peek(self, name: str) -> object | None:
        """The current occupant of ``name`` in the kind's registry (for the
        collision precheck), or ``None``."""
        ...

    def matches(self, occupant: object, impl_cls: type) -> bool:
        """Whether ``occupant`` is the SAME impl as ``impl_cls`` (an
        idempotent re-registration), by exact identity."""
        ...

    def prepare(self, impl_cls: type) -> object:
        """Return the class registry payload without constructing it."""
        ...

    def seat(self, name: str, payload: object) -> None:
        """Write a prepared ``payload`` into the kind's registry under
        ``name``. A pure dict write that cannot throw."""
        ...

    def build_row(self, name: str, origin: Origin) -> Any:
        """The kind's ``Entry`` dataclass for the seated ``name``, stamped
        with ``origin``. Raises ``StateError`` if ``name`` is not
        seated."""
        ...


class _DescriptorAdapter:
    """The ``CapabilityAdapter`` for whichever kind ``descriptor`` describes."""

    def __init__(self, descriptor: CapabilityKindDescriptor) -> None:
        self.descriptor = descriptor
        self.kind = descriptor.kind

    def peek(self, name: str) -> object | None:
        return self.descriptor.registry().get(name)

    def matches(self, occupant: object, impl_cls: type) -> bool:
        return occupant is impl_cls

    def prepare(self, impl_cls: type) -> object:
        return impl_cls

    def seat(self, name: str, payload: object) -> None:
        self.descriptor.registry()[name] = payload

    def build_row(self, name: str, origin: Origin) -> Any:
        seated = self.descriptor.registry().get(name)
        if seated is None:
            raise StateError(_unseated_message(self.kind, name))
        return self.descriptor.entry_factory(name, seated, origin)


def _unseated_message(kind: str, name: str) -> str:
    return (
        f"cannot build a {kind} row for {name!r}: no seated implementation "
        f"(publication is tied to seating; this is a framework/publisher bug)"
    )


@cache
def capability_adapters() -> Mapping[str, CapabilityAdapter]:
    """The adapter per capability kind, keyed by kind.

    Derived from the descriptor table, so the key set cannot drift from the
    capability kinds and a future kind arrives with a working adapter rather
    than a missing one. Read-only and cached: the adapters are stateless
    views onto their descriptors, so one per kind is enough.

    An accessor rather than a module-level constant, for UNIFORMITY with the
    derived sites where laziness is forced. Verified, rather than assumed:
    no cycle is reachable here today, because none of the four contributing
    ``kinds.py`` modules loads anything under ``agentworks.plugins``. The
    graph's accessors are the forced ones (the capability packages do load
    ``resources.graph``), and every switchboard site reading the table the
    same way is worth more than each site relitigating whether its own
    import graph happens to permit a constant. It also keeps the load
    boundary where the descriptor module puts it: the table is collected on
    first use, not at import of whoever imports this.
    """
    return MappingProxyType({d.kind: _DescriptorAdapter(d) for d in capability_descriptors()})
