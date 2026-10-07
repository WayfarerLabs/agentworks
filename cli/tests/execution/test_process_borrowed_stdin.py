"""Explicit native stdin borrowing without terminal policy in the launch owner."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agentworks.execution import _process as process_core

pytestmark = pytest.mark.windows


def _request(descriptor: int, source: str) -> process_core.LocalProcessRequest:
    return process_core.LocalProcessRequest(
        (sys.executable, "-I", "-S", "-B", "-c", source),
        process_core.BorrowedProcessStdin(descriptor),
    )


def _wait_exit(owner: process_core.LocalProcessOwner) -> process_core.LocalProcessSnapshot:
    expires = time.monotonic() + 5
    while time.monotonic() < expires:
        snapshot = owner.snapshot()
        if snapshot.exit_status is not None:
            return snapshot
        if snapshot.terminal is not None:
            pytest.fail("native child did not establish its exit status")
        time.sleep(0.01)
    pytest.fail("native child did not exit within its observation budget")


def test_borrowed_stdin_construction_is_passive(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("request construction accessed native state")

    for name in ("open", "dup", "fstat", "close", "get_blocking", "set_blocking"):
        monkeypatch.setattr(os, name, forbidden)

    request = _request(12345, "pass")

    assert isinstance(request.input, process_core.BorrowedProcessStdin)
    assert request.input.descriptor == 12345
    assert "12345" not in repr(request)


@pytest.mark.parametrize("descriptor", [-1, -3, True, 1.5])
def test_borrowed_stdin_rejects_subprocess_sentinels_and_non_descriptors(descriptor: Any) -> None:
    with pytest.raises(ValueError):
        process_core.BorrowedProcessStdin(descriptor)


def test_request_refuses_the_old_ambiguous_boolean_input() -> None:
    invalid_input: Any = True
    with pytest.raises(ValueError):
        process_core.LocalProcessRequest((sys.executable,), invalid_input)


def test_borrowed_file_is_child_stdin_but_never_owner_closed(tmp_path: Path) -> None:
    path = tmp_path / "input"
    content = b"literal\x00\xff\n"
    path.write_bytes(content)
    owner = process_core.LocalProcessOwner()
    with path.open("rb") as borrowed:
        descriptor = borrowed.fileno()
        identity = os.fstat(descriptor)
        try:
            owner.start(
                _request(
                    descriptor,
                    "import os; data=os.read(0,100); os.write(1,data); os.write(2,b'err\\x00\\xff')",
                )
            )
            snapshot = _wait_exit(owner)
            assert snapshot.pipes is not None and snapshot.pipes.stdin is None
            assert snapshot.pipes.stdout.read() == content
            assert snapshot.pipes.stderr.read() == b"err\x00\xff"
        finally:
            terminal = owner.close()
        assert terminal.cleaned and terminal.exit_status == 0
        assert not borrowed.closed
        assert os.fstat(descriptor).st_ino == identity.st_ino
        assert owner.close() is terminal


@pytest.mark.skipif(os.name != "posix", reason="native PTY admission is POSIX-only")
def test_borrowed_pty_stdin_keeps_separate_raw_output_and_queued_input() -> None:
    import termios

    master, slave = os.openpty()
    owner = process_core.LocalProcessOwner()
    try:
        modes = termios.tcgetattr(slave)
        blocking = os.get_blocking(slave)
        os.write(master, b"queued-before-launch\n")
        owner.start(
            _request(
                slave,
                "import os; assert os.isatty(0); assert not os.isatty(1); assert not os.isatty(2); "
                "assert os.read(0,100)==b'queued-before-launch\\n'; "
                "os.write(1,b'out\\x00\\xff'); os.write(2,b'err\\x00\\xfe')",
            )
        )
        snapshot = _wait_exit(owner)
        assert snapshot.pipes is not None and snapshot.pipes.stdin is None
        assert snapshot.pipes.stdout.read() == b"out\x00\xff"
        assert snapshot.pipes.stderr.read() == b"err\x00\xfe"
    finally:
        try:
            terminal = owner.close()
            assert termios.tcgetattr(slave) == modes
            assert os.get_blocking(slave) == blocking
        finally:
            os.close(slave)
            os.close(master)
    assert terminal.cleaned and terminal.exit_status == 0


def test_interrupted_admission_settles_pending_constructor_before_returning_borrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "input"
    path.write_bytes(b"owned")
    original_spawn = subprocess.Popen
    original_admit = process_core.LocalProcessOwner._admit
    entered = threading.Event()
    release = threading.Event()
    inspected: list[int] = []
    interruption = KeyboardInterrupt()
    owner = process_core.LocalProcessOwner()

    def interrupt_after_admission(
        target: process_core.LocalProcessOwner, request: process_core.LocalProcessRequest
    ) -> bool:
        original_admit(target, request)
        raise interruption

    with path.open("rb") as borrowed:
        descriptor = borrowed.fileno()
        expected_inode = os.fstat(descriptor).st_ino

        def delayed_spawn(argv: tuple[str, ...], **kwargs: Any) -> subprocess.Popen[bytes]:
            assert kwargs["stdin"] == descriptor
            inspected.append(os.fstat(descriptor).st_ino)
            entered.set()
            if not release.wait(5):
                raise OSError("test constructor was not released")
            inspected.append(os.fstat(descriptor).st_ino)
            return original_spawn(argv, **kwargs)

        def permit_constructor() -> None:
            if entered.wait(5):
                time.sleep(0.05)
            release.set()

        helper = threading.Thread(target=permit_constructor)
        monkeypatch.setattr(subprocess, "Popen", delayed_spawn)
        monkeypatch.setattr(process_core.LocalProcessOwner, "_admit", interrupt_after_admission)
        helper.start()
        try:
            with pytest.raises(KeyboardInterrupt) as caught:
                owner.start(_request(descriptor, "raise SystemExit(0)"))
            assert caught.value is interruption
            terminal = owner.snapshot().terminal
            assert terminal is not None and terminal.started and terminal.cleaned
            assert entered.is_set() and release.is_set()
            assert inspected == [expected_inode, expected_inode]
            assert os.fstat(descriptor).st_ino == expected_inode
        finally:
            release.set()
            helper.join(5)
            owner.close()
        assert not helper.is_alive()
