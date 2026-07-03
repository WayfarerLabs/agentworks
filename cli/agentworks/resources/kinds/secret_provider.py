"""``SecretProviderKind``: framework strategy for the ``"secret-provider"``
descriptor kind.

Providers are code capabilities (``agentworks.secrets.providers``); the
registry rows exist so ``secret-backend.provider`` references validate
through the framework's uniform miss policy and providers are visible in
``agw resource list``. Read-only: not manifest-declarable, reserved
against operator override.
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
class SecretProviderEntry:
    """A name-keyed marker for one secret provider implementation.

    The actual capability (validate_config / instantiate) lives in
    ``agentworks.secrets.providers.PROVIDER_REGISTRY``; this row is what
    a ``secret-backend``'s ``provider`` field resolves against in the
    framework.
    """

    name: str
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


@dataclass(frozen=True)
class _SecretProviderKind:
    """Implementation of ``ResourceKind`` for ``"secret-provider"``."""

    kind: str = "secret-provider"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    manifest_declarable: bool = False  # descriptor rows come from the app
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> SecretProviderEntry:
        raise NoUnreferencedDefaultError(
            "the secret-provider kind has miss_policy='error'; synthesize "
            "should never be dispatched"
        )


KIND_REGISTRY["secret-provider"] = _SecretProviderKind()
