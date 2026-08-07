"""The per-capability-kind ``CapabilityAdapter`` table (R5, R6).

One adapter per core capability kind, reconciling the four heterogeneous
capability registries behind a uniform peek / match / prepare / seat /
build-row contract. There used to be four hand-written adapters; there is
now one :class:`_DescriptorAdapter`, parameterized by the kind's
``CapabilityKindDescriptor``, because everything the four copies differed
in (which registry, which ``Entry`` row, whether the registry holds the
class or a constructed instance) is a descriptor field.

Three design points the LLD (and the Phase 2 review) pin, preserved here by
construction: they are exactly the ``registry_policy`` branch plus the
seated read.

- **The instance trap is confined to ``prepare``.** ``secret-backend`` is
  the one kind whose registry holds a constructed instance
  (``RegistryPolicy.CONSTRUCTED_SINGLETON``), so it alone calls
  ``impl_cls()`` (once). Crucially that construction happens in
  ``prepare`` (fallible, no mutation), NOT in ``seat``: ``register_plugin``
  runs every ``prepare`` during its collision precheck, before touching any
  registry, so the seat phase is pure dict writes that cannot fail partway.
  That is what makes atomicity true by construction rather than by hope.
- **``matches`` is exact identity.** The idempotency rule is "the SAME
  class"; under the constructed-singleton policy the occupant is an
  instance, so the comparison is ``type(occupant) is impl_cls``, never
  ``isinstance`` (which would merge a subclass under the same name).
- **``build_row`` reads the SEATED impl** (the live registry occupant),
  never re-instantiating and never trusting an unseated descriptor claim.
  If the name is unseated, ``build_row`` raises ``StateError`` (a
  publisher-invariant violation), the by-construction tie between
  publication and seating.

Reaching the registries through ``descriptor.registry()`` means this module
names none of them: the sanctioned registry read moved to the four
capability ``kinds.py`` modules that own the descriptors, and the graph
guard's allow-list no longer exempts this file.
"""

from __future__ import annotations

from functools import cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from agentworks.capabilities.descriptor import RegistryPolicy, capability_descriptors
from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.descriptor import CapabilityKindDescriptor
    from agentworks.origin import Origin


class CapabilityAdapter(Protocol):
    """The uniform contract each capability kind implements.

    Seating is split into a FALLIBLE ``prepare`` (build the registry
    payload, no mutation) and a PURE ``seat`` (write the prepared payload),
    so all failure-prone work happens before any registry is touched.
    ``matches`` is the per-kind idempotency check: the class-vs-instance
    reconciliation the descriptor's ``registry_policy`` records and this
    contract acts on. Keeping both here confines the one asymmetry to the
    adapter rather than leaking a ``secret-backend`` special-case into
    ``register_plugin``.
    """

    kind: str

    def peek(self, name: str) -> object | None:
        """The current occupant of ``name`` in the kind's registry (for the
        collision precheck), or ``None``."""
        ...

    def matches(self, occupant: object, impl_cls: type) -> bool:
        """Whether ``occupant`` is the SAME impl as ``impl_cls`` (an
        idempotent re-registration), reconciling the class-vs-instance
        asymmetry per kind by exact identity."""
        ...

    def prepare(self, impl_cls: type) -> object:
        """Build the registry payload for ``impl_cls`` (the class itself for
        the three class-kinds; a freshly CONSTRUCTED instance for
        ``secret-backend``). Fallible and side-effect-free: it may raise
        (a backend constructor throwing) but must not mutate any
        registry."""
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
    """The ``CapabilityAdapter`` for whichever kind ``descriptor`` describes.

    Every method reads a descriptor field. Only the class-vs-instance
    asymmetry branches, on ``registry_policy``, and only in ``matches`` and
    ``prepare``. Retiring ``CONSTRUCTED_SINGLETON`` is therefore a matter of
    flipping ``secret-backend``'s field and deleting each branch's second
    arm.
    """

    def __init__(self, descriptor: CapabilityKindDescriptor) -> None:
        self.descriptor = descriptor
        self.kind = descriptor.kind

    def _constructs(self) -> bool:
        return self.descriptor.registry_policy is RegistryPolicy.CONSTRUCTED_SINGLETON

    def peek(self, name: str) -> object | None:
        return self.descriptor.registry().get(name)

    def matches(self, occupant: object, impl_cls: type) -> bool:
        # Under the constructed-singleton policy the occupant is an
        # instance, so identity is asked of its TYPE. Exact either way,
        # never isinstance: a subclass under the same name is a genuine
        # collision, not an idempotent match.
        if self._constructs():
            return type(occupant) is impl_cls
        return occupant is impl_cls

    def prepare(self, impl_cls: type) -> object:
        # The instance trap lives here and nowhere else, and it runs during
        # the precheck, so a throwing constructor never leaves a
        # partially-seated descriptor behind.
        if self._constructs():
            return impl_cls()
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
