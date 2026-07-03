"""Framework strategy for the ``secret-backend`` kind: the backend
kinds referenced by ``[secret_backends.<kind>]`` and
``[secret_config].backends``.

The kind uses the error miss policy. Known backend implementations
(``env-var``, ``prompt``) are published as built-in rows by the
``agentworks.secrets`` publisher; operator-declared
``[secret_backends.<kind>]`` blocks re-publish the same row with
operator-declared origin (same pattern as catalog overrides).

Phase 2b.2 partial migration: this kind landing makes the per-backend
config queryable via ``agw resource list --kind secret-backend`` and
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
    """Implementation of ``ResourceKind`` for ``"secret-backend"``."""

    kind: str = "secret-backend"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    manifest_declarable: bool = True
    # "allow" preserves today's TOML behavior ([secret_backends.env-var]
    # replaces the built-in row) through the dual-source window. Phase 3
    # enforces reserved names for manifest-declared backends at the
    # manifest publisher (origin variants can't distinguish TOML from
    # manifest rows); this flag flips to "reserved" at the Phase 5
    # cutover when the TOML surface is deleted.
    builtin_override: Literal["allow", "reserved"] = "allow"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        raise NoUnreferencedDefaultError(
            "the secret_backend kind has miss_policy='error'; "
            "synthesize should never be invoked (the framework raises "
            "ConfigError first)"
        )


KIND_REGISTRY["secret-backend"] = _SecretBackendKind()
