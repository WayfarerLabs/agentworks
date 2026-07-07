"""``SecretBackendKind``: framework strategy for the ``"secret-backend"``
capability kind.

Backends are code capabilities (``agentworks.secrets.backends``); the
registry rows exist so the ``[secret_config].backends`` chain and
per-secret ``backend_mappings`` validate through the framework's
uniform miss policy and the backends are visible in
``agw resource list``. Read-only: not manifest-declarable (the
capability collapse, 2026-07-07, removed the declarable instantiation
layer this kind once carried -- see ADR 0016).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.resources.kind import KIND_REGISTRY, NoUnreferencedDefaultError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry, ResourceReference


@dataclass(frozen=True)
class SecretBackendEntry:
    """The capability resource for one registered secret backend.

    The actual capability (the ``SecretBackend`` API) lives in
    ``agentworks.secrets.backends.SECRET_BACKEND_REGISTRY``; this row is
    what the chain and mapping names resolve against in the framework.
    ``description`` comes from the capability, for inspection surfaces.
    """

    name: str
    description: str = ""
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


@dataclass(frozen=True)
class _SecretBackendKind:
    """Implementation of ``ResourceKind`` for ``"secret-backend"``."""

    kind: str = "secret-backend"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    manifest_declarable: bool = False  # capability resources come from the app
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> SecretBackendEntry:
        raise NoUnreferencedDefaultError(
            "the secret-backend kind has miss_policy='error'; synthesize "
            "should never be dispatched"
        )


KIND_REGISTRY["secret-backend"] = _SecretBackendKind()
