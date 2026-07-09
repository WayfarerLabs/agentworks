"""Polymorphic transports for reaching VMs.

Three named factories plus one low-level helper:

- :func:`transport` -- the canonical transport for a VM as the admin user
  (Tailscale SSH). Used by every normal operator workflow.
- :func:`agent_transport` -- the canonical transport as a named agent's
  Linux user. Same mechanism as :func:`transport`, different SSH user.
- :func:`native_transport` -- the platform-native transport
  (``limactl shell``, ``wsl.exe``, Azure-via-public-IP, ...). Used only
  at bootstrap and via the explicit ``vm shell --platform`` opt-in.
- :func:`transport_for_user` -- low-level helper used by the named
  factories. Direct use is reserved for the mid-create case where the
  agent row doesn't exist yet (today's only direct caller is
  ``agents/manager.py``).

The named factories never fall back. If the canonical transport is
unavailable, the canonical factory raises -- it does NOT silently switch
to the native transport (polymorphic-transports SDD R3).
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

from agentworks.errors import StateError
from agentworks.transports.base import Transport
from agentworks.transports.lima import LimaTransport
from agentworks.transports.remote_lima import RemoteLimaTransport
from agentworks.transports.ssh import SSHTransport
from agentworks.transports.wsl2 import WSL2Transport

if TYPE_CHECKING:
    import contextlib

    from agentworks.config import Config
    from agentworks.db import AgentRow, VMRow
    from agentworks.ssh import SSHLogger
    from agentworks.vms.base import VMPlatform


__all__ = [
    "LimaTransport",
    "RemoteLimaTransport",
    "SSHTransport",
    "Transport",
    "WSL2Transport",
    "agent_transport",
    "native_transport",
    "transport",
    "transport_for_user",
    "wait_for_reconnect",
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
    call this. Direct use is reserved for the mid-create case where an
    agent row doesn't exist yet but the on-VM identity already accepts
    the operator's key (see ``agents/manager.py``).

    Always uses ``config.operator.ssh_private_key`` as the SSH identity:
    that's the key the on-VM authorized_keys reconciler installs for
    every user agentworks manages, and it's the only credential the
    refactor needs to know about. No override -- if a future use case
    needs a different key, plumb it through then.

    Raises :class:`StateError` if the VM has no Tailscale IP (today's
    underlying assert disappears under ``python -O``; the typed error
    doesn't, per SDD R6).
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
        user=user,
        identity_file=config.operator.ssh_private_key,
        force_tty=sys.platform == "win32",
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

    Used by every normal operator workflow. Raises :class:`StateError`
    if the canonical transport is unavailable (no Tailscale host).
    Never falls back to the provisioner transport (SDD R3).
    """
    return transport_for_user(
        vm, config,
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
        vm, config,
        user=agent.linux_user,
        default_timeout=default_timeout,
        logger=logger,
    )


def native_transport(
    vm: VMRow,
    platform: VMPlatform,
    config: Config,
    *,
    stack: contextlib.ExitStack,
) -> Transport:
    """Platform-native transport for a VM. Used only at bootstrap and
    via the explicit ``vm shell --platform`` opt-in.

    ``platform`` is the VM's bound platform, resolved at the caller's
    composition root (``agentworks.vms.sites.platform_for``). ``stack``
    bounds the lifetime of any transient network state the platform
    needs (Azure attaches a public IP on enter and detaches on exit):
    the platform's :meth:`VMPlatform.transient_route` runs first; once
    that context is held, the per-platform transport builder runs.

    A ``None`` from :meth:`VMPlatform.native_transport` (proxmox: the
    one-shot QEMU guest-agent exec can't host an interactive shell)
    re-raises as a typed :class:`StateError` with the console hint.
    Surfaces a typed error if the transport resolves to an SSH target
    with an empty host (Azure's defensive guard from PR #118).

    Probes the resulting transport with ``echo ok`` and retries up to
    six times with a 3-second sleep between attempts (total budget ~15s
    of sleeps plus the per-attempt 10s timeout) so the Azure SDN has
    time to propagate a freshly-attached public IP before the first
    real command lands. Local transports (Lima, WSL2) succeed on the
    first probe and skip the sleeps entirely.
    """
    from agentworks import output
    from agentworks.ssh import SSHError

    stack.enter_context(platform.transient_route(vm))
    target = platform.native_transport(vm, config=config)
    if target is None:
        raise StateError(
            f"No native transport for VM '{vm.name}' "
            f"(platform '{platform.name}').",
            entity_kind="vm",
            entity_name=vm.name,
            hint=platform.no_native_transport_hint,
        )

    # Defensive: any SSH-backed native transport that returns an empty
    # host gets the same typed-error treatment. Azure is today's only
    # such case (host="" when the public IP attach silently failed); a
    # future SSH-backed platform would inherit the guard. After
    # transient_route this shouldn't happen on Azure (the context
    # manager attaches before yielding). If it does, surface clearly
    # rather than letting downstream calls hang on an empty hostname.
    if isinstance(target, SSHTransport) and not target.host:
        raise StateError(
            f"Native transport on platform '{platform.name}' resolved to an "
            f"SSH target with no host. VM '{vm.name}' may not be reachable.",
            entity_kind="vm",
            entity_name=vm.name,
            hint=(
                "For Azure: the temporary public IP attach may have silently "
                "failed; check the Azure portal for the VM's network "
                "configuration, or use the serial console (Connect > Serial "
                "console on the VM resource page)."
            ),
        )

    for attempt in range(6):
        try:
            target.run("echo ok", timeout=10)
            break
        except SSHError:
            if attempt == 0:
                output.detail("Waiting for provisioning transport...")
            if attempt == 5:
                raise
            time.sleep(3)
    return target


def wait_for_reconnect(target: Transport, *, max_attempts: int = 16) -> bool:
    """Poll ``target`` with ``echo ok`` until reachable or out of attempts.

    Used after network disruptions (e.g. Azure public IP changes) that
    temporarily break the transport. Double-checks once on first success
    to handle flapping. Polymorphic over any :class:`Transport` via its
    :meth:`Transport.run`. Returns ``True`` if the connection
    stabilized, ``False`` if it timed out.
    """
    from agentworks import output
    from agentworks.ssh import SSHError

    output.detail("Waiting for Tailscale to reconnect (this may take several minutes)...")
    for attempt in range(max_attempts):
        try:
            target.run("echo ok", timeout=10)
            if attempt > 0:
                time.sleep(2)
                target.run("echo ok", timeout=10)
            output.detail("Tailscale SSH reconnected")
            return True
        except SSHError:
            if attempt == max_attempts - 1:
                output.warn("Tailscale SSH did not reconnect after ~240s, proceeding anyway")
            time.sleep(5)
    return False
