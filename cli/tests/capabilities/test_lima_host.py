"""Where Lima's limactl runs: local commands, the placement host's argv and the VM's root channel."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.vm_platform.lima_host import LimaCommandFailed, LimaHost, _Completed
from agentworks.execution import root_script
from agentworks.execution.client import SSHClient
from agentworks.execution.root_script import RawRun

if TYPE_CHECKING:
    from pathlib import Path

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


def test_a_detached_command_clears_a_stale_result_and_runs_fresh(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A leftover status file is an interrupted earlier run's: consuming it would report a phantom result."""
    commands: list[str] = []
    launched = False

    def execute(host: LimaHost, command: str, *, input: str | None, timeout: float | None) -> _Completed:
        nonlocal launched
        commands.append(command)
        launched = launched or command.startswith("nohup ")
        if command.startswith("test -f") and command.endswith(".status"):
            return _Completed(0, "", "")  # stale before the launch, this run's after it
        if command.startswith("cat") and ".status" in command:
            return _Completed(0, "0\n" if launched else "7\n", "")
        return _Completed(0, "", "")

    monkeypatch.setattr(LimaHost, "_execute", execute)
    monkeypatch.setattr("agentworks.capabilities.vm_platform.lima_host.time.sleep", lambda seconds: None)

    result = LimaHost("me@gpu-box").run_detached("limactl start vm1", base_path="/var/tmp/agw-x", timeout=600)

    assert result.exit_code == 0
    assert sum(command.startswith("nohup ") for command in commands) == 1
    assert len(captured_output.warnings) == 1


def test_a_placement_host_that_drops_for_a_while_does_not_end_the_detached_command(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """ssh's 255 is an unanswered check, asked again, not a finished or vanished command."""
    dropped = _Completed(255, "", "ssh: connect to host gpu-box port 22: Connection refused")
    # A login banner on stdout does not make a dropped connection an answer.
    dropped_after_banner = _Completed(255, "Welcome to gpu-box\n", "Connection closed by gpu-box")
    answers = {
        "test -f /var/tmp/agw-x.status": [dropped, dropped, _Completed(1, "", ""), _Completed(0, "", "")],
        "ps -p": [dropped, _Completed(0, "", "")],
        "cat /var/tmp/agw-x.out 2>/dev/null": [dropped_after_banner, dropped, _Completed(0, "limactl output\n", "")],
        "cat /var/tmp/agw-x.status 2>/dev/null": [dropped, _Completed(0, "0\n", "")],
    }
    launched = False

    def execute(host: LimaHost, command: str, *, input: str | None, timeout: float | None) -> _Completed:
        nonlocal launched
        launched = launched or command.startswith("nohup ")
        for prefix, queue in answers.items():
            if launched and command.startswith(prefix):
                return queue.pop(0) if len(queue) > 1 else queue[0]
        if command.startswith("test -f"):
            return _Completed(1, "", "")
        return _Completed(0, "", "")

    monkeypatch.setattr(LimaHost, "_execute", execute)
    monkeypatch.setattr("agentworks.capabilities.vm_platform.lima_host.time.sleep", lambda seconds: None)

    result = LimaHost("me@gpu-box").run_detached("limactl start vm1", base_path="/var/tmp/agw-x", timeout=600)

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
        down = down or (command.startswith("nohup ") if down_from == "launch" else command.startswith("wc -c"))
        if down:
            return _Completed(255, "", "ssh: connect to host gpu-box port 22: Operation timed out")
        return _Completed(1 if command.startswith("test -f") else 0, "", "")

    monkeypatch.setattr(LimaHost, "_execute", execute)

    with pytest.raises(LimaCommandFailed) as caught:
        LimaHost("me@gpu-box").run_detached("limactl start vm1", base_path="/var/tmp/agw-x", timeout=600)

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
    wrapper: subprocess.Popen[bytes] | None = None
    try:
        (tmp_path / "agw-x.pid").write_text(f"{bystander.pid}\n")
        subprocess.run(["bash", "-c", _kill_wrapper(files)], check=False)
        assert bystander.poll() is None  # not the wrapper: left alone

        (tmp_path / "agw-x.sh").write_text("sleep 30\n")
        wrapper = subprocess.Popen(["/bin/bash", files.wrapper])
        (tmp_path / "agw-x.pid").write_text(f"{wrapper.pid}\n")
        subprocess.run(["bash", "-c", _kill_wrapper(files)], check=False)
        assert wrapper.wait(timeout=10) != 0  # the wrapper: killed
    finally:
        for process in (bystander, wrapper):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
