"""Sensitive stdin stays out of SSH argv, errors, and logger surfaces."""

from __future__ import annotations

import inspect
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from agentworks.ssh import SSHError, SSHTarget, run

if TYPE_CHECKING:
    from types import FrameType


def _assert_agentworks_tracebacks_scrubbed(exc: BaseException, secret: str) -> set[str]:
    functions: set[str] = set()
    traceback = exc.__traceback__
    while traceback is not None:
        module = str(traceback.tb_frame.f_globals.get("__name__", ""))
        if module.startswith("agentworks."):
            functions.add(traceback.tb_frame.f_code.co_name)
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    return functions


def _complete_frame_functions(exc: BaseException) -> set[str]:
    functions: set[str] = set()
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            functions.add(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return functions


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
    assert "run" in _assert_agentworks_tracebacks_scrubbed(caught.value, secret)


@pytest.mark.parametrize(
    "failure",
    [KeyboardInterrupt("stop"), SystemExit(8), GeneratorExit()],
    ids=("keyboard-interrupt", "system-exit", "generator-exit"),
)
def test_ssh_run_scrubs_input_when_argv_building_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    from agentworks import ssh

    secret = "ssh-argv-build-sentinel"

    def fail_argv(*args: object, **kwargs: object) -> list[str]:
        del args, kwargs
        raise failure

    monkeypatch.setattr(ssh, "_ssh_base_args", fail_argv)
    with pytest.raises(type(failure)) as caught:
        run(SSHTarget(host="vm-host"), "cat > /tmp/key", input_text=secret)

    assert caught.value is failure
    assert "run" in _assert_agentworks_tracebacks_scrubbed(caught.value, secret)


def test_ssh_run_translates_sensitive_native_failure_without_exception_link() -> None:
    secret = "ssh-native-failure-swordfish"
    native_failure = OSError(f"write reflected {secret}")

    def native_run(*args: object, **kwargs: object) -> None:
        del args
        retained_input = kwargs["input"]
        assert retained_input == secret
        raise native_failure

    with patch("agentworks.ssh.subprocess.run", side_effect=native_run), pytest.raises(SSHError) as caught:
        run(
            SSHTarget(host="vm-host"),
            "cat > /tmp/template.yaml",
            input_text=secret,
        )

    assert str(caught.value) == "SSH stdin command could not be executed: cat > /tmp/template.yaml"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in repr(caught.value)
    assert "native_run" not in _complete_frame_functions(caught.value)


@pytest.mark.parametrize(
    "control_flow",
    [KeyboardInterrupt("operator interrupt"), SystemExit(9), GeneratorExit()],
    ids=("keyboard-interrupt", "system-exit", "generator-exit"),
)
def test_ssh_run_sensitive_control_flow_strips_native_graph_and_preserves_identity(
    control_flow: BaseException,
) -> None:
    secret = "ssh-interrupt-swordfish"

    def native_run(*args: object, **kwargs: object) -> None:
        del args
        retained_input = kwargs["input"]
        assert retained_input == secret
        raise control_flow

    with patch("agentworks.ssh.subprocess.run", side_effect=native_run), pytest.raises(type(control_flow)) as caught:
        run(
            SSHTarget(host="vm-host"),
            "cat > /tmp/template.yaml",
            input_text=secret,
        )

    assert caught.value is control_flow
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "native_run" not in _complete_frame_functions(caught.value)
    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name == "run" and traceback.tb_frame.f_globals.get("__name__") == (
            "agentworks.ssh"
        ):
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


def test_ssh_run_non_sensitive_native_error_keeps_identity_and_traceback() -> None:
    native_failure = OSError("native failure")

    def native_run(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise native_failure

    with patch("agentworks.ssh.subprocess.run", side_effect=native_run), pytest.raises(OSError) as caught:
        run(SSHTarget(host="vm-host"), "echo ok")

    assert caught.value is native_failure
    assert "native_run" in _complete_frame_functions(caught.value)


def test_exception_graph_detach_interrupt_scrubs_mutable_owner_and_preserves_identity() -> None:
    from agentworks import ssh

    secret = "detach-owner-interrupt-sentinel"
    native_failure = OSError("native failure")
    interruption = GeneratorExit()
    detach_line = next(
        line_number
        for line_number, line in enumerate(
            inspect.getsourcelines(ssh._SensitiveExceptionGraphCleanup.detach)[0],  # noqa: SLF001
            start=inspect.getsourcelines(ssh._SensitiveExceptionGraphCleanup.detach)[1],  # noqa: SLF001
        )
        if "self.current.__cause__ = None" in line
    )

    def native_run() -> None:
        retained_input = secret
        assert retained_input == secret
        raise native_failure

    def interrupt_after_detach(frame: FrameType, event: str, arg: object) -> object | None:
        del arg
        if event == "line" and frame.f_code.co_name == "detach" and frame.f_lineno == detach_line:
            sys.settrace(None)
            raise interruption
        return interrupt_after_detach

    try:
        native_run()
    except OSError as failure:
        sys.settrace(interrupt_after_detach)
        try:
            with pytest.raises(GeneratorExit) as caught:
                ssh._strip_sensitive_exception_graph(failure)  # noqa: SLF001
        finally:
            sys.settrace(None)

    assert caught.value is interruption
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "native_run" not in _complete_frame_functions(caught.value)
    traceback = caught.value.__traceback__
    found_detach = False
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name == "detach":
            found_detach = True
            cleanup = traceback.tb_frame.f_locals["self"]
            assert cleanup.pending == []
            assert cleanup.current is None
            assert cleanup.tracebacks == []
            assert cleanup.seen == set()
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    assert found_detach


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
