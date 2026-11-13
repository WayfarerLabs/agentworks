"""The guest detached program against real processes and a temporary base.

Each operation runs the program's ``main`` in its own interpreter, under each
interpreter the L lane uses, with the base under the test's own directory, so
launches really fork, lock and outlive the operation that started them. Linux
only, as on the guest: the tests read ``/proc``. The one root test is the sudo
lane's, required where passwordless sudo is granted (CI).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.execution import detached, detached_program
from agentworks.execution.results import Completed, Result
from tests.execution.conformance.lanes import python_interpreters, sudo_granted, unavailable

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the detached program is a Linux guest program")

_MAIN = (
    "import sys; sys.path.insert(0, sys.argv[1]); import detached_program\n"
    "{patch}\n"
    "sys.exit(detached_program.main(sys.argv[4:], base=sys.argv[2], interrupt_grace=float(sys.argv[3])))"
)


@dataclass
class Observed:
    state: str
    status: int | None
    work: dict[str, int]
    tail: bytes


@dataclass
class Guest:
    """The program and a detached base under one test's directory."""

    root: Path
    python: str
    interrupt_grace: float = detached_program.INTERRUPT_GRACE_SECONDS

    @property
    def base(self) -> Path:
        return self.root / "lib" / "detached"

    def directory(self, launch_id: str) -> Path:
        return self.base / launch_id

    def start(
        self, operation: str, launch_id: str, *, stdin: bytes = b"", patch: str = "", sudo: bool = False
    ) -> subprocess.Popen[bytes]:
        argv = [
            self.python,
            "-I",
            "-S",
            "-c",
            _MAIN.format(patch=patch),
            str(self.root / "program"),
            str(self.base),
            str(self.interrupt_grace),
            operation,
            launch_id,
        ]
        process = subprocess.Popen(
            ["sudo", "-n", "--", *argv] if sudo else argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        process.stdin.write(stdin)
        process.stdin.close()
        return process

    def finish(self, process: subprocess.Popen[bytes]) -> subprocess.CompletedProcess[bytes]:
        # The launching process's streams close when it exits: the supervisor holds none of them. Both
        # are small, so reading one and then the other cannot stall the program.
        assert process.stdout is not None and process.stderr is not None
        stdout, stderr = process.stdout.read(), process.stderr.read()
        process.wait(timeout=30)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

    def run(self, operation: str, launch_id: str, **options: Any) -> subprocess.CompletedProcess[bytes]:
        return self.finish(self.start(operation, launch_id, **options))

    def launch(
        self, argv: Sequence[str], *, env: dict[str, str] | None = None, launch_id: str | None = None, **options: Any
    ) -> tuple[str, subprocess.CompletedProcess[bytes]]:
        launch_id = secrets.token_hex(16) if launch_id is None else launch_id
        spec = json.dumps({"argv": list(argv), "env": env or {}}).encode()
        return launch_id, self.run("launch", launch_id, stdin=spec, **options)

    def observe(self, launch_id: str, operation: str = "observe", **options: Any) -> Observed:
        done = self.run(operation, launch_id, **options)
        assert done.returncode == 0, done.stderr
        line, _, tail = done.stdout.partition(b"\n")
        observed = json.loads(line)
        return Observed(observed["state"], observed["status"], observed["work"], tail)

    def wait_until_ended(self, launch_id: str, seconds: float = 20) -> Observed:
        deadline = time.monotonic() + seconds
        while (observed := self.observe(launch_id)).state == detached_program.RUNNING:
            assert time.monotonic() < deadline, "the launch did not end"
            time.sleep(0.05)
        return observed

    def payload_pid(self, launch_id: str) -> int:
        return int((self.directory(launch_id) / "process").read_text())


def parent_of(pid: int) -> int:
    # The command name is in parentheses and may hold spaces; the fields after it are fixed.
    fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    return int(fields[1])


def group_members(group: int) -> list[int]:
    members = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError, IndexError):
                if int((entry / "stat").read_text().rpartition(")")[2].split()[2]) == group:
                    members.append(int(entry.name))
    return members


def kill_every_launch(base: Path) -> None:
    if not base.is_dir():
        return
    for directory in base.iterdir():
        with contextlib.suppress(OSError, ValueError):
            os.killpg(int((directory / "process").read_text()), signal.SIGKILL)


@pytest.fixture(params=sorted(python_interpreters()))
def guest(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Guest]:
    program = tmp_path / "program"
    program.mkdir()
    (program / "detached_program.py").write_text(detached.program_source())
    found = Guest(tmp_path, python_interpreters()[request.param])
    try:
        yield found
    finally:
        kill_every_launch(found.base)


def wait_for(path: Path, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} did not appear"
        time.sleep(0.02)


# -- Launch identity (I80, R6) -----------------------------------------------------------


def test_two_launches_of_one_command_each_report_only_their_own_status_and_output(guest: Guest) -> None:
    command = ("sh", "-c", 'echo "launch $$"; exit $(( $$ % 50 + 1 ))')

    first, launched_first = guest.launch(command)
    second, launched_second = guest.launch(command)

    assert (launched_first.returncode, launched_second.returncode) == (0, 0)
    assert guest.directory(first) != guest.directory(second)
    for launch_id in (first, second):
        observed = guest.wait_until_ended(launch_id)
        pid = guest.payload_pid(launch_id)
        assert observed.state == detached_program.EXITED
        assert observed.status == pid % 50 + 1
        assert observed.tail == f"launch {pid}\n".encode()


def test_an_existing_id_is_refused_and_the_earlier_launch_is_unchanged(guest: Guest) -> None:
    launch_id, _ = guest.launch(("sh", "-c", "echo first > result; echo said"))
    guest.wait_until_ended(launch_id)
    before = {path: path.read_bytes() for path in guest.directory(launch_id).rglob("*") if path.is_file()}

    _, again = guest.launch(("sh", "-c", "echo second > result"), launch_id=launch_id)

    assert again.returncode == detached_program.EXIT_EXISTS
    after = {path: path.read_bytes() for path in guest.directory(launch_id).rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("launch_id", ["A" * 32, "a" * 31, "../" + "a" * 29, "a" * 33, ""])
def test_a_malformed_id_is_refused_and_creates_nothing(guest: Guest, launch_id: str) -> None:
    for operation in ("observe", "interrupt", "discard"):
        assert guest.run(operation, launch_id).returncode == detached_program.EXIT_INVALID
    _, launched = guest.launch(("true",), launch_id=launch_id)

    assert launched.returncode == detached_program.EXIT_INVALID
    assert not guest.base.exists()


# -- How a launch runs (I8) -------------------------------------------------------------


def test_the_payload_has_a_session_of_its_own_and_output_holds_both_streams_of_it_and_its_child(
    guest: Guest,
) -> None:
    script = (
        "echo out; echo err >&2; sh -c 'echo child-out; echo child-err >&2';"
        " python3 -c 'import os; print(os.getsid(0), os.getpgid(0))'"
    )

    launch_id, launched = guest.launch(("sh", "-c", script))
    observed = guest.wait_until_ended(launch_id)

    assert launched.returncode == 0, launched.stderr
    assert (observed.state, observed.status) == (detached_program.EXITED, 0)
    payload = guest.payload_pid(launch_id)
    assert observed.tail == f"out\nerr\nchild-out\nchild-err\n{payload} {payload}\n".encode()


def test_the_environment_reaches_the_payload_and_no_file_of_the_launch(guest: Guest) -> None:
    value = "secret value with\nnewline " + secrets.token_hex(8)

    launch_id, _ = guest.launch(("sh", "-c", 'printf %s "$DETACHED_SECRET" > got'), env={"DETACHED_SECRET": value})
    guest.wait_until_ended(launch_id)

    files = [path for path in guest.directory(launch_id).rglob("*") if path.is_file()]
    holding = [path for path in files if value.encode() in path.read_bytes()]
    assert holding == [guest.directory(launch_id) / "work" / "got"]


def test_launch_returns_once_the_payload_runs_and_work_sizes_are_observed(guest: Guest) -> None:
    started = time.monotonic()
    launch_id, launched = guest.launch(("sh", "-c", "head -c 4096 /dev/zero > archive; sleep 600"))

    assert launched.returncode == 0
    assert time.monotonic() - started < 15
    deadline = time.monotonic() + 20
    while (observed := guest.observe(launch_id)).work.get("archive") != 4096:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert (observed.state, observed.status, observed.tail) == (detached_program.RUNNING, None, b"")


def test_a_command_that_cannot_be_executed_reads_exited_with_a_process(guest: Guest) -> None:
    launch_id, launched = guest.launch(("/nonexistent/command",))

    # What the client reads as its refusal code, from the program's real report.
    reported = Result(Completed(launched.returncode), stderr=launched.stderr)
    assert detached._exec_refusal(launched.returncode, reported) == "exec ENOENT"  # noqa: SLF001
    observed = guest.wait_until_ended(launch_id)
    assert (observed.state, observed.status) == (detached_program.EXITED, 127)
    assert (guest.directory(launch_id) / "process").exists()


def test_a_launching_process_that_dies_after_the_fork_leaves_the_payload_to_finish(guest: Guest) -> None:
    # The launching process exits the moment it has forked, as a dropped connection can end it.
    patch = (
        "import os\n"
        "_fork = os.fork\n"
        "def fork():\n"
        "    pid = _fork()\n"
        "    if pid:\n"
        "        os._exit(9)\n"
        "    return pid\n"
        "os.fork = fork\n"
    )

    launch_id, launched = guest.launch(("sh", "-c", "sleep 0.5; exit 7"), patch=patch)

    assert launched.returncode == 9
    observed = guest.wait_until_ended(launch_id)
    assert (observed.state, observed.status) == (detached_program.EXITED, 7)


# -- States ------------------------------------------------------------------------------


def test_a_launch_reads_starting_until_its_payload_runs(guest: Guest) -> None:
    release = guest.root / "release"
    # Holds the supervisor just before it starts the payload, so the launch is between mkdir and rename.
    patch = (
        "import os, time\n"
        "_popen = detached_program.subprocess.Popen\n"
        "def held(*args, **kwargs):\n"
        f"    while not os.path.exists({str(release)!r}):\n"
        "        time.sleep(0.02)\n"
        "    return _popen(*args, **kwargs)\n"
        "detached_program.subprocess.Popen = held\n"
    )
    launch_id = secrets.token_hex(16)
    spec = json.dumps({"argv": ["sleep", "600"], "env": {}}).encode()
    launching = guest.start("launch", launch_id, stdin=spec, patch=patch)
    try:
        wait_for(guest.directory(launch_id) / "process.new")
        assert guest.observe(launch_id).state == detached_program.STARTING
    finally:
        release.touch()
    assert guest.finish(launching).returncode == 0
    assert guest.observe(launch_id).state == detached_program.RUNNING


def test_a_killed_supervisor_leaves_its_payload_running_and_then_lost(guest: Guest) -> None:
    launch_id, _ = guest.launch(("sleep", "600"))
    payload = guest.payload_pid(launch_id)

    os.kill(parent_of(payload), signal.SIGKILL)
    time.sleep(0.2)
    assert guest.observe(launch_id).state == detached_program.RUNNING

    os.killpg(payload, signal.SIGKILL)
    observed = guest.wait_until_ended(launch_id)
    assert (observed.state, observed.status) == (detached_program.LOST, None)


def test_an_observer_paused_between_its_two_reads_never_reports_a_finished_launch_lost(guest: Guest) -> None:
    release, reached, go = guest.root / "release", guest.root / "reached", guest.root / "go"
    # Pauses the observer after whichever of its two reads comes first, while the launch finishes.
    patch = (
        "import os, time\n"
        "def pausing(read):\n"
        "    def paused(*args):\n"
        "        found = read(*args)\n"
        f"        if not os.path.exists({str(reached)!r}):\n"
        f"            open({str(reached)!r}, 'w').close()\n"
        f"            while not os.path.exists({str(go)!r}):\n"
        "                time.sleep(0.02)\n"
        "        return found\n"
        "    return paused\n"
        "detached_program._lock_held = pausing(detached_program._lock_held)\n"
        "detached_program._read_status = pausing(detached_program._read_status)\n"
    )
    launch_id, _ = guest.launch(("sh", "-c", f"while [ ! -e {release} ]; do sleep 0.02; done"))

    observer = guest.start("observe", launch_id, patch=patch)
    wait_for(reached)
    release.touch()
    assert guest.wait_until_ended(launch_id).state == detached_program.EXITED
    go.touch()
    done = guest.finish(observer)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.partition(b"\n")[0])["state"] in (detached_program.RUNNING, detached_program.EXITED)


def test_a_launch_prunes_old_launches_that_are_not_running(guest: Guest) -> None:
    old = time.time() - detached_program.PRUNE_AGE_SECONDS - 60
    exited, _ = guest.launch(("true",))
    guest.wait_until_ended(exited)
    lost, _ = guest.launch(("sleep", "600"))
    payload = guest.payload_pid(lost)
    os.kill(parent_of(payload), signal.SIGKILL)
    os.killpg(payload, signal.SIGKILL)
    assert guest.wait_until_ended(lost).state == detached_program.LOST
    running, _ = guest.launch(("sleep", "600"))
    starting = secrets.token_hex(16)
    guest.directory(starting).mkdir()
    young, _ = guest.launch(("true",))
    guest.wait_until_ended(young)
    for launch_id in (exited, lost, running, starting):
        os.utime(guest.directory(launch_id), (old, old))

    newest, _ = guest.launch(("true",))

    remaining = {path.name for path in guest.base.iterdir()}
    assert remaining == {running, young, newest}


# -- Interrupt and discard (I82) ---------------------------------------------------------


def test_interrupt_stops_the_whole_group_and_reads_exited_with_sigterms_status(guest: Guest) -> None:
    launch_id, _ = guest.launch(("sh", "-c", "sleep 600 & sleep 600"))
    group = guest.payload_pid(launch_id)
    deadline = time.monotonic() + 10
    while len(group_members(group)) < 3:  # the shell and both sleeps
        assert time.monotonic() < deadline
        time.sleep(0.02)

    observed = guest.observe(launch_id, "interrupt")

    assert (observed.state, observed.status) == (detached_program.EXITED, 128 + signal.SIGTERM)
    assert group_members(group) == []


def test_interrupt_kills_a_payload_that_ignores_sigterm_after_the_grace(guest: Guest) -> None:
    guest.interrupt_grace = 1.0
    launch_id, _ = guest.launch(("sh", "-c", "trap '' TERM; echo ready; sleep 600"))
    output, deadline = guest.directory(launch_id) / "output", time.monotonic() + 10
    while output.read_bytes() != b"ready\n":  # the trap is set
        assert time.monotonic() < deadline
        time.sleep(0.02)

    observed = guest.observe(launch_id, "interrupt")

    assert (observed.state, observed.status) == (detached_program.EXITED, 128 + signal.SIGKILL)
    assert group_members(guest.payload_pid(launch_id)) == []


def test_interrupt_of_a_launch_that_is_not_running_signals_nothing(guest: Guest) -> None:
    launch_id, _ = guest.launch(("sh", "-c", "exit 3"))
    guest.wait_until_ended(launch_id)

    observed = guest.observe(launch_id, "interrupt")

    assert (observed.state, observed.status) == (detached_program.EXITED, 3)
    assert guest.observe(secrets.token_hex(16), "interrupt").state == detached_program.ABSENT


def test_discard_refuses_a_running_launch_and_removes_an_ended_or_absent_one(guest: Guest) -> None:
    running, _ = guest.launch(("sleep", "600"))
    ended, _ = guest.launch(("true",))
    guest.wait_until_ended(ended)

    assert guest.run("discard", running).returncode == detached_program.EXIT_RUNNING
    assert guest.directory(running).is_dir()
    assert guest.run("discard", ended).returncode == 0
    assert not guest.directory(ended).exists()
    assert guest.run("discard", ended).returncode == 0
    assert guest.observe(ended).state == detached_program.ABSENT


# -- The base (I83) ----------------------------------------------------------------------


def test_root_creates_the_base_private_under_a_readable_parent(guest: Guest) -> None:
    """The sudo lane: required where passwordless sudo is granted (CI), skipped elsewhere."""
    if not sudo_granted():
        unavailable("the sudo lane needs passwordless sudo")
    try:
        launch_id, launched = guest.launch(("true",), sudo=True)
        assert launched.returncode == 0, launched.stderr
        base, parent = guest.base.stat(), guest.base.parent.stat()
        assert (base.st_uid, stat.S_IMODE(base.st_mode)) == (0, 0o700)
        assert (parent.st_uid, stat.S_IMODE(parent.st_mode)) == (0, 0o755)
        observed = guest.observe(launch_id, sudo=True)
        assert observed.state in (detached_program.RUNNING, detached_program.EXITED)
    finally:
        subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(guest.base.parent)], check=False)
