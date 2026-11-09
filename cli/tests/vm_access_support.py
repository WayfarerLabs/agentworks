"""``VMAccess`` stand-ins for tests: a recording fake, and a real access whose guest is this machine.

``RecordingVMAccess`` subclasses ``VMAccess``, overrides its four operations to
record what was asked and return a canned outcome, and spawns nothing. The
``AccessRecorder`` stands in for ``agentworks.execution.vm_access``,
``enrollment_access`` and ``resolve_ssh``, keeping one access object per
identity (and one for enrollment) and one ordered call list, so a test can
assert both what each identity was asked and in what order. Manager tests built
on it run on every CI host.

``local_files`` runs file operations' real program on this machine behind a
recording access, for tests of what a caller publishes.

``local_vm_access`` is the conformance suite's L lane for a caller's own
tests: the real ``VMAccess``, pump and launcher, with the account shell run
locally instead of over ssh, so a probe's or a program's real request runs
against files the test controls. It needs a POSIX host.
"""

from __future__ import annotations

import getpass
import re
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentworks.execution import client, envelope, ssh_process, trust
from agentworks.execution.access import Admin, Agent, Identity, VMAccess
from agentworks.execution.client import SSHClient
from agentworks.execution.requests import CaptureOutput, DiscardOutput
from agentworks.execution.results import Completed, Result
from agentworks.execution.trust import TrustMode
from tests.ssh_fixtures import fake_host_key

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence
    from typing import BinaryIO

    import pytest

    from agentworks.execution.files import Files
    from agentworks.execution.requests import Forward, LoginShell, OutputSink, RunRequest


@dataclass(frozen=True)
class Call:
    """One operation asked of an access object."""

    identity: Identity
    operation: str
    request: Any
    options: dict[str, Any] = field(default_factory=dict)
    enrolling: bool = False


class RecordingVMAccess(VMAccess):
    """Records ``run``, ``stream``, ``attach`` and ``forward`` and returns canned outcomes.

    ``answer``, when a test sets it, is asked first with each recorded call
    and may return the outcome, which suits a stateful double that answers
    several probes. A request given an answer with :meth:`respond` gets it
    every time it is asked. Any other ``run`` consumes ``run_outcomes`` in
    order, then gets ``default_outcome``, ``Completed(0)`` unless a test sets
    it. An outcome that is an exception is raised. A stream ``run`` was asked
    to discard comes back empty, as it does from a real access.
    ``stream_status``, ``attach_status`` and ``forward_status`` are returned
    (or raised) by their operation.
    """

    def __init__(
        self, calls: list[Call], identity: Identity, *, vm_name: str = "box", trust_mode: TrustMode = TrustMode.VM
    ) -> None:
        super().__init__(
            vm_name=vm_name,
            host="100.64.0.9",
            user="agentworks" if isinstance(identity, Admin) else identity.linux_user,
            identity=identity,
            identity_file=Path("/nonexistent/key"),
            trust_mode=trust_mode,
        )
        self.calls = calls
        self.answer: Callable[[Call], Result | BaseException | None] | None = None
        self.run_outcomes: list[Result | BaseException] = []
        self.responses: list[tuple[RunRequest, Result | BaseException]] = []
        self.default_outcome: Result | BaseException = Result(Completed(0))
        self.stream_status: int | BaseException = 0
        self.attach_status: int | BaseException = 0
        self.forward_status: int | BaseException = 0

    def run(
        self,
        request: RunRequest,
        *,
        stdout: OutputSink = CaptureOutput(),
        stderr: OutputSink = CaptureOutput(),
        check: bool = False,
    ) -> Result:
        call = Call(
            self.identity,
            "run",
            request,
            {"check": check, "stdout": stdout, "stderr": stderr},
            enrolling=self.trust_mode is TrustMode.ENROLL,
        )
        self.calls.append(call)
        answer = self.answer(call) if self.answer is not None else None
        if answer is None:
            answer = next((outcome for known, outcome in self.responses if known == request), None)
        if answer is not None:
            outcome = answer
        else:
            outcome = self.run_outcomes.pop(0) if self.run_outcomes else self.default_outcome
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(stdout, DiscardOutput):
            outcome = replace(outcome, stdout=b"")
        if isinstance(stderr, DiscardOutput):
            outcome = replace(outcome, stderr=b"")
        enrolls = self.trust_mode is TrustMode.ENROLL and isinstance(outcome.completion, Completed)
        if enrolls and not trust.has_record(self.vm_name):
            # What accept-new does when a connection reaches sshd and none is recorded.
            trust.write_records(self.vm_name, [fake_host_key()])
        if check and outcome.require_completed(self.vm_name) != 0:
            raise AssertionError("a canned non-zero result under check=True; script an exception instead")
        return outcome

    def respond(self, request: RunRequest, outcome: Result | BaseException) -> None:
        """Answer ``run`` with ``outcome`` whenever it is asked a request equal to ``request``.

        Requests come from one named builder per probe, so a test names the
        probe it answers by calling that builder.
        """
        self.responses.append((request, outcome))

    def requests(self) -> list[RunRequest]:
        """Every request ``run`` was asked, in order."""
        return [call.request for call in self.calls if call.operation == "run" and call.identity == self.identity]

    def stream(self, request: RunRequest, *, stdin: BinaryIO | None, stdout: BinaryIO, stderr: BinaryIO) -> int:
        self.calls.append(Call(self.identity, "stream", request, {"stdin": stdin, "stdout": stdout, "stderr": stderr}))
        return self._status(self.stream_status)

    def attach(self, request: LoginShell) -> int:
        self.calls.append(Call(self.identity, "attach", request))
        return self._status(self.attach_status)

    def forward(self, forwards: Sequence[Forward], *, verbose: bool = False) -> int:
        self.calls.append(Call(self.identity, "forward", tuple(forwards), {"verbose": verbose}))
        return self._status(self.forward_status)

    @staticmethod
    def _status(outcome: int | BaseException) -> int:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class AccessRecorder:
    """Stands in for ``agentworks.execution.vm_access``: one recording access per identity."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.built: list[tuple[str, Identity]] = []
        self.rows: list[Any] = []
        self.logs: list[Any] = []
        self.client = SSHClient("/usr/bin/ssh", (9, 2))
        self._accesses: dict[Identity, RecordingVMAccess] = {}
        self._enrollment: RecordingVMAccess | None = None
        self.enrolled: list[str] = []
        self.answer: Callable[[Call], Result | BaseException | None] | None = None
        """Asked first by every access it builds, as ``RecordingVMAccess.answer`` is."""

    def vm_access(self, vm: Any, config: Any, identity: Identity, *, log: Any = None) -> RecordingVMAccess:
        self.built.append((vm.name, identity))
        self.rows.append(vm)
        self.logs.append(log)
        return self.access(identity, vm_name=vm.name)

    def enrollment_access(self, vm: Any, config: Any, *, log: Any = None) -> RecordingVMAccess:
        """Stands in for ``execution.enrollment_access``; its first completed run records a host key."""
        self.enrolled.append(vm.name)
        self.rows.append(vm)
        self.logs.append(log)
        return self.enrollment(vm_name=vm.name)

    def enrollment(self, *, vm_name: str = "box") -> RecordingVMAccess:
        """The admin's Enroll-mode access, created on first use; its calls join the one ordered list."""
        if self._enrollment is None:
            self._enrollment = RecordingVMAccess(self.calls, Admin(), vm_name=vm_name, trust_mode=TrustMode.ENROLL)
        return self._enrollment

    def access(self, identity: Identity | None = None, *, vm_name: str = "box") -> RecordingVMAccess:
        """The access object for ``identity`` (the admin by default), created on first use."""
        identity = identity or Admin()
        if identity not in self._accesses:
            access = RecordingVMAccess(self.calls, identity, vm_name=vm_name)
            access.answer = lambda call: self.answer(call) if self.answer is not None else None
            self._accesses[identity] = access
        return self._accesses[identity]

    @property
    def operations(self) -> list[tuple[str, str]]:
        """``(identity label, operation)`` for each recorded call, in order."""
        return [(_label(call), call.operation) for call in self.calls]

    def requests(self, operation: str) -> list[Any]:
        return [call.request for call in self.calls if call.operation == operation]


def install_access_recorder(monkeypatch: pytest.MonkeyPatch) -> AccessRecorder:
    """Route ``execution.vm_access`` and ``enrollment_access`` to a new recording fake.

    ssh resolves without a client. A later call replaces the earlier recorder.
    """
    recorder = AccessRecorder()
    monkeypatch.setattr("agentworks.execution.vm_access", recorder.vm_access)
    monkeypatch.setattr("agentworks.execution.enrollment_access", recorder.enrollment_access)
    monkeypatch.setattr("agentworks.execution.resolve_ssh", lambda: recorder.client)
    return recorder


def is_command_check(request: object) -> bool:
    """Whether ``request`` is a ``command_checks`` probe."""
    from agentworks.command_checks import STATUS_PREFIX
    from agentworks.execution.requests import Script

    return isinstance(request, Script) and STATUS_PREFIX in request.source


def _check_nonce(source: str) -> str | None:
    """The nonce of the command check whose script is ``source``, or ``None`` when it is not one."""
    from agentworks.command_checks import STATUS_PREFIX

    match = re.search(re.escape(STATUS_PREFIX) + r"([0-9a-f]+):", source)
    return None if match is None else match[1]


def check_result(request: Any, status: int) -> Result:
    """What the command check ``request`` reports for a command that exited ``status``, with its own nonce."""
    from agentworks.command_checks import STATUS_PREFIX

    nonce = _check_nonce(request.source)
    assert nonce is not None, "not a command check"
    return Result(Completed(0), f"\n{STATUS_PREFIX}{nonce}:{status}\n".encode())


def presence_check(request: object) -> str | None:
    """The command ``request`` checks for, when it is exactly ``presence_probe``'s request for it; else ``None``."""
    from agentworks.command_checks import presence_probe
    from agentworks.execution.requests import Script

    if not isinstance(request, Script):
        return None
    nonce = _check_nonce(request.source)
    command = re.search(r"^command -v (.+) >/dev/null 2>&1 \|\| exit \d+$", request.source, re.MULTILINE)
    if nonce is None or command is None:
        return None
    (name,) = shlex.split(command[1])
    return name if request == presence_probe(name, nonce=nonce, deadline=request.deadline) else None


def answer_command_checks(*, missing: Collection[str] = (), status: int | None = 0) -> Callable[[Call], Result | None]:
    """An ``answer`` for command checks: each presence check finds its command unless it is in ``missing``.

    Any other command check reports ``status``; with ``None`` it is left to
    the access's other outcomes, as is every request that is not a check.
    """

    def answer(call: Call) -> Result | None:
        command = presence_check(call.request)
        if command is not None:
            return check_result(call.request, 20 if command in missing else 0)
        if status is not None and is_command_check(call.request):
            return check_result(call.request, status)
        return None

    return answer


def command_check_access(*, missing: Collection[str] = (), identity: Identity | None = None) -> RecordingVMAccess:
    """An access on which every required-command check finds its command, except those in ``missing``.

    Any request that is not a command check fails the test.
    """
    access = RecordingVMAccess([], identity or Admin())
    access.answer = answer_command_checks(missing=missing)
    access.default_outcome = AssertionError("an unscripted request reached the access")
    return access


# -- A real access whose guest is this machine ------------------------------------------

ACCOUNT_SHELL = """#!/bin/bash
# A test account's login shell. Login startup reads only $HOME/.profile; nothing else is read.
login=
while :; do
  case "$1" in
    -l) login=1; shift ;;
    -i) shift ;;
    *) break ;;
  esac
done
if [ -n "$login" ] && [ -f "$HOME/.profile" ]; then . "$HOME/.profile"; fi
exec /bin/bash --noprofile --norc "$@"
"""


def account_shell(directory: Path) -> Path:
    """Write :data:`ACCOUNT_SHELL` into ``directory``: a login shell that reads only the test's own ``.profile``."""
    path = directory / "account-shell"
    path.write_text(ACCOUNT_SHELL)
    path.chmod(0o755)
    return path


def local_remote_argv(remote_command: str, *, home: Path, shell: str, python: str) -> list[str]:
    """What runs in place of ``ssh``: the account ``shell`` given the remote command, as sshd gives it.

    The environment is the one sshd starts the account shell with, ``SSH_CLIENT``
    included (Debian's bash reads ``~/.bashrc`` when it sees it), and
    ``python`` stands in for the guest's ``/usr/bin/python3`` in stage 0.
    """
    user = getpass.getuser()
    environment = [
        f"HOME={home}",
        "PATH=/usr/bin:/bin",
        f"USER={user}",
        f"LOGNAME={user}",
        f"SHELL={shell}",
        "LANG=C.UTF-8",
        "SSH_CLIENT=203.0.113.5 52000 22",
        "SSH_CONNECTION=203.0.113.5 52000 10.0.0.2 22",
    ]
    command = remote_command.replace(ssh_process.DEFAULT_PYTHON, python, 1)
    return ["/usr/bin/env", "-i", *environment, shell, "-c", command]


def run_requests_locally(monkeypatch: pytest.MonkeyPatch, remote_argv: Callable[[str], list[str]]) -> None:
    """Make every ``VMAccess`` in the test spawn ``remote_argv(remote_command)`` instead of ``ssh``."""

    def build_argv(
        _client: object,
        _policy: object,
        _destination: object,
        _mode: object,
        remote_command: str | None = None,
        *_args: object,
        **_kwargs: object,
    ) -> list[str]:
        assert remote_command is not None
        return remote_argv(remote_command)

    monkeypatch.setattr(client, "resolve_ssh", lambda: SSHClient("/nonexistent/ssh", (9, 9)))
    monkeypatch.setattr(ssh_process, "build_argv", build_argv)


_ORIGINAL_LAUNCHER = envelope.launcher_source()
"""The launcher as the package ships it, before any test rewrites it."""


def rewrite_launcher(monkeypatch: pytest.MonkeyPatch, old: bytes, new: bytes) -> None:
    """Send a launcher whose source has ``old`` replaced by ``new``; the replacement must apply."""
    source = envelope.launcher_source()
    rewritten = source.replace(old, new)
    assert rewritten != source
    monkeypatch.setattr(envelope, "launcher_source", lambda: rewritten)


def local_vm_access(
    monkeypatch: pytest.MonkeyPatch,
    *,
    home: Path,
    shell: Path,
    identity: Identity | None = None,
    log: Callable[[RunRequest, Result], None] | None = None,
) -> VMAccess:
    """A real ``VMAccess`` to this machine as an account whose passwd entry has ``home`` and ``shell``.

    The launcher is sent with that home and shell in place of the runner's own
    passwd entry, so the payload's ``HOME``, its default shell and every
    startup file it reads are the test's. The guest's python3 is
    ``/usr/bin/python3``. The access never elevates: root's passwd entry would
    get the same home and shell.

    Build it once per test and reuse it: the launcher rewrite it installs
    holds for the rest of the test, so a second build raises.
    """
    if envelope.launcher_source() != _ORIGINAL_LAUNCHER:
        raise RuntimeError("local_vm_access was already built in this test; reuse that access")
    run_requests_locally(
        monkeypatch,
        lambda command: local_remote_argv(command, home=home, shell=str(shell), python=ssh_process.DEFAULT_PYTHON),
    )
    rewrite_launcher(monkeypatch, b"home = entry.pw_dir", b"home = " + repr(str(home)).encode())
    rewrite_launcher(monkeypatch, b'shell = entry.pw_shell or "/bin/sh"', b"shell = " + repr(str(shell)).encode())
    user = getpass.getuser()
    return VMAccess(
        vm_name="local",
        host="127.0.0.1",
        user=user,
        identity=identity or Admin(),
        identity_file=Path("/nonexistent/identity"),
        log=log,
    )


def _label(call: Call) -> str:
    if call.enrolling:
        return "enroll"
    return "admin" if isinstance(call.identity, Admin) else call.identity.name


__all__ = [
    "AccessRecorder",
    "Admin",
    "Agent",
    "Call",
    "RecordingVMAccess",
    "account_shell",
    "answer_command_checks",
    "answer_destination_identity",
    "check_result",
    "command_check_access",
    "is_command_check",
    "presence_check",
    "local_files",
    "local_remote_argv",
    "local_vm_access",
    "rewrite_launcher",
    "run_file_program_locally",
    "run_requests_locally",
]


def run_file_program_locally(call: Call) -> Result | None:
    """Run a file operation's real program on this machine, as the test's own user; ``None`` for any other request.

    The program's source and arguments are the request's, run by this
    interpreter with no startup (``-I -S``), its input on stdin. Elevation
    is not applied: a caller test's paths are the test's own. The suite's
    conftest sends the program with a private lock (``file_lock_path``), so
    no test takes the host's.
    """
    import subprocess
    import sys

    from agentworks.execution.files import program_source
    from agentworks.execution.requests import Bytes, PythonProgram

    request = call.request
    if not isinstance(request, PythonProgram) or request.source != program_source():
        return None
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", request.source, *request.args],
        input=request.stdin.data if isinstance(request.stdin, Bytes) else b"",
        capture_output=True,
        timeout=60,
        check=False,
    )
    return Result(Completed(completed.returncode), completed.stdout, completed.stderr)


def local_files(vm_name: str = "box") -> Files:
    """``Files`` over a recording access whose file operations run on this machine (:func:`run_file_program_locally`).

    Unlike :func:`local_vm_access`, any number may exist in one test and no
    launcher runs, which suits a caller's tests of what it publishes.
    """
    access = RecordingVMAccess([], Admin(), vm_name=vm_name)
    access.answer = run_file_program_locally
    return access.files()


def answer_destination_identity(call: Call) -> Result | None:
    """Answer setup's destination identity probes with one fixed identity; ``None`` for any other request.

    They are ``harness_setup.dispatch.destination_id``'s two ``Command``s:
    ``cat /etc/machine-id`` and ``stat`` of the destination.
    """
    from agentworks.execution.requests import Command

    request = call.request
    if isinstance(request, Command) and request.argv[:2] in (("cat", "/etc/machine-id"), ("stat", "-Lc")):
        return Result(Completed(0), b"native-identity\n")
    return None
