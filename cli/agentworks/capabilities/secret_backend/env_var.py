"""The ``env-var`` secret backend."""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.capabilities.secret_backend.client import (
    InteractionBroker,
    RemainingTime,
    SecretLookupRequest,
    SecretSourceClient,
)
from agentworks.errors import ConfigError
from agentworks.schema import AgwModel, AgwRootModel, NonEmptyStr
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from agentworks.resources.graph import Readiness
    from agentworks.secrets.base import MappingValue, SecretDecl


def env_var_name_for(secret_name: str) -> str:
    """Return the default ``AW_SECRET_<NAME>`` environment variable."""
    return "AW_SECRET_" + secret_name.upper().replace("-", "_")


class EnvVarSourceConfig(AgwModel):
    """The tag-only config for an env-var source."""

    name: Literal["env-var"]


class EnvVarMapping(AgwRootModel[NonEmptyStr]):
    """An explicit environment-variable name for one secret lookup."""


class _EnvVarClient:
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
        resolved: dict[str, str] = {}
        for request in requests:
            mapping = request.mapping
            env_name = mapping.root if isinstance(mapping, EnvVarMapping) else env_var_name_for(request.name)
            raw = os.environ.get(env_name)
            if raw is not None:
                resolved[request.name] = raw.rstrip("\r\n")
        return resolved


class _EnvVarContext(AbstractContextManager[SecretSourceClient]):
    def __enter__(self) -> SecretSourceClient:
        return _EnvVarClient()

    def __exit__(self, *args: object) -> None:
        return None


class EnvVarBackend(SecretBackend):
    """Resolve secrets from operator-side environment variables."""

    contract_version: ClassVar[int] = 2
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
        unset variable is a soft miss, so resolution can continue to a later source.
        """,
    )
    interactive: ClassVar[bool] = False

    @classmethod
    def backend_readiness(cls) -> Readiness:
        from agentworks.resources.graph import Readiness

        return Readiness.ready()

    @classmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
        return True

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> str | None:
        if mapping is None:
            return env_var_name_for(secret_name)
        if not isinstance(mapping, EnvVarMapping):
            raise ConfigError(f"env-var received {type(mapping).__name__}, not EnvVarMapping")
        return mapping.root

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        return _EnvVarContext()

    @classmethod
    def _legacy_describe_lookup(cls, secret: SecretDecl, mapping: MappingValue | None) -> str | None:
        if isinstance(mapping, str):
            return mapping
        if mapping is not None:
            raise ConfigError(
                f"secret {secret.name!r}: backend_mappings for the env-var backend must be "
                "a non-empty string (an env var name) or false"
            )
        return env_var_name_for(secret.name)

    @classmethod
    def _legacy_batch_get(
        cls,
        wants: list[tuple[SecretDecl, MappingValue | None]],
    ) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for secret, mapping in wants:
            env_name = cls._legacy_describe_lookup(secret, mapping)
            assert env_name is not None
            raw = os.environ.get(env_name)
            if raw is not None:
                resolved[secret.name] = raw.rstrip("\r\n")
        return resolved
