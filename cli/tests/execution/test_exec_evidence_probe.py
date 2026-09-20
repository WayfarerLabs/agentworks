"""Behavioral checks for the bounded native CPython exec experiment."""

from __future__ import annotations

import errno
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

if os.name != "posix" or platform.python_implementation() != "CPython":  # pragma: no cover
    pytest.skip("The native exec-evidence experiment requires POSIX CPython", allow_module_level=True)

PROBE = Path(__file__).with_name("exec_evidence_probe.py")
PYTHON_311 = Path("/usr/bin/python3.11")
CASES = (
    "exit-sweep",
    "launch-failures",
    "ignored-sigchld",
    "competing-reaper",
    "adversarial",
    "broken-error-channel",
    "wait-failure",
)
CANARIES = (
    "missing-argument-canary-9c27",
    "argument-canary-4691",
    "environment-canary-0d3a",
    "input-canary-e670",
)


def _run(case: str, python: Path | None = None) -> Any:
    completed = subprocess.run(
        [os.fspath(python) if python else sys.executable, "-I", "-S", "-B", os.fspath(PROBE), case],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert completed.stderr == b""
    assert all(canary.encode() not in completed.stdout for canary in CANARIES)
    return json.loads(completed.stdout)


def test_exact_pid_native_wait_covers_every_exit_without_eager_entry_claim() -> None:
    observations = _run("exit-sweep")

    assert observations == [["exit", code] for code in range(256)]


def test_native_error_channel_rejects_launch_failures() -> None:
    (missing, non_executable, bad_cwd), no_children = _run("launch-failures")

    assert missing == ["FileNotFoundError", errno.ENOENT]
    assert non_executable == ["PermissionError", errno.EACCES]
    assert bad_cwd == ["FileNotFoundError", errno.ENOENT]
    assert no_children is True


@pytest.mark.parametrize(
    ("case", "competing_exit"),
    [("ignored-sigchld", None), ("competing-reaper", 42)],
)
def test_lost_raw_wait_is_unknown_not_popen_synthetic_zero(case: str, competing_exit: int | None) -> None:
    raw, popen_wait, reaped = _run(case)

    assert raw == ["unknown"]
    assert popen_wait == 0
    assert reaped == competing_exit


def test_preexec_and_signal_counterexamples_bound_the_inference() -> None:
    preexec_exits, killed_before_exec, killed_after_exec = _run("adversarial")

    assert preexec_exits == [["exit", 0], ["exit", 42], ["exit", 255]]
    assert killed_before_exec == killed_after_exec == ["signal", 9]


def test_forced_native_error_writer_failure_makes_missing_exec_look_like_exit_255() -> None:
    assert _run("broken-error-channel") == ["exit", 255]


def test_unexpected_raw_wait_failure_kills_and_reaps_owned_child() -> None:
    assert _run("wait-failure") is True


def test_complete_closed_experiment_runs_on_distribution_python_311() -> None:
    if not PYTHON_311.is_file():
        pytest.skip("This host has no /usr/bin/python3.11 compatibility interpreter")
    version = subprocess.run(
        [os.fspath(PYTHON_311), "-I", "-S", "-B", "-c", "import sys; print(*sys.version_info[:2])"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=5,
        text=True,
    )
    if version.stdout.strip() != "3 11":
        pytest.skip(f"Expected Python 3.11 at {PYTHON_311}, found {version.stdout.strip()}")

    results = {case: _run(case, PYTHON_311) for case in CASES}

    assert results["exit-sweep"] == [["exit", code] for code in range(256)]
    assert results["ignored-sigchld"][0] == ["unknown"]
    assert results["competing-reaper"][0] == ["unknown"]
    assert results["adversarial"][1] == results["adversarial"][2]
    assert results["broken-error-channel"] == ["exit", 255]
    assert results["wait-failure"] is True
