#!/usr/bin/env python3
"""Bounded CPython POSIX exec-evidence experiment, not a production API.

The runner accepts only closed fixture cases. Its candidate path has one owner,
uses the native ``Popen`` fork/exec configuration measured by this experiment,
and obtains status with an exact-PID ``waitpid``. The adversarial cases are
kept separate so their deliberately invalid launch configurations cannot look
like supported application launch options.
"""

from __future__ import annotations

import argparse
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
_MISSING_EXECUTABLE = "/agw-exec-evidence/missing-argument-canary-9c27"
_ARGUMENT_CANARY = "argument-canary-4691"
_ENVIRONMENT_CANARY = "environment-canary-0d3a"
_INPUT_CANARY = "input-canary-e670"


def _configuration() -> dict[str, object]:
    return {
        "shell": False,
        "close_fds": True,
        "preexec_fn": None,
        "waiter": "waitpid_exact_pid",
        "implementation": platform.python_implementation(),
        "python": list(sys.version_info[:3]),
    }


def _finish_wait(process: subprocess.Popen[bytes], status: int) -> None:
    """Prevent ``Popen`` from attempting a second wait during finalization."""
    process.returncode = os.waitstatus_to_exitcode(status)


def _raw_wait(process: subprocess.Popen[bytes]) -> dict[str, object]:
    try:
        waited_pid, status = os.waitpid(process.pid, 0)
    except ChildProcessError:
        return {"fact": "wait_unknown"}
    if waited_pid != process.pid:
        return {"fact": "wait_unknown"}
    _finish_wait(process, status)
    if os.WIFEXITED(status):
        return {
            "fact": "ordinary_exit",
            "exit_code": os.WEXITSTATUS(status),
            "application_entry": "retrospective",
            "application_completion": True,
        }
    if os.WIFSIGNALED(status):
        return {
            "fact": "signal",
            "signal": os.WTERMSIG(status),
            "application_entry": "unknown",
            "application_completion": False,
        }
    return {"fact": "wait_unknown"}


def _candidate(exit_code: int) -> dict[str, object]:
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-c", _CHILD, str(exit_code)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        preexec_fn=None,
    )
    return _raw_wait(process)


def _exit_sweep() -> dict[str, object]:
    return {
        "case": "exit_sweep",
        "configuration": _configuration(),
        "eager_application_entry": False,
        "observations": [_candidate(exit_code) for exit_code in range(256)],
    }


def _safe_launch_failure(error: OSError) -> dict[str, object]:
    return {
        "fact": "launch_failed",
        "error": type(error).__name__,
        "errno": error.errno,
        "application_entry": False,
        "application_completion": False,
    }


def _expect_launch_failure(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            preexec_fn=None,
        )
    except OSError as error:
        return _safe_launch_failure(error)
    observation = _raw_wait(process)
    observation["unexpected_launch"] = True
    return observation


def _launch_failures() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agw-exec-evidence-") as directory:
        non_executable = os.path.join(directory, "non-executable")
        with open(non_executable, "wb") as fixture:
            fixture.write(b"not executable\n")
        os.chmod(non_executable, 0o600)
        missing_cwd = os.path.join(directory, "missing-cwd-input-canary-e670")
        environment = {
            "PATH": "/usr/bin:/bin",
            "AGW_EXEC_EVIDENCE_CANARY": _ENVIRONMENT_CANARY,
            "AGW_EXEC_EVIDENCE_INPUT_LABEL": _INPUT_CANARY,
        }
        failures = {
            "missing_executable": _expect_launch_failure([_MISSING_EXECUTABLE, _ARGUMENT_CANARY], env=environment),
            "non_executable": _expect_launch_failure([non_executable, _ARGUMENT_CANARY], env=environment),
            "missing_cwd": _expect_launch_failure(
                [sys.executable, "-I", "-S", "-B", "-c", "pass", _ARGUMENT_CANARY],
                cwd=missing_cwd,
                env=environment,
            ),
        }
    return {
        "case": "launch_failures",
        "configuration": _configuration(),
        "failures": failures,
    }


def _lost_wait(mode: str) -> dict[str, object]:
    previous_handler: Any = None
    if mode == "ignored_sigchld":
        previous_handler = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c", _CHILD, "42"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            preexec_fn=None,
        )
        competing_status: int | None = None
        if mode == "competing_reaper":
            waited_pid, competing_status = os.waitpid(process.pid, 0)
            if waited_pid != process.pid:
                raise RuntimeError("competing fixture reaped a different process")
        raw = _raw_wait(process)
        synthesized = process.wait()
    finally:
        if mode == "ignored_sigchld":
            signal.signal(signal.SIGCHLD, previous_handler)
    result: dict[str, object] = {
        "case": mode,
        "configuration": _configuration(),
        "raw_wait": raw,
        "popen_wait_after_loss": synthesized,
    }
    if competing_status is not None:
        result["competing_reaper_exit"] = os.waitstatus_to_exitcode(competing_status)
    return result


def _preexec_exit(exit_code: int) -> Callable[[], None]:
    def exit_before_exec() -> None:
        os._exit(exit_code)

    return exit_before_exec


def _preexec_false_positive() -> dict[str, object]:
    observations = []
    for exit_code in (0, 42, 255):
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c", _CHILD, "17"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            preexec_fn=_preexec_exit(exit_code),
        )
        observations.append(_raw_wait(process))
    return {
        "case": "preexec_false_positive",
        "candidate_configuration": _configuration(),
        "fixture_uses_preexec": True,
        "observations": observations,
    }


def _preexec_sigkill() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _signal_ambiguity() -> dict[str, object]:
    preexec_process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        preexec_fn=_preexec_sigkill,
    )
    application_process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        preexec_fn=None,
    )
    return {
        "case": "signal_ambiguity",
        "candidate_configuration": _configuration(),
        "fixture_uses_preexec": True,
        "before_exec": _raw_wait(preexec_process),
        "after_exec": _raw_wait(application_process),
    }


def _broken_error_channel() -> dict[str, object]:
    """Replace only this fixture's exec-error writer with a read-only fd."""
    original_pipe = os.pipe
    error_reader, error_writer = original_pipe()
    os.close(error_writer)
    read_only_writer = os.open(os.devnull, os.O_RDONLY)

    def broken_pipe() -> tuple[int, int]:
        return error_reader, read_only_writer

    os.pipe = broken_pipe
    try:
        process = subprocess.Popen(
            [_MISSING_EXECUTABLE, _ARGUMENT_CANARY],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            preexec_fn=None,
        )
    finally:
        os.pipe = original_pipe
    return {
        "case": "broken_error_channel",
        "candidate_requires_intact_native_error_channel": True,
        "missing_executable_observation": _raw_wait(process),
    }


_CASES: dict[str, Callable[[], dict[str, object]]] = {
    "broken-error-channel": _broken_error_channel,
    "competing-reaper": lambda: _lost_wait("competing_reaper"),
    "exit-sweep": _exit_sweep,
    "ignored-sigchld": lambda: _lost_wait("ignored_sigchld"),
    "launch-failures": _launch_failures,
    "preexec-false-positive": _preexec_false_positive,
    "signal-ambiguity": _signal_ambiguity,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=sorted(_CASES))
    arguments = parser.parse_args()
    if os.name != "posix" or platform.python_implementation() != "CPython":
        parser.error("the native exec-evidence experiment requires POSIX CPython")
    result = _CASES[arguments.case]()
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
