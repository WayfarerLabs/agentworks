"""Framework strategy for the ``secret_backend`` kind: the backend
kinds referenced by ``[secret_backends.<kind>]`` and
``[secret_config].backends``.

The kind uses the error miss policy. Known backend implementations
(``env-var``, ``prompt``) are published as built-in rows by the
``agentworks.secrets`` publisher; operator-declared
``[secret_backends.<kind>]`` blocks re-publish the same row with
operator-declared origin (same pattern as catalog overrides).

Phase 2b.2 partial migration: this kind landing makes the per-backend
config queryable via ``agw resource list --kind secret_backend`` and
restricts what names land via the operator-declared publish path. The
``[secret_config].backends`` active-chain validation (config.py's
``_build_secret_resolver``) keeps its bespoke check for now because
``SecretConfig`` isn't a Resource today; promoting it to a framework
kind is a follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.resources.kind import KIND_REGISTRY, NoUnreferencedDefaultError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True)
class _SecretBackendKind:
    """Implementation of ``ResourceKind`` for ``"secret_backend"``."""

    kind: str = "secret_backend"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        raise NoUnreferencedDefaultError(
            "the secret_backend kind has miss_policy='error'; "
            "synthesize should never be invoked (the framework raises "
            "ConfigError first)"
        )


KIND_REGISTRY["secret_backend"] = _SecretBackendKind()
