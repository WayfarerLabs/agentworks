"""Sensitive stdin stays out of SSH argv, errors, and logger surfaces."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agentworks.ssh import SSHError, SSHTarget, run


def test_ssh_run_streams_input_without_logging_it() -> None:
    secret = "ssh-stdin-swordfish"

    with patch("agentworks.ssh.subprocess.run") as process:
        process.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=f"stored {secret}",
            stderr=f"diagnostic {secret}",
        )

        result = run(
            SSHTarget(host="vm-host"),
            "umask 077 && cat > /tmp/template.yaml",
            input_text=secret,
        )

    assert result.stdout == ""
    assert result.stderr == ""
    argv = process.call_args.args[0]
    assert secret not in repr(argv)
    assert process.call_args.kwargs["input"] == secret


def test_ssh_run_failure_diagnostic_omits_input() -> None:
    secret = "ssh-stdin-swordfish"

    with patch("agentworks.ssh.subprocess.run") as process:
        process.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr=f"remote echoed {secret}")

        with pytest.raises(SSHError) as caught:
            run(
                SSHTarget(host="vm-host"),
                "cat > /tmp/template.yaml",
                input_text=secret,
            )

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_ssh_run_check_false_discards_input_reflections() -> None:
    secret = "ssh-stdin-swordfish"

    with patch("agentworks.ssh.subprocess.run") as process:
        process.return_value = subprocess.CompletedProcess(
            [],
            1,
            stdout=f"stored {secret}",
            stderr=f"diagnostic {secret}",
        )

        result = run(
            SSHTarget(host="vm-host"),
            "cat > /tmp/template.yaml",
            input_text=secret,
            check=False,
        )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == ""


def test_ssh_run_rejects_logging_sensitive_input_before_subprocess() -> None:
    secret = "ssh-stdin-swordfish"

    with patch("agentworks.ssh.subprocess.run") as process, pytest.raises(ValueError) as caught:
        run(
            SSHTarget(host="vm-host"),
            "cat > /tmp/template.yaml",
            input_text=secret,
            logger=MagicMock(),
        )

    process.assert_not_called()
    assert secret not in str(caught.value)
