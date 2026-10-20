"""Passive independent SSH bindings for literal VM endpoints."""

from __future__ import annotations

from ipaddress import IPv4Address
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError, StateError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.binding import NativeExecutionBinding, _EarlyGuestFactsRoute, _IndependentJobAvailability
from agentworks.execution.carriers.ssh import ManagedSSHTrust, SSHCarrier, SSHConnection
from agentworks.vms.identity import validate_vm_instance_marker

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.execution.carriers.ssh import SSHSettings


def require_ssh_settings(vm: VMRow, ctx: RunContext, config: Config | None) -> SSHSettings:
    """Require loaded independent operator policy before any provider read."""
    selected = config if config is not None else ctx.config
    settings = selected.operator.ssh if selected is not None else None
    if settings is None:
        raise ConfigError(
            "VM execution requires explicit operator SSH settings",
            entity_kind="vm",
            entity_name=vm.name,
        )
    return settings


def vm_ssh_connection(
    vm_name: str,
    admin_username: str,
    endpoint: object,
    settings: SSHSettings,
    *,
    instance_marker: str | None,
) -> SSHConnection:
    """Construct passive connection policy for a core-selected literal VM endpoint."""
    marker = validate_vm_instance_marker(instance_marker)
    try:
        if not isinstance(endpoint, str):
            raise ValueError
        IPv4Address(endpoint)
    except ValueError:
        raise StateError(
            "VM execution requires a literal IPv4 endpoint",
            entity_kind="vm",
            entity_name=vm_name,
        ) from None
    return SSHConnection(
        host=endpoint,
        port=22,
        user=admin_username,
        identity_file=settings.identity_file,
        trust=ManagedSSHTrust(settings.trust_store),
        host_key_alias="agw-vm-" + marker,
        agent_socket=settings.agent_socket,
        ssh_executable=settings.ssh_executable,
        keepalive_interval=settings.keepalive_interval,
        keepalive_count_max=settings.keepalive_count_max,
    )


def ssh_execution_binding(
    vm: VMRow,
    endpoint: object,
    settings: SSHSettings,
    *,
    independent_availability: _IndependentJobAvailability | None = None,
) -> NativeExecutionBinding:
    """Bind one selected literal IPv4 without admitting local files."""
    connection = vm_ssh_connection(vm.name, vm.admin_username, endpoint, settings, instance_marker=vm.instance_marker)
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
        _independent_availability=independent_availability,
    )
