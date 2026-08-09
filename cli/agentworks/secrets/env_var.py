"""The ``env-var`` secret backend: reads operator-side environment
variables. A capability implementation, consumed by the resolution loop through
the ``SecretBackend`` API.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from agentworks.errors import ConfigError
from agentworks.schema import AgwRootModel, NonEmptyStr
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from agentworks.resources.graph import Readiness
    from agentworks.secrets.base import MappingValue, SecretDecl


def env_var_name_for(secret_name: str) -> str:
    """Default convention: secret 'github-token' -> 'AW_SECRET_GITHUB_TOKEN'.

    Note: the Python helper name stays snake_case (Python convention); the
    backend name ``env-var`` is kebab-case (operator-typed identifier).
    """
    return "AW_SECRET_" + secret_name.upper().replace("-", "_")


class EnvVarMapping(AgwRootModel[NonEmptyStr]):
    """An env-var mapping is the NAME of the environment variable to read.

    A bare string, not a table, which is why it is a root model: there is
    no key vocabulary here, only the identifier. Omit the mapping to use
    the ``AW_SECRET_<NAME>`` convention derived from the secret's own
    name.
    """


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

    contract_version = 1
    config_model: type[AgwRootModel[Any]] = EnvVarMapping
    name = "env-var"
    description = "resolves from AW_SECRET_<NAME> environment variables"
    prose = TopicProse(
        title="Environment variables",
        overview="""
        Reads a secret's value from an environment variable. Every secret has one by
        convention, `AW_SECRET_` plus its name upper-cased with hyphens as underscores,
        so a secret needs no mapping at all to be resolvable this way.

        A `backend_mappings.env-var` entry overrides that name with the variable you
        actually have. An unset variable is a soft miss, not an error: resolution falls
        through to the next backend in the chain.
        """,
    )
    interactive = False

    def not_ready(self) -> Readiness:
        """Always ready: reading an environment variable needs no host tool
        (an unset variable is a per-secret soft miss at resolution, not a
        backend-level readiness failure)."""
        from agentworks.resources.graph import Readiness

        return Readiness.ready()

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
