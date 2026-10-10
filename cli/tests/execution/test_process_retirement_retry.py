"""Exact wait loss grants bookkeeping once, then requires explicit retry."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from queue import Queue
from typing import Any

import pytest

from agentworks.execution import _process as core
from tests.execution.test_process_launch_owner import _request, _wait_snapshot


@pytest.mark.skipif(os.name != "posix", reason="exact native wait loss is POSIX-only")
@pytest.mark.parametrize("loss_during_wait", [False, True])
def test_exact_loss_and_missing_record_wait_for_explicit_retry(
    monkeypatch: pytest.MonkeyPatch, loss_during_wait: bool
) -> None:
    owner = core.LocalProcessOwner()
    spawn, native_wait, native_kill = subprocess.Popen, os.waitpid, os.kill
    allocate, poll, cleanup_process = core._PipeClose, core._ProcessStatus.poll, core._cleanup_process
    passive_wait, publish, entry = (
        owner._wait_cleanup_retry_or_exit,
        owner._publish_terminal,
        core._local_process_owner_entry,
    )
    children: list[subprocess.Popen[bytes]] = []
    publications: list[core.LocalProcessTerminal] = []
    polls: Queue[None] = Queue()
    advance = threading.Semaphore(0)
    finished = threading.Event()
    passive = False
    release = False
    allocation_available = False
    allocations = 0
    cleanups = 0
    lost = False
    actions_after_loss: list[str] = []

    def create(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = spawn(*args, **kwargs)
        children.append(child)
        if not loss_during_wait:
            native_wait(child.pid, 0)
        return child

    def wait(pid: int, flags: int) -> tuple[int, int]:
        nonlocal lost
        if lost:
            actions_after_loss.append("wait")
            raise AssertionError("native wait after exact loss")
        try:
            return native_wait(pid, flags)
        except ChildProcessError:
            lost = True
            raise

    def kill(pid: int, sig: int) -> None:
        if lost:
            actions_after_loss.append("kill")
            raise AssertionError("signal after exact loss")
        native_kill(pid, sig)

    def record(pipe):
        nonlocal allocations
        allocations += 1
        if allocations >= 3 and not allocation_available:
            raise MemoryError()
        return allocate(pipe)

    def retire(status: core._ProcessStatus) -> bool:
        nonlocal cleanups
        cleanups += 1
        # Retain the live child for a genuine loss first observed by passive poll.
        if loss_during_wait and cleanups == 1:
            return False
        return cleanup_process(status)

    def observe(status: core._ProcessStatus) -> int | None:
        if passive and not release:
            polls.put(None)
            assert advance.acquire(timeout=10), "fixture did not release passive poll"
        return poll(status)

    def waiting(status: core._ProcessStatus | None) -> None:
        nonlocal passive
        passive = True
        passive_wait(status)

    def publication(terminal: core.LocalProcessTerminal) -> None:
        publications.append(terminal)
        publish(terminal)

    def complete(target: core.LocalProcessOwner) -> None:
        try:
            entry(target)
        finally:
            finished.set()

    monkeypatch.setattr(subprocess, "Popen", create)
    monkeypatch.setattr(os, "waitpid", wait)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(core, "_PipeClose", record)
    monkeypatch.setattr(core, "_cleanup_process", retire)
    monkeypatch.setattr(core._ProcessStatus, "poll", observe)
    monkeypatch.setattr(owner, "_wait_cleanup_retry_or_exit", waiting)
    monkeypatch.setattr(owner, "_publish_terminal", publication)
    monkeypatch.setattr(core, "_local_process_owner_entry", complete)
    owner.start(
        _request("import time; time.sleep(30)" if loss_during_wait else "raise SystemExit(7)", input_piped=True)
    )
    try:
        _wait_snapshot(owner, lambda snapshot: snapshot.observation_failed)
        first = owner.close()
        polls.get(timeout=5)
        assert first.started and first.observation_failed and not first.cleaned and first.cleanup_retryable
        assert first.local_status is first.exit_status is None
        status = owner._retained_status
        assert status is not None and status.process is children[0]
        pipes = status.pipes
        assert pipes is not None and len(pipes._records) == 2
        records = tuple(pipes._records)
        assert all(record.attempted and record.confirmed for record in records)
        assert not pipes.stderr.closed and children[0].returncode is None
        if loss_during_wait:
            assert not status.lost
            native_kill(children[0].pid, signal.SIGKILL)
            native_wait(children[0].pid, 0)
            advance.release()
            polls.get(timeout=5)
            assert status.lost and cleanups == 2 and len(publications) == 2
        else:
            assert status.lost and cleanups == 1 and len(publications) == 1
        current = owner.snapshot().terminal
        assert current is not None and current.cleanup_retryable and not current.cleaned
        previous_allocations, previous_cleanups = allocations, cleanups
        previous_publications = tuple(publications)
        # Traverse passive polls with allocation still unavailable, then available.
        # A prior loss must authorize neither cleanup nor publication in either phase.
        for available in (False, True):
            allocation_available = available
            advance.release()
            polls.get(timeout=5)
            assert allocations == previous_allocations and cleanups == previous_cleanups
            assert tuple(publications) == previous_publications
            assert owner.snapshot().terminal is current
            assert owner._retained_status is status and status.pipes is pipes
            assert len(pipes._records) == 2 and all(a is b for a, b in zip(records, pipes._records, strict=True))
            assert not pipes.stderr.closed and not finished.is_set()
        release = True
        advance.release()
        final = owner.close_bounded(core.Deadline(time.monotonic() + 5))
        assert final is not None and final.cleaned and not final.cleanup_retryable
        assert final.observation_failed and final.local_status is final.exit_status is None
        assert finished.wait(5)
        assert cleanups == previous_cleanups + 1 and allocations == previous_allocations + 1
        assert len(pipes._records) == 3 and all(a is b for a, b in zip(records, pipes._records, strict=False))
        assert pipes.stderr.closed and all(record.confirmed for record in pipes._records)
        assert owner.close() is first and not first.cleaned
        assert children[0].returncode is None and actions_after_loss == []
        assert owner._retained_status is owner._retained_process is None
    finally:
        allocation_available = release = True
        advance.release()
        owner.close_bounded(core.Deadline(time.monotonic() + 5))
        assert finished.wait(5)
        for child in children:
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None:
                    pipe.close()
