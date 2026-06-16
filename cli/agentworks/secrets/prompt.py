"""Prompt SecretSource: interactive last-resort.

Just another SecretSource. Returns None when stdin is not a TTY or the CLI
was invoked with --non-interactive; the resolver then raises
SecretUnavailableError. A future controller-process caller omits this
source entirely from its backends list.
"""

from __future__ import annotations

from agentworks import output
from agentworks.secrets.base import SecretDecl, SecretSourceBase


class PromptSource(SecretSourceBase):
    """Interactive prompt source.

    ``would_attempt`` returns True for any secret unless the operator opted
    out via ``backend_mappings.prompt = false``. That opt-out matches the
    contract on ``SecretDecl.backend_mappings``. It's most useful for testing
    in an interactive shell -- the operator wants to verify the env-var path
    resolves cleanly without quietly falling through to a prompt when the
    env var happens to be unset. Non-interactive mode (no TTY /
    ``--non-interactive``) already makes prompt a no-op via ``get``'s TTY
    check, so the opt-out is not needed there.

    The runtime decision to actually prompt or return None is made inside
    ``get`` / ``batch_get`` based on ``output.is_interactive()``.

    ``batch_get`` emits all prompts in one operator interaction so the
    "prompt once at the start" UX is preserved even though prompt is just
    another source in the chain.
    """

    kind = "prompt"

    def would_attempt(self, secret: SecretDecl) -> bool:
        return secret.backend_mappings.get(self.kind) is not False

    def get(self, secret: SecretDecl) -> str | None:
        if not self.would_attempt(secret):
            return None
        if not output.is_interactive():
            return None
        return self._prompt_one(secret)

    def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
        if not output.is_interactive():
            return {}
        return {
            s.name: self._prompt_one(s)
            for s in secrets
            if self.would_attempt(s)
        }

    @staticmethod
    def _prompt_one(secret: SecretDecl) -> str:
        label = f"Secret '{secret.name}': {secret.description}"
        return output.prompt_secret(label, hint=secret.hint)
