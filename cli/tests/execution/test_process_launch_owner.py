"""Behavioral boundaries for private local launch ownership."""

from __future__ import annotations

import _thread
import dis
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
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


@pytest.mark.parametrize("native_started", [False, True], ids=["before-native-start", "after-native-start"])
def test_ambiguous_native_start_interruption_terminally_denies_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    native_started: bool,
) -> None:
    original_start = _thread.start_new_thread
    dispatches = 0

    def forbidden_dispatch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal dispatches
        dispatches += 1
        raise AssertionError("a canceled bootstrap dispatched")

    def interrupted_start(function: Callable[..., object], args: tuple[object, ...]) -> int:
        if native_started:
            original_start(function, args)
        raise KeyboardInterrupt("native-start-boundary")

    monkeypatch.setattr(subprocess, "Popen", forbidden_dispatch)
    monkeypatch.setattr(_thread, "start_new_thread", interrupted_start)

    with pytest.raises(KeyboardInterrupt, match="native-start-boundary"):
        _run_sleeping_child()

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
        owner: process_core._LaunchOwner,
        request: process_core._LaunchRequest,
    ) -> bool:
        raise KeyboardInterrupt("pre-admission-boundary")

    def forbidden_dispatch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal dispatches
        dispatches += 1
        raise AssertionError("an unadmitted bootstrap dispatched")

    monkeypatch.setattr(process_core._LaunchOwner, "admit", interrupt_admission)
    monkeypatch.setattr(subprocess, "Popen", forbidden_dispatch)

    with pytest.raises(KeyboardInterrupt, match="pre-admission-boundary"):
        _run_sleeping_child()

    time.sleep(0.05)
    assert dispatches == 0


def test_interruption_between_request_store_and_admission_clears_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_start = _thread.start_new_thread
    owners: list[process_core._LaunchOwner] = []
    dispatches = 0
    admission_line = None
    for instruction in dis.get_instructions(process_core._LaunchOwner.admit):
        if instruction.opname == "STORE_ATTR" and instruction.argval == "_admission":
            positions = instruction.positions
            admission_line = None if positions is None else positions.lineno
            break
    assert admission_line is not None

    def capture_owner(function: Callable[..., object], args: tuple[object, ...]) -> int:
        owner = args[0]
        assert isinstance(owner, process_core._LaunchOwner)
        owners.append(owner)
        return original_start(function, args)

    def forbidden_dispatch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal dispatches
        dispatches += 1
        raise AssertionError("a canceled admission dispatched")

    def interrupt_between_stores(frame: Any, event: str, argument: object) -> Callable[..., object] | None:
        if (
            frame.f_code is process_core._LaunchOwner.admit.__code__
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
    owners: list[process_core._LaunchOwner] = []
    request_release_line = None
    for instruction in dis.get_instructions(run_owned_process):
        if instruction.opname == "DELETE_FAST" and instruction.argval == "request":
            positions = instruction.positions
            request_release_line = None if positions is None else positions.lineno
            break
    assert request_release_line is not None

    def capture_owner(function: Callable[..., object], args: tuple[object, ...]) -> int:
        owner = args[0]
        assert isinstance(owner, process_core._LaunchOwner)
        owners.append(owner)
        return original_start(function, args)

    def interrupt_after_admission(frame: Any, event: str, argument: object) -> Callable[..., object] | None:
        if frame.f_code is run_owned_process.__code__ and event == "line" and frame.f_lineno == request_release_line:
            raise KeyboardInterrupt("post-admission-guard-boundary")
        return interrupt_after_admission

    monkeypatch.setattr(_thread, "start_new_thread", capture_owner)
    sys.settrace(interrupt_after_admission)
    try:
        with pytest.raises(KeyboardInterrupt, match="post-admission-guard-boundary"):
            _run_sleeping_child()
    finally:
        sys.settrace(None)

    assert len(owners) == 1
    terminal_before_fixture = owners[0].snapshot().terminal
    try:
        assert terminal_before_fixture is not None
    finally:
        owners[0].stop_pump()
        owners[0].wait_terminal()
    _assert_exact_cleanup(children)


@pytest.mark.skipif(os.name != "posix", reason="real SIGINT launch-boundary proof is POSIX-only")
def test_sigint_after_admission_reaps_one_exact_child_before_propagating(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_admit = process_core._LaunchOwner.admit

    def interrupt_after_admit(
        owner: process_core._LaunchOwner,
        request: process_core._LaunchRequest,
    ) -> bool:
        admitted = original_admit(owner, request)
        os.kill(os.getpid(), signal.SIGINT)
        return admitted

    monkeypatch.setattr(process_core._LaunchOwner, "admit", interrupt_after_admit)

    with pytest.raises(KeyboardInterrupt):
        _run_sleeping_child()

    _assert_exact_cleanup(children)


@pytest.mark.skipif(os.name != "posix", reason="real SIGINT launch-boundary proof is POSIX-only")
def test_sigint_before_ready_publication_reaps_one_exact_child_before_propagating(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_publish = process_core._LaunchOwner.publish_ready

    def interrupt_before_publish(
        owner: process_core._LaunchOwner,
        pipes: process_core._ProcessPipes,
    ) -> None:
        os.kill(os.getpid(), signal.SIGINT)
        original_publish(owner, pipes)

    monkeypatch.setattr(process_core._LaunchOwner, "publish_ready", interrupt_before_publish)

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
