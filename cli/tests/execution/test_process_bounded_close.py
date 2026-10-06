"""Bounded observation and retained custody of one local process owner."""

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
from tests.execution.test_process_launch_owner import (
    _assert_exact_cleanup,
    _fake_process,
    _request,
    _wait_snapshot,
)
from tests.execution.test_process_launch_owner import children as children

pytestmark = pytest.mark.windows


def _deadline(seconds: float = 2) -> core.Deadline:
    return core.Deadline(time.monotonic() + seconds)


@pytest.mark.parametrize("expires_at", [None, float("inf"), float("-inf"), float("nan")])
def test_close_requires_finite_observation_deadline(expires_at: float | None) -> None:
    owner = core.LocalProcessOwner()
    with pytest.raises(ValueError):
        owner.close_bounded(core.Deadline(expires_at))
    assert owner.snapshot().terminal is None
    terminal = owner.close_bounded(_deadline())
    assert terminal is not None and terminal.cleaned and not terminal.admitted
    assert owner.close() is terminal


def test_pending_constructor_keeps_same_owner_until_later_settlement(
    monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    original_spawn = subprocess.Popen
    entered = threading.Event()
    release = threading.Event()
    owner = core.LocalProcessOwner()

    def delayed_spawn(argv: tuple[str, ...], **kwargs: Any) -> subprocess.Popen[bytes]:
        entered.set()
        assert release.wait(5)
        return original_spawn(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", delayed_spawn)
    owner.start(_request("import time; time.sleep(30)", input_piped=True))
    try:
        assert entered.wait(5)
        assert owner.close_bounded(_deadline(0.02)) is None
        assert owner.close_bounded(_deadline(-1)) is None
        assert not release.is_set()
        snapshot = owner.snapshot()
        assert snapshot.terminal is None
        assert snapshot.pipes is None
        assert owner.close_bounded(_deadline(0.02)) is None
        with pytest.raises(RuntimeError):
            owner.start(_request("raise SystemExit(99)"))
    finally:
        release.set()
        terminal = owner.close()
    assert terminal.started and terminal.cleaned
    assert not terminal.cleanup_retryable
    _assert_exact_cleanup(children)


def test_pending_borrowed_stdin_survives_until_constructor_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    path = tmp_path / "borrowed-input"
    path.write_bytes(b"borrowed")
    entered = threading.Event()
    release = threading.Event()
    original_spawn = subprocess.Popen
    publish_ready = core.LocalProcessOwner._publish_ready
    owner = core.LocalProcessOwner()
    descriptor_identities: list[int] = []
    late_publications: list[core.LocalProcessPipes | None] = []

    def late_ready(target: core.LocalProcessOwner, pipes: core.LocalProcessPipes) -> None:
        publish_ready(target, pipes)
        late_publications.append(target.snapshot().pipes)

    with path.open("rb") as borrowed:
        descriptor = borrowed.fileno()
        identity = os.fstat(descriptor).st_ino

        def delayed_spawn(argv: tuple[str, ...], **kwargs: Any) -> subprocess.Popen[bytes]:
            assert kwargs["stdin"] == descriptor
            descriptor_identities.append(os.fstat(descriptor).st_ino)
            entered.set()
            assert release.wait(5)
            descriptor_identities.append(os.fstat(descriptor).st_ino)
            return original_spawn(argv, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", delayed_spawn)
        monkeypatch.setattr(core.LocalProcessOwner, "_publish_ready", late_ready)
        owner.start(
            core.LocalProcessRequest(
                (sys.executable, "-I", "-c", "import time; time.sleep(30)"),
                core.BorrowedProcessStdin(descriptor),
            )
        )
        try:
            assert entered.wait(5)
            assert owner.close_bounded(_deadline(0.02)) is None
            assert not borrowed.closed and os.fstat(descriptor).st_ino == identity
        finally:
            release.set()
            terminal = owner.close()
        assert terminal.cleaned and not borrowed.closed
        assert os.fstat(descriptor).st_ino == identity
    assert descriptor_identities == [identity, identity]
    assert late_publications == [None]
    _assert_exact_cleanup(children)


def test_bounded_close_preserves_first_control_during_repeated_wait_interruption(
    monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    original_spawn = subprocess.Popen
    wait_terminal = core.LocalProcessOwner._wait_terminal
    entered = threading.Event()
    release = threading.Event()
    owner = core.LocalProcessOwner()
    first_control = KeyboardInterrupt()
    controls: list[BaseException] = [first_control, SystemExit()]

    def delayed_spawn(argv: tuple[str, ...], **kwargs: Any) -> subprocess.Popen[bytes]:
        entered.set()
        assert release.wait(5)
        return original_spawn(argv, **kwargs)

    def interrupted_wait(target: core.LocalProcessOwner, deadline=None, *, first=True):
        if controls:
            raise controls.pop(0)
        return wait_terminal(target, deadline, first=first)

    monkeypatch.setattr(subprocess, "Popen", delayed_spawn)
    monkeypatch.setattr(core.LocalProcessOwner, "_wait_terminal", interrupted_wait)
    owner.start(_request("import time; time.sleep(30)"))
    try:
        assert entered.wait(5)
        with pytest.raises(KeyboardInterrupt) as caught:
            owner.close_bounded(_deadline(0.02))
        assert caught.value is first_control and not controls
        assert owner.snapshot().terminal is None and not release.is_set()
    finally:
        release.set()
        terminal = owner.close()
    assert terminal.cleaned
    _assert_exact_cleanup(children)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_start_interruption_can_escape_with_bounded_pending_custody(
    monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]], interruption: type[BaseException]
) -> None:
    original_spawn = subprocess.Popen
    original_admit = core.LocalProcessOwner._admit
    entered = threading.Event()
    release = threading.Event()
    owner = core.LocalProcessOwner()
    control = interruption()

    def delayed_spawn(argv: tuple[str, ...], **kwargs: Any) -> subprocess.Popen[bytes]:
        entered.set()
        assert release.wait(5)
        return original_spawn(argv, **kwargs)

    def interrupt_after_admission(target: core.LocalProcessOwner, request: core.LocalProcessRequest) -> bool:
        original_admit(target, request)
        assert entered.wait(5)
        raise control

    monkeypatch.setattr(subprocess, "Popen", delayed_spawn)
    monkeypatch.setattr(core.LocalProcessOwner, "_admit", interrupt_after_admission)
    try:
        with pytest.raises(interruption) as caught:
            owner.start(_request("import time; time.sleep(30)"), close_deadline=_deadline(0.03))
        assert caught.value is control
        assert not release.is_set() and owner.snapshot().terminal is None
    finally:
        release.set()
        terminal = owner.close()
    assert terminal.started and terminal.cleaned
    _assert_exact_cleanup(children)


def test_cleanup_retry_keeps_exact_status_and_is_not_requested_by_pending_observation(
    monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    cleanup = core._cleanup
    retry_entered = threading.Event()
    release_retry = threading.Event()
    statuses: list[core._ProcessStatus] = []
    owner = core.LocalProcessOwner()

    def fail_then_hold_retry(status: core._ProcessStatus) -> bool:
        statuses.append(status)
        if len(statuses) == 1:
            return False
        retry_entered.set()
        assert release_retry.wait(5)
        return cleanup(status)

    monkeypatch.setattr(core, "_cleanup", fail_then_hold_retry)
    owner.start(_request("import time; time.sleep(30)", input_piped=True))
    _wait_snapshot(owner, lambda snapshot: snapshot.pipes is not None)
    try:
        first = owner.close_bounded(_deadline())
        assert first is not None and not first.cleaned and first.cleanup_retryable
        assert owner.close() is first
        assert owner._retained_status is statuses[0]
        assert owner.snapshot().pipes is None
        assert len(statuses) == 1
        assert owner.close_bounded(_deadline(0.02)) is None
        assert retry_entered.wait(5)
        assert owner.close_bounded(_deadline(0.02)) is None
        assert len(statuses) == 2
    finally:
        release_retry.set()
        settled = _wait_snapshot(owner, lambda snapshot: snapshot.terminal is not None and snapshot.terminal.cleaned)
        final = settled.terminal
    assert final is not None and final.cleaned and not final.cleanup_retryable
    assert owner.close() is first
    assert not first.cleaned and first.cleanup_retryable
    assert statuses[0] is statuses[1]
    assert owner._retained_status is None
    assert owner.close_bounded(_deadline()) is final
    _assert_exact_cleanup(children)


def test_native_kill_failure_closes_pipes_then_retries_same_owned_process(
    monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    owner = core.LocalProcessOwner()
    owner.start(_request("import time; time.sleep(30)", input_piped=True))
    _wait_snapshot(owner, lambda snapshot: snapshot.pipes is not None)
    child = children[0]
    attempts: list[tuple[int, int]] = []
    signal = os.kill if os.name == "posix" else child.kill

    def fail_first_signal(*args: Any) -> None:
        attempts.append((threading.get_ident(), child.pid))
        if len(attempts) == 1:
            raise PermissionError("injected native kill failure")
        signal(*args)

    if os.name == "posix":
        monkeypatch.setattr(os, "kill", fail_first_signal)
    else:
        monkeypatch.setattr(child, "kill", fail_first_signal)
    try:
        first = owner.close_bounded(_deadline())
        assert first is not None and not first.cleaned and first.cleanup_retryable
        assert child.returncode is None
        assert child.stdin is not None and child.stdout is not None and child.stderr is not None
        assert child.stdin.closed and child.stdout.closed and child.stderr.closed
        final = owner.close_bounded(_deadline())
        assert final is not None and final.cleaned and not final.cleanup_retryable
        assert first is owner.close() and not first.cleaned
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert final.local_status is not None and final.exit_status is None
    finally:
        terminal = owner.snapshot().terminal
        if terminal is not None and terminal.cleanup_retryable:
            owner.close_bounded(_deadline())
    _assert_exact_cleanup(children)


def test_retained_owner_observes_natural_exit_without_retry_or_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    permit_exit = tmp_path / "permit-exit"
    cleanup = core._cleanup
    entry = core._local_process_owner_entry
    thread_returned = threading.Event()
    calls = 0
    owner = core.LocalProcessOwner()

    def tracked_entry(target: core.LocalProcessOwner) -> None:
        try:
            entry(target)
        finally:
            thread_returned.set()

    def fail_once(status: core._ProcessStatus) -> bool:
        nonlocal calls
        calls += 1
        return False if calls == 1 else cleanup(status)

    def forbidden_signal(*args: object, **kwargs: object) -> None:
        raise AssertionError("natural exit cleanup signaled a process")

    monkeypatch.setattr(core, "_cleanup", fail_once)
    monkeypatch.setattr(core, "_local_process_owner_entry", tracked_entry)
    owner.start(
        _request(
            f"import time; from pathlib import Path; marker=Path({str(permit_exit)!r}); "
            "\nwhile not marker.exists(): time.sleep(0.01)\nraise SystemExit(7)"
        )
    )
    _wait_snapshot(owner, lambda snapshot: snapshot.pipes is not None)
    try:
        first = owner.close_bounded(_deadline())
        assert first is not None and first.cleanup_retryable
        assert calls == 1 and not thread_returned.is_set()
        with monkeypatch.context() as context:
            context.setattr(os, "kill", forbidden_signal)
            context.setattr(children[0], "kill", forbidden_signal)
            permit_exit.touch()
            final = _wait_snapshot(owner, lambda snapshot: snapshot.terminal is not None and snapshot.terminal.cleaned)
        assert final.terminal is not None
        assert final.terminal.local_status == 7 and final.terminal.exit_status is None
        assert not final.terminal.cleanup_retryable
        assert owner.close() is first
        assert thread_returned.wait(5)
        assert calls == 2 and not first.cleaned
    finally:
        permit_exit.touch()
        owner.close()
    _assert_exact_cleanup(children)


@pytest.mark.skipif(os.name != "posix", reason="exact wait loss and numeric signals are POSIX-only")
def test_lost_exclusive_ownership_stops_retention_and_denies_every_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _fake_process()
    owner = core.LocalProcessOwner()
    cleanup = core._cleanup
    lost = threading.Event()
    signals: list[tuple[int, int]] = []
    calls = 0

    def poll(status: core._ProcessStatus) -> int | None:
        if lost.is_set():
            status.lost = True
        return None

    def fail_once(status: core._ProcessStatus) -> bool:
        nonlocal calls
        calls += 1
        return False if calls == 1 else cleanup(status)

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(core._ProcessStatus, "poll", poll)
    monkeypatch.setattr(core, "_cleanup", fail_once)
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    owner.start(_request("pass"))
    _wait_snapshot(owner, lambda snapshot: snapshot.pipes is not None)
    try:
        first = owner.close_bounded(_deadline())
        assert first is not None and first.cleanup_retryable
        lost.set()
        snapshot = _wait_snapshot(
            owner, lambda snapshot: snapshot.terminal is not None and not snapshot.terminal.cleanup_retryable
        )
        terminal = snapshot.terminal
        assert terminal is not None and not terminal.cleaned
        assert terminal.observation_failed
        assert owner.close_bounded(_deadline()) is terminal
        assert owner.close_bounded(_deadline()) is terminal
        assert owner._retained_status is None
        assert signals == [] and calls == 2
        assert process.stdout.closed and process.stderr.closed
    finally:
        lost.set()
        owner.close()


def test_waiting_close_returns_first_failure_even_after_later_natural_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    permit_exit = tmp_path / "permit-exit"
    cleanup = core._cleanup
    wait_terminal = core.LocalProcessOwner._wait_terminal
    owner = core.LocalProcessOwner()
    attempts = 0
    first_observation: list[core.LocalProcessTerminal] = []

    def fail_once(status: core._ProcessStatus) -> bool:
        nonlocal attempts
        attempts += 1
        return False if attempts == 1 else cleanup(status)

    def delay_waiter_until_later_settlement(target: core.LocalProcessOwner, deadline=None, *, first=True):
        snapshot = _wait_snapshot(target, lambda snapshot: snapshot.terminal is not None)
        assert snapshot.terminal is not None and snapshot.terminal.cleanup_retryable
        first_observation.append(snapshot.terminal)
        permit_exit.touch()
        _wait_snapshot(target, lambda snapshot: snapshot.terminal is not None and snapshot.terminal.cleaned)
        return wait_terminal(target, deadline, first=first)

    monkeypatch.setattr(core, "_cleanup", fail_once)
    owner.start(
        _request(
            f"import time; from pathlib import Path; marker=Path({str(permit_exit)!r}); "
            "\nwhile not marker.exists(): time.sleep(0.01)"
        )
    )
    _wait_snapshot(owner, lambda snapshot: snapshot.pipes is not None)
    try:
        with monkeypatch.context() as context:
            context.setattr(core.LocalProcessOwner, "_wait_terminal", delay_waiter_until_later_settlement)
            first = owner.close()
        assert first is first_observation[0] and not first.cleaned
        assert owner.close() is first
        current = owner.snapshot().terminal
        assert current is not None and current.cleaned and current is not first
        assert owner.close_bounded(_deadline()) is current
    finally:
        permit_exit.touch()
        owner.close_bounded(_deadline())
    _assert_exact_cleanup(children)


def test_bounded_owner_remains_standalone_stdlib_on_guest_python(interpreter: Path) -> None:
    script = """
import importlib.util
import sys
import time
spec = importlib.util.spec_from_file_location('owned_process', sys.argv[1])
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)
owner = core.LocalProcessOwner()
owner.start(core.LocalProcessRequest((sys.executable, '-I', '-c', 'import time; time.sleep(30)'),
                                   core.LocalProcessInput.EOF))
terminal = owner.close_bounded(core.Deadline(time.monotonic() + 5))
assert terminal is not None and terminal.cleaned
assert not terminal.cleanup_retryable
assert owner.close() is terminal
"""
    result = subprocess.run([str(interpreter), "-I", "-c", script, core.__file__], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
