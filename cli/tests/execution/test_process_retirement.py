"""Original close evidence, exact wait loss and retained local retirement."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agentworks.execution import _process as core
from tests.execution.test_process_core import interpreter as interpreter
from tests.execution.test_process_launch_owner import _request, _wait_snapshot

CORE_PATH = Path(__file__).parents[2] / "agentworks" / "execution" / "_process.py"


@pytest.mark.skipif(os.name != "posix", reason="exact native wait loss is POSIX-only")
@pytest.mark.parametrize("ignored_sigchld", [False, True])
def test_real_exact_loss_retires_pipes_without_later_pid_actions(ignored_sigchld: bool, interpreter: Path) -> None:
    script = r"""
import gc, importlib.util, os, signal, subprocess, sys, threading, time, weakref
spec = importlib.util.spec_from_file_location('retirement_core', sys.argv[1])
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)
if sys.argv[2] == 'True': signal.signal(signal.SIGCHLD, signal.SIG_IGN)
native_spawn, native_wait = subprocess.Popen, os.waitpid
children, lost, waits = [], False, 0
def spawn(*args, **kwargs):
    child = native_spawn(*args, **kwargs)
    children.append(child)
    if sys.argv[2] == 'True':
        assert child.stdout.read() == b'finished'
        time.sleep(0.05)
    else:
        native_wait(child.pid, 0)
    return child
def wait(pid, flags):
    global lost, waits
    assert not lost, 'wait after exact ownership loss'
    waits += 1
    try: return native_wait(pid, flags)
    except ChildProcessError:
        lost = True
        raise
def kill(*args): raise AssertionError('signal after exact ownership loss')
subprocess.Popen, os.waitpid, os.kill = spawn, wait, kill
finished, entry = threading.Event(), core._local_process_owner_entry
def complete_entry(target):
    try: entry(target)
    finally: finished.set()
core._local_process_owner_entry = complete_entry
owner = core.LocalProcessOwner()
result = core.run_owned_process(
    [sys.executable, '-I', '-S', '-B', '-c', "import os; os.write(1,b'finished')"],
    input=core.ProcessInput(data=b''), output=core.ProcessOutput(capture_limit=100),
    deadline=core.Deadline(time.monotonic()+3), owner=owner)
assert lost and waits == 1
assert result.started and result.failure is core.ProcessFailure.OBSERVATION
assert result.local_status is result.exit_status is None
terminal = owner.close()
assert finished.wait(2)
assert terminal.cleaned and terminal.observation_failed and not terminal.cleanup_retryable
assert terminal.local_status is terminal.exit_status is None
assert owner.notify_resize(core.Deadline(time.monotonic()+1)) is core.ResizeNotification.NOT_SENT
assert owner.close_bounded(core.Deadline(time.monotonic()+1)) is terminal
assert children[0].returncode is None
assert all(pipe.closed for pipe in (children[0].stdin,children[0].stdout,children[0].stderr))
assert owner._retained_status is owner._retained_process is owner._retained_pipes is None
# Exercise the actual Popen destructor bookkeeping while PID actions are forbidden.
child = children.pop()
reference = weakref.ref(child)
retired_pid = child.pid
assert child not in subprocess._active
def forbidden_internal_poll(*args, **kwargs):
    raise AssertionError('Popen destructor probed a lost numeric PID')
child._internal_poll = forbidden_internal_poll
child.__del__()
assert waits == 1
del child
gc.collect()
assert reference() is None
assert all(item.pid != retired_pid for item in subprocess._active)
print('exact-loss-retired')
"""
    completed = subprocess.run(
        [str(interpreter), "-I", "-S", "-B", "-c", script, str(CORE_PATH), str(ignored_sigchld)],
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stdout == b"exact-loss-retired\n" and completed.stderr == b""


@pytest.mark.skipif(os.name != "posix", reason="native descriptor reuse and exact wait are POSIX-only")
@pytest.mark.parametrize("state", ["known", "lost", "running"])
@pytest.mark.parametrize("after_close", [False, True])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_permanent_close_failure_retains_original_objects_without_retry(
    monkeypatch: pytest.MonkeyPatch, state: str, after_close: bool, error_type: type[BaseException]
) -> None:
    spawn = subprocess.Popen
    native_wait = os.waitpid
    children: list[subprocess.Popen[bytes]] = []
    lost = False
    post_loss_actions: list[str] = []
    native_kill = os.kill

    def create(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = spawn(*args, **kwargs)
        children.append(child)
        if state == "lost":
            native_wait(child.pid, 0)
        return child

    def wait(pid: int, flags: int) -> tuple[int, int]:
        nonlocal lost
        if lost:
            post_loss_actions.append("wait")
            raise AssertionError("wait after loss")
        try:
            return native_wait(pid, flags)
        except ChildProcessError:
            lost = True
            raise

    def kill(pid: int, sig: int) -> None:
        if lost:
            post_loss_actions.append("kill")
            raise AssertionError("kill after loss")
        native_kill(pid, sig)

    monkeypatch.setattr(subprocess, "Popen", create)
    monkeypatch.setattr(os, "waitpid", wait)
    monkeypatch.setattr(os, "kill", kill)
    finished = threading.Event()
    entry = core._local_process_owner_entry

    def complete_entry(target: core.LocalProcessOwner) -> None:
        try:
            entry(target)
        finally:
            finished.set()

    monkeypatch.setattr(core, "_local_process_owner_entry", complete_entry)
    owner = core.LocalProcessOwner()
    owner.start(_request("import time; time.sleep(30)" if state == "running" else "raise SystemExit(7)"))
    ready = _wait_snapshot(owner, lambda snapshot: snapshot.pipes is not None)
    assert ready.pipes is not None
    pipes = ready.pipes
    if state == "known":
        _wait_snapshot(owner, lambda snapshot: snapshot.exit_status == 7)
    elif state == "lost":
        _wait_snapshot(owner, lambda snapshot: snapshot.observation_failed)
    original_records = tuple(pipes._records)
    pipe = pipes.stdout
    native_close = pipe.close
    descriptor = pipe.fileno()
    replacement: int | None = None
    attempts = 0

    def fail_close() -> None:
        nonlocal replacement, attempts
        attempts += 1
        if after_close:
            native_close()
            replacement = os.open(os.devnull, os.O_RDONLY)
            if replacement != descriptor:
                os.dup2(replacement, descriptor)
                os.close(replacement)
                replacement = descriptor
        raise error_type()

    monkeypatch.setattr(pipe, "close", fail_close)
    try:
        first = owner.close()
        assert finished.wait(2)
        assert not first.cleaned and not first.cleanup_retryable
        assert pipes.stderr.closed
        assert tuple(pipes._records) == original_records
        assert attempts == 1
        record = next(record for record in pipes._records if record.pipe is pipe)
        assert record.attempted and not record.confirmed
        assert owner._retained_pipes is pipes
        assert owner._retained_process is children[0]
        assert owner._retained_status is not None and owner._retained_status.pipes is pipes
        assert owner._retained_status.process is children[0]
        assert owner.close_bounded(core.Deadline(time.monotonic() + 1)) is first
        assert owner.close() is first and attempts == 1
        if state == "lost":
            assert first.observation_failed and first.local_status is first.exit_status is None
            assert children[0].returncode is None
        elif state == "known":
            assert first.local_status == first.exit_status == 7
        else:
            assert first.local_status == -9 and first.exit_status is None
        assert post_loss_actions == []
        if replacement is not None:
            os.fstat(replacement)
        else:
            assert not pipe.closed
            os.fstat(descriptor)
    finally:
        # Raw fixture aliases release test resources only after custody assertions.
        native_close()
        if replacement is not None:
            os.close(replacement)


@pytest.mark.skipif(os.name != "posix", reason="native kill retry uses POSIX ownership")
def test_uncertain_pipe_does_not_prevent_retry_of_separately_owned_process(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = core.LocalProcessOwner()
    owner.start(_request("import time; time.sleep(30)"))
    ready = _wait_snapshot(owner, lambda snapshot: snapshot.pipes is not None)
    assert ready.pipes is not None
    pipes = ready.pipes
    close = pipes.stdout.close
    kill = os.kill
    closes = 0
    signals = 0

    def fail_close() -> None:
        nonlocal closes
        closes += 1
        raise OSError()

    def fail_first_signal(pid: int, sig: int) -> None:
        nonlocal signals
        signals += 1
        if signals == 1:
            raise PermissionError()
        kill(pid, sig)

    monkeypatch.setattr(pipes.stdout, "close", fail_close)
    monkeypatch.setattr(os, "kill", fail_first_signal)
    try:
        first = owner.close()
        assert not first.cleaned and first.cleanup_retryable
        assert first.local_status is first.exit_status is None
        second = owner.close_bounded(core.Deadline(time.monotonic() + 2))
        assert second is not None and not second.cleaned and not second.cleanup_retryable
        assert second.local_status == -9 and second.exit_status is None
        assert closes == 1 and signals == 2
        assert pipes.stderr.closed and owner._retained_pipes is pipes
        assert owner.close() is first and first.cleanup_retryable
        assert owner.close_bounded(core.Deadline(time.monotonic() + 1)) is second
        assert closes == 1 and signals == 2
    finally:
        close()


@pytest.mark.skipif(os.name != "posix", reason="native EOF close faults are exercised on POSIX")
@pytest.mark.parametrize("after_close", [False, True])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_early_eof_and_teardown_share_sticky_close_and_original_control(
    monkeypatch: pytest.MonkeyPatch, after_close: bool, error_type: type[BaseException]
) -> None:
    spawn = subprocess.Popen
    original_close = None
    attempts = 0
    error = error_type()
    owner = core.LocalProcessOwner()
    original_pipes: list[core.LocalProcessPipes] = []
    publish = owner._publish_ready

    def ready(pipes: core.LocalProcessPipes) -> None:
        original_pipes.append(pipes)
        publish(pipes)

    def create(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        nonlocal original_close
        child = spawn(*args, **kwargs)
        assert child.stdin is not None
        original_close = child.stdin.close

        def fail() -> None:
            nonlocal attempts
            attempts += 1
            if after_close:
                assert original_close is not None
                original_close()
            raise error

        monkeypatch.setattr(child.stdin, "close", fail)
        return child

    monkeypatch.setattr(subprocess, "Popen", create)
    monkeypatch.setattr(owner, "_publish_ready", ready)
    try:
        args = [sys.executable, "-I", "-S", "-B", "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(30)"]
        if issubclass(error_type, Exception):
            result = core.run_owned_process(
                args,
                input=core.ProcessInput(data=b""),
                output=core.ProcessOutput(),
                deadline=core.Deadline(time.monotonic() + 3),
                owner=owner,
            )
            assert result.failure is core.ProcessFailure.OBSERVATION
        else:
            with pytest.raises(error_type) as raised:
                core.run_owned_process(
                    args,
                    input=core.ProcessInput(data=b""),
                    output=core.ProcessOutput(),
                    deadline=core.Deadline(time.monotonic() + 3),
                    owner=owner,
                )
            assert raised.value is error
        terminal = owner.close()
        assert not terminal.cleaned and not terminal.cleanup_retryable
        assert terminal.local_status == -9 and terminal.exit_status is None
        pipes = original_pipes[0]
        assert owner._retained_pipes is pipes and len(pipes._records) == 3
        assert pipes.stdout.closed and pipes.stderr.closed
        assert pipes.stdin is not None and pipes.stdin.closed is after_close
        assert owner._retained_status is not None
        assert owner._retained_status.pipes is pipes
        pipes.close_stdin()
        assert owner.close_bounded(core.Deadline(time.monotonic() + 1)) is terminal
        assert attempts == 1
    finally:
        assert original_close is not None
        original_close()


@pytest.mark.parametrize("failure_site", ["status", "collection", "record", "publication"])
def test_construction_failure_keeps_installed_records_and_actual_child_custody(
    monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    owner = core.LocalProcessOwner()
    installed: list[core._PipeClose] = []
    allocations = 0
    target: Any
    if failure_site == "status":
        target, name = core, "_ProcessStatus"
    elif failure_site == "collection":
        target, name = core, "LocalProcessPipes"
    elif failure_site == "record":
        target, name = core, "_PipeClose"
    else:
        target, name = owner, "_publish_ready"
    original = getattr(target, name)

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal allocations
        allocations += 1
        if allocations == (2 if failure_site == "record" else 1):
            raise MemoryError()
        result = original(*args, **kwargs)
        if failure_site == "record":
            installed.append(result)
        return result

    monkeypatch.setattr(target, name, fail_once)
    owner.start(_request("import time; time.sleep(30)", input_piped=True))
    _wait_snapshot(owner, lambda snapshot: snapshot.observation_failed)
    retained_process = owner._retained_process
    retained_pipes = owner._retained_pipes
    terminal = owner.close()
    assert terminal.started and terminal.observation_failed and terminal.cleaned
    assert terminal.local_status is not None and terminal.exit_status is None
    assert retained_process is not None and retained_process.returncode is not None
    for pipe in (retained_process.stdin, retained_process.stdout, retained_process.stderr):
        assert pipe is not None and pipe.closed
    if failure_site == "record":
        assert retained_pipes is not None and installed[0] is retained_pipes._records[0]
        assert len(retained_pipes._records) == 3
        assert all(record.attempted and record.confirmed for record in retained_pipes._records)


def test_unattempted_record_allocation_failure_retries_only_safe_work(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = core.LocalProcessOwner()
    allocate = core._PipeClose
    calls = 0

    def deny_stderr(pipe):
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise MemoryError()
        return allocate(pipe)

    monkeypatch.setattr(core, "_PipeClose", deny_stderr)
    owner.start(_request("import time; time.sleep(30)", input_piped=True))
    _wait_snapshot(owner, lambda snapshot: snapshot.observation_failed)
    first = owner.close()
    assert not first.cleaned and first.cleanup_retryable
    pipes = owner._retained_pipes
    assert pipes is not None and len(pipes._records) == 2
    records = tuple(pipes._records)
    assert all(record.confirmed for record in records)
    assert not pipes.stderr.closed
    assert first.local_status is not None
    monkeypatch.setattr(core, "_PipeClose", allocate)
    final = owner.close_bounded(core.Deadline(time.monotonic() + 2))
    assert final is not None and final.cleaned and not final.cleanup_retryable
    assert len(pipes._records) == 3 and all(a is b for a, b in zip(records, pipes._records, strict=False))
    assert owner.close() is first and not first.cleaned


def test_permanent_status_allocation_failure_retains_child_until_explicit_safe_retry(monkeypatch: pytest.MonkeyPatch):
    owner = core.LocalProcessOwner()
    allocate = core._ProcessStatus
    monkeypatch.setattr(core, "_ProcessStatus", lambda process: (_ for _ in ()).throw(MemoryError()))
    owner.start(_request("import time; time.sleep(30)"))
    _wait_snapshot(owner, lambda snapshot: snapshot.observation_failed)
    first = owner.close()
    assert not first.cleaned and first.cleanup_retryable
    child = owner._retained_process
    assert child is not None and child.returncode is None
    assert child.stdout is not None and not child.stdout.closed
    assert child.stderr is not None and not child.stderr.closed
    assert owner._retained_status is None
    monkeypatch.setattr(core, "_ProcessStatus", allocate)
    final = owner.close_bounded(core.Deadline(time.monotonic() + 2))
    assert final is not None and final.cleaned and final.local_status is not None
    assert child.stdout.closed and child.stderr.closed
    assert owner.close() is first and not first.cleaned


def test_generic_poll_error_preserves_separately_owned_process_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = core.LocalProcessOwner()
    poll = core._ProcessStatus.poll
    calls = 0

    def fail_once(status: core._ProcessStatus) -> int | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError()
        return poll(status)

    monkeypatch.setattr(core._ProcessStatus, "poll", fail_once)
    owner.start(_request("import time; time.sleep(30)"))
    _wait_snapshot(owner, lambda snapshot: snapshot.observation_failed)
    status = owner._retained_status
    assert status is not None and not status.lost
    terminal = owner.close()
    assert terminal.cleaned and terminal.observation_failed
    assert terminal.local_status is not None and terminal.exit_status is None
    assert not status.lost


@pytest.mark.parametrize("allowance", [None, 0.1])
def test_deadline_and_invalid_request_retire_the_provided_inert_owner(
    monkeypatch: pytest.MonkeyPatch, allowance: float | None
) -> None:
    for refused in ("deadline", "request"):
        owner = core.LocalProcessOwner()
        with monkeypatch.context() as fault:
            if refused == "request":
                fault.setattr(core, "LocalProcessRequest", lambda *args: (_ for _ in ()).throw(ValueError()))
            result = core.run_owned_process(
                [sys.executable, "-c", "pass"],
                input=core.ProcessInput(),
                output=core.ProcessOutput(),
                deadline=core.Deadline(0 if refused == "deadline" else None),
                owner=owner,
                cleanup_allowance=allowance,
            )
        terminal = owner.close()
        assert terminal.cleaned and not terminal.admitted and not terminal.started
        assert result.failure is (
            core.ProcessFailure.DEADLINE if refused == "deadline" else core.ProcessFailure.DISPATCH
        )
        with pytest.raises(RuntimeError):
            owner.start(_request("pass"))
