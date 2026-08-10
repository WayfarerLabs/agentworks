"""The nominal secret-backend capability contract and class registry."""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, final

from agentworks.capabilities.base import Capability, RunContext

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from pydantic import BaseModel

    from agentworks.capabilities.secret_backend.client import (
        InteractionBroker,
        RemainingTime,
        SecretSourceClient,
    )
    from agentworks.resources.graph import Readiness
    from agentworks.schema import AgwModel, AgwRootModel


class SecretBackend(Capability):
    """One implementation kind from which configured sources create clients.

    Backends are registered as classes. Source config and per-secret mapping
    config are deliberately separate declarations: a source binds one
    ``config_model`` once, while each lookup validates one ``mapping_model``.
    Ordinary capability preflight and runup are fixed no-ops because secret
    resolution runs before either lifecycle stage.
    """

    owner_kind: ClassVar[str] = "secret-source"
    config_model: ClassVar[type[AgwModel]]
    mapping_model: ClassVar[type[AgwRootModel[Any]]]
    interactive: ClassVar[bool]

    @classmethod
    @abstractmethod
    def backend_readiness(cls) -> Readiness:
        """Return the config-independent, offline capability readiness."""

    @classmethod
    @abstractmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
        """Whether this backend could attempt one lookup, without I/O."""

    @classmethod
    @abstractmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> str | None:
        """Return a safe, value-free identifier for one validated lookup."""

    @classmethod
    def external_operation_timeout(cls, config: AgwModel) -> float | None:
        """The source-turn timeout for non-human blocking work, if any."""
        return None

    @classmethod
    @abstractmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        """Create an unentered, resource-free client context for one source."""

    @final
    def preflight(self, ctx: RunContext) -> None:
        """Do nothing: source resolution precedes ordinary preflight/runup."""
        return None

    @final
    def runup(self, ctx: RunContext) -> None:
        """Do nothing: source resolution precedes ordinary preflight/runup."""
        return None


from agentworks.capabilities.secret_backend.env_var import EnvVarBackend  # noqa: E402
from agentworks.capabilities.secret_backend.prompt import PromptBackend  # noqa: E402

SECRET_BACKEND_REGISTRY: dict[str, type[SecretBackend]] = {
    EnvVarBackend.name: EnvVarBackend,
    PromptBackend.name: PromptBackend,
}
"""The live class registry for core and plugin secret backends."""
