"""The interactive ``prompt`` secret backend."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, model_validator

from agentworks import output
from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.capabilities.secret_backend.client import (
    InteractionBroker,
    RemainingTime,
    SecretLookupRequest,
    SecretSourceClient,
)
from agentworks.errors import StateError
from agentworks.schema import AgwModel, AgwRootModel
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import GetJsonSchemaHandler
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import CoreSchema

    from agentworks.resources.graph import Readiness
    from agentworks.secrets.base import MappingValue, SecretDecl


class PromptSourceConfig(AgwModel):
    """The tag-only config for a prompt source."""

    name: Literal["prompt"]


class PromptMapping(AgwRootModel[Any]):
    """Prompt has no mapping vocabulary, so every authored value fails."""

    @model_validator(mode="before")
    @classmethod
    def _reject_every_mapping(cls, value: object) -> object:
        raise ValueError("the prompt backend has no mapping vocabulary; remove it, or use false to opt out")

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        _core_schema: CoreSchema,
        _handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        return {"not": {}}


class _PromptClient:
    def __init__(self, broker: InteractionBroker | None) -> None:
        self._broker = broker

    def prepare(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> None:
        return None

    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> Mapping[str, str]:
        if self._broker is None:
            raise StateError("the prompt source client requires an interaction broker")
        return {request.name: self._broker.request_secret(request.name) for request in requests}


class _PromptContext(AbstractContextManager[SecretSourceClient]):
    def __init__(self, broker: InteractionBroker | None) -> None:
        self._broker = broker

    def __enter__(self) -> SecretSourceClient:
        return _PromptClient(self._broker)

    def __exit__(self, *args: object) -> None:
        return None


class PromptBackend(SecretBackend):
    """Resolve secrets through a caller-authorized interactive prompt."""

    contract_version: ClassVar[int] = 2
    config_model: ClassVar[type[AgwModel]] = PromptSourceConfig
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = PromptMapping
    name: ClassVar[str] = "prompt"
    description: ClassVar[str] = "prompts interactively at resolution time"
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Interactive prompt",
        overview="""
        Asks the operator for a value at the moment a command needs it. It is the
        last source in the simple default chain, so only values no earlier source
        resolved are requested. Prompt has no per-secret mapping vocabulary.
        """,
    )
    interactive: ClassVar[bool] = True

    @classmethod
    def backend_readiness(cls) -> Readiness:
        from agentworks.resources.graph import Readiness

        return Readiness.ready()

    @classmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
        return True

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> str | None:
        return None

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        return _PromptContext(interaction_broker)

    @classmethod
    def _legacy_describe_lookup(cls, secret: SecretDecl, mapping: MappingValue | None) -> None:
        return None

    @classmethod
    def _legacy_batch_get(
        cls,
        wants: list[tuple[SecretDecl, MappingValue | None]],
    ) -> dict[str, str]:
        if not output.is_interactive():
            return {}
        return {secret.name: cls._legacy_prompt_one(secret) for secret, _ in wants}

    @staticmethod
    def _legacy_prompt_one(secret: SecretDecl) -> str:
        label = f"Secret '{secret.name}': {secret.description}"
        return output.prompt_secret(label, hint=secret.hint)
