"""Env-var SecretSource: reads from operator-side environment variables."""

from __future__ import annotations

import os

from agentworks.secrets.base import SecretDecl, SecretSource


def env_var_name_for(secret_name: str) -> str:
    """Default convention: secret 'github-token' -> 'AW_SECRET_GITHUB_TOKEN'."""
    return "AW_SECRET_" + secret_name.upper().replace("-", "_")


class EnvVarSource(SecretSource):
    """Reads from operator-side environment variables.

    Resolution:

    - ``backend_mappings.env_var`` is ``False``: opt out; return None.
    - ``backend_mappings.env_var`` is a string: use that as the env var name.
    - Otherwise: derive ``AW_SECRET_<NAME>`` from the secret's name.

    ``would_attempt`` returns False only for explicit opt-out; the source
    always tries its derived or overridden env var name, even if that var
    isn't set in the current shell.
    """

    kind = "env_var"

    def _resolved_name(self, secret: SecretDecl) -> str | None:
        mapping = secret.backend_mappings.get(self.kind)
        if mapping is False:
            return None
        if isinstance(mapping, str):
            return mapping
        return env_var_name_for(secret.name)

    def would_attempt(self, secret: SecretDecl) -> bool:
        return self._resolved_name(secret) is not None

    def get(self, secret: SecretDecl) -> str | None:
        name = self._resolved_name(secret)
        if name is None:
            return None
        return os.environ.get(name)
