"""Bounded local process faults for the Linux no-staging bootstrap."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.execution.carrier import CapturedOutput, FiniteInput, Provenance
from agentworks.execution.models import Script, Shell
from agentworks.execution.preparation import PreparedExecution, decode_output, prepare

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Fault injection requires Linux /proc")


@contextmanager
def _running_bootstrap(
    prepared: PreparedExecution, *, restore_signals: bool
) -> Iterator[tuple[subprocess.Popen[bytes], bytes, list[int]]]:
    """Own the fixture process group and every pinned process handle."""
    assert isinstance(prepared.io.input, FiniteInput)
    with subprocess.Popen(
        prepared.invocation.argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        restore_signals=restore_signals,
    ) as process:
        handles: list[int] = []
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(prepared.io.input.data)
            process.stdin.close()
            process.stdin = None
            assert select.select([process.stdout], [], [], 2)[0]
            prefix = process.stdout.readline()
            assert prefix == prepared.token.encode() + b" B\n"
            yield process, prefix, handles
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
            for handle in handles:
                os.close(handle)


def _descendants(root_pid: int) -> list[int]:
    """Snapshot only descendants of this test's owned bootstrap."""
    children_file = Path(f"/proc/{root_pid}/task/{root_pid}/children")
    try:
        children = [int(child) for child in children_file.read_text().split()]
    except (FileNotFoundError, ProcessLookupError):
        return []
    return children + [descendant for child in children for descendant in _descendants(child)]


def _stopped(pid: int) -> bool:
    """Read the kernel's process state, not an elapsed-time assumption."""
    status = Path(f"/proc/{pid}/status").read_text().splitlines()
    return any(line.startswith("State:") and line.split()[1] == "T" for line in status)


def _matches_fixture_process(
    pid: int, root_pid: int, argv: tuple[bytes, ...], pipe: tuple[int, str] | None, stopped: bool
) -> bool:
    observed = tuple(Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1])
    return (
        os.getsid(pid) == root_pid
        and observed == argv
        and (pipe is None or os.readlink(f"/proc/{pid}/fd/{pipe[0]}") == pipe[1])
        and (not stopped or _stopped(pid))
    )


def _find_fixture_process(
    root_pid: int, argv: tuple[bytes, ...], *, pipe: tuple[int, str] | None = None, stopped: bool = False
) -> tuple[int, int] | None:
    """Select before pinning, then verify identity again with the handle held."""
    for child_pid in _descendants(root_pid):
        try:
            if not _matches_fixture_process(child_pid, root_pid, argv, pipe, stopped):
                continue
            handle = os.pidfd_open(child_pid)
        except (FileNotFoundError, ProcessLookupError):
            continue
        try:
            if _matches_fixture_process(child_pid, root_pid, argv, pipe, stopped):
                return child_pid, handle
        except (FileNotFoundError, ProcessLookupError):
            pass
        os.close(handle)
    return None


def _wait_fixture_process(
    root_pid: int,
    argv: tuple[bytes, ...],
    handles: list[int],
    *,
    pipe: tuple[int, str] | None = None,
    stopped: bool = False,
) -> tuple[int, int]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        found = _find_fixture_process(root_pid, argv, pipe=pipe, stopped=stopped)
        if found is not None:
            handles.append(found[1])
            return found
        time.sleep(0.005)
    pytest.fail("The owned fixture process did not reach its synchronization point")


def _kill_helper(pid: int, handle: int, *, pipe: tuple[int, str]) -> None:
    """Confirm a live, matching pipe participant before terminating it."""
    signal.pidfd_send_signal(handle, signal.SIGSTOP)
    deadline = time.monotonic() + 2
    while not _stopped(pid) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert _stopped(pid)
    assert os.readlink(f"/proc/{pid}/fd/{pipe[0]}") == pipe[1]
    assert not select.select([handle], [], [], 0)[0]
    signal.pidfd_send_signal(handle, signal.SIGTERM)
    signal.pidfd_send_signal(handle, signal.SIGCONT)
    assert select.select([handle], [], [], 2)[0]


@pytest.mark.parametrize("producer", ["stdin", "source"])
@pytest.mark.parametrize("restore_signals", [True, False])
def test_killed_producer_cannot_report_successful_partial_delivery(producer: str, restore_signals: bool) -> None:
    import fcntl

    source = 'kill -STOP "$$"; /bin/cat' if producer == "stdin" else 'kill -STOP "$$"\n#' + "x" * 150_000
    prepared = prepare(Script(source, Shell.SH), stdin=b"x" * 150_000 if producer == "stdin" else b"")
    with _running_bootstrap(prepared, restore_signals=restore_signals) as (process, prefix, handles):
        pipe_path = f"/proc/{process.pid}/fd/{5 if producer == 'source' else 6}"
        pipe = os.readlink(pipe_path)
        pipe_handle = os.open(pipe_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            capacity = fcntl.fcntl(pipe_handle, fcntl.F_GETPIPE_SZ)
        finally:
            os.close(pipe_handle)
        if capacity >= 150_000:
            pytest.skip(f"Producer fault fixture needs backpressure; this pipe holds {capacity} bytes")
        _, payload_handle = _wait_fixture_process(process.pid, (b"/bin/sh", b"/dev/fd/5"), handles, stopped=True)
        decoder_pid, decoder_handle = _wait_fixture_process(
            process.pid, (b"/usr/bin/base64", b"--decode"), handles, pipe=(1, pipe)
        )
        _kill_helper(decoder_pid, decoder_handle, pipe=(1, pipe))
        signal.pidfd_send_signal(payload_handle, signal.SIGCONT)
        stdout, _ = process.communicate(timeout=5)
        decoded = decode_output(prepared, CapturedOutput(prefix + stdout, True, Provenance.CARRIER_STDOUT))
        assert process.returncode == 125
        assert decoded.bootstrap_failed
        assert not decoded.stdout_complete and not decoded.stderr_complete
        if producer == "stdin":
            assert len(decoded.stdout) < 150_000


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("restore_signals", [True, False])
def test_failed_encoder_cannot_report_successful_complete_delivery(stream: str, restore_signals: bool) -> None:
    prepared = prepare(Script('kill -STOP "$$"; exit 0', Shell.SH))
    with _running_bootstrap(prepared, restore_signals=restore_signals) as (process, prefix, handles):
        _, payload_handle = _wait_fixture_process(process.pid, (b"/bin/sh", b"/dev/fd/5"), handles, stopped=True)
        pipe = os.readlink(f"/proc/{process.pid}/fd/{8 if stream == 'stdout' else 9}")
        encoder_pid, encoder_handle = _wait_fixture_process(
            process.pid, (b"/usr/bin/base64", b"--wrap=76"), handles, pipe=(0, pipe)
        )
        _kill_helper(encoder_pid, encoder_handle, pipe=(0, pipe))
        signal.pidfd_send_signal(payload_handle, signal.SIGCONT)
        stdout, _ = process.communicate(timeout=5)
        decoded = decode_output(prepared, CapturedOutput(prefix + stdout, True, Provenance.CARRIER_STDOUT))
        assert process.returncode == 125
        assert decoded.bootstrap_failed
        assert not decoded.stdout_complete and not decoded.stderr_complete


@pytest.mark.parametrize("field", ["source", "stdin"])
def test_malformed_input_fails_before_application_execution(field: str, tmp_path: Path) -> None:
    marker = tmp_path / "payload-started"
    prepared = prepare(Script('printf must-not-run; : > "$MARKER"', Shell.SH), env={"MARKER": str(marker)})
    assert isinstance(prepared.io.input, FiniteInput)
    lines = prepared.io.input.data.split(b"\n")
    index = -3 if field == "source" else -2
    lines[index] += b"!"
    corrupted = replace(prepared, io=replace(prepared.io, input=FiniteInput(b"\n".join(lines))))
    assert isinstance(corrupted.io.input, FiniteInput)
    result = subprocess.run(
        corrupted.invocation.argv, input=corrupted.io.input.data, capture_output=True, timeout=10, check=False
    )
    decoded = decode_output(corrupted, CapturedOutput(result.stdout, True, Provenance.CARRIER_STDOUT))
    assert result.returncode == 125
    assert decoded.bootstrap_failed
    assert not decoded.stdout_complete and not decoded.stderr_complete
    assert decoded.stdout == b""
    assert not marker.exists()
