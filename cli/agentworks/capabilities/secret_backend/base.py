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
        LookupDescription,
        RemainingTime,
        SecretClientIntent,
        SecretSourceClient,
        TtyInteractionAccess,
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
    contract_version: ClassVar[int] = 1
    config_model: ClassVar[type[AgwModel]]
    mapping_model: ClassVar[type[AgwRootModel[Any]]]
    supports_tty_interaction: ClassVar[bool]

    @classmethod
    @abstractmethod
    def backend_readiness(cls) -> Readiness:
        """Return the config-independent, offline capability readiness."""

    @classmethod
    @abstractmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        """Return the structured, no-I/O projection for one lookup."""

    @classmethod
    @abstractmethod
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
