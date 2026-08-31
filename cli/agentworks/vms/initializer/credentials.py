"""Tailscale connectivity preflight/rejoin and Git credential preparation."""

from __future__ import annotations

import ipaddress
import subprocess
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.tailscale_join import join_tailscale_ephemerally
from agentworks.errors import ConnectivityError
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from agentworks.db import Database
    from agentworks.transports import Transport


def verify_tailscale_available() -> None:
    """Pre-flight: verify the local machine is on Tailscale."""
    try:
        result = subprocess.run(
            ["tailscale", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except FileNotFoundError:
        raise ConnectivityError("'tailscale' command not found. Install Tailscale on this machine.") from None
    except subprocess.TimeoutExpired:
        raise ConnectivityError("'tailscale status' timed out. Is Tailscale running?") from None

    if result.returncode != 0:
        raise ConnectivityError(
            "This machine is not connected to Tailscale. "
            "VM initialization requires Tailscale to switch from the provisioning "
            "transport to direct SSH. Run 'tailscale up' first."
        )


def rejoin_tailscale(
    db: Database,
    vm_name: str,
    exec_target: Transport,
    *,
    auth_key: str,
) -> str:
    """Re-join Tailscale on a VM that lost its node (e.g. ephemeral key).

    Installs Tailscale if needed, joins the tailnet, and updates the DB
    with the new Tailscale IP. ``auth_key`` is resolved by the caller via
    the framework's eager-resolve.

    Returns the new Tailscale IP.
    """
    output.info("Tailscale node not reachable. Re-joining tailnet...")

    # Ensure Tailscale is installed (idempotent)
    exec_target.run(
        "bash -c 'command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh'",
        sudo=True,
        check=False,
    )

    return _join_tailscale(db, vm_name, exec_target, auth_key=auth_key)


def _join_tailscale(
    db: Database,
    vm_name: str,
    exec_target: Transport,
    *,
    auth_key: str,
) -> str:
    """Join Tailscale, update DB. Returns the Tailscale IP.

    The Tailscale auth key arrives via the ``auth_key`` keyword argument
    from the framework's eager-resolve at manager-entry. The legacy
    env-var fallback and
    prompt-here-if-missing path are gone; callers must thread the
    resolved value in.
    """
    # Daemon-side flags (e.g. --tun=userspace-networking for WSL2) live in
    # /etc/default/tailscaled, set during bootstrap. `tailscale up` is the
    # client and only takes client-side flags.
    join_tailscale_ephemerally(exec_target, auth_key)
    result = exec_target.run("tailscale ip -4", sudo=True)

    raw_ip_output = result.stdout.strip()
    tailscale_ip = raw_ip_output.splitlines()[0].strip() if raw_ip_output else ""
    try:
        ipaddress.IPv4Address(tailscale_ip)
    except ValueError:
        raise SSHError(f"tailscale ip -4 returned invalid address: {raw_ip_output!r}") from None
    output.detail(f"Tailscale IP: {tailscale_ip}")
    db.update_vm_tailscale(vm_name, tailscale_ip)
    return tailscale_ip
