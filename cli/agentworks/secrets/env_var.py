"""Env-var SecretSource: reads from operator-side environment variables."""

from __future__ import annotations

import os

from agentworks.secrets.base import SecretDecl, SecretSourceBase


def env_var_name_for(secret_name: str) -> str:
    """Default convention: secret 'github-token' -> 'AW_SECRET_GITHUB_TOKEN'.

    Note: the Python helper name stays snake_case (Python convention); the
    backend-kind string ``env-var`` is kebab-case (operator-typed identifier).
    """
    return "AW_SECRET_" + secret_name.upper().replace("-", "_")


class EnvVarSource(SecretSourceBase):
    """Reads from operator-side environment variables.

    Resolution:

    - ``backend_mappings.env-var`` is ``False``: opt out; return None.
    - ``backend_mappings.env-var`` is a string: use that as the env var name.
    - Otherwise: derive ``AW_SECRET_<NAME>`` from the secret's name.

    ``would_attempt`` returns False only for explicit opt-out; the source
    always tries its derived or overridden env var name, even if that var
    isn't set in the current shell.

    The kind string ``env-var`` is kebab-case (operator-typed identifier
    convention); the Python module / class / helper stays snake_case
    (Python convention). See HLA "Naming conventions".
    """

    kind = "env-var"

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
        raw = os.environ.get(name)
        if raw is None:
            return None
        # Strip trailing carriage-returns / newlines. Tokens copied from
        # `op read`, `pbpaste`, vim-yanked lines, etc. routinely carry
        # one. Embedded newlines (rare; usually a malformed secret) are
        # surfaced by the resolver's resolve_all layer so the operator
        # sees a clear error instead of an opaque SSH SetEnv rejection.
        return raw.rstrip("\r\n")

    def describe_lookup(self, secret: SecretDecl) -> str | None:
        return self._resolved_name(secret)
