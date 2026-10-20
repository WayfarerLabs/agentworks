"""Prepared guest steps shared by independent VM creation composition."""

from __future__ import annotations

from ipaddress import IPv4Address
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.bootstrap_script import generate_bootstrap_script
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.debian import verify_os_release
from agentworks.errors import StateError
from agentworks.execution.models import Command, Input, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.secrets.line_safety import LineOrientedSecretUse, require_line_safe_secret

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform.base import ProvisionRequest
    from agentworks.debian import DebianRelease
    from agentworks.execution.carrier import Deadline
    from agentworks.vms._native_operation import VMOperation


def bootstrap_created_guest(operation: VMOperation, request: ProvisionRequest, deadline: Deadline) -> str:
    """Configure a prepared guest, then join using sensitive non-staging input.

    These steps establish no provider acceptance or database checkpoint. Their
    caller retains the original workflow, including uncertain execution custody,
    and closes the platform rollback window only after durable addressability.
    """
    key = require_line_safe_secret(request.tailscale_auth_key, use=LineOrientedSecretUse.TAILSCALE).encode("utf-8")
    public_script = generate_bootstrap_script(
        admin_username=request.admin_username,
        ssh_public_key=request.ssh_public_key,
        provisioning_packages=PROVISIONING_PACKAGES,
        tailscale_auth_key=None,
        hostname=request.hostname,
        swap=request.swap_gib,
        instance_marker=request.instance_marker,
    )
    operation.execution.run(
        Script(public_script, Shell.BASH),
        profile=Protection.MANAGED,
        sudo=True,
        output=Output.discard(),
        deadline=deadline,
        check=True,
    )
    operation.execution.run(
        Command(("tailscale", "up", "--auth-key=file:/dev/stdin")),
        profile=Protection.DIRECT,
        sudo=True,
        stdin=Input.sensitive(key, allow_staging=False),
        output=Output.discard(),
        deadline=deadline,
        check=True,
    )
    address = operation.execution.run(
        Command(("tailscale", "ip", "-4")),
        profile=Protection.DIRECT,
        sudo=True,
        output=Output.capture(64),
        deadline=deadline,
        check=True,
    )
    try:
        return str(IPv4Address(address.stdout.data.decode("ascii").strip()))
    except (UnicodeError, ValueError):
        raise StateError(
            "Created VM did not report one IPv4 address", entity_kind="vm", entity_name=request.vm_name
        ) from None


def attest_created_guest(operation: VMOperation, expected: DebianRelease, deadline: Deadline) -> DebianRelease:
    """Observe release through the prepared interface, without a legacy probe."""
    result = operation.execution.run(
        Command(("cat", "/etc/os-release")),
        profile=Protection.DIRECT,
        output=Output.capture(16_384),
        deadline=deadline,
        check=True,
    )
    try:
        text = result.stdout.data.decode("utf-8")
    except UnicodeError:
        raise StateError("Created VM returned an invalid release observation") from None
    return verify_os_release(text, expected=expected)
