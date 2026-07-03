"""The ``prompt`` secret provider: interactive last-resort. A raw
capability -- invoked only through a ``secret-backend`` resource's door
methods.

Resolves nothing when stdin is not a TTY or the CLI was invoked with
--non-interactive; the resolve loop then raises SecretUnavailableError.
A future controller-process caller omits the prompt backend from its
chain entirely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.secrets.base import MappingValue, SecretDecl


class PromptProvider:
    """Interactive prompt provider.

    Always attempts (the opt-out ``backend_mappings.<backend> = false``
    is handled generically at the backend door). The opt-out is most
    useful for testing in an interactive shell -- the operator wants to
    verify the env-var path resolves cleanly without quietly falling
    through to a prompt. Non-interactive mode (no TTY /
    ``--non-interactive``) already makes prompt a no-op via the
    ``batch_get`` TTY check.

    ``interactive = True``: inspection previews must not probe this
    provider -- calling ``batch_get`` IS the operator interaction.
    """

    name = "prompt"
    interactive = True

    def validate_config(
        self, backend_name: str, config: Mapping[str, object]
    ) -> Mapping[str, object]:
        if config:
            raise ConfigError(
                f'secret-backend "{backend_name}": the {self.name} provider '
                f"accepts no configuration; got {sorted(config)}"
            )
        return {}

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
        # No static identifier: the "lookup" is the operator typing at
        # command time.
        return None

    def batch_get(
        self,
        config: Mapping[str, object],
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
