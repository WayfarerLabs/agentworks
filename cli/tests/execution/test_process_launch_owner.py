"""Behavioral boundaries for private local launch ownership."""

from __future__ import annotations

import _thread
import dis
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from agentworks.execution import _process as process_core
from agentworks.execution._process import Deadline, ProcessFailure, ProcessInput, ProcessOutput, run_owned_process

pytestmark = pytest.mark.windows


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    original = subprocess.Popen

    def spawn(argv: tuple[str, ...], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(argv, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    yield started
    for child in started:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()


def _run_sleeping_child() -> process_core.ProcessResult:
    return run_owned_process(
        [sys.executable, "-I", "-S", "-B", "-c", "import time; time.sleep(30)"],
        input=ProcessInput(),
        output=ProcessOutput(capture_limit=4096),
        deadline=Deadline(time.monotonic() + 10),
    )


def _assert_exact_cleanup(children: list[subprocess.Popen[bytes]]) -> None:
    assert len(children) == 1
    child = children[0]
    assert child.returncode is not None
    for pipe in (child.stdin, child.stdout, child.stderr):
        assert pipe is None or pipe.closed


def _request(source: str, *, input_piped: bool = False) -> process_core.LocalProcessRequest:
    return process_core.LocalProcessRequest(
        (sys.executable, "-I", "-S", "-B", "-c", source),
        input_piped,
    )


def _wait_snapshot(
    owner: process_core.LocalProcessOwner,
    predicate: Callable[[process_core.LocalProcessSnapshot], bool],
) -> process_core.LocalProcessSnapshot:
    until = time.monotonic() + 2
    while time.monotonic() < until:
        snapshot = owner.snapshot()
        if predicate(snapshot):
            return snapshot
        time.sleep(0.01)
    pytest.fail("local process owner did not publish the expected state")


def _fake_process() -> Any:
    process = type("FakeProcess", (), {})()
    process.pid = 424_242
    process.returncode = None
    process.stdin = None
    process.stdout = tempfile.TemporaryFile("w+b")  # noqa: SIM115
    process.stderr = tempfile.TemporaryFile("w+b")  # noqa: SIM115
    return process


def test_close_before_start_is_explicit_idempotent_and_does_not_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = process_core.LocalProcessOwner()

    def forbidden_start(function: Callable[..., object], args: tuple[object, ...]) -> int:
        raise AssertionError("closed owner started a native thread")

    monkeypatch.setattr(_thread, "start_new_thread", forbidden_start)

    first = owner.close()
    second = owner.close()

    assert first is second
    assert not first.admitted and not first.started
    assert first.cleaned and not first.dispatch_failed
    assert owner.snapshot().terminal is first
    with pytest.raises(RuntimeError):
        owner.start(_request("pass"))


def test_start_admits_at_most_one_request(
    children: list[subprocess.Popen[bytes]],
) -> None:
    owner = process_core.LocalProcessOwner()
    owner.start(_request("import time; time.sleep(30)"))

    with pytest.raises(RuntimeError):
        owner.start(_request("raise SystemExit(99)"))

    terminal = owner.close()
    assert terminal.admitted and terminal.started and terminal.cleaned
    assert len(children) == 1


def test_dispatch_failure_is_terminal_without_process_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = process_core.LocalProcessOwner()

    def fail_dispatch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise OSError("dispatch-canary")

    monkeypatch.setattr(subprocess, "Popen", fail_dispatch)
    owner.start(_request("pass"))
    terminal = owner.close()

    assert terminal.admitted and not terminal.started
    assert terminal.dispatch_failed and terminal.cleaned
    assert terminal.local_status is terminal.exit_status is None
    assert owner.snapshot().pipes is None


def test_unexpected_prestart_denial_keeps_snapshot_and_terminal_consistent(tmp_path: Path) -> None:
    script = r"""
import sys
from pathlib import Path

from agentworks.execution import _process as process_core

audit_events = 0


def deny_process_start(event, arguments):
    global audit_events
    if event == "subprocess.Popen":
        audit_events += 1
        raise RuntimeError("deny native process start")


sys.addaudithook(deny_process_start)
owner = process_core.LocalProcessOwner()
marker = sys.argv[1]
child = "import sys; from pathlib import Path; Path(sys.argv[1]).touch()"
request = process_core.LocalProcessRequest((sys.executable, "-c", child, marker), input_piped=False)
owner.start(request)
first = owner.close()
snapshot = owner.snapshot()
second = owner.close()

assert audit_events == 1
assert not Path(marker).exists()
assert first is second
assert first.admitted and not first.started
assert first.dispatch_failed and not first.observation_failed
assert first.cleaned
assert first.local_status is first.exit_status is None
assert snapshot.pipes is None
assert snapshot.exit_status is None
assert not snapshot.observation_failed
assert snapshot.terminal is first
"""
    marker = tmp_path / "unexpected-child"
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, os.fspath(marker)],
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_natural_exit_is_observed_while_stdin_and_pipes_remain_borrowed() -> None:
    owner = process_core.LocalProcessOwner()
    owner.start(_request("raise SystemExit(23)", input_piped=True))
    ready = _wait_snapshot(owner, lambda snapshot: snapshot.pipes is not None)
    assert ready.pipes is not None
    pipes = ready.pipes

    exited = _wait_snapshot(owner, lambda snapshot: snapshot.exit_status == 23)

    assert exited.pipes is pipes
    assert exited.terminal is None
    assert pipes.stdin is not None and not pipes.stdin.closed
    assert not pipes.stdout.closed and not pipes.stderr.closed

    terminal = owner.close()
    assert terminal.local_status == terminal.exit_status == 23
    assert pipes.stdin.closed and pipes.stdout.closed and pipes.stderr.closed


def test_stdin_eof_does_not_request_owner_cleanup() -> None:
    owner = process_core.LocalProcessOwner()
    owner.start(
        _request(
            "import sys; assert sys.stdin.buffer.read() == b''; sys.stdout.buffer.write(b'eof'); raise SystemExit(7)",
            input_piped=True,
        )
    )
    ready = _wait_snapshot(owner, lambda snapshot: snapshot.pipes is not None)
    assert ready.pipes is not None and ready.pipes.stdin is not None
    ready.pipes.stdin.close()

    exited = _wait_snapshot(owner, lambda snapshot: snapshot.exit_status == 7)
    assert exited.terminal is None
    assert exited.pipes is ready.pipes
    assert exited.pipes.stdout.read() == b"eof"

    terminal = owner.close()
    assert terminal.exit_status == 7 and terminal.cleaned


@pytest.mark.skipif(os.name != "posix", reason="exact wait ownership is POSIX-only")
def test_exact_wait_loss_never_signals_a_numeric_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _fake_process()
    signals: list[tuple[int, int]] = []
    owner = process_core.LocalProcessOwner()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(os, "waitpid", lambda pid, options: (_ for _ in ()).throw(ChildProcessError()))
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

    owner.start(_request("pass"))
    snapshot = _wait_snapshot(owner, lambda value: value.observation_failed)
    terminal = owner.close()

    assert snapshot.exit_status is None
    assert terminal.observation_failed and not terminal.cleaned
    assert terminal.local_status is terminal.exit_status is None
    assert signals == []


@pytest.mark.skipif(os.name != "posix", reason="exact wait ownership is POSIX-only")
def test_cleanup_only_status_is_not_promoted_to_natural_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _fake_process()
    owner = process_core.LocalProcessOwner()

    def cleanup_only_poll(status: process_core._ProcessStatus) -> int | None:
        if process.stdout.closed:
            status.status = 23
            return 23
        return None

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(process_core._ProcessStatus, "poll", cleanup_only_poll)

    owner.start(_request("pass"))
    _wait_snapshot(owner, lambda snapshot: snapshot.pipes is not None)
    terminal = owner.close()

    assert terminal.cleaned
    assert terminal.local_status == 23
    assert terminal.exit_status is None


def test_incomplete_reap_is_reported_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _fake_process()
    owner = process_core.LocalProcessOwner()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(process_core._ProcessStatus, "poll", lambda status: None)
    monkeypatch.setattr(process_core, "_cleanup", lambda status: False)

    owner.start(_request("pass"))
    _wait_snapshot(owner, lambda snapshot: snapshot.pipes is not None)
    terminal = owner.close()
    for pipe in (process.stdout, process.stderr):
        pipe.close()

    assert terminal.started and not terminal.cleaned
    assert terminal.local_status is terminal.exit_status is None


def test_repeated_close_interruptions_preserve_first_identity_and_cleanup_once(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = process_core.LocalProcessOwner()
    first = KeyboardInterrupt("first-control")
    second = SystemExit("second-control")
    stop_attempts = 0
    cleanup_calls = 0
    original_stop = process_core.LocalProcessOwner._stop_borrowers
    original_cleanup = process_core._cleanup

    def interrupt_stop(target: process_core.LocalProcessOwner) -> None:
        nonlocal stop_attempts
        stop_attempts += 1
        if stop_attempts == 1:
            raise first
        if stop_attempts == 2:
            raise second
        original_stop(target)

    def count_cleanup(status: process_core._ProcessStatus) -> bool:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return original_cleanup(status)

    monkeypatch.setattr(process_core.LocalProcessOwner, "_stop_borrowers", interrupt_stop)
    monkeypatch.setattr(process_core, "_cleanup", count_cleanup)
    owner.start(_request("import time; time.sleep(30)"))

    with pytest.raises(KeyboardInterrupt) as caught:
        owner.close()

    assert caught.value is first
    terminal = owner.close()
    assert terminal.cleaned
    assert cleanup_calls == 1
    _assert_exact_cleanup(children)


@pytest.mark.parametrize("native_started", [False, True], ids=["before-native-start", "after-native-start"])
def test_ambiguous_native_start_interruption_terminally_denies_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    native_started: bool,
) -> None:
    original_start = _thread.start_new_thread
    dispatches = 0
    interruption = KeyboardInterrupt("native-start-boundary")

    def forbidden_dispatch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal dispatches
        dispatches += 1
        raise AssertionError("a canceled bootstrap dispatched")

    def interrupted_start(function: Callable[..., object], args: tuple[object, ...]) -> int:
        if native_started:
            original_start(function, args)
        raise interruption

    monkeypatch.setattr(subprocess, "Popen", forbidden_dispatch)
    monkeypatch.setattr(_thread, "start_new_thread", interrupted_start)

    with pytest.raises(KeyboardInterrupt) as caught:
        _run_sleeping_child()

    assert caught.value is interruption
    until = time.monotonic() + 1
    while dispatches == 0 and native_started and time.monotonic() < until:
        time.sleep(0.01)
    assert dispatches == 0


def test_known_native_start_failure_is_closed_dispatch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_start(function: Callable[..., object], args: tuple[object, ...]) -> int:
        raise RuntimeError("private-start-canary")

    monkeypatch.setattr(_thread, "start_new_thread", fail_start)

    result = _run_sleeping_child()

    assert not result.started
    assert result.local_status is result.exit_status is None
    assert result.failure is ProcessFailure.DISPATCH
    assert "private-start-canary" not in repr(result)


def test_interruption_before_admission_keeps_started_bootstrap_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatches = 0

    def interrupt_admission(
        owner: process_core.LocalProcessOwner,
        request: process_core.LocalProcessRequest,
    ) -> bool:
        raise KeyboardInterrupt("pre-admission-boundary")

    def forbidden_dispatch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal dispatches
        dispatches += 1
        raise AssertionError("an unadmitted bootstrap dispatched")

    monkeypatch.setattr(process_core.LocalProcessOwner, "_admit", interrupt_admission)
    monkeypatch.setattr(subprocess, "Popen", forbidden_dispatch)

    with pytest.raises(KeyboardInterrupt, match="pre-admission-boundary"):
        _run_sleeping_child()

    time.sleep(0.05)
    assert dispatches == 0


def test_interruption_between_request_store_and_admission_clears_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_start = _thread.start_new_thread
    owners: list[process_core.LocalProcessOwner] = []
    dispatches = 0
    admission_line = None
    for instruction in dis.get_instructions(process_core.LocalProcessOwner._admit):
        if instruction.opname == "STORE_ATTR" and instruction.argval == "_admission":
            positions = instruction.positions
            admission_line = None if positions is None else positions.lineno
            break
    assert admission_line is not None

    def capture_owner(function: Callable[..., object], args: tuple[object, ...]) -> int:
        owner = args[0]
        assert isinstance(owner, process_core.LocalProcessOwner)
        owners.append(owner)
        return original_start(function, args)

    def forbidden_dispatch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal dispatches
        dispatches += 1
        raise AssertionError("a canceled admission dispatched")

    def interrupt_between_stores(frame: Any, event: str, argument: object) -> Callable[..., object] | None:
        if (
            frame.f_code is process_core.LocalProcessOwner._admit.__code__
            and event == "line"
            and frame.f_lineno == admission_line
        ):
            raise KeyboardInterrupt("request-publication-boundary")
        return interrupt_between_stores

    monkeypatch.setattr(_thread, "start_new_thread", capture_owner)
    monkeypatch.setattr(subprocess, "Popen", forbidden_dispatch)
    sys.settrace(interrupt_between_stores)
    try:
        with pytest.raises(KeyboardInterrupt, match="request-publication-boundary"):
            _run_sleeping_child()
    finally:
        sys.settrace(None)

    assert len(owners) == 1
    assert owners[0]._request is None
    time.sleep(0.05)
    assert dispatches == 0


def test_interruption_after_admission_remains_inside_continuous_cleanup_guard(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_start = _thread.start_new_thread
    owners: list[process_core.LocalProcessOwner] = []
    original_admit = process_core.LocalProcessOwner._admit

    def capture_owner(function: Callable[..., object], args: tuple[object, ...]) -> int:
        owner = args[0]
        assert isinstance(owner, process_core.LocalProcessOwner)
        owners.append(owner)
        return original_start(function, args)

    def interrupt_after_admission(
        owner: process_core.LocalProcessOwner,
        request: process_core.LocalProcessRequest,
    ) -> bool:
        original_admit(owner, request)
        raise KeyboardInterrupt("post-admission-guard-boundary")

    monkeypatch.setattr(_thread, "start_new_thread", capture_owner)
    monkeypatch.setattr(process_core.LocalProcessOwner, "_admit", interrupt_after_admission)
    with pytest.raises(KeyboardInterrupt, match="post-admission-guard-boundary"):
        _run_sleeping_child()

    assert len(owners) == 1
    terminal_before_fixture = owners[0].snapshot().terminal
    try:
        assert terminal_before_fixture is not None
    finally:
        owners[0].close()
    _assert_exact_cleanup(children)


@pytest.mark.skipif(os.name != "posix", reason="real SIGINT launch-boundary proof is POSIX-only")
def test_sigint_after_admission_reaps_one_exact_child_before_propagating(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_admit = process_core.LocalProcessOwner._admit

    def interrupt_after_admit(
        owner: process_core.LocalProcessOwner,
        request: process_core.LocalProcessRequest,
    ) -> bool:
        admitted = original_admit(owner, request)
        os.kill(os.getpid(), signal.SIGINT)
        return admitted

    monkeypatch.setattr(process_core.LocalProcessOwner, "_admit", interrupt_after_admit)

    with pytest.raises(KeyboardInterrupt):
        _run_sleeping_child()

    _assert_exact_cleanup(children)


@pytest.mark.skipif(os.name != "posix", reason="real SIGINT launch-boundary proof is POSIX-only")
def test_sigint_before_ready_publication_reaps_one_exact_child_before_propagating(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_publish = process_core.LocalProcessOwner._publish_ready

    def interrupt_before_publish(
        owner: process_core.LocalProcessOwner,
        pipes: process_core.LocalProcessPipes,
    ) -> None:
        os.kill(os.getpid(), signal.SIGINT)
        original_publish(owner, pipes)

    monkeypatch.setattr(process_core.LocalProcessOwner, "_publish_ready", interrupt_before_publish)

    with pytest.raises(KeyboardInterrupt):
        _run_sleeping_child()

    _assert_exact_cleanup(children)


@pytest.mark.skipif(os.name != "posix", reason="real repeated SIGINT cleanup proof is POSIX-only")
def test_repeated_sigint_during_cleanup_preserves_interruption_and_exact_reap(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_cleanup = process_core._cleanup

    class InterruptingSource:
        def try_read(self, limit: int) -> bytes | None:
            os.kill(os.getpid(), signal.SIGINT)
            return None

    def interrupt_cleanup(status: process_core._ProcessStatus) -> bool:
        for _ in range(4):
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.01)
        return original_cleanup(status)

    monkeypatch.setattr(process_core, "_cleanup", interrupt_cleanup)

    with pytest.raises(KeyboardInterrupt):
        run_owned_process(
            [sys.executable, "-I", "-S", "-B", "-c", "import time; time.sleep(30)"],
            input=ProcessInput(source=InterruptingSource()),
            output=ProcessOutput(capture_limit=4096),
            deadline=Deadline(time.monotonic() + 10),
        )

    _assert_exact_cleanup(children)


def test_raw_owner_failure_is_reported_without_unraisable_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "raw-owner-private-canary"
    unraisable: list[object] = []

    class PrivateOwnerFailure(BaseException):
        pass

    def fail_dispatch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise PrivateOwnerFailure(canary)

    monkeypatch.setattr(subprocess, "Popen", fail_dispatch)
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)

    result = _run_sleeping_child()

    assert not result.started
    assert result.failure is ProcessFailure.DISPATCH
    assert canary not in repr(result)
    assert not unraisable
