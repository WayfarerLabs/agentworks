"""The clouds' SSH to a VM's public address, under the VM policy.

A cloud VM's root channel, before and outside Tailscale, is SSH as the admin
to the address its provider reports, inside the platform's transient route.
:class:`PublicSSH` builds that connection with the same policy as every other
VM connection (``-F none``, the operator's key, the VM's host-key trust), so
an operator's ``ssh_config`` stanza for the address does not apply and an
encrypted key must be loaded in the agent. The trust is the VM's store
record, or an Unverified provisioning binding where a create or a host-key
read has no record to verify against.

This module imports nothing from the legacy transport stack.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import agentworks.execution as execution
from agentworks import output
from agentworks.errors import ConnectivityError, StateError
from agentworks.execution import root_script, ssh_process
from agentworks.execution.errors import TrustMismatch

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.capabilities.base import RunContext
    from agentworks.execution.root_script import RawRun
    from agentworks.execution.trust import TrustBinding

ROOT_COMMAND = "sudo -n /bin/bash -s"
"""The remote command: a constant every account shell Agentworks allows parses unchanged."""

PROBE_ATTEMPTS = 6
PROBE_DEADLINE_SECONDS = 10
PROBE_INTERVAL_SECONDS = 3


class PublicKeyRefused(ConnectivityError):
    """The VM refused the operator's key at its public address.

    Recovery logs in with the same key it would repair, so such a VM needs the
    provider's own access or recreation; waiting cannot change it.
    """

    def __init__(self, vm_name: str, host: str) -> None:
        super().__init__(
            f"VM '{vm_name}' refused the configured SSH key at its public address {host}",
            entity_kind="vm",
            entity_name=vm_name,
            hint=(
                "Reach the VM through the provider's own access (its serial console, or EC2 Instance Connect) "
                "to restore the admin's authorized key, or delete and recreate the VM."
            ),
        )


class PublicSSH:
    """Root scripts over SSH to one VM's public address, as its admin."""

    def __init__(self, host: str, user: str, identity_file: Path, trust: TrustBinding) -> None:
        # Destination validates the provider-reported address and the admin name.
        self._destination = ssh_process.Destination(host, user)
        self._policy = ssh_process.VMPolicy(identity_file, trust)
        self._vm_name = trust.vm_name

    def run_as_root(self, script: bytes, deadline: float) -> RawRun:
        """Run ``script`` under ``sudo -n /bin/bash -s`` once, then type a host-key or key failure from ssh's log."""
        with ssh_process.client_log() as log:
            argv = ssh_process.build_argv(
                execution.resolve_ssh(),
                self._policy,
                self._destination,
                ssh_process.ArgvMode.PIPE,
                ROOT_COMMAND,
                client_log=log,
            )
            raw = root_script.run_process(argv, script, deadline)
            text = ssh_process.client_log_text(log)
        if ssh_process.HOST_KEY_FAILURE in text:
            raise TrustMismatch(self._vm_name, ssh_process.tail_lines(text))
        if ssh_process.AUTH_FAILURE in text:
            raise PublicKeyRefused(self._vm_name, self._destination.host)
        return raw

    def wait_until_answering(self, *, failure_hint: str | None) -> None:
        """Probe with ``true`` until one completes: a fresh firewall allow can take seconds to apply.

        ``true`` reads nothing, so probing again is safe. A refused key or a
        host-key mismatch raises at once. When no probe completes,
        ``failure_hint`` (the platform's guidance for an allow that does not
        match the operator's egress) is warned before the error.
        """
        for attempt in range(PROBE_ATTEMPTS):
            if root_script.run(self.run_as_root, "true", deadline=PROBE_DEADLINE_SECONDS).status == 0:
                return
            if attempt == 0:
                output.detail("Waiting for the VM to answer at its public address...")
            if attempt < PROBE_ATTEMPTS - 1:
                time.sleep(PROBE_INTERVAL_SECONDS)
        if failure_hint:
            output.warn(failure_hint)
        raise ConnectivityError(
            f"VM '{self._vm_name}' did not answer over SSH at its public address {self._destination.host}",
            entity_kind="vm",
            entity_name=self._vm_name,
        )


def operator_identity_file(ctx: RunContext, vm_name: str) -> Path:
    """The operator's private key from the op context, which every native operation carries."""
    if ctx.config is None:
        raise StateError(
            f"cannot reach VM '{vm_name}' at its public address: the operation carries no operator configuration",
            entity_kind="vm",
            entity_name=vm_name,
        )
    return ctx.config.operator.ssh_private_key


def require_public_address(address: str | None, vm_name: str, *, hint: str) -> str:
    """The provider-reported address, or a ``StateError`` with the platform's console hint when it has none."""
    if not address:
        raise StateError(
            f"VM '{vm_name}' has no public IP address",
            entity_kind="vm",
            entity_name=vm_name,
            hint=hint,
        )
    return address
