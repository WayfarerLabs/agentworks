"""Passive independent SSH bindings for cloud-selected endpoints."""

from __future__ import annotations

from ipaddress import IPv4Address
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError, StateError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.binding import NativeExecutionBinding, _EarlyGuestFactsRoute
from agentworks.execution.carriers.ssh import ManagedSSHTrust, SSHCarrier, SSHConnection, SSHSettings

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import VMRow


def require_ssh_settings(vm: VMRow, ctx: RunContext, config: Config | None) -> SSHSettings:
    """Require loaded independent operator policy before any provider read."""
    selected = config if config is not None else ctx.config
    settings = getattr(getattr(selected, "operator", None), "ssh", None)
    if not isinstance(settings, SSHSettings):
        raise ConfigError(
            "Native cloud execution requires explicit operator SSH settings",
            entity_kind="vm",
            entity_name=vm.name,
        )
    return settings


def ssh_native_binding(vm: VMRow, endpoint: object, settings: SSHSettings) -> NativeExecutionBinding:
    """Bind a provider's current literal IPv4 without admitting local files."""
    try:
        if not isinstance(endpoint, str):
            raise ValueError
        IPv4Address(endpoint)
    except ValueError:
        raise StateError(
            "Native cloud execution requires a current public IPv4 endpoint",
            entity_kind="vm",
            entity_name=vm.name,
        ) from None
    connection = SSHConnection(
        host=endpoint,
        port=22,
        user=vm.admin_username,
        identity_file=settings.identity_file,
        trust=ManagedSSHTrust(settings.trust_store),
        host_key_alias=None,
        agent_socket=settings.agent_socket,
        ssh_executable=settings.ssh_executable,
        keepalive_interval=settings.keepalive_interval,
        keepalive_count_max=settings.keepalive_count_max,
    )
    carrier = SSHCarrier(connection)
    root_entry = IdentityPlan(
        IdentityExpectation(0, 0, (0,)),
        IdentityMode.DIRECT if vm.admin_username == "root" else IdentityMode.SUDO_ROOT,
    )
    return NativeExecutionBinding(
        carrier,
        vm.admin_username,
        RuntimeSelection(RuntimeTargetOS.LINUX),
        _early_guest_facts_route=_EarlyGuestFactsRoute(carrier, root_entry, vm.admin_username),
        _new_managed_delivery=lambda: SSHCarrier(connection),
    )
