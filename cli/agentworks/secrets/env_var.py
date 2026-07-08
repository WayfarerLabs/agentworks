"""The ``env-var`` secret backend: reads operator-side environment
variables. A capability implementation, consumed by the resolution loop through
the ``SecretBackend`` API.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from agentworks.secrets.base import MappingValue, SecretDecl


def env_var_name_for(secret_name: str) -> str:
    """Default convention: secret 'github-token' -> 'AW_SECRET_GITHUB_TOKEN'.

    Note: the Python helper name stays snake_case (Python convention); the
    backend name ``env-var`` is kebab-case (operator-typed identifier).
    """
    return "AW_SECRET_" + secret_name.upper().replace("-", "_")


class EnvVarBackend:
    """Reads from operator-side environment variables.

    Identifier resolution (the ``False`` opt-out never reaches a
    backend -- the resolution loop handles it):

    - mapping is a string: use it as the env var name.
    - mapping absent (or structured): derive ``AW_SECRET_<NAME>`` from
      the secret's name.

    Always attempts (a derived name always exists); an unset env var is
    a soft miss -- just-not-set, fall through to the next backend.
    """

    name = "env-var"
    description = "resolves from AW_SECRET_<NAME> environment variables"
    interactive = False

    def validate_mapping(self, owner: str, mapping: MappingValue) -> None:
        # The load-time gate; ``_resolved_name`` keeps its own check as
        # defense in depth for hand-built decls that never pass through
        # validate_chain.
        if not isinstance(mapping, str) or not mapping:
            raise ConfigError(
                f"{owner}: backend_mappings for the env-var backend must "
                f"be a non-empty string (an env var name) or false"
            )

    def _resolved_name(self, secret: SecretDecl, mapping: MappingValue | None) -> str:
        if isinstance(mapping, str):
            return mapping
        if mapping is not None:
            # A structured (dict) mapping has no meaning for env-var;
            # silently applying the default convention would resolve
            # from a different identifier than the operator wrote.
            raise ConfigError(
                f"secret {secret.name!r}: backend_mappings for the "
                f"env-var backend must be a non-empty string (an env "
                f"var name) or false"
            )
        return env_var_name_for(secret.name)

    def would_attempt(
        self,
        secret: SecretDecl,
        mapping: MappingValue | None,
    ) -> bool:
        return True

    def describe_lookup(
        self,
        secret: SecretDecl,
        mapping: MappingValue | None,
    ) -> str | None:
        return self._resolved_name(secret, mapping)

    def batch_get(
        self,
        wants: list[tuple[SecretDecl, MappingValue | None]],
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        for secret, mapping in wants:
            raw = os.environ.get(self._resolved_name(secret, mapping))
            if raw is None:
                continue
            # Strip trailing carriage-returns / newlines. Tokens copied
            # from `op read`, `pbpaste`, vim-yanked lines, etc. routinely
            # carry one. Embedded newlines (rare; usually a malformed
            # secret) are surfaced by the resolve loop so the operator
            # sees a clear error instead of an opaque SSH SetEnv
            # rejection.
            out[secret.name] = raw.rstrip("\r\n")
        return out
