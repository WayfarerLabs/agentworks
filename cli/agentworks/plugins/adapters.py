"""The per-capability-kind ``CapabilityAdapter`` table (R5, R6).

One adapter per core capability kind, reconciling the four heterogeneous
capability registries behind a uniform peek / match / prepare / seat /
build-row contract. The adapters are the single place that knows each
kind's registry, its ``Entry`` dataclass, and the one asymmetry that
matters: ``secret-backend`` holds a constructed instance, the other three
hold the class.

Three design points the LLD (and the Phase 2 review) pin:

- **The instance trap is confined to ``prepare``.** ``secret-backend`` is
  the one kind whose registry holds a constructed instance, so its adapter
  alone calls ``impl_cls()`` (once). Crucially that construction happens in
  ``prepare`` (fallible, no mutation), NOT in ``seat``: ``register_plugin``
  runs every ``prepare`` during its collision precheck, before touching any
  registry, so the seat phase is pure dict writes that cannot fail partway.
  That is what makes atomicity true by construction rather than by hope.
- **``matches`` is exact identity.** The idempotency rule is "the SAME
  class"; ``secret-backend`` compares ``type(occupant) is impl_cls`` (the
  occupant is a constructed instance), never ``isinstance`` (which would
  merge a subclass under the same name).
- **``build_row`` reads description off the SEATED impl** (the live
  registry occupant), never re-instantiating and never trusting an
  unseated descriptor claim. If the name is unseated, ``build_row`` raises
  ``StateError`` (a publisher-invariant violation), the by-construction tie
  between publication and seating.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.git_credential.base import GitCredentialProvider
    from agentworks.capabilities.harness.base import Harness
    from agentworks.capabilities.vm_platform.base import VMPlatform
    from agentworks.resources.origin import Origin
    from agentworks.secrets.backends import SecretBackend


class CapabilityAdapter(Protocol):
    """The uniform contract each capability kind implements.

    Seating is split into a FALLIBLE ``prepare`` (build the registry
    payload, no mutation) and a PURE ``seat`` (write the prepared payload),
    so all failure-prone work happens before any registry is touched.
    ``matches`` is the per-kind idempotency check: the class-vs-instance
    reconciliation the descriptor deliberately leaves to the adapter.
    Keeping both here confines the one asymmetry to the adapters rather
    than leaking a ``secret-backend`` special-case into ``register_plugin``.
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


class _VMPlatformAdapter:
    kind = "vm-platform"

    def peek(self, name: str) -> object | None:
        from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

        return VM_PLATFORM_REGISTRY.get(name)

    def matches(self, occupant: object, impl_cls: type) -> bool:
        return occupant is impl_cls

    def prepare(self, impl_cls: type) -> object:
        return impl_cls

    def seat(self, name: str, payload: object) -> None:
        from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

        VM_PLATFORM_REGISTRY[name] = cast("type[VMPlatform]", payload)

    def build_row(self, name: str, origin: Origin) -> Any:
        from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY, VMPlatformEntry

        seated = VM_PLATFORM_REGISTRY.get(name)
        if seated is None:
            raise StateError(_unseated_message(self.kind, name))
        return VMPlatformEntry(name=name, description=seated.description, origin=origin)


class _HarnessAdapter:
    kind = "harness-integration"

    def peek(self, name: str) -> object | None:
        from agentworks.capabilities.harness import HARNESS_REGISTRY

        return HARNESS_REGISTRY.get(name)

    def matches(self, occupant: object, impl_cls: type) -> bool:
        return occupant is impl_cls

    def prepare(self, impl_cls: type) -> object:
        return impl_cls

    def seat(self, name: str, payload: object) -> None:
        from agentworks.capabilities.harness import HARNESS_REGISTRY

        HARNESS_REGISTRY[name] = cast("type[Harness]", payload)

    def build_row(self, name: str, origin: Origin) -> Any:
        from agentworks.capabilities.harness import HARNESS_REGISTRY
        from agentworks.capabilities.harness.kinds import HarnessEntry

        if HARNESS_REGISTRY.get(name) is None:
            raise StateError(_unseated_message(self.kind, name))
        return HarnessEntry(name=name, origin=origin)


class _GitCredentialProviderAdapter:
    kind = "git-credential-provider"

    def peek(self, name: str) -> object | None:
        from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY

        return GIT_CREDENTIAL_PROVIDER_REGISTRY.get(name)

    def matches(self, occupant: object, impl_cls: type) -> bool:
        return occupant is impl_cls

    def prepare(self, impl_cls: type) -> object:
        return impl_cls

    def seat(self, name: str, payload: object) -> None:
        from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY

        GIT_CREDENTIAL_PROVIDER_REGISTRY[name] = cast("type[GitCredentialProvider]", payload)

    def build_row(self, name: str, origin: Origin) -> Any:
        from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY
        from agentworks.capabilities.git_credential.kinds import GitCredentialProviderEntry

        if GIT_CREDENTIAL_PROVIDER_REGISTRY.get(name) is None:
            raise StateError(_unseated_message(self.kind, name))
        return GitCredentialProviderEntry(name=name, origin=origin)


class _SecretBackendAdapter:
    kind = "secret-backend"

    def peek(self, name: str) -> object | None:
        from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

        return SECRET_BACKEND_REGISTRY.get(name)

    def matches(self, occupant: object, impl_cls: type) -> bool:
        # The occupant is a constructed instance, not the class. EXACT
        # identity, never isinstance: a subclass under the same name is a
        # genuine collision, not an idempotent match.
        return type(occupant) is impl_cls

    def prepare(self, impl_cls: type) -> object:
        # The one kind whose registry holds a constructed INSTANCE; the
        # instance trap (``impl_cls()``) is confined here, and it runs
        # during the precheck so a throwing constructor never leaves a
        # partially-seated descriptor behind.
        return cast("SecretBackend", impl_cls())

    def seat(self, name: str, payload: object) -> None:
        from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

        SECRET_BACKEND_REGISTRY[name] = cast("SecretBackend", payload)

    def build_row(self, name: str, origin: Origin) -> Any:
        from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY
        from agentworks.secrets.kinds import SecretBackendEntry

        seated = SECRET_BACKEND_REGISTRY.get(name)
        if seated is None:
            raise StateError(_unseated_message(self.kind, name))
        return SecretBackendEntry(name=name, description=seated.description, origin=origin)


def _unseated_message(kind: str, name: str) -> str:
    return (
        f"cannot build a {kind} row for {name!r}: no seated implementation "
        f"(publication is tied to seating; this is a framework/publisher bug)"
    )


CAPABILITY_ADAPTERS: Mapping[str, CapabilityAdapter] = {
    "vm-platform": _VMPlatformAdapter(),
    "harness-integration": _HarnessAdapter(),
    "git-credential-provider": _GitCredentialProviderAdapter(),
    "secret-backend": _SecretBackendAdapter(),
}
"""Keyed by capability kind. A guard test pins this key set equal to the
``category == "capability"`` kinds in ``KIND_REGISTRY``, so a future
capability kind fails until its adapter exists (R6: plugins contribute
existing kinds only, by construction not convention)."""
