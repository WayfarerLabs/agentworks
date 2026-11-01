"""The identity-bound entry point to one VM.

``vm_access`` copies names and paths and resolves nothing (R9): no key is read,
no trust file is opened and nothing is spawned until an operation runs. Each
operation then runs in one order, so everything before the spawn raises with
nothing dispatched: resolve the client, encode the request, build the options
(which reads the trust record), spawn, pump, classify.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution import attach as attach_module
from agentworks.execution import client, envelope, forwarding, pump, ssh_process
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
        port: int = 22,
    ) -> None:
        self.vm_name = vm_name
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
        stdout, stderr = pump.Capture(), pump.Capture()
        result = self._execute(request, stdin=None, on_stdout=stdout, on_stderr=stderr)
        result = Result(result.completion, bytes(stdout.data), bytes(stderr.data), result.client_log_tail)
        if check and result.require_completed(self.vm_name, identity_file=self._identity_file) != 0:
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

        def deliver(sink: BinaryIO) -> pump.Sink:
            def write(payload: bytes) -> None:
                sink.write(payload)  # a full non-blocking pipe raises BlockingIOError(characters_written)
                try:
                    sink.flush()
                except BlockingIOError as error:
                    # The payload is in the stream's buffer; the pump offers an empty remainder to flush it.
                    raise BlockingIOError(error.errno, error.strerror, len(payload)) from None

            return write

        result = self._execute(request, stdin=stdin, on_stdout=deliver(stdout), on_stderr=deliver(stderr))
        completion = result.completion
        if isinstance(completion, NotDelivered) and completion.reason is NotDeliveredReason.REFUSED:
            code = completion.code or ""
            with contextlib.suppress(OSError):
                stderr.write(f"agw: {describe_refusal(code)}\n".encode())
                stderr.flush()
            return refusal_status(code)
        return result.require_completed(self.vm_name, identity_file=self._identity_file)

    def attach(self, request: LoginShell) -> int:
        """Attach the operator's terminal to a login shell and return its exit status."""
        if request.deadline is not None:
            raise ValidationError(
                "an attached shell cannot carry a deadline", entity_kind="vm", entity_name=self.vm_name
            )
        ssh = client.resolve_ssh()
        policy, destination = self._policy(), self._destination()
        token, nonce = envelope.new_handover_token(), envelope.new_nonce()
        term = os.environ.get("TERM") or envelope.DEFAULT_TERM
        wire = envelope.message(request, nonce=nonce, term=term, handover=token)
        with ssh_process.client_log() as log:
            # Both argvs are built before anything spawns, so a missing trust record raises first.
            parking = ssh_process.build_argv(
                ssh, policy, destination, ssh_process.ArgvMode.PIPE, ssh_process.stage0_command(), client_log=log
            )
            terminal = ssh_process.build_argv(
                ssh,
                policy,
                destination,
                ssh_process.ArgvMode.TERMINAL,
                ssh_process.stage0_command(attach_module.socket_path(token)),
            )
            return attach_module.attach(
                vm_name=self.vm_name,
                wire=wire,
                nonce=nonce,
                parking_argv=parking,
                terminal_argv=terminal,
                log=log,
                identity_file=self._identity_file,
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
        on_stdout: pump.Sink,
        on_stderr: pump.Sink,
    ) -> Result:
        if isinstance(request, LoginShell):
            raise ValidationError("a login shell can only be attached", entity_kind="vm", entity_name=self.vm_name)
        ssh = client.resolve_ssh()
        nonce = envelope.new_nonce()
        wire = envelope.message(request, nonce=nonce)
        policy, destination = self._policy(), self._destination()
        with ssh_process.client_log() as log:
            argv = ssh_process.build_argv(
                ssh, policy, destination, ssh_process.ArgvMode.PIPE, ssh_process.stage0_command(), client_log=log
            )
            with pump.PipeSession(
                argv,
                request=wire,
                nonce=nonce,
                stdin=stdin,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                deadline=pump.Deadline.after(request.deadline),
            ) as session:
                stop = session.pump()
                session.end()
            return ssh_process.outcome(session, stop, log)


def stream_to_operator(access: VMAccess, request: Request) -> int:
    """Stream ``request`` with the process's own stdio and return the guest's status.

    Newline translation by the text layers (a Windows console's, and the
    bare-LF setting in ``cli/_entry.py``) must not touch a binary stream, so
    the callers get the binary buffers, with the text layers flushed first so
    earlier output keeps its order (I2). Stdin is the raw stream: a daemon
    thread blocked in a buffered read aborts the interpreter at exit.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    stdin = None if sys.stdin is None else getattr(sys.stdin.buffer, "raw", sys.stdin.buffer)
    return access.stream(request, stdin=stdin, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer)


def vm_access(vm: VMRow, config: Config, identity: Identity) -> VMAccess:
    """The access object for ``identity`` on ``vm``. Passive: it copies names and paths and nothing else."""
    user = vm.admin_username if isinstance(identity, Admin) else identity.linux_user
    return VMAccess(
        vm_name=vm.name,
        host=vm.tailscale_host,
        user=user,
        identity_file=config.operator.ssh_private_key,
    )
