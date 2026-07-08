"""The ``prompt`` secret backend: interactive last-resort. A capability
implementation, consumed by the resolution loop through the
``SecretBackend`` API.

Resolves nothing when stdin is not a TTY or the CLI was invoked with
--non-interactive; the resolve loop then raises SecretUnavailableError.
A future controller-process caller omits the prompt backend from its
chain entirely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output

if TYPE_CHECKING:
    from agentworks.secrets.base import MappingValue, SecretDecl


class PromptBackend:
    """Interactive prompt backend.

    Always attempts (the opt-out ``backend_mappings.<backend> = false``
    is handled generically by the resolution loop). The opt-out is most
    useful for testing in an interactive shell -- the operator wants to
    verify the env-var path resolves cleanly without quietly falling
    through to a prompt. Non-interactive mode (no TTY /
    ``--non-interactive``) already makes prompt a no-op via the
    ``batch_get`` TTY check.

    ``interactive = True``: inspection previews must not probe this
    backend -- calling ``batch_get`` IS the operator interaction.
    """

    name = "prompt"
    description = "prompts interactively at resolution time"
    interactive = True

    def validate_mapping(self, owner: str, mapping: MappingValue) -> None:
        # Accept anything: prompt has no identifier vocabulary, and
        # released configs may carry mapping values it ignores.
        return

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
        # No static identifier: the "lookup" is the operator typing at
        # command time.
        return None

    def batch_get(
        self,
        wants: list[tuple[SecretDecl, MappingValue | None]],
    ) -> dict[str, str]:
        if not output.is_interactive():
            return {}
        # All prompts in one operator interaction: the "prompt once at
        # the start" UX, preserved even though prompt is just another
        # backend in the chain.
        return {secret.name: self._prompt_one(secret) for secret, _ in wants}

    @staticmethod
    def _prompt_one(secret: SecretDecl) -> str:
        label = f"Secret '{secret.name}': {secret.description}"
        return output.prompt_secret(label, hint=secret.hint)
