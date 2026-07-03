"""Framework strategy for the ``secret-backend`` kind: named
instantiations of secret providers, referenced by
``[secret_config].backends``.

The kind uses the error miss policy. The built-in backends (``env-var``,
``prompt``) ship as bundled manifests (``agentworks/manifests/builtin/
secret-backends.yaml``) as ``SecretBackendDecl`` rows; operator-declared
backends arrive as manifests too, while legacy TOML
``[secret_backends.<kind>]`` blocks (``SecretBackendConfig`` rows)
override the bundled rows until the cutover deletes that path. The
``[secret_config].backends`` chain is validated at resolver assembly
(``agentworks.secrets.providers.resolver_for``); promoting
``SecretConfig`` itself to a framework kind remains a follow-up.
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
