"""The ``env-var`` secret provider: reads operator-side environment
variables. A raw capability -- invoked only through a ``secret-backend``
resource's door methods.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.secrets.base import MappingValue, SecretDecl


def env_var_name_for(secret_name: str) -> str:
    """Default convention: secret 'github-token' -> 'AW_SECRET_GITHUB_TOKEN'.

    Note: the Python helper name stays snake_case (Python convention); the
    provider name ``env-var`` is kebab-case (operator-typed identifier).
    """
    return "AW_SECRET_" + secret_name.upper().replace("-", "_")


class EnvVarProvider:
    """Reads from operator-side environment variables.

    Identifier resolution (the ``False`` opt-out never reaches a
    provider -- the backend handles it):

    - mapping is a string: use it as the env var name.
    - mapping absent (or structured): derive ``AW_SECRET_<NAME>`` from
      the secret's name.

    Always attempts (a derived name always exists); an unset env var is
    a soft miss -- just-not-set, fall through to the next backend.
    """

    name = "env-var"
    interactive = False

    def validate_config(
        self, backend_name: str, config: Mapping[str, object]
    ) -> Mapping[str, object]:
        if config:
            raise ConfigError(
                f'secret-backend "{backend_name}": the {self.name} provider '
                f"accepts no configuration; got {sorted(config)}"
            )
        return {}

    def _resolved_name(self, secret: SecretDecl, mapping: MappingValue | None) -> str:
        if isinstance(mapping, str):
            return mapping
        return env_var_name_for(secret.name)

    def would_attempt(
        self,
        config: Mapping[str, object],
        secret: SecretDecl,
        mapping: MappingValue | None,
    ) -> bool:
        return True

    def describe_lookup(
        self,
        config: Mapping[str, object],
        secret: SecretDecl,
        mapping: MappingValue | None,
    ) -> str | None:
        return self._resolved_name(secret, mapping)

    def batch_get(
        self,
        config: Mapping[str, object],
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
