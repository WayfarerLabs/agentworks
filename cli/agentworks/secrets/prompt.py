"""Prompt SecretSource: interactive last-resort.

Just another SecretSource. Returns None when stdin is not a TTY or the CLI
was invoked with --non-interactive; the resolver then raises
SecretUnavailableError. A future controller-process caller omits this
source entirely from its backends list.
"""

from __future__ import annotations

from agentworks import output
from agentworks.secrets.base import SecretDecl, SecretSource


class PromptSource(SecretSource):
    """Interactive prompt source.

    ``would_attempt`` returns True for any secret: prompting always applies
    when the source is in the chain. The runtime decision to actually prompt
    or return None is made inside ``get`` / ``batch_get`` based on
    ``output.is_interactive()``.

    ``batch_get`` emits all prompts in one operator interaction so the
    "prompt once at the start" UX is preserved even though prompt is just
    another source in the chain.
    """

    kind = "prompt"

    def would_attempt(self, secret: SecretDecl) -> bool:  # noqa: ARG002 - intentionally ignores
        return True

    def get(self, secret: SecretDecl) -> str | None:
        if not output.is_interactive():
            return None
        return self._prompt_one(secret)

    def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
        if not output.is_interactive():
            return {}
        return {s.name: self._prompt_one(s) for s in secrets}

    @staticmethod
    def _prompt_one(secret: SecretDecl) -> str:
        label = f"Secret '{secret.name}': {secret.description}"
        return output.prompt_secret(label, hint=secret.hint)
