"""The clouds' SSH to a VM's public address, under the VM policy.

A cloud VM's root channel, before and outside Tailscale, is SSH as the admin
to the address its provider reports, inside the platform's transient route.
:class:`PublicSSH` builds that connection with the same policy as every other
VM connection (``-F none``, the operator's key, the VM's host-key trust), so
an operator's ``ssh_config`` stanza for the address does not apply and an
encrypted key must be loaded in the agent. The trust is the VM's store
record, or an Unverified provisioning binding where a create or a host-key
read has no record to verify against.

:class:`PublicAddressPlatform` is the clouds' shared base: it implements
``run_as_root``, ``read_host_keys`` and ``native_shell`` over one route-scoped connection,
and each cloud supplies only how to read its live public address.
"""

from __future__ import annotations

import contextlib
import time
from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar

import agentworks.execution as execution
from agentworks import output
from agentworks.capabilities.vm_platform.base import VMPlatform
from agentworks.errors import ConnectivityError, StateError
from agentworks.execution import client, envelope, root_script, ssh_process, trust
from agentworks.execution.errors import TrustMismatch
from agentworks.execution.trust import TrustBinding, TrustMode
from agentworks.interrupts import termination_as_interrupt

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager
    from pathlib import Path

    from agentworks.capabilities.base import RunContext
    from agentworks.db import VMRow
    from agentworks.execution.root_script import RawRun, RootScriptResult
    from agentworks.execution.trust import HostKey

ROOT_COMMAND = "sudo -n /bin/bash -s"
"""The remote command: a constant every account shell Agentworks allows parses unchanged."""

_PROVIDER_ACCESS_HINT = (
    "Reach the VM through the provider's own access, such as its serial console, to restore the admin's "
    "authorized key, or delete and recreate the VM."
)

# sudo's own prefix for the lines it prints when it refuses to run a command,
# such as "sudo: a password is required" under -n. Guest output, read only
# when the script never started.
_SUDO_REFUSAL_PREFIX = "sudo: "

PROBE_WAIT_SECONDS = 120
"""How long a VM gets to answer at its public address: a fresh allow, or a VM that has just started, needs time."""
PROBE_DEADLINE_SECONDS = 10
PROBE_INTERVAL_SECONDS = 3
REFUSED_KEY_INTERVAL_SECONDS = 10
"""The pause after a refused key, when the wait retries it: sshd's PerSourcePenalties (Debian 13) penalizes an
address that fails to authenticate often, after which every probe's outcome is unknown."""

PROBE_FAILURE_HINT = (
    "The temporary SSH allow is scoped to your detected public IP; if your SSH traffic leaves through "
    "another address (VPN split tunnel, proxy, CGNAT), add it to operator.ssh_allow_cidrs in your "
    "agentworks config."
)
"""Warned when a VM never answers: every cloud scopes its allow to the detected egress address, so an operator
whose SSH traffic leaves through another one gets an allow that does not match, and every probe times out."""


class KeyRefused(ConnectivityError):
    """The VM answered at its public address and refused the configured SSH key."""


class ConnectionRefused(ConnectivityError):
    """The VM's public address refused the connection: nothing listens on the SSH port, so nothing ran."""


class SudoRefused(StateError):
    """The VM answered at its public address, and ``sudo -n`` refused to run the root script for its admin."""


class PublicSSH:
    """Root scripts over SSH to one VM's public address, as its admin."""

    def __init__(self, host: str, user: str, identity_file: Path, trust: TrustBinding) -> None:
        # Destination validates the provider-reported address and the admin name.
        self._destination = ssh_process.Destination(host, user)
        self._policy = ssh_process.VMPolicy(identity_file, trust)
        self._identity_file = identity_file
        self._vm_name = trust.vm_name

    def run_as_root(self, script: bytes, deadline: float) -> RawRun:
        """Run ``script`` under ``sudo -n /bin/bash -s`` once, then type a host-key or key failure from ssh's log.

        The log is read only when the script never started: ssh writes the
        server's banner there too, and a script that started has already
        connected and logged in, so text in the log cannot mean either failure.
        A refused connection raises :class:`ConnectionRefused` the same way.
        """
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
        if envelope.holds_a_start_line(raw.stderr):
            return raw
        if ssh_process.HOST_KEY_FAILURE in text:
            raise TrustMismatch(self._vm_name, ssh_process.tail_lines(text))
        if ssh_process.AUTH_FAILURE in text:
            # Recovery logs in with the key it would repair, so waiting or retrying cannot help,
            # unless the key is encrypted and the agent does not hold it. That hint comes first:
            # it is given only when it is the likely cause.
            load_hint = client.agent_load_hint(self._identity_file)
            raise KeyRefused(
                f"VM '{self._vm_name}' refused the configured SSH key at its public address {self._destination.host}",
                entity_kind="vm",
                entity_name=self._vm_name,
                hint=" ".join(part for part in (load_hint, _PROVIDER_ACCESS_HINT) if part),
            )
        if ssh_process.CONNECTION_REFUSED in text:
            raise ConnectionRefused(
                f"VM '{self._vm_name}' refused SSH connections at its public address {self._destination.host}",
                entity_kind="vm",
                entity_name=self._vm_name,
            )
        return raw

    def shell(self) -> int:
        """An interactive login shell as the admin on the operator's terminal; returns ssh's status."""
        argv = ssh_process.build_argv(
            execution.resolve_ssh(), self._policy, self._destination, ssh_process.ArgvMode.TERMINAL
        )
        return ssh_process.run_terminal(argv)

    def wait_until_answering(self, *, seconds: float = PROBE_WAIT_SECONDS, retry_refused_key: bool = False) -> None:
        """Probe with ``true`` until one completes, for up to ``seconds``.

        A fresh firewall allow can take seconds to apply, and a VM that has
        just started (the clouds' ``start`` returns at the hypervisor's
        state, before sshd runs) can take a minute or more, so the wait is
        bounded by time rather than by a count of probes, which fast
        refusals would use up. ``true`` reads nothing, so probing again is
        safe. A host-key mismatch or a ``sudo -n`` that refused to start the
        script raises at once: the VM answered, and waiting cannot change
        either. So does a refused key, unless ``retry_refused_key`` (a VM
        being created, whose guest agent may install the key after sshd
        starts); then the next probe waits :data:`REFUSED_KEY_INTERVAL_SECONDS`,
        and a refusal still standing at the bound is what raises.
        When no probe completes, :class:`ChannelUnreachable` raises naming a
        restart: a VM whose address refused the last probe's connection has
        no SSH server listening, and one that never answered may have hung.
        Only the second warns :data:`PROBE_FAILURE_HINT` first, since an
        address that refuses is one the allow already lets through.
        """
        expires = time.monotonic() + seconds
        waited = False
        refused: KeyRefused | None = None
        while True:
            connection_refused: ConnectionRefused | None = None
            try:
                result = root_script.run(self.run_as_root, "true", deadline=PROBE_DEADLINE_SECONDS)
            except ConnectionRefused as error:
                # A VM that has just started refuses until its sshd listens, so this is waited out too.
                refused, connection_refused = None, error
            except KeyRefused as error:
                if not retry_refused_key:
                    raise
                refused = error
            else:
                refused = None
                if result.status == 0:
                    return
                self._refuse_without_sudo(result)
            remaining = expires - time.monotonic()
            if remaining <= 0:
                break
            if not waited:
                output.detail("Waiting for the VM to answer at its public address...")
                waited = True
            interval = PROBE_INTERVAL_SECONDS if refused is None else REFUSED_KEY_INTERVAL_SECONDS
            time.sleep(min(interval, remaining))
        if refused is not None:
            raise refused
        restart = f"'agw vm stop {self._vm_name}' and 'agw vm start {self._vm_name}'"
        if connection_refused is not None:
            raise root_script.ChannelUnreachable(
                str(connection_refused),
                entity_kind="vm",
                entity_name=self._vm_name,
                hint=(
                    "Nothing on the VM accepts connections on the SSH port, so its SSH server is likely not running. "
                    f"Restart the VM with {restart}, or repair the server through the provider's serial console."
                ),
            ) from connection_refused
        output.warn(PROBE_FAILURE_HINT)
        raise root_script.ChannelUnreachable(
            f"VM '{self._vm_name}' did not answer over SSH at its public address {self._destination.host}",
            entity_kind="vm",
            entity_name=self._vm_name,
            hint=(
                f"If the VM itself has hung, restart it with {restart}, or reach it through the provider's "
                "serial console."
            ),
        )

    def _refuse_without_sudo(self, result: RootScriptResult) -> None:
        """Raise when a probe never started because ``sudo -n`` refused it: the VM answered."""
        if result.status is None and any(
            line.startswith(_SUDO_REFUSAL_PREFIX) for line in result.channel_tail.splitlines()
        ):
            raise SudoRefused(
                f"VM '{self._vm_name}' answered at its public address {self._destination.host}, but its admin "
                f"'{self._destination.user}' cannot run sudo without a password",
                entity_kind="vm",
                entity_name=self._vm_name,
                hint=(
                    "Agentworks runs its root scripts there with 'sudo -n'. Restore the admin's passwordless "
                    "sudo through the provider's console, or delete and recreate the VM."
                ),
            )


class PublicAddressPlatform(VMPlatform):
    """A cloud platform whose root channel is :class:`PublicSSH` to the VM's public address.

    A subclass supplies its route (:meth:`_transient_route`), the address
    read (:meth:`_public_address`) and :attr:`public_address_hint`. All of it is private to the
    cloud: core reaches the VM only through ``run_as_root``,
    ``read_host_keys`` and ``native_shell``, each of which opens and closes its
    own route, so no firewall allow outlives one call.
    """

    recovery_needs_guest_sshd: ClassVar[bool] = True

    @property
    @abstractmethod
    def public_address_hint(self) -> str:
        """Where to look when the provider reports no public address: the platform's console.

        Abstract, so registration refuses a cloud that leaves it out; a
        subclass supplies it as a class attribute.
        """

    @abstractmethod
    def _transient_route(self, vm: VMRow, ctx: RunContext) -> AbstractContextManager[None]:
        """Open an SSH route to the VM for the context's duration, and close exactly it on every exit.

        A cloud's VM denies all inbound traffic at baseline, so the route is
        this call's own firewall allow, scoped to the operator's egress
        prefixes (widened by ``ctx.config.operator.ssh_allow_cidrs``).
        ``ctx`` is the op-start context: opening and closing the route are
        backend calls that read their credential through it, with no ambient
        fallback.
        """

    @abstractmethod
    def _public_address(self, vm: VMRow, ctx: RunContext) -> str:
        """The VM's public address, read live from the provider, or ``""`` when it has none."""

    def run_as_root(self, vm: VMRow, ctx: RunContext, script: bytes, deadline: float) -> RawRun:
        """Run ``script`` as root over SSH to the public address, inside this call's own route.

        A VM with no host-key record raises ``TrustMissing`` before any route
        opens, and no route outlives the one script.
        """
        trust.require_record(vm.name)
        with self._public_ssh(vm, ctx, TrustBinding(TrustMode.VM, vm.name)) as target:
            return target.run_as_root(script, deadline)

    def native_shell(self, vm: VMRow, ctx: RunContext) -> int:
        """An interactive shell as the admin over SSH to the public address, inside its own route.

        Like :meth:`run_as_root`, a VM with no host-key record raises
        ``TrustMissing`` before any route opens.
        """
        trust.require_record(vm.name)
        with self._public_ssh(vm, ctx, TrustBinding(TrustMode.VM, vm.name)) as target:
            return target.shell()

    def read_host_keys(self, vm: VMRow, ctx: RunContext) -> tuple[HostKey, ...]:
        """The host keys of a VM with no record, read at its public address under an Unverified file.

        Nothing can be verified without a record, so the caller shows the
        fingerprints for the operator to compare with the VM's console before
        anything is recorded. A VM that has a record refuses: its keys are
        read under that record, never around it.
        """
        if trust.has_record(vm.name):
            raise StateError(
                f"VM '{vm.name}' already has a recorded host key, so its keys are not read unverified",
                entity_kind="vm",
                entity_name=vm.name,
            )
        with trust.provisioning_binding(vm.name) as binding, self._public_ssh(vm, ctx, binding) as target:
            return root_script.read_host_keys(target.run_as_root)

    @contextlib.contextmanager
    def _public_ssh(self, vm: VMRow, ctx: RunContext, binding: TrustBinding) -> Iterator[PublicSSH]:
        """An answering :class:`PublicSSH` to the VM under ``binding``, for as long as the route is open.

        ``SIGTERM`` and ``SIGHUP`` unwind like Ctrl-C for as long as the route
        may be open, so ending any command that reaches a cloud VM this way
        still closes its allow.
        """
        with termination_as_interrupt(), self._transient_route(vm, ctx):
            address = self._public_address(vm, ctx)
            if not address:
                raise StateError(
                    f"VM '{vm.name}' has no public IP address",
                    entity_kind="vm",
                    entity_name=vm.name,
                    hint=self.public_address_hint,
                )
            # Every operation's context is built with the operator's configuration.
            assert ctx.config is not None
            target = PublicSSH(address, vm.admin_username, ctx.config.operator.ssh_private_key, binding)
            target.wait_until_answering()
            yield target
