"""The identity-bound entry point to one VM.

``vm_access`` copies names and paths and resolves nothing (R9): no key is read,
no trust file is opened and nothing is spawned until an operation runs. Each
operation then runs in one order, so everything before the spawn raises with
nothing dispatched: refuse an agent's elevated request, resolve the client,
encode the request, build the options (which reads the trust record), spawn,
pump, classify.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import StateError, ValidationError
from agentworks.execution import attach as attach_module
from agentworks.execution import client, envelope, forwarding, pump, ssh_process
from agentworks.execution.agw_launch import refusal_status
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete, PermissionDenied, describe_refusal
from agentworks.execution.files import Files
from agentworks.execution.requests import Bytes, CaptureOutput, DiscardOutput, NoInput
from agentworks.execution.results import Completed, NotDelivered, NotDeliveredReason, Result, Unknown, UnknownReason
from agentworks.execution.trust import TrustBinding, TrustMode

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path
    from typing import BinaryIO

    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.execution.requests import Forward, LoginShell, OutputSink, RunRequest


SIGPIPE_STATUS = 128 + 13
"""What ``stream`` reports when a consumer left and the guest reported no status: a writer killed by ``SIGPIPE``."""


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
    """Run, stream, attach and forward as one identity on one VM.

    ``identity`` is the account the access was built for, with no default,
    so a directly constructed access chooses; ``run`` and ``stream`` refuse an
    elevated request on an agent-bound one. ``trust_mode`` is ``VM`` except for
    the access :func:`enrollment_access` builds. ``log`` is an operation log's
    record callable (``SSHLogger.record``), which ``run`` calls once per
    request. ``stream``, ``attach`` and ``forward`` serve operator commands that
    keep no log.
    """

    def __init__(
        self,
        *,
        vm_name: str,
        host: str | None,
        user: str,
        identity: Identity,
        identity_file: Path,
        port: int = 22,
        trust_mode: TrustMode = TrustMode.VM,
        log: Callable[[RunRequest, Result], None] | None = None,
    ) -> None:
        if isinstance(identity, Agent) and user != identity.linux_user:
            raise ValueError("an agent-bound access connects as the agent's Linux user")
        self.vm_name = vm_name
        self.identity = identity
        self.trust_mode = trust_mode
        self._host = host
        self._user = user
        self._identity_file = identity_file
        self._port = port
        self._log = log

    # -- Operations ------------------------------------------------------------

    def run(
        self,
        request: RunRequest,
        *,
        stdout: OutputSink = CaptureOutput(),
        stderr: OutputSink = CaptureOutput(),
        check: bool = False,
    ) -> Result:
        """Run ``request`` and return its buffered outcome.

        The client writes the request's ``stdin`` after it, then closes ssh's
        stdin. Each stream goes to its sink: a ``CaptureOutput`` keeps up to
        its limit, and more is ``Unknown(OUTPUT_LIMIT)``; a ``DiscardOutput``
        keeps nothing, and the result's field for it is empty. With ``check``
        a result that is not ``Completed(0)`` raises its typed error. The
        access's operation log, when it has one, records every request that
        reaches an outcome, before raising for a bad one. A request that
        raises before it is dispatched (no client, no trust record) is not
        logged.

        Plugins hold an access and call this directly, so it validates what
        the type checker cannot promise them: ``stdin`` is ``NoInput()`` or
        ``Bytes`` of ``bytes``, and each sink is a ``DiscardOutput`` or a
        ``CaptureOutput`` with a positive ``int`` limit. Anything else is a
        ``ValidationError`` before anything resolves.
        """
        _check_run_inputs(request, (stdout, stderr))
        kept_stdout, kept_stderr = _capture(stdout), _capture(stderr)
        with self._passphrase_hint_on_connection_failure():
            result = self._execute(
                request, stdin=None, on_stdout=kept_stdout or _discard, on_stderr=kept_stderr or _discard
            )
            result = Result(result.completion, _kept(kept_stdout), _kept(kept_stderr), result.client_log_tail)
            if self._log is not None:
                self._log(request, result)
            if check and result.require_completed(self.vm_name) != 0:
                assert isinstance(result.completion, Completed)
                raise CommandFailed(self.vm_name, result.completion.status, result)
            return result

    def stream(self, request: RunRequest, *, stdin: BinaryIO | None, stdout: BinaryIO, stderr: BinaryIO) -> int:
        """Run ``request`` with live output and return the guest's status.

        ``stdin`` is copied to the guest until end of file; pass the raw
        binary stream, or ``None`` to close the guest's input at once. The
        request itself carries no input here: one whose ``stdin`` is not
        ``NoInput`` is a ``ValidationError`` before anything resolves. A refusal
        becomes the shell's status with one line on ``stderr``: 127 for a
        missing command, 126 for any other ``exec`` failure and 125 for a
        working directory. A consumer that leaves (``| head``) gets what ssh gave
        it: the guest's own status when it still ends and reports one, else 141,
        the status of a writer killed by ``SIGPIPE``. Anything else that is not
        a completion raises.
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

        # A boundary: plugins hold this access through their run context.
        if not isinstance(request.stdin, NoInput):
            raise ValidationError("stream sends the operator's stdin; a request with its own input runs under run")
        with self._passphrase_hint_on_connection_failure():
            result = self._execute(request, stdin=stdin, on_stdout=deliver(stdout), on_stderr=deliver(stderr))
            completion = result.completion
            if completion == Unknown(UnknownReason.CONSUMER_LEFT):
                return SIGPIPE_STATUS
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
            with self._passphrase_hint_on_connection_failure():
                return attach_module.attach(
                    vm_name=self.vm_name, wire=wire, nonce=nonce, parking_argv=parking, terminal_argv=terminal, log=log
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

    def files(self, *, deadline: float | None = None) -> Files:
        """This identity's file operations on the VM, each request bounded by ``deadline`` seconds.

        Building it connects to nothing; each operation is one ``run``.
        """
        return Files(self, deadline=deadline)

    def passphrase_hint(self) -> str | None:
        """:func:`client.agent_load_hint` for the operator's key; ask only after a connection failure."""
        return client.agent_load_hint(self._identity_file)

    # -- Internals -------------------------------------------------------------

    @contextlib.contextmanager
    def _passphrase_hint_on_connection_failure(self) -> Iterator[None]:
        """Add :meth:`passphrase_hint` to a connection or authentication failure, reading the key only after one.

        An encrypted key the agent does not hold is refused like any other, so
        it fails as ``AUTH_FAILED`` when ssh logs the refusal and as
        ``CONNECTION`` when it does not.
        """
        try:
            yield
        except ExecutionIncomplete as error:
            if error.result.completion in (Unknown(UnknownReason.CONNECTION), Unknown(UnknownReason.AUTH_FAILED)):
                error.add_hint(self.passphrase_hint())
            raise

    def _policy(self) -> ssh_process.VMPolicy:
        return ssh_process.VMPolicy(self._identity_file, TrustBinding(self.trust_mode, self.vm_name))

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
        request: RunRequest,
        *,
        stdin: BinaryIO | None,
        on_stdout: pump.Sink,
        on_stderr: pump.Sink,
    ) -> Result:
        # R3: only the admin elevates, whatever the guest's sudoers says.
        if request.elevated and not isinstance(self.identity, Admin):
            raise PermissionDenied(self.vm_name, self._user)
        ssh = client.resolve_ssh()
        nonce = envelope.new_nonce()
        wire = envelope.message(request, nonce=nonce)
        if isinstance(request.stdin, Bytes):
            wire += request.stdin.data
        policy, destination = self._policy(), self._destination()
        with ssh_process.client_log() as log:
            argv = ssh_process.build_argv(
                ssh,
                policy,
                destination,
                ssh_process.ArgvMode.PIPE,
                ssh_process.stage0_command(elevated=request.elevated),
                client_log=log,
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


def _check_run_inputs(request: RunRequest, sinks: tuple[OutputSink, OutputSink]) -> None:
    """Refuse ``run`` input a plugin's untyped code could pass (see :meth:`VMAccess.run`)."""
    stdin = request.stdin
    if not isinstance(stdin, NoInput | Bytes):
        raise ValidationError(f"a request's stdin is NoInput() or Bytes(data), not {type(stdin).__name__}")
    if isinstance(stdin, Bytes) and type(stdin.data) is not bytes:
        raise ValidationError(f"Bytes data is bytes, not {type(stdin.data).__name__}")
    for sink in sinks:
        if isinstance(sink, DiscardOutput):
            continue
        if not isinstance(sink, CaptureOutput):
            raise ValidationError(f"a run sink is CaptureOutput or DiscardOutput, not {type(sink).__name__}")
        if type(sink.limit) is not int or sink.limit <= 0:
            raise ValidationError("a CaptureOutput limit is a positive int")


def _capture(sink: OutputSink) -> pump.Capture | None:
    return pump.Capture(sink.limit) if isinstance(sink, CaptureOutput) else None


def _kept(capture: pump.Capture | None) -> bytes:
    return b"" if capture is None else bytes(capture.data)


def _discard(_chunk: bytes) -> None:
    """The pump sink for ``DiscardOutput``: the filter has already read the markers."""


def stream_to_operator(access: VMAccess, request: RunRequest) -> int:
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
    try:
        status = access.stream(request, stdin=stdin, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer)
    finally:
        # Both streams are always attempted, and nothing is raised here, so a failure in flight wins.
        failures = [_flush_or_point_at_null_device(stream) for stream in (sys.stdout.buffer, sys.stderr.buffer)]
    for failure in failures:
        if failure is not None:
            raise failure
    return status


def _flush_or_point_at_null_device(stream: BinaryIO) -> OSError | None:
    """Flush ``stream``, pointing its descriptor at the null device if its consumer has left.

    Bytes that could not be flushed to a departed consumer (``| head``) stay
    in the stream's buffer and would fail again at interpreter shutdown,
    turning the exit status into 120 with a traceback; the null device gives
    shutdown somewhere to write them (:func:`output.tolerating_dead_sink`).
    Any other flush failure is returned for the caller to raise, not raised.
    """
    try:
        with output.tolerating_dead_sink(stream):
            stream.flush()
    except OSError as error:
        return error
    return None


def vm_access(
    vm: VMRow, config: Config, identity: Identity, *, log: Callable[[RunRequest, Result], None] | None = None
) -> VMAccess:
    """The access object for ``identity`` on ``vm``. Passive: it copies names and paths and nothing else."""
    user = vm.admin_username if isinstance(identity, Admin) else identity.linux_user
    return VMAccess(
        vm_name=vm.name,
        host=vm.tailscale_host,
        user=user,
        identity=identity,
        identity_file=config.operator.ssh_private_key,
        log=log,
    )


def enrollment_access(
    vm: VMRow, config: Config, *, log: Callable[[RunRequest, Result], None] | None = None
) -> VMAccess:
    """The admin's access in Enroll mode: the only access that may record a new host key.

    Create's first contact is its only caller. Every connection it makes runs
    under ``accept-new`` against the VM's freshly reset store file, so the
    first one that reaches sshd records the key and the rest verify against
    it. Like :func:`vm_access` it copies the row's address at construction, so
    pass a row read after the address was recorded.
    """
    return VMAccess(
        vm_name=vm.name,
        host=vm.tailscale_host,
        user=vm.admin_username,
        identity=Admin(),
        identity_file=config.operator.ssh_private_key,
        trust_mode=TrustMode.ENROLL,
        log=log,
    )
