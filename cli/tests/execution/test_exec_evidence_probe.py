"""Executable evidence for the bounded native CPython exec experiment."""

from __future__ import annotations

import errno
import json
import os
import platform
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

if os.name != "posix" or platform.python_implementation() != "CPython":  # pragma: no cover
    pytest.skip("The native exec-evidence experiment requires POSIX CPython", allow_module_level=True)

PROBE = Path(__file__).with_name("exec_evidence_probe.py")
PYTHON_311 = Path("/usr/bin/python3.11")
CANARIES = (
    "argument-canary-4691",
    "environment-canary-0d3a",
    "input-canary-e670",
)


def _run(case: str, *, python: Path | None = None) -> dict[str, Any]:
    interpreter = os.fspath(python) if python is not None else sys.executable
    completed = subprocess.run(
        [interpreter, "-I", "-S", "-B", os.fspath(PROBE), case],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert completed.stderr == b""
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)
    return result


def _assert_candidate_configuration(configuration: dict[str, object]) -> None:
    assert configuration["shell"] is False
    assert configuration["close_fds"] is True
    assert configuration["preexec_fn"] is None
    assert configuration["waiter"] == "waitpid_exact_pid"
    assert configuration["implementation"] == "CPython"


def test_exact_pid_ordinary_wait_proves_every_application_exit_value() -> None:
    result = _run("exit-sweep")

    _assert_candidate_configuration(result["configuration"])
    assert result["eager_application_entry"] is False
    observations = result["observations"]
    assert len(observations) == 256
    assert [observation["exit_code"] for observation in observations] == list(range(256))
    assert all(observation["fact"] == "ordinary_exit" for observation in observations)
    assert all(observation["application_entry"] == "retrospective" for observation in observations)
    assert all(observation["application_completion"] is True for observation in observations)


def test_native_launch_failures_never_become_application_evidence() -> None:
    result = _run("launch-failures")

    _assert_candidate_configuration(result["configuration"])
    failures = result["failures"]
    assert failures["missing_executable"]["errno"] == errno.ENOENT
    assert failures["non_executable"]["errno"] == errno.EACCES
    assert failures["missing_cwd"]["errno"] == errno.ENOENT
    for failure in failures.values():
        assert failure["fact"] == "launch_failed"
        assert failure["application_entry"] is False
        assert failure["application_completion"] is False


@pytest.mark.parametrize("case", ["ignored-sigchld", "competing-reaper"])
def test_missing_raw_wait_is_unknown_even_when_popen_wait_synthesizes_zero(case: str) -> None:
    result = _run(case)

    _assert_candidate_configuration(result["configuration"])
    assert result["raw_wait"] == {"fact": "wait_unknown"}
    assert result["popen_wait_after_loss"] == 0
    if case == "competing-reaper":
        assert result["competing_reaper_exit"] == 42


def test_preexec_callback_exit_would_create_false_completion_evidence() -> None:
    result = _run("preexec-false-positive")

    _assert_candidate_configuration(result["candidate_configuration"])
    assert result["fixture_uses_preexec"] is True
    assert [observation["exit_code"] for observation in result["observations"]] == [0, 42, 255]
    assert all(observation["fact"] == "ordinary_exit" for observation in result["observations"])
    assert all(observation["application_completion"] is True for observation in result["observations"])


def test_waited_sigkill_cannot_locate_death_before_or_after_exec() -> None:
    result = _run("signal-ambiguity")

    _assert_candidate_configuration(result["candidate_configuration"])
    assert result["fixture_uses_preexec"] is True
    expected = {
        "fact": "signal",
        "signal": signal.SIGKILL,
        "application_entry": "unknown",
        "application_completion": False,
    }
    assert result["before_exec"] == expected
    assert result["after_exec"] == expected


def test_broken_native_error_writer_turns_missing_exec_into_false_255() -> None:
    result = _run("broken-error-channel")

    assert result["candidate_requires_intact_native_error_channel"] is True
    assert result["missing_executable_observation"] == {
        "fact": "ordinary_exit",
        "exit_code": 255,
        "application_entry": "retrospective",
        "application_completion": True,
    }


@pytest.mark.parametrize(
    "case",
    [
        "exit-sweep",
        "launch-failures",
        "ignored-sigchld",
        "competing-reaper",
        "preexec-false-positive",
        "signal-ambiguity",
        "broken-error-channel",
    ],
)
def test_probe_reports_never_expose_fixture_canaries(case: str) -> None:
    rendered = json.dumps(_run(case), sort_keys=True)

    assert all(canary not in rendered for canary in CANARIES)


def test_complete_experiment_runs_on_distribution_python_311() -> None:
    if not PYTHON_311.is_file():
        pytest.skip("This host has no /usr/bin/python3.11 compatibility interpreter")
    version = subprocess.run(
        [os.fspath(PYTHON_311), "-I", "-S", "-B", "-c", "import sys; print(*sys.version_info[:3])"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=5,
        text=True,
    )
    if not version.stdout.startswith("3 11 "):
        pytest.skip(f"Expected Python 3.11 at {PYTHON_311}, found {version.stdout.strip()}")

    results = {
        case: _run(case, python=PYTHON_311)
        for case in (
            "exit-sweep",
            "launch-failures",
            "ignored-sigchld",
            "competing-reaper",
            "preexec-false-positive",
            "signal-ambiguity",
            "broken-error-channel",
        )
    }

    assert results["exit-sweep"]["configuration"]["python"][:2] == [3, 11]
    assert len(results["exit-sweep"]["observations"]) == 256
    assert results["launch-failures"]["failures"]["missing_executable"]["fact"] == "launch_failed"
    assert results["ignored-sigchld"]["raw_wait"] == {"fact": "wait_unknown"}
    assert results["competing-reaper"]["raw_wait"] == {"fact": "wait_unknown"}
    assert results["preexec-false-positive"]["observations"][-1]["exit_code"] == 255
    assert results["signal-ambiguity"]["before_exec"] == results["signal-ambiguity"]["after_exec"]
    assert results["broken-error-channel"]["missing_executable_observation"]["exit_code"] == 255
