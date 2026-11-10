"""The legacy SSH transport to a VM over its tailnet address, for the callers not yet migrated.

Two named factories plus one low-level helper:

- :func:`transport`: the admin's transport to a VM (SSH to its tailnet
  address).
- :func:`agent_transport`: the same as a named agent's Linux user.
- :func:`transport_for_user`: the helper both call.

A platform's own channel is not a transport: core reaches it through
``VMPlatform.run_as_root`` and ``native_shell``. Commands migrated onto
``agentworks.execution`` use ``VMAccess`` instead of this module, which goes
once the last caller moves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError
from agentworks.execution.trust import TrustBinding, TrustMode
from agentworks.transports.base import Transport
from agentworks.transports.ssh import SSHTransport

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import AgentRow, VMRow
    from agentworks.ssh import SSHLogger


__all__ = [
    "SSHTransport",
    "Transport",
    "agent_transport",
    "transport",
    "transport_for_user",
]


def transport_for_user(
    vm: VMRow,
    config: Config,
    *,
    user: str,
    default_timeout: int | None = None,
    logger: SSHLogger | None = None,
) -> Transport:
    """Build an ``SSHTransport`` for ``user`` on this VM via Tailscale.

    The two named factories (:func:`transport`, :func:`agent_transport`)
    call this.

    Always uses ``config.operator.ssh_private_key`` as the SSH identity:
    that's the key the on-VM authorized_keys reconciler installs for
    every user agentworks manages, and it's the only credential the
    refactor needs to know about. No override -- if a future use case
    needs a different key, plumb it through then.

    Raises :class:`StateError` if the VM has no Tailscale IP (a typed
    error rather than an assert, which would disappear under
    ``python -O``).

    The transport verifies the VM's host key against its store (VM trust). It
    does not check for a record when built; each spawn does, and a VM with no
    record raises :class:`TrustMissing` there instead of falling back to first
    use.
    """
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{vm.name}' has no Tailscale host; cannot build a canonical transport.",
            entity_kind="vm",
            entity_name=vm.name,
            hint="The VM may not have completed bootstrap, or Tailscale is broken on the VM.",
        )
    return SSHTransport(
        host=vm.tailscale_host,
        trust=TrustBinding(TrustMode.VM, vm.name),
        user=user,
        identity_file=config.operator.ssh_private_key,
        default_timeout=default_timeout,
        logger=logger,
    )


def transport(
    vm: VMRow,
    config: Config,
    *,
    default_timeout: int | None = None,
    logger: SSHLogger | None = None,
) -> Transport:
    """Canonical admin transport for a VM (Tailscale SSH).

    Raises :class:`StateError` if the VM has no Tailscale host.
    """
    return transport_for_user(
        vm,
        config,
        user=vm.admin_username,
        default_timeout=default_timeout,
        logger=logger,
    )


def agent_transport(
    vm: VMRow,
    config: Config,
    agent: AgentRow,
    *,
    default_timeout: int | None = None,
    logger: SSHLogger | None = None,
) -> Transport:
    """Canonical transport for an agent's Linux user (Tailscale SSH).

    Same mechanism as :func:`transport`; just a different SSH user. The
    agent's authorized_keys must already accept the operator's key (see
    ``initializer._reconcile_authorized_keys``, applied at agent create
    and reinit).
    """
    return transport_for_user(
        vm,
        config,
        user=agent.linux_user,
        default_timeout=default_timeout,
        logger=logger,
    )
