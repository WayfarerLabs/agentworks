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
    contract on ``SecretDecl.backend_mappings`` and is the way to force a
    secret to error rather than fall through to interactive prompt -- useful
    for testing (the secret must come from env-var) or for non-interactive
    pipelines where a prompt would defeat the point.

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
