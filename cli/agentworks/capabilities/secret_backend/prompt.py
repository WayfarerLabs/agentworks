"""The terminal ``prompt`` secret backend."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NoReturn

from pydantic import BaseModel, model_validator

from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.capabilities.secret_backend.client import (
    BackendBlocked,
    BackendFailed,
    BackendPreview,
    BackendResolution,
    BackendResolved,
    BlockReason,
    FailureReason,
    IndeterminateReason,
    InteractionBroker,
    LookupDescription,
    LookupDisposition,
    OperatorImpact,
    PreviewAvailable,
    PreviewBlocked,
    PreviewFailed,
    PreviewIndeterminate,
    PreviewIntent,
    ResolutionIntent,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
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


class PromptSourceConfig(AgwModel):
    """The tag-only config for a prompt source."""

    name: Literal["prompt"]


class PromptMapping(AgwRootModel[Any]):
    """Prompt has no mapping vocabulary, so every authored value fails."""

    operator_input_annotation: ClassVar[object | None] = NoReturn

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
    def __init__(
        self,
        *,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        broker: InteractionBroker | None,
    ) -> None:
        self._intent = intent
        self._tty_access = tty_access
        self._broker = broker

    def _block_reason(self) -> BlockReason | None:
        if self._tty_access is TtyInteractionAccess.UNAVAILABLE:
            return BlockReason.TTY_UNAVAILABLE
        if self._tty_access is TtyInteractionAccess.DISABLED:
            return BlockReason.TTY_INTERACTION_DISABLED
        return None

    def preview(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendPreview]:
        if not isinstance(self._intent, PreviewIntent):
            raise StateError("resolution client cannot perform preview")
        block_reason = self._block_reason()
        if block_reason is not None:
            return {request.name: PreviewBlocked(block_reason) for request in requests}
        if self._intent.impact is OperatorImpact.NONE:
            return {
                request.name: PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED) for request in requests
            }
        if self._broker is None:
            raise StateError("available prompt preview requires an interaction broker")
        results: dict[str, BackendPreview] = {}
        for request in requests:
            value = self._broker.request_secret(request.name)
            results[request.name] = (
                PreviewFailed(FailureReason.MALFORMED_VALUE) if "\0" in value else PreviewAvailable()
            )
        return results

    def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
        if not isinstance(self._intent, ResolutionIntent):
            raise StateError("preview client cannot perform resolution")
        block_reason = self._block_reason()
        if block_reason is not None:
            return {request.name: BackendBlocked(block_reason) for request in requests}
        if self._broker is None:
            raise StateError("available prompt resolution requires an interaction broker")
        results: dict[str, BackendResolution] = {}
        for request in requests:
            value = self._broker.request_secret(request.name)
            results[request.name] = (
                BackendFailed(FailureReason.MALFORMED_VALUE) if "\0" in value else BackendResolved(value)
            )
        return results


class _PromptContext(AbstractContextManager[SecretSourceClient]):
    def __init__(
        self,
        *,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        broker: InteractionBroker | None,
    ) -> None:
        self._intent = intent
        self._tty_access = tty_access
        self._broker = broker

    def __enter__(self) -> SecretSourceClient:
        return _PromptClient(intent=self._intent, tty_access=self._tty_access, broker=self._broker)

    def __exit__(self, *args: object) -> None:
        return None


class PromptBackend(SecretBackend):
    """Resolve secrets through a caller-authorized terminal prompt."""

    contract_version: ClassVar[int] = 1
    config_model: ClassVar[type[AgwModel]] = PromptSourceConfig
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = PromptMapping
    name: ClassVar[str] = "prompt"
    description: ClassVar[str] = "prompts through terminal input at resolution time"
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Terminal prompt",
        overview="""
        Asks the operator for a value at the moment a command needs it. It is the
        last source in the simple default chain, so only values no earlier source
        resolved are requested. Prompt has no per-secret mapping vocabulary.
        """,
    )
    supports_tty_interaction: ClassVar[bool] = True

    @classmethod
    def backend_readiness(cls) -> Readiness:
        from agentworks.resources.graph import Readiness

        return Readiness.ready()

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        return LookupDescription(LookupDisposition.CANDIDATE, None)

    @classmethod
    def create_client(
        cls,
        *,
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
    ) -> AbstractContextManager[SecretSourceClient]:
        return _PromptContext(intent=intent, tty_access=tty_access, broker=interaction_broker)
