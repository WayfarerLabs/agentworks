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
            stdout=f"stored {secret}".encode(),
            stderr=f"diagnostic {secret}".encode(),
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
    # An input_text write keeps stdin open (no -n) and never forces a pty
    # (-tt corrupts the byte-exact payload); it uses neither flag.
    assert "-n" not in argv
    assert "-tt" not in argv
    # Byte-exact delivery: the payload crosses the pipe with no newline rewriting.
    assert process.call_args.kwargs["input"] == secret.encode()


def test_ssh_run_plain_closes_stdin_with_dash_n_and_no_tt() -> None:
    """A plain ``ssh_run`` (no ``input_text``, no forced TTY) closes stdin with
    ssh's own ``-n``: a stdin-reading remote command then cannot hang and ssh
    cannot pull from the operator's console. This mirrors ``SSHTransport.run``'s
    default, extending it to the bare-target primitive (``_run_lima``'s remote
    Lima hop). ``-tt`` is never forced by default."""
    with patch("agentworks.ssh.subprocess.run") as process:
        process.return_value = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        run(SSHTarget(host="vm-host"), "limactl list --json")

    argv = process.call_args.args[0]
    assert "-n" in argv
    assert "-tt" not in argv
    # -n closes stdin, so no payload is written.
    assert process.call_args.kwargs["input"] is None


def test_ssh_run_forced_tty_uses_tt_and_omits_dash_n() -> None:
    """A forced TTY (``SSHTarget.force_tty=True``) allocates a pty with ``-tt``.
    ``-n`` and ``-tt`` are mutually exclusive: ``-tt`` already attaches stdin to
    the pty, so the stdin-closing ``-n`` must not appear."""
    with patch("agentworks.ssh.subprocess.run") as process:
        process.return_value = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        run(SSHTarget(host="vm-host", force_tty=True), "echo hi")

    argv = process.call_args.args[0]
    assert "-tt" in argv
    assert "-n" not in argv


def test_ssh_run_failure_diagnostic_omits_input() -> None:
    secret = "ssh-stdin-swordfish"

    with patch("agentworks.ssh.subprocess.run") as process:
        process.return_value = subprocess.CompletedProcess([], 1, stdout=b"", stderr=f"remote echoed {secret}".encode())

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
            stdout=f"stored {secret}".encode(),
            stderr=f"diagnostic {secret}".encode(),
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


def test_ssh_run_rejects_a_forced_tty_before_subprocess() -> None:
    secret = "ssh-stdin-swordfish"

    with patch("agentworks.ssh.subprocess.run") as process, pytest.raises(ValueError) as caught:
        run(
            SSHTarget(host="vm-host", force_tty=True),
            "read -r token",
            input_text=secret,
        )

    process.assert_not_called()
    assert secret not in str(caught.value)


def test_transport_stdin_allocates_no_tty_and_leaves_stdin_open() -> None:
    """The production path. ``join_tailscale_ephemerally`` sends the
    tailnet auth key over stdin with no ``tty`` override, so the stdin
    branch must neither force a TTY (``-tt`` would corrupt the byte-exact
    payload) nor close stdin (``-n`` would starve the remote reader). It
    goes through ``ssh.run``, which builds argv without either flag.
    """
    from agentworks.transports.ssh import SSHTransport

    secret = "ssh-stdin-swordfish"

    with patch("agentworks.ssh.subprocess.run") as process:
        process.return_value = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        SSHTransport("vm-host").run("read -r token", input_text=secret)

    argv = process.call_args.args[0]
    assert "-tt" not in argv
    assert "-n" not in argv
    assert process.call_args.kwargs["input"] == secret.encode()


def test_transport_stdin_still_refuses_an_explicit_tty() -> None:
    """Overriding the default on purpose is a contradiction, not a default."""
    from agentworks.transports.ssh import SSHTransport

    with patch("agentworks.ssh.subprocess.run") as process, pytest.raises(ValueError):
        SSHTransport("vm-host").run("read -r token", input_text="s", tty=True)

    process.assert_not_called()


def test_ssh_run_translates_sensitive_native_failure_without_exception_link() -> None:
    secret = "ssh-native-failure-swordfish"
    native_failure = OSError(f"write reflected {secret}")

    with patch("agentworks.ssh.subprocess.run", side_effect=native_failure), pytest.raises(SSHError) as caught:
        run(
            SSHTarget(host="vm-host"),
            "cat > /tmp/template.yaml",
            input_text=secret,
        )

    assert str(caught.value) == "SSH stdin command could not be executed: cat > /tmp/template.yaml"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in repr(caught.value)


def test_ssh_run_sensitive_timeout_drops_partial_output_and_native_exception() -> None:
    secret = "ssh-timeout-swordfish"
    timeout = subprocess.TimeoutExpired(
        ["ssh", "vm-host"],
        5,
        output=f"partial {secret}",
        stderr=f"diagnostic {secret}",
    )

    with patch("agentworks.ssh.subprocess.run", side_effect=timeout), pytest.raises(SSHError) as caught:
        run(
            SSHTarget(host="vm-host"),
            "cat > /tmp/template.yaml",
            input_text=secret,
            timeout=5,
            retries=1,
        )

    assert str(caught.value) == "SSH command timed out after 1 attempts (5s each): cat > /tmp/template.yaml"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in repr(caught.value)


def test_ssh_run_non_sensitive_timeout_retries_logs_and_chains_last_timeout() -> None:
    timeout = subprocess.TimeoutExpired(["ssh", "vm-host"], 5)
    logger = MagicMock()
    retried: list[tuple[int, int]] = []

    with patch("agentworks.ssh.subprocess.run", side_effect=timeout), pytest.raises(SSHError) as caught:
        run(
            SSHTarget(host="vm-host"),
            "echo ok",
            timeout=5,
            retries=2,
            logger=logger,
            on_retry=lambda attempt, retries: retried.append((attempt, retries)),
        )

    assert caught.value.__cause__ is timeout
    assert retried == [(1, 2)]
    assert logger.log_timeout.call_args_list == [
        (("echo ok", 1, 2),),
        (("echo ok", 2, 2),),
    ]
