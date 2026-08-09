"""Remote-Lima create scrubs every secret-bearing traceback carrier."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.capabilities.vm_platform import lima as lima_mod
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.remote_exec import DetachedResult
from agentworks.ssh import SSHError, SSHLogger

_REMOTE_DIR = "/tmp/agentworks-lima-template.A1b2C3d4E5"


def _assert_secret_absent_from_agentworks_exception_graph(exc: BaseException, secret: str) -> None:
    pending = [exc]
    seen: set[int] = set()
    found_functions: set[str] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert secret not in repr(current)
        traceback = current.__traceback__
        while traceback is not None:
            module = str(traceback.tb_frame.f_globals.get("__name__", ""))
            if module.startswith("agentworks."):
                found_functions.add(traceback.tb_frame.f_code.co_name)
                assert secret not in repr(traceback.tb_frame.f_locals)
            traceback = traceback.tb_next
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    assert {"_create_remote", "_create_remote_sensitive"} <= found_functions


class _HostTransport:
    def __init__(self, logger: SSHLogger) -> None:
        self.logger: SSHLogger | None = logger


def _wire_remote_create(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    secret: str,
) -> tuple[list[SSHLogger], list[_HostTransport]]:
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)

    def _ssh_run(target: object, command: str, **kwargs: object) -> SimpleNamespace:
        del target
        if "mktemp -d" in command:
            return SimpleNamespace(stdout=f"{_REMOTE_DIR}\n")
        if "cat >" in command:
            assert kwargs["input_text"] == f"embedded: {secret}"
        return SimpleNamespace(returncode=0, stdout="", stderr="", ok=True)

    loggers: list[SSHLogger] = []
    hosts: list[_HostTransport] = []

    def _host_transport(self: LimaPlatform, logger: SSHLogger | None = None) -> _HostTransport:
        del self
        assert logger is not None
        loggers.append(logger)
        host = _HostTransport(logger)
        hosts.append(host)
        return host

    monkeypatch.setattr(lima_mod, "ssh_run", _ssh_run)
    monkeypatch.setattr(LimaPlatform, "_host_transport", _host_transport)
    return loggers, hosts


def test_remote_create_failure_scrubs_raw_result_bootstrap_logger_and_host_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agentworks import remote_exec

    secret = "remote-create-result-sentinel"
    loggers, hosts = _wire_remote_create(monkeypatch, tmp_path, secret)
    monkeypatch.setattr(
        remote_exec,
        "run_detached",
        lambda *args, **kwargs: DetachedResult(
            exit_code=1,
            output=f"{secret}\n##STEP## Provision\n##ERROR## reflected {secret}\n",
        ),
    )

    with pytest.raises(SSHError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "myvm",
            f"embedded: {secret}",
            redactions=(secret,),
        )

    assert str(caught.value).startswith("limactl create/start failed (exit 1)")
    assert loggers and all(logger._redact == () for logger in loggers)  # noqa: SLF001
    assert hosts and all(host.logger is None for host in hosts)
    _assert_secret_absent_from_agentworks_exception_graph(caught.value, secret)


@pytest.mark.parametrize(
    "control_flow",
    [KeyboardInterrupt("operator interrupt"), SystemExit(31), GeneratorExit()],
    ids=("keyboard-interrupt", "system-exit", "generator-exit"),
)
def test_remote_create_control_flow_scrubs_every_agentworks_frame_and_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    control_flow: BaseException,
) -> None:
    from agentworks import remote_exec

    secret = "remote-create-control-sentinel"
    loggers, hosts = _wire_remote_create(monkeypatch, tmp_path, secret)

    def _raise(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise control_flow

    monkeypatch.setattr(remote_exec, "run_detached", _raise)

    with pytest.raises(type(control_flow)) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._create_remote(
            "myvm",
            f"embedded: {secret}",
            redactions=(secret,),
        )

    assert caught.value is control_flow
    assert loggers and all(logger._redact == () for logger in loggers)  # noqa: SLF001
    assert hosts and all(host.logger is None for host in hosts)
    _assert_secret_absent_from_agentworks_exception_graph(caught.value, secret)
