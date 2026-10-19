"""Explicit SSH platform-host access and ordinary context composition."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, model_validator

from agentworks.db import OperationResourceKind, OperationScope
from agentworks.errors import ConfigError, ValidationError
from agentworks.execution._platform_host_context import platform_host_execution_context
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carriers.ssh import ManagedSSHTrust, SSHCarrier, SSHConnection
from agentworks.schema import AgwModel, NonEmptyStr

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import PurePosixPath

    from agentworks.capabilities.base import RunContext
    from agentworks.db import Database
    from agentworks.execution.carrier import Deadline


class SSHPlatformHostAccessConfig(AgwModel):
    """Complete host declaration, independent of legacy SSH destinations.

    Sites sharing a host must declare the same resource key. It coordinates
    Agentworks operations without proving physical machine identity.
    """

    resource_key: NonEmptyStr
    """Explicit shared platform-host coordination key."""

    endpoint: NonEmptyStr
    """Literal DNS name or unbracketed IP, without SSH-config expansion."""

    user: NonEmptyStr
    """Login account used for both delivery and ordinary workload execution."""

    port: int = Field(default=22, ge=1, le=65535)
    """SSH port on the declared endpoint."""

    target_os: Literal["linux", "darwin"]
    """Explicit host operating system for fixed runtime selection."""

    python_path: str | None = None
    """Sole absolute Python candidate, or the existing OS-specific candidates."""

    @model_validator(mode="after")
    def _validate_selection(self) -> Self:
        """Admit operator-authored scope and runtime values without I/O."""
        OperationScope(OperationResourceKind.PLATFORM_HOST, self.resource_key)
        try:
            RuntimeSelection(RuntimeTargetOS(self.target_os), self.python_path)
        except ValidationError as error:
            raise ValueError(str(error)) from error
        return self


@contextmanager
def ssh_platform_host_execution_context(
    db: Database,
    ctx: RunContext,
    access: SSHPlatformHostAccessConfig | None,
    *,
    trusted_root: PurePosixPath,
    deadline: Deadline,
) -> Iterator[RunContext]:
    """Explicitly compose ordinary host access under the existing host owner.

    Connection construction is passive. The host composer claims the exact
    declared key before discovery or SSH policy admission. Core must select a
    trusted file root; the existing file rules retain confinement beneath it.
    The login account receives no elevated preparation or new OS permissions.
    Native SSH/macOS and platform provisioning are separate gates.
    """
    if access is None:
        raise ConfigError("SSH platform-host execution requires a complete access declaration")
    settings = ctx.config.operator.ssh if ctx.config is not None else None
    if settings is None:
        raise ConfigError("SSH platform-host execution requires explicit operator SSH settings")
    scope = OperationScope(OperationResourceKind.PLATFORM_HOST, access.resource_key)
    runtime = RuntimeSelection(RuntimeTargetOS(access.target_os), access.python_path)
    connection = SSHConnection(
        host=access.endpoint,
        user=access.user,
        port=access.port,
        identity_file=settings.identity_file,
        trust=ManagedSSHTrust(settings.trust_store),
        agent_socket=settings.agent_socket,
        ssh_executable=settings.ssh_executable,
        keepalive_interval=settings.keepalive_interval,
        keepalive_count_max=settings.keepalive_count_max,
    )
    with platform_host_execution_context(
        db,
        ctx,
        scope,
        SSHCarrier(connection),
        delivery_account=access.user,
        workload_account=access.user,
        include_elevated=False,
        runtime_selection=runtime,
        trusted_root=trusted_root,
        deadline=deadline,
    ) as composed:
        yield composed
