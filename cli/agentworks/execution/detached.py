"""Detached work on a managed VM: a root payload that outlives the connection that started it.

``VMAccess.detached()`` returns a :class:`Detached`. Each operation but
``new_launch`` is one elevated request: ``launch``, ``observe``, ``interrupt``
and ``discard`` run the guest program (``detached_program.py``) as a
``PythonProgram``, and ``download`` is a plain ``cat`` of one file of the
launch's ``work/`` into a ``ToFile`` sink. The program keeps each launch in
its own root-only directory, named by an id the workstation chose, and the
guest refuses an id that exists, so a launch never adopts another's result.

Resuming after a dropped connection is the caller's polling loop: ``observe``
changes nothing, so a poll that fails costs nothing and the next one reads
the current state. ``launch`` is a mutation and is never dispatched again.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import resources
from typing import TYPE_CHECKING

from agentworks.errors import ExternalError, ValidationError
from agentworks.execution import detached_program, reachability
from agentworks.execution.envelope import checked_argv, checked_env
from agentworks.execution.errors import CommandFailed, DetachedRefused, ExecutionIncomplete, RequestRefused
from agentworks.execution.requests import Bytes, Command, NoInput, PythonProgram

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution.access import VMAccess
    from agentworks.execution.requests import ToFile
    from agentworks.execution.results import Result

LAUNCH_DEADLINE = 60
OBSERVE_DEADLINE = 30
INTERRUPT_DEADLINE = 30
DISCARD_DEADLINE = 60

_ID = re.compile(r"[0-9a-f]{32}")
_EXEC_REFUSAL = re.compile(r"exec [A-Z][A-Z0-9]*")


def _exec_refusal(status: int, result: Result) -> str | None:
    """The ``exec`` refusal code ``launch`` reported with status 126 or 127, or ``None``.

    A boundary: the code is guest output, so only a last stderr line that is
    exactly an ``exec`` code counts.
    """
    lines = result.stderr.decode("ascii", errors="replace").strip().splitlines()
    code = lines[-1] if lines else ""
    return code if status in (126, 127) and _EXEC_REFUSAL.fullmatch(code) else None


def program_source() -> str:
    """The text of ``detached_program.py``, which every program operation sends."""
    return resources.files("agentworks.execution").joinpath("detached_program.py").read_text(encoding="utf-8")


class State(StrEnum):
    """What a launch's directory shows."""

    ABSENT = detached_program.ABSENT
    """No launch directory: never launched, or discarded."""
    RUNNING = detached_program.RUNNING
    """The payload's process tree or its supervisor still lives."""
    EXITED = detached_program.EXITED
    """The payload ended and its status was recorded."""
    LOST = detached_program.LOST
    """Nothing of the launch lives and no status was recorded: a reboot, both killed, or a failed setup."""


@dataclass(frozen=True)
class Launch:
    """One launch: its VM and the id ``new_launch`` chose. Plain data, held by the caller before dispatch."""

    vm_name: str
    id: str


@dataclass(frozen=True)
class Observation:
    """A launch's state; ``status`` only for ``EXITED``, ``output_tail`` only once it is not ``RUNNING``.

    ``work`` is each regular file in the launch's working directory and its
    size in bytes. ``output_tail`` is at most ``OUTPUT_TAIL_BYTES`` of the end
    of everything the payload wrote to either stream.
    """

    state: State
    status: int | None = None
    work: Mapping[str, int] = field(default_factory=dict)
    output_tail: bytes = field(default=b"", repr=False)


OUTPUT_TAIL_BYTES = detached_program.OUTPUT_TAIL_BYTES


class Detached:
    """The admin's detached work on one VM, every request elevated. Building it connects to nothing."""

    def __init__(self, access: VMAccess) -> None:
        self._access = access

    def new_launch(self) -> Launch:
        """A fresh launch id on this VM. Dispatches nothing, so the caller can discard by id whatever follows."""
        return Launch(self._access.vm_name, secrets.token_hex(16))

    def launch(self, launch: Launch, command: Command) -> None:
        """Start ``command`` as root under ``launch``, returning once the payload has started.

        ``command`` must be elevated and take no input, no working directory
        and no deadline: it runs in the launch's ``work/`` directory, bounded
        by its observer. Its ``env`` reaches the payload's environment on
        stdin, never an argument or a file. A connection outcome (the request
        may or may not have reached the guest) is followed by one ``observe``:
        any state but ``ABSENT`` means it launched. ``ABSENT``, or a follow-up
        that does not complete either, raises ``ExecutionIncomplete`` naming
        the id. An existing id is ``DetachedRefused``, and a command the guest
        cannot execute is ``RequestRefused`` with the ``exec`` failure's code.
        """
        launch_id = self._checked(launch)
        # A boundary: plugins holding an admin access can call this directly.
        if not isinstance(command, Command):
            raise ValidationError(f"a detached launch runs a Command, not {type(command).__name__}")
        if not command.elevated or not isinstance(command.stdin, NoInput) or command.cwd is not None:
            raise ValidationError("a detached launch is elevated, with no stdin and no working directory")
        if command.deadline is not None:
            raise ValidationError("a detached launch takes no deadline: its observer bounds it")
        # The envelope's own rules: the spec travels on stdin, where no envelope checks it.
        spec = json.dumps({"argv": checked_argv(command.argv), "env": checked_env(command.env)}, ensure_ascii=True)
        result = self._access.run(
            self._program("launch", launch_id, LAUNCH_DEADLINE, stdin=Bytes(spec.encode("ascii")))
        )
        if result.completion in reachability.POLLED:
            unknown = ExecutionIncomplete(self._access.vm_name, result)
            unknown.add_hint(f"Detached launch {launch_id} may still start on the VM.")
            try:
                observed = self.observe(launch)
            except ExecutionIncomplete:
                raise unknown from None
            if observed.state is State.ABSENT:
                raise unknown
            return
        status = result.require_completed(self._access.vm_name)
        if status == detached_program.EXIT_EXISTS:
            raise DetachedRefused(self._access.vm_name, launch_id, running=False)
        code = _exec_refusal(status, result)
        if code is not None:
            raise RequestRefused(self._access.vm_name, code, result)
        if status != 0:
            raise CommandFailed(self._access.vm_name, status, result, streams_logged=True)

    def observe(self, launch: Launch) -> Observation:
        """The launch's state, its ``work/`` sizes and, once it is not running, its status and output tail."""
        return self._observation(self._run_program("observe", launch, OBSERVE_DEADLINE))

    def interrupt(self, launch: Launch) -> Observation:
        """Stop a running payload's process group (``SIGTERM``, then ``SIGKILL``) and return the state after.

        A launch that is not running is returned as it is, with nothing
        signalled. Never dispatched again.
        """
        return self._observation(self._run_program("interrupt", launch, INTERRUPT_DEADLINE))

    def download(self, launch: Launch, name: str, to: ToFile) -> None:
        """Copy ``work/<name>`` to the workstation through ``to``, which keeps the file only for a complete copy.

        No deadline: the copy takes as long as the link needs, and the
        connection's keepalives end a dead one. Download only once the launch
        has exited, so nothing is still writing the file.
        """
        launch_id = self._checked(launch)
        if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or not name.isprintable():
            raise ValidationError(f"a detached download names one file of the launch's work directory: {name!r}")
        path = f"{detached_program.BASE}/{launch_id}/work/{name}"
        self._access.run(Command(("cat", "--", path), elevated=True, log_streams=False), stdout=to, check=True)

    def discard(self, launch: Launch) -> None:
        """Remove the launch's directory; an absent one is done, and a running one is ``DetachedRefused``."""
        launch_id = self._checked(launch)
        result = self._access.run(self._program("discard", launch_id, DISCARD_DEADLINE))
        status = result.require_completed(self._access.vm_name)
        if status == detached_program.EXIT_RUNNING:
            raise DetachedRefused(self._access.vm_name, launch_id, running=True)
        if status != 0:
            raise CommandFailed(self._access.vm_name, status, result, streams_logged=True)

    # -- Internals -------------------------------------------------------------

    def _checked(self, launch: Launch) -> str:
        """The launch's id, once it is one this VM's ``new_launch`` could have chosen.

        A boundary: a ``Launch`` is plain data a plugin can build, and the id
        becomes a root path on the guest.
        """
        if not isinstance(launch, Launch) or launch.vm_name != self._access.vm_name:
            raise ValidationError(f"the launch is not one on VM '{self._access.vm_name}'")
        if not isinstance(launch.id, str) or not _ID.fullmatch(launch.id):
            raise ValidationError("a launch id is 32 lowercase hex digits")
        return launch.id

    @staticmethod
    def _program(operation: str, launch_id: str, deadline: float, *, stdin: Bytes | None = None) -> PythonProgram:
        return PythonProgram(
            program_source(),
            args=(operation, launch_id),
            elevated=True,
            deadline=deadline,
            stdin=stdin or NoInput(),
        )

    def _run_program(self, operation: str, launch: Launch, deadline: float) -> Result:
        """Run an operation that answers with an observation, raising for any status but 0."""
        result = self._access.run(self._program(operation, self._checked(launch), deadline))
        status = result.require_completed(self._access.vm_name)
        if status != 0:
            raise CommandFailed(self._access.vm_name, status, result, streams_logged=True)
        return result

    def _observation(self, result: Result) -> Observation:
        """Parse the program's JSON line and the output tail after it. A boundary: guest output."""
        line, _, tail = result.stdout.partition(b"\n")
        try:
            observed = json.loads(line)
            state, status, work = State(observed["state"]), observed["status"], observed["work"]
            if (status is not None) is not (state is State.EXITED) or (status is not None and type(status) is not int):
                raise ValueError
            if not isinstance(work, dict) or not all(
                isinstance(name, str) and type(size) is int for name, size in work.items()
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ExternalError(
                f"the detached program's observation on VM '{self._access.vm_name}' was unreadable"
            ) from None
        return Observation(state, status, work, tail)
