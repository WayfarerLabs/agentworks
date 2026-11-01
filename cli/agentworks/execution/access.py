"""The identity-bound entry point to one VM.

``vm_access`` copies names and paths and resolves nothing (R9): no key is read,
no trust file is opened and nothing is spawned until an operation runs. Each
operation then runs in one order, so everything before the spawn raises with
nothing dispatched: resolve the client, encode the request, build the options
(which reads the trust record), spawn, pump, classify.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution import attach as attach_module
from agentworks.execution import client, envelope, forwarding, ssh_process
from agentworks.execution.agw_launch import refusal_status
from agentworks.execution.errors import CommandFailed, describe_refusal
from agentworks.execution.requests import LoginShell
from agentworks.execution.results import Completed, NotDelivered, NotDeliveredReason, Result
from agentworks.execution.trust import TrustBinding, TrustMode

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import BinaryIO

    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.execution.requests import Forward, Request


@dataclass(frozen=True)
class Admin:
    """The VM's admin account."""


@dataclass(frozen=True)
class Agent:
    """An agent's Linux account on the VM."""

    name: str
    linux_user: str


Identity = Admin | Agent


class VMAccess:
    """Run, stream, attach and forward as one identity on one VM."""

    def __init__(
        self,
        *,
        vm_name: str,
        host: str | None,
        user: str,
        identity_file: Path,
        identity: Identity,
        port: int = 22,
    ) -> None:
        self.vm_name = vm_name
        self.identity = identity
        self._host = host
        self._user = user
        self._identity_file = identity_file
        self._port = port

    # -- Operations ------------------------------------------------------------

    def run(self, request: Request, *, check: bool = False) -> Result:
        """Run ``request`` and return its buffered outcome.

        The client closes ssh's stdin right after the request. Each stream is
        read up to one pump constant (1 MiB); more is ``Unknown(OUTPUT_LIMIT)``.
        With ``check`` a result that is not ``Completed(0)`` raises its typed error.
        """
        stdout, stderr = ssh_process.Capture(), ssh_process.Capture()
        result = self._execute(request, stdin=None, on_stdout=stdout, on_stderr=stderr)
        result = Result(result.completion, bytes(stdout.data), bytes(stderr.data), result.client_log_tail)
        if check and result.require_completed(self.vm_name) != 0:
            assert isinstance(result.completion, Completed)
            raise CommandFailed(self.vm_name, result.completion.status, result)
        return result

    def stream(self, request: Request, *, stdin: BinaryIO | None, stdout: BinaryIO, stderr: BinaryIO) -> int:
        """Run ``request`` with live output and return the guest's status.

        ``stdin`` is copied to the guest until end of file; pass the raw
        binary stream, or ``None`` to close the guest's input at once. A refusal
        becomes the shell's status with one line on ``stderr``: 127 for a
        missing command, 126 for any other ``exec`` failure and 125 for a
        working directory. Anything else that is not a completion raises.
        """

        def deliver(sink: BinaryIO) -> ssh_process.Sink:
            def write(payload: bytes) -> None:
                sink.write(payload)
                sink.flush()

            return write

        result = self._execute(request, stdin=stdin, on_stdout=deliver(stdout), on_stderr=deliver(stderr))
        completion = result.completion
        if isinstance(completion, NotDelivered) and completion.reason is NotDeliveredReason.REFUSED:
            code = completion.code or ""
            with contextlib.suppress(OSError):
                stderr.write(f"agw: {describe_refusal(code)}\n".encode())
                stderr.flush()
            return refusal_status(code)
        return result.require_completed(self.vm_name)

    def attach(self, request: LoginShell) -> int:
        """Attach the operator's terminal to a login shell and return its exit status."""
        ssh = client.resolve_ssh()
        policy, destination = self._policy(), self._destination()

        def parking_argv(log: Path) -> list[str]:
            command = ssh_process.stage0_command()
            return ssh_process.build_argv(ssh, policy, destination, ssh_process.ArgvMode.PIPE, command, client_log=log)

        def terminal_argv(socket_path: str) -> list[str]:
            command = ssh_process.stage0_command(socket_path)
            return ssh_process.build_argv(ssh, policy, destination, ssh_process.ArgvMode.TERMINAL, command)

        return attach_module.attach(
            request, vm_name=self.vm_name, parking_argv=parking_argv, terminal_argv=terminal_argv
        )

    def forward(self, forwards: Sequence[Forward], *, verbose: bool = False) -> int:
        """Forward local ports until the client exits, and return its status."""
        ssh = client.resolve_ssh()
        policy, destination = self._policy(), self._destination()
        with contextlib.ExitStack() as stack:
            log = None if verbose else stack.enter_context(ssh_process.client_log())
            argv = ssh_process.build_argv(
                ssh,
                policy,
                destination,
                ssh_process.ArgvMode.FORWARD,
                forwards=forwards,
                client_log=log,
                verbose=verbose,
            )
            return forwarding.run_forward(argv, vm_name=self.vm_name, log=log)

    # -- Internals -------------------------------------------------------------

    def _policy(self) -> ssh_process.VMPolicy:
        return ssh_process.VMPolicy(self._identity_file, TrustBinding(TrustMode.VM, self.vm_name))

    def _destination(self) -> ssh_process.Destination:
        # The first use of a persisted value: a VM with no address is a state
        # problem, not a request problem.
        if self._host is None:
            raise StateError(
                f"VM '{self.vm_name}' has no Tailscale IP",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="VM init may not be complete. Check 'vm describe' for status.",
            )
        return ssh_process.Destination(self._host, self._user, self._port)

    def _execute(
        self,
        request: Request,
        *,
        stdin: BinaryIO | None,
        on_stdout: ssh_process.Sink,
        on_stderr: ssh_process.Sink,
    ) -> Result:
        if isinstance(request, LoginShell):
            raise ValidationError("a login shell can only be attached", entity_kind="vm", entity_name=self.vm_name)
        ssh = client.resolve_ssh()
        nonce = envelope.new_nonce()
        wire = envelope.frame(envelope.launcher_source()) + envelope.frame(envelope.encode(request, nonce=nonce))
        policy, destination = self._policy(), self._destination()
        with ssh_process.client_log() as log:
            argv = ssh_process.build_argv(
                ssh, policy, destination, ssh_process.ArgvMode.PIPE, ssh_process.stage0_command(), client_log=log
            )
            with ssh_process.PipeSession(
                argv,
                request=wire,
                nonce=nonce,
                stdin=stdin,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                deadline=ssh_process.Deadline.after(request.deadline),
            ) as session:
                stop = session.pump()
                session.end()
            return ssh_process.outcome(session, stop, log)


def vm_access(vm: VMRow, config: Config, identity: Identity) -> VMAccess:
    """The access object for ``identity`` on ``vm``. Passive: it copies names and paths and nothing else."""
    user = vm.admin_username if isinstance(identity, Admin) else identity.linux_user
    return VMAccess(
        vm_name=vm.name,
        host=vm.tailscale_host,
        user=user,
        identity_file=config.operator.ssh_private_key,
        identity=identity,
    )
