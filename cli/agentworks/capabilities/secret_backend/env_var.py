"""The ``env-var`` secret backend."""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.capabilities.secret_backend.client import (
    BackendFailed,
    BackendMissing,
    BackendPreview,
    BackendResolution,
    BackendResolved,
    FailureReason,
    InteractionBroker,
    LookupDescription,
    LookupDisposition,
    PreviewAvailable,
    PreviewFailed,
    PreviewMissing,
    RemainingTime,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.errors import ConfigError
from agentworks.schema import AgwModel, AgwRootModel, NonEmptyStr
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from agentworks.resources.graph import Readiness


def env_var_name_for(secret_name: str) -> str:
    """Return the default ``AW_SECRET_<NAME>`` environment variable."""
    return "AW_SECRET_" + secret_name.upper().replace("-", "_")


class EnvVarSourceConfig(AgwModel):
    """The tag-only config for an env-var source."""

    name: Literal["env-var"]


class EnvVarMapping(AgwRootModel[NonEmptyStr]):
    """An explicit environment-variable name for one secret lookup."""


def _read(request: SecretLookupRequest) -> str | None:
    mapping = request.mapping
    env_name = mapping.root if isinstance(mapping, EnvVarMapping) else env_var_name_for(request.name)
    return os.environ.get(env_name)


class _EnvVarClient:
    def preview(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendPreview]:
        results: dict[str, BackendPreview] = {}
        for request in requests:
            value = _read(request)
            if value is None:
                results[request.name] = PreviewMissing()
            elif "\0" in value:
                results[request.name] = PreviewFailed(FailureReason.MALFORMED_VALUE)
            else:
                results[request.name] = PreviewAvailable()
        return results

    def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
        results: dict[str, BackendResolution] = {}
        for request in requests:
            value = _read(request)
            if value is None:
                results[request.name] = BackendMissing()
            elif "\0" in value:
                results[request.name] = BackendFailed(FailureReason.MALFORMED_VALUE)
            else:
                results[request.name] = BackendResolved(value)
        return results


class _EnvVarContext(AbstractContextManager[SecretSourceClient]):
    def __enter__(self) -> SecretSourceClient:
        return _EnvVarClient()

    def __exit__(self, *args: object) -> None:
        return None


class EnvVarBackend(SecretBackend):
    """Resolve secrets from operator-side environment variables."""

    contract_version: ClassVar[int] = 1
    config_model: ClassVar[type[AgwModel]] = EnvVarSourceConfig
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = EnvVarMapping
    name: ClassVar[str] = "env-var"
    description: ClassVar[str] = "resolves from AW_SECRET_<NAME> environment variables"
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Environment variables",
        overview="""
        Reads a secret's value from an environment variable. Every secret has one by
        convention, `AW_SECRET_` plus its name upper-cased with hyphens as underscores,
        so a secret needs no mapping at all to be resolvable this way.

        An explicit mapping overrides that name with the variable you actually have. An
        unset variable is an ordinary miss, so resolution can continue to a later source.
        """,
    )
    supports_tty_interaction: ClassVar[bool] = False

    @classmethod
    def backend_readiness(cls) -> Readiness:
        from agentworks.resources.graph import Readiness

        return Readiness.ready()

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        if mapping is None:
            identifier = env_var_name_for(secret_name)
        elif isinstance(mapping, EnvVarMapping):
            identifier = mapping.root
        else:
            raise ConfigError(f"env-var received {type(mapping).__name__}, not EnvVarMapping")
        return LookupDescription(LookupDisposition.CANDIDATE, identifier)

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        return _EnvVarContext()
