"""Commands without prompts also work for detached callers."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize("stdin_setup", ["sys.stdin = None", "sys.stdin.close()"])
def test_version_command_with_unavailable_stdin(stdin_setup: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; from agentworks.cli import main; {stdin_setup}; sys.argv = ['agw', 'version']; main()",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


@pytest.mark.parametrize("stdin_setup", ["sys.stdin = None", "sys.stdin.close()"])
def test_unavailable_stdin_does_not_enable_interaction(stdin_setup: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; from agentworks import output; {stdin_setup}; "
            "raise SystemExit(1 if output.is_interactive() else 0)",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("stdin_setup", ["sys.stdin = None", "sys.stdin.close()"])
def test_ordinary_operation_tty_authority_handles_unavailable_stdin(stdin_setup: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; {stdin_setup}; "
            "from agentworks.cli._helpers import ordinary_tty_interaction_access; "
            "access = ordinary_tty_interaction_access(); print(type(access).__name__)",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
