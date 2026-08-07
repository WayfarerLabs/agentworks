"""The ``prompt`` secret backend: interactive last-resort. A capability
implementation, consumed by the resolution loop through the
``SecretBackend`` API.

Resolves nothing when stdin is not a TTY or the CLI was invoked with
--non-interactive; the resolve loop then raises SecretUnavailableError.
A future controller-process caller omits the prompt backend from its
chain entirely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import model_validator

from agentworks import output
from agentworks.schema import AgwRootModel
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from agentworks.resources.graph import Readiness
    from agentworks.secrets.base import MappingValue, SecretDecl


class PromptMapping(AgwRootModel[object]):
    """Prompt has NO mapping vocabulary, so every value is rejected.

    Not expressible as a type: ``typing.Never`` is not a shape pydantic
    can build a schema for (verified), so the refusal is a validator. It
    rejects an empty table too, deliberately: any value addressed to
    prompt is dead config, almost certainly a typo for another backend,
    and silently ignoring it would leave the operator believing something
    was configured.
    """

    @model_validator(mode="before")
    @classmethod
    def _reject_every_mapping(cls, value: object) -> object:
        raise ValueError("the prompt backend has no mapping vocabulary; remove it, or use false to opt out")


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

    contract_version = 1
    config_model: type[AgwRootModel[Any]] = PromptMapping
    name = "prompt"
    description = "prompts interactively at resolution time"
    prose = TopicProse(
        title="Interactive prompt",
        overview="""
        Asks the operator for the value, at the moment a command needs it. It is the
        last backend in the default chain: whatever nothing else could resolve is what
        you get asked for, with the secret's description and hint as the prompt text.

        It needs no mapping and takes no configuration. Opting a secret out with
        `backend_mappings.prompt: false` is mostly a testing tool: it proves another
        backend really resolves the secret instead of quietly falling through to a
        prompt. With no TTY (or under `--non-interactive`) prompting is skipped anyway.
        """,
    )
    interactive = True

    def not_ready(self) -> Readiness:
        """Always ready: prompting needs no host tool. Whether a prompt can
        actually run (TTY / non-interactive mode) is resolution-time
        interactivity, kept optimistically previewed (LLD c/e), not a
        backend readiness failure."""
        from agentworks.resources.graph import Readiness

        return Readiness.ready()

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
