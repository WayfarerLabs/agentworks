"""Independent reuse proof for the stdlib-only owned-process core."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentworks.execution import _process as process_core
from agentworks.execution._process import (
    Deadline,
    ProcessFailure,
    ProcessInput,
    ProcessOutput,
    ProcessResult,
    StreamResult,
    run_owned_process,
)
from tests.execution.test_process_launch_owner import _assert_exact_cleanup, _wait_snapshot
from tests.execution.test_process_launch_owner import children as children

CORE_PATH = Path(__file__).parents[2] / "agentworks" / "execution" / "_process.py"
PYTHON_311 = Path("/usr/bin/python3.11")


@pytest.fixture(scope="module", params=[Path(sys.executable), PYTHON_311], ids=["current", "distribution-3.11"])
def interpreter(request: pytest.FixtureRequest) -> Path:
    python: Path = request.param
    if python == PYTHON_311:
        if not python.is_file():
            pytest.skip("This host has no /usr/bin/python3.11 compatibility interpreter")
        version = subprocess.run(
            [str(python), "-I", "-S", "-B", "-c", "import sys; print(*sys.version_info[:2])"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=5,
            text=True,
        )
        if version.stdout.strip() != "3 11":
            pytest.skip(f"Expected Python 3.11 at {python}, found {version.stdout.strip()}")
    return python


def test_process_result_representation_hides_nested_stream_bytes() -> None:
    secret = b"process-core-repr-canary"
    stream = StreamResult(secret, complete=True)
    result = ProcessResult(True, 0, 0, stream, stream, None)

    assert secret.decode() not in repr(stream)
    assert secret.decode() not in repr(result)


@pytest.mark.windows
def test_runner_preserves_close_interruption_while_latest_cleanup_observation_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    permit_exit = tmp_path / "permit-exit"
    cleanup = process_core._cleanup
    wait_terminal = process_core.LocalProcessOwner._wait_terminal
    owner = process_core.LocalProcessOwner()
    later_cleanup_entered = threading.Event()
    release_later_cleanup = threading.Event()
    control = KeyboardInterrupt()
    first_observation: list[process_core.LocalProcessTerminal] = []
    cleanup_attempts = 0
    signal_attempts = 0
    interrupted = False

    def denied_signal(*args: object) -> None:
        nonlocal signal_attempts
        signal_attempts += 1
        raise PermissionError("injected native kill failure")

    def hold_natural_exit_cleanup(status: process_core._ProcessStatus) -> bool:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1 and os.name == "nt":
            monkeypatch.setattr(status.process, "kill", denied_signal)
        if cleanup_attempts == 2:
            later_cleanup_entered.set()
            assert release_later_cleanup.wait(30)
        return cleanup(status)

    def interrupt_wait_once(deadline=None, *, first=True):
        nonlocal interrupted
        if not interrupted:
            snapshot = _wait_snapshot(owner, lambda snapshot: snapshot.terminal is not None)
            assert snapshot.terminal is not None and snapshot.terminal.cleanup_retryable
            first_observation.append(snapshot.terminal)
            permit_exit.touch()
            assert later_cleanup_entered.wait(30)
            assert owner.snapshot().terminal is None
            interrupted = True
            raise control
        return wait_terminal(owner, deadline, first=first)

    monkeypatch.setattr(process_core, "_cleanup", hold_natural_exit_cleanup)
    monkeypatch.setattr(owner, "_wait_terminal", interrupt_wait_once)
    if os.name == "posix":
        monkeypatch.setattr(os, "kill", denied_signal)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_owned_process(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    f"import time; from pathlib import Path; marker=Path({str(permit_exit)!r}); "
                    "\nwhile not marker.exists(): time.sleep(0.01)\nraise SystemExit(7)",
                ],
                input=ProcessInput(),
                output=ProcessOutput(capture_limit=4096),
                deadline=Deadline(time.monotonic() + 0.05),
                owner=owner,
            )
        assert caught.value is control
        assert owner.snapshot().terminal is None
        assert owner.close() is first_observation[0]
        assert not first_observation[0].cleaned
    finally:
        permit_exit.touch()
        release_later_cleanup.set()
        settled = _wait_snapshot(owner, lambda snapshot: snapshot.terminal is not None and snapshot.terminal.cleaned)
    assert settled.terminal is not first_observation[0]
    assert settled.terminal is not None and settled.terminal.local_status == 7
    assert settled.terminal.exit_status is None
    assert cleanup_attempts == 2 and signal_attempts == 1
    _assert_exact_cleanup(children)


@pytest.mark.windows
def test_process_cwd_does_not_change_parent_directory(tmp_path: Path) -> None:
    parent_cwd = Path.cwd()
    result = run_owned_process(
        [sys.executable, "-I", "-S", "-B", "-c", "import os,sys; sys.stdout.write(os.getcwd())"],
        input=ProcessInput(),
        output=ProcessOutput(capture_limit=4096),
        deadline=Deadline(time.monotonic() + 10),
        cwd=str(tmp_path),
        owner=process_core.LocalProcessOwner(),
    )

    assert result.failure is None
    assert result.exit_status == 0
    assert Path(os.fsdecode(result.stdout.data)).samefile(tmp_path)
    assert result.stdout.complete and result.stderr.complete
    assert Path.cwd() == parent_cwd


@pytest.mark.windows
def test_invalid_process_cwd_refuses_before_child_start(tmp_path: Path) -> None:
    marker = tmp_path / "child-started"
    result = run_owned_process(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        input=ProcessInput(),
        output=ProcessOutput(capture_limit=4096),
        deadline=Deadline(time.monotonic() + 10),
        cwd=str(tmp_path / "missing"),
        owner=process_core.LocalProcessOwner(),
    )

    assert not result.started
    assert result.local_status is None and result.exit_status is None
    assert result.failure == ProcessFailure.DISPATCH
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="new process sessions are a POSIX launch control")
def test_process_can_start_as_new_session_leader() -> None:
    result = run_owned_process(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            "import json,os; print(json.dumps([os.getpid(),os.getsid(0),os.getpgrp()]))",
        ],
        input=ProcessInput(),
        output=ProcessOutput(capture_limit=4096),
        deadline=Deadline(time.monotonic() + 10),
        start_new_session=True,
        owner=process_core.LocalProcessOwner(),
    )

    assert result.failure is None
    assert result.exit_status == 0
    pid, session, process_group = json.loads(result.stdout.data)
    assert pid == session == process_group


@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash"])
@pytest.mark.skipif(sys.platform != "linux" or not hasattr(os, "memfd_create"), reason="requires Linux memfd")
def test_memfd_script_keeps_binary_stdin_separate_and_parent_fd_owned(shell: str) -> None:
    if not Path(shell).is_file():
        pytest.skip(f"This host has no {shell}")

    import fcntl

    stdin = bytes(range(256)) * 1024
    stdout = b"\x00\xffstdout\r\n"
    stderr = b"\x80stderr\x00\n"
    python = (
        "import hashlib,sys; "
        "data=sys.stdin.buffer.read(); "
        f"assert len(data)=={len(stdin)}; "
        f"assert hashlib.sha256(data).hexdigest()=={hashlib.sha256(stdin).hexdigest()!r}; "
        f"sys.stdout.buffer.write({stdout!r}); "
        f"sys.stderr.buffer.write({stderr!r})"
    )
    source = (
        b"#"
        + b"x" * 1_100_000
        + b"\n"
        + f"{shlex.quote(sys.executable)} -I -S -B -c {shlex.quote(python)}\nexit 255\n".encode()
    )
    assert len(source) > 1_048_576

    fd = os.memfd_create("agentworks-helper-source")
    try:
        remaining = memoryview(source)
        while remaining:
            written = os.write(fd, remaining)
            remaining = remaining[written:]
        os.lseek(fd, 0, os.SEEK_SET)
        inheritable = os.get_inheritable(fd)
        descriptor_flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        status_flags = fcntl.fcntl(fd, fcntl.F_GETFL)

        result = run_owned_process(
            [shell, f"/proc/self/fd/{fd}"],
            input=ProcessInput(data=stdin),
            output=ProcessOutput(capture_limit=4096),
            deadline=Deadline(time.monotonic() + 20),
            pass_fds=(fd,),
            owner=process_core.LocalProcessOwner(),
        )

        assert result.failure is None
        assert result.local_status == result.exit_status == 255
        assert result.stdout.data == stdout
        assert result.stderr.data == stderr
        assert result.stdout.complete and result.stderr.complete
        os.fstat(fd)
        assert os.get_inheritable(fd) is inheritable
        assert fcntl.fcntl(fd, fcntl.F_GETFD) == descriptor_flags
        assert fcntl.fcntl(fd, fcntl.F_GETFL) == status_flags
    finally:
        os.close(fd)


@pytest.mark.windows
def test_standalone_core_reuse_roundtrips_binary_input_and_exit_status(interpreter: Path) -> None:
    bootstrap = r"""
import hashlib
import importlib.util
import json
import sys
import time

core_path = sys.argv[1]
spec = importlib.util.spec_from_file_location("_standalone_process_core", core_path)
assert spec is not None and spec.loader is not None
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)
assert "agentworks" not in sys.modules

payload = bytes(range(256)) * 257
child = (
    "import hashlib,sys; "
    "data=sys.stdin.buffer.read(); "
    "sys.stdout.buffer.write(data); "
    "sys.stderr.buffer.write(hashlib.sha256(data).digest()); "
    "sys.exit(37)"
)
result = core.run_owned_process(
    [sys.executable, "-I", "-S", "-B", "-c", child],
    input=core.ProcessInput(data=payload),
    output=core.ProcessOutput(capture_limit=len(payload)),
    deadline=core.Deadline(time.monotonic() + 10),
    cwd=None,
    pass_fds=(),
    start_new_session=False,
    owner=core.LocalProcessOwner(),
)
print(json.dumps({
    "agentworks_loaded": "agentworks" in sys.modules,
    "exit": result.exit_status,
    "failure": result.failure,
    "local": result.local_status,
    "stderr": result.stderr.data.hex(),
    "stderr_complete": result.stderr.complete,
    "stdout": result.stdout.data.hex(),
    "stdout_complete": result.stdout.complete,
}))
"""
    completed = subprocess.run(
        [str(interpreter), "-I", "-S", "-B", "-c", bootstrap, str(CORE_PATH)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=20,
    )
    observed = json.loads(completed.stdout)
    payload = bytes(range(256)) * 257

    assert completed.stderr == b""
    assert observed == {
        "agentworks_loaded": False,
        "exit": 37,
        "failure": None,
        "local": 37,
        "stderr": hashlib.sha256(payload).digest().hex(),
        "stderr_complete": True,
        "stdout": payload.hex(),
        "stdout_complete": True,
    }
