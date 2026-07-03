"""Framework strategy for the ``secret-backend`` kind: named
instantiations of secret providers, activated by the
``[secret_config].backends`` chain (which is config, not a resource --
the chain's names are validated by ``secrets.validate_chain`` at
``build_registry``, not by this kind's miss policy).

The kind uses the error miss policy. The built-in backends (``env-var``,
``prompt``) ship as bundled manifests (``agentworks/manifests/builtin/
secret-backends.yaml``) as ``SecretBackendDecl`` rows; operator-declared
backends arrive as manifests too. Their names are reserved via
``builtin_override = "reserved"``: an operator manifest colliding with a
bundled row is a ``ConfigError`` at ``Registry.add``. Legacy TOML
``[secret_backends.<kind>]`` sections stopped publishing entirely (they
were semantically empty; the loader warns them as deprecated no-ops).
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
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        raise NoUnreferencedDefaultError(
            "the secret_backend kind has miss_policy='error'; "
            "synthesize should never be invoked (the framework raises "
            "ConfigError first)"
        )


KIND_REGISTRY["secret-backend"] = _SecretBackendKind()
