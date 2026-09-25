#!/usr/bin/env python3
"""Closed-case native CPython exec-evidence experiment, not a production API.

The candidate uses only public ``Popen`` controls. One case observes the native
function selected by known interpreter builds without changing that selection.
This is not a claim about every current or future CPython configuration. Every
launched child has fixed, immediately terminating code and is explicitly
waited; an unexpected wait failure kills and reaps the one owned PID.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from typing import Any

_CHILD = "import os, sys; os._exit(int(sys.argv[1]))"
_SESSION_CHILD = "import os; os._exit(0 if os.getsid(0) == os.getpid() else 1)"
_MISSING = "/agw-exec-evidence/missing-argument-canary-9c27"
_ARG_CANARY = "argument-canary-4691"
_ENV = {"PATH": "/usr/bin:/bin", "CANARY": "environment-canary-0d3a"}


def _candidate(
    argv: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        preexec_fn=None,
        start_new_session=True,
    )


def _child(exit_code: int) -> subprocess.Popen[bytes]:
    return _candidate([sys.executable, "-I", "-S", "-B", "-c", _CHILD, str(exit_code)])


def _wait(process: subprocess.Popen[bytes]) -> list[object]:
    while True:
        try:
            waited_pid, status = os.waitpid(process.pid, 0)
            break
        except InterruptedError:
            continue
        except ChildProcessError:
            return ["unknown"]
        except BaseException:
            _kill_and_reap(process)
            raise
    if waited_pid != process.pid:
        return ["unknown"]
    process.returncode = os.waitstatus_to_exitcode(status)
    if os.WIFEXITED(status):
        return ["exit", os.WEXITSTATUS(status)]
    if os.WIFSIGNALED(status):
        return ["signal", os.WTERMSIG(status)]
    return ["unknown"]


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(process.pid, signal.SIGKILL)
    try:
        _, status = os.waitpid(process.pid, 0)
    except ChildProcessError:
        return
    process.returncode = os.waitstatus_to_exitcode(status)


def _sweep() -> object:
    return [_wait(_child(code)) for code in range(256)]


def _launch_error(argv: list[str], *, cwd: str | None = None) -> list[object]:
    try:
        process = _candidate(argv, cwd=cwd, env=_ENV)
    except OSError as error:
        return [type(error).__name__, error.errno]
    return ["unexpected", _wait(process)]


def _launch_failures() -> object:
    with tempfile.TemporaryDirectory(prefix="agw-exec-evidence-") as directory:
        non_executable = os.path.join(directory, "non-executable")
        with open(non_executable, "wb") as fixture:
            fixture.write(b"input-canary-e670\n")
        os.chmod(non_executable, 0o600)
        missing_cwd = os.path.join(directory, "missing-cwd")
        failures = [
            _launch_error([_MISSING, _ARG_CANARY]),
            _launch_error([non_executable, _ARG_CANARY]),
            _launch_error([sys.executable, "-I", "-S", "-B", "-c", "pass", _ARG_CANARY], cwd=missing_cwd),
        ]
        return [failures, _no_children()]


def _no_children() -> bool:
    try:
        os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


def _wait_failure() -> object:
    process = _child(60)
    original_waitpid = os.waitpid
    failed_once = False

    def fail_once(pid: int, options: int) -> tuple[int, int]:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("fixture wait failure")
        return original_waitpid(pid, options)

    os.waitpid = fail_once
    try:
        try:
            _wait(process)
        except OSError:
            pass
        else:
            raise AssertionError("fixture wait failure was not raised")
    finally:
        os.waitpid = original_waitpid
    return _no_children()


def _lost_wait(mode: str) -> object:
    previous: Any = None
    if mode == "ignored":
        previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        process = _child(42)
        reaped: int | None = None
        if mode == "competing":
            waited_pid, status = os.waitpid(process.pid, 0)
            if waited_pid != process.pid:
                raise RuntimeError("fixture reaped a different process")
            reaped = os.waitstatus_to_exitcode(status)
        raw = _wait(process)
        synthesized = process.wait()
    finally:
        if mode == "ignored":
            signal.signal(signal.SIGCHLD, previous)
    return [raw, synthesized, reaped]


def _exit_before_exec(code: int) -> Callable[[], None]:
    return lambda: os._exit(code)


def _kill_before_exec() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _adversarial() -> object:
    preexec_exits = []
    for code in (0, 42, 255):
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            preexec_fn=_exit_before_exec(code),
        )
        preexec_exits.append(_wait(process))
    before_exec = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        preexec_fn=_kill_before_exec,
    )
    after_exec = _candidate([sys.executable, "-I", "-S", "-B", "-c", "import os; os.kill(os.getpid(), 9)"])
    return [preexec_exits, _wait(before_exec), _wait(after_exec)]


def _broken_error_channel() -> object:
    original_pipe = os.pipe
    reader, writer = original_pipe()
    os.close(writer)
    read_only_writer = os.open(os.devnull, os.O_RDONLY)
    os.pipe = lambda: (reader, read_only_writer)
    try:
        process = _candidate([_MISSING, _ARG_CANARY])
    finally:
        os.pipe = original_pipe
    return _wait(process)


def _public_launch() -> object:
    routes: list[str] = []
    original_fork_exec = subprocess._fork_exec  # type: ignore[attr-defined]

    def observed_fork_exec(*args: Any) -> int:
        routes.append("fork-exec")
        return int(original_fork_exec(*args))

    subprocess._fork_exec = observed_fork_exec  # type: ignore[attr-defined]
    try:
        process = _candidate([sys.executable, "-I", "-S", "-B", "-c", _SESSION_CHILD])
    finally:
        subprocess._fork_exec = original_fork_exec  # type: ignore[attr-defined]
    return [routes, _wait(process)]


_CASES = {
    "adversarial": _adversarial,
    "broken-error-channel": _broken_error_channel,
    "competing-reaper": lambda: _lost_wait("competing"),
    "exit-sweep": _sweep,
    "ignored-sigchld": lambda: _lost_wait("ignored"),
    "launch-failures": _launch_failures,
    "public-launch": _public_launch,
    "wait-failure": _wait_failure,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=sorted(_CASES))
    arguments = parser.parse_args()
    if os.name != "posix" or platform.python_implementation() != "CPython":
        parser.error("this experiment requires POSIX CPython")
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    sys.stdout.write(json.dumps(_CASES[arguments.case](), separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
