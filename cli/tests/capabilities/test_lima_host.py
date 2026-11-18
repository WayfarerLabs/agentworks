"""Where Lima's limactl runs: local commands, the placement host's argv and the VM's root channel."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.vm_platform.lima_host import LimaCommandFailed, LimaHost, _Completed
from agentworks.execution import root_script, ssh_process
from agentworks.execution.client import SSHClient
from agentworks.execution.root_script import RawRun
from tests.conftest import pretend_windows_console

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import CapturedOutput


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, Any]]]:
    """Record each spawn and answer with an empty, successful process."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class Process:
        returncode = 0

        def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"", b""

    def spawn(argv: list[str], **kwargs: Any) -> Process:
        calls.append((list(argv), kwargs))
        return Process()

    monkeypatch.setattr("agentworks.capabilities.vm_platform.lima_host.ssh_process.spawn", spawn)
    monkeypatch.setattr("agentworks.execution.resolve_ssh", lambda: SSHClient("/usr/bin/ssh", (9, 2)))
    return calls


def test_a_remote_command_runs_under_the_hosts_login_shell_with_no_terminal_input(
    spawned: list[tuple[list[str], dict[str, Any]]],
) -> None:
    LimaHost("me@gpu-box").run("limactl list --json vm1", check=False)

    ((argv, kwargs),) = spawned
    assert argv[-3:] == ["--", "me@gpu-box", "$SHELL -lc 'limactl list --json vm1'"]
    assert "-F" not in argv  # the host policy honors operator configuration
    assert kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize(
    ("host", "tail"),
    [
        (None, ["limactl", "shell", "vm1", "sudo", "-n", "/bin/bash", "-s"]),
        ("me@gpu-box", ["me@gpu-box", "$SHELL -lc 'limactl shell vm1 sudo -n /bin/bash -s'"]),
    ],
    ids=["local", "remote"],
)
def test_the_root_channel_is_limactl_shell_with_the_script_only_on_stdin(
    monkeypatch: pytest.MonkeyPatch, host: str | None, tail: list[str]
) -> None:
    calls: list[tuple[list[str], bytes]] = []
    monkeypatch.setattr("agentworks.execution.resolve_ssh", lambda: SSHClient("/usr/bin/ssh", (9, 2)))

    def run_process(argv: list[str], script: bytes, deadline: float) -> RawRun:
        calls.append((list(argv), script))
        return RawRun(b"", b"")

    monkeypatch.setattr(root_script, "run_process", run_process)

    LimaHost(host).run_as_root("vm1", b"{\nAGW_SECRET=tskey-private\n}\n", 30)

    ((argv, script),) = calls
    assert argv[-len(tail) :] == tail
    assert b"tskey-private" in script
    assert not any("tskey-private" in argument for argument in argv)


@pytest.mark.parametrize(
    ("host", "tail"),
    [
        (None, ["limactl", "shell", "vm1"]),
        ("me@gpu-box", ["--", "me@gpu-box", "$SHELL -lc 'limactl shell vm1'"]),
    ],
    ids=["local", "remote"],
)
def test_the_native_shell_is_limactl_shell_on_the_operators_terminal(
    monkeypatch: pytest.MonkeyPatch, host: str | None, tail: list[str]
) -> None:
    argvs: list[list[str]] = []
    monkeypatch.setattr("agentworks.execution.resolve_ssh", lambda: SSHClient("/usr/bin/ssh", (9, 2)))
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.lima_host.ssh_process.run_terminal",
        lambda argv: argvs.append(list(argv)) or 7,
    )

    assert LimaHost(host).shell("vm1") == 7

    (argv,) = argvs
    assert argv[-len(tail) :] == tail
    if host is not None:
        # A terminal for the hop, and no batch mode: the operator's interactive hop may prompt.
        assert "-t" in argv
        assert "BatchMode=yes" not in argv


def test_only_the_remote_native_shell_leads_its_host_command_with_a_nul_on_a_windows_console(
    monkeypatch: pytest.MonkeyPatch, spawned: list[tuple[list[str], dict[str, Any]]]
) -> None:
    pretend_windows_console(monkeypatch)
    argvs: list[list[str]] = []
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.lima_host.ssh_process.run_terminal",
        lambda argv: argvs.append(list(argv)) or 0,
    )

    LimaHost("me@gpu-box").shell("vm1")
    LimaHost(None).shell("vm1")
    LimaHost("me@gpu-box").run("limactl list", check=False)

    remote, local = argvs
    assert remote[-1] == ssh_process.nul_first_on_a_windows_console("$SHELL -lc 'limactl shell vm1'")
    assert remote[-1].startswith("printf ")
    assert local == ["limactl", "shell", "vm1"]  # no ssh client, so no console write to guard
    ((piped, _),) = spawned
    assert piped[-1] == "$SHELL -lc 'limactl list'"  # a piped connection has no raw-mode console write


@pytest.mark.skipif(shutil.which("sleep") is None, reason="needs sleep")
def test_a_command_that_outlives_its_timeout_is_a_typed_failure() -> None:
    with pytest.raises(LimaCommandFailed):
        LimaHost(None).run("sleep 5", timeout=0.2)


@pytest.mark.skipif(shutil.which("cat") is None, reason="needs cat")
def test_a_command_given_input_exposes_no_output() -> None:
    assert LimaHost(None).run("cat", input="tskey-reflected") == ""
    with pytest.raises(LimaCommandFailed) as caught:
        LimaHost(None).run("sh -c 'cat; exit 3'", input="tskey-reflected")
    assert "tskey-reflected" not in str(caught.value)


_RUN_DIRECTORY = "/var/tmp/agw-x.AbCdEf1234"
_MKTEMP = "umask 077 && mktemp -d /var/tmp/agw-x.XXXXXXXXXX"


def test_a_detached_command_stages_its_files_in_a_private_directory_it_removes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Predictable names in a world-writable /var/tmp let another account plant or read a run's files."""
    commands: list[str] = []

    def execute(host: LimaHost, command: str, *, input: str | None, timeout: float | None) -> _Completed:
        commands.append(command)
        if command == _MKTEMP:
            return _Completed(0, _RUN_DIRECTORY + "\n", "")
        if command.startswith("cat") and ".status" in command:
            return _Completed(0, "0\n", "")
        return _Completed(0, "", "")

    monkeypatch.setattr(LimaHost, "_execute", execute)
    monkeypatch.setattr("agentworks.capabilities.vm_platform.lima_host.time.sleep", lambda seconds: None)

    result = LimaHost("me@gpu-box").run_detached("limactl start vm1", prefix="/var/tmp/agw-x", timeout=600)

    assert result.exit_code == 0
    assert commands[0] == _MKTEMP
    # Every file the run touches is inside the directory, which is removed at the end.
    touched = [command for command in commands[1:-1] if "/var/tmp/" in command]
    assert touched and all(f"{_RUN_DIRECTORY}/run." in command for command in touched)
    assert commands[-1] == f"rm -rf -- {_RUN_DIRECTORY}"


def test_a_detached_command_refuses_a_directory_the_host_did_not_make_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    def execute(host: LimaHost, command: str, *, input: str | None, timeout: float | None) -> _Completed:
        commands.append(command)
        return _Completed(0, "/var/tmp/agw-x\n", "")

    monkeypatch.setattr(LimaHost, "_execute", execute)

    with pytest.raises(LimaCommandFailed):
        LimaHost("me@gpu-box").run_detached("limactl start vm1", prefix="/var/tmp/agw-x", timeout=600)
    assert commands == [_MKTEMP]


def _run_here(host: LimaHost, command: str, *, input: str | None, timeout: float | None) -> _Completed:
    """Run a placement host command in this machine's shell instead."""
    completed = subprocess.run(["/bin/bash", "-c", command], input=input, capture_output=True, text=True, check=False)
    return _Completed(completed.returncode, completed.stdout, completed.stderr)


@pytest.mark.skipif(sys.platform == "win32", reason="runs the listing in a POSIX shell")
def test_a_kill_reaches_only_directories_with_a_runs_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing keeps directories this user owns with a run's name; nothing else beside them is touched."""
    run = tmp_path / "agw-x.AbCdEf1234"
    (run / "run.out").parent.mkdir()
    (run / "run.out").write_text("")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "agw-x.Link567890").symlink_to(elsewhere, target_is_directory=True)
    (tmp_path / "agw-x.File123456").write_text("")
    (tmp_path / "agw-y.AbCdEf1234").mkdir()
    (tmp_path / "agw-x.short").mkdir()
    bystanders = sorted(path.name for path in tmp_path.iterdir() if path != run)
    monkeypatch.setattr(LimaHost, "_execute", _run_here)

    LimaHost("me@gpu-box").kill_detached(str(tmp_path / "agw-x"))

    assert not run.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == bystanders
    assert (tmp_path / "agw-x.Link567890").is_symlink() and elsewhere.is_dir()


def test_a_kill_that_cannot_list_the_runs_warns_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A run the listing missed keeps going alongside the caller's ``limactl delete``, so the operator hears of it."""
    commands: list[str] = []

    def execute(host: LimaHost, command: str, *, input: str | None, timeout: float | None) -> _Completed:
        commands.append(command)
        return _Completed(1, "", "find: '/var/tmp': Permission denied\n")

    monkeypatch.setattr(LimaHost, "_execute", execute)

    LimaHost("me@gpu-box").kill_detached("/var/tmp/agw-x")

    assert len(commands) == 1
    assert len(captured_output.warnings) == 1


def test_a_placement_host_that_drops_for_a_while_does_not_end_the_detached_command(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """ssh's 255 is an unanswered check, asked again, not a finished or vanished command."""
    dropped = _Completed(255, "", "ssh: connect to host gpu-box port 22: Connection refused")
    # A login banner on stdout does not make a dropped connection an answer.
    dropped_after_banner = _Completed(255, "Welcome to gpu-box\n", "Connection closed by gpu-box")
    answers = {
        f"test -f {_RUN_DIRECTORY}/run.status": [dropped, dropped, _Completed(1, "", ""), _Completed(0, "", "")],
        "ps -p": [dropped, _Completed(0, "", "")],
        f"cat {_RUN_DIRECTORY}/run.out 2>/dev/null": [
            dropped_after_banner,
            dropped,
            _Completed(0, "limactl output\n", ""),
        ],
        f"cat {_RUN_DIRECTORY}/run.status 2>/dev/null": [dropped, _Completed(0, "0\n", "")],
    }
    launched = False

    def execute(host: LimaHost, command: str, *, input: str | None, timeout: float | None) -> _Completed:
        nonlocal launched
        if command == _MKTEMP:
            return _Completed(0, _RUN_DIRECTORY + "\n", "")
        launched = launched or "nohup " in command
        for prefix, queue in answers.items():
            if launched and command.startswith(prefix):
                return queue.pop(0) if len(queue) > 1 else queue[0]
        if command.startswith("test -f"):
            return _Completed(1, "", "")
        return _Completed(0, "", "")

    monkeypatch.setattr(LimaHost, "_execute", execute)
    monkeypatch.setattr("agentworks.capabilities.vm_platform.lima_host.time.sleep", lambda seconds: None)

    result = LimaHost("me@gpu-box").run_detached("limactl start vm1", prefix="/var/tmp/agw-x", timeout=600)

    assert (result.exit_code, result.output) == (0, "limactl output\n")
    assert all(len(queue) == 1 for queue in answers.values())  # every dropped answer was asked again
    assert len(captured_output.warnings) == 1  # the poll's first unanswered check, once


class _Clock:
    """A fake clock that moves only when the code under test sleeps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 0.0
        monkeypatch.setattr("agentworks.capabilities.vm_platform.lima_host.time.monotonic", lambda: self.now)
        monkeypatch.setattr("agentworks.capabilities.vm_platform.lima_host.time.sleep", self.sleep)

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize("down_from", ["launch", "poll"])
def test_a_placement_host_that_stops_answering_is_an_unknown_outcome_not_an_exit_status(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput, down_from: str
) -> None:
    """A create whose host went down polled to its timeout, then blamed 'exit 1', a status nothing had read."""
    _Clock(monkeypatch)
    commands: list[str] = []
    down = False

    def execute(host: LimaHost, command: str, *, input: str | None, timeout: float | None) -> _Completed:
        nonlocal down
        commands.append(command)
        if command == _MKTEMP:
            return _Completed(0, _RUN_DIRECTORY + "\n", "")
        down = down or ("nohup " in command if down_from == "launch" else command.startswith("wc -c"))
        if down:
            return _Completed(255, "", "ssh: connect to host gpu-box port 22: Operation timed out")
        return _Completed(1 if command.startswith("test -f") else 0, "", "")

    monkeypatch.setattr(LimaHost, "_execute", execute)

    with pytest.raises(LimaCommandFailed) as caught:
        LimaHost("me@gpu-box").run_detached("limactl start vm1", prefix="/var/tmp/agw-x", timeout=600)

    assert "gpu-box" in str(caught.value)
    # Down at the launch, nothing is polled; down mid-poll, the poll still waits out its bound.
    assert any(command.startswith("wc -c") for command in commands) is (down_from == "poll")


def test_a_kill_reaches_only_the_wrapper_its_pid_file_names(tmp_path: Path) -> None:
    """A delete can run days after an abandoned create, when the PID file's number belongs to another process."""
    if shutil.which("ps") is None or shutil.which("bash") is None:
        pytest.skip("needs ps and bash")
    from agentworks.capabilities.vm_platform.lima_host import _DetachedFiles, _kill_wrapper

    files = _DetachedFiles(str(tmp_path / "agw-x"))
    bystander = subprocess.Popen(["sleep", "30"])
    try:
        (tmp_path / "agw-x.pid").write_text(f"{bystander.pid}\n")
        subprocess.run(["bash", "-c", _kill_wrapper(files)], check=False)
        assert bystander.poll() is None  # not the wrapper: left alone
    finally:
        bystander.kill()
        bystander.wait()


@pytest.mark.parametrize("names_the_wrapper", ["as_an_argument", "as_a_longer_script_path"])
def test_a_kill_leaves_a_process_that_only_mentions_the_wrapper_alone(tmp_path: Path, names_the_wrapper: str) -> None:
    """A reused PID whose command line mentions the wrapper's path (a ``tail`` of it, say) is not the wrapper."""
    if shutil.which("ps") is None or not Path("/bin/bash").exists():
        pytest.skip("needs ps and /bin/bash")
    from agentworks.capabilities.vm_platform.lima_host import _DetachedFiles, _kill_wrapper

    files = _DetachedFiles(str(tmp_path / "agw-x"))
    if names_the_wrapper == "as_an_argument":
        argv = [sys.executable, "-c", "import time; time.sleep(30)", files.wrapper]
    else:
        lookalike = Path(f"{files.wrapper}.bak")
        lookalike.write_text("sleep 30\n")
        argv = ["/bin/bash", str(lookalike)]
    # Its own group's leader, as the wrapper would be, so only the command line can tell them apart.
    bystander = subprocess.Popen(argv, start_new_session=True)
    try:
        Path(files.pid).write_text(f"{bystander.pid}\n")
        subprocess.run(["bash", "-c", _kill_wrapper(files)], check=False)
        assert not _eventually(lambda: bystander.poll() is not None, within=0.5)
    finally:
        os.killpg(bystander.pid, signal.SIGKILL)
        bystander.wait()


def test_a_kill_stops_the_command_the_wrapper_started(tmp_path: Path) -> None:
    """Killing only the bash wrapper left the limactl it started running on the placement host."""
    if shutil.which("ps") is None or not Path("/bin/bash").exists():
        pytest.skip("needs ps and /bin/bash")
    from agentworks.capabilities.vm_platform.lima_host import (
        _WRAPPER_TEMPLATE,
        _DetachedFiles,
        _kill_wrapper,
        _launch_wrapper,
    )

    files = _DetachedFiles(str(tmp_path / "agw-x"))
    child_pid_file = tmp_path / "child.pid"
    # The command records its own child's PID, standing in for the limactl the wrapper runs.
    command = f"sleep 300 & echo $! > {child_pid_file}; wait"
    (tmp_path / "agw-x.sh").write_text(_WRAPPER_TEMPLATE.format(command=command, **files.template_fields()))
    subprocess.run(["bash", "-c", _launch_wrapper(files)], check=True)
    pids = [_wait_for_pid(Path(files.pid)), _wait_for_pid(child_pid_file)]
    try:
        assert all(_alive(pid) for pid in pids)

        subprocess.run(["bash", "-c", _kill_wrapper(files)], check=False)

        assert _eventually(lambda: not any(_alive(pid) for pid in pids))
    finally:
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_a_kill_reaches_a_wrapper_that_leads_no_group(tmp_path: Path) -> None:
    """A host bash that gives the job no group of its own still has its wrapper killed, and only it."""
    if shutil.which("ps") is None or not Path("/bin/bash").exists():
        pytest.skip("needs ps and /bin/bash")
    from agentworks.capabilities.vm_platform.lima_host import _WRAPPER_TEMPLATE, _DetachedFiles, _kill_wrapper

    files = _DetachedFiles(str(tmp_path / "agw-x"))
    child_pid_file = tmp_path / "child.pid"
    command = f"sleep 300 & echo $! > {child_pid_file}; wait"
    Path(files.wrapper).write_text(_WRAPPER_TEMPLATE.format(command=command, **files.template_fields()))
    # Started in this test's own process group, the group a group kill must never reach.
    wrapper = subprocess.Popen(["/bin/bash", files.wrapper])
    child_pid = _wait_for_pid(child_pid_file)
    try:
        assert _wait_for_pid(Path(files.pid)) == wrapper.pid

        subprocess.run(["bash", "-c", _kill_wrapper(files)], check=False)

        assert wrapper.wait(timeout=10) == -signal.SIGTERM
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(wrapper.pid, signal.SIGKILL)
        wrapper.wait()
        with contextlib.suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGKILL)


def _wait_for_pid(path: Path) -> int:
    assert _eventually(lambda: path.exists() and path.read_text().strip().isdigit())
    return int(path.read_text())


def _alive(pid: int) -> bool:
    """Whether ``pid`` is a running process: a zombie awaiting its reaper counts as gone."""
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _eventually(condition: Callable[[], bool], *, within: float = 10) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False
