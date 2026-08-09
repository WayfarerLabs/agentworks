"""Lima Tailscale join values do not survive failure tracebacks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.ssh import SSHError

_REMOTE_JOIN_FRAMES = {
    ("agentworks.capabilities.vm_platform.lima", "create"),
    ("agentworks.capabilities.vm_platform.lima", "_create"),
    ("agentworks.capabilities.vm_platform.lima", "_join_tailscale_ephemerally"),
    ("agentworks.capabilities.vm_platform.lima", "_run_lima"),
    ("agentworks.ssh", "run"),
}


def _request(secret: str) -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="myvm",
        hostname="lima--myvm",
        system_slug=None,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        tailscale_auth_key=secret,
        cpus=4,
        memory_gib=8,
        disk_gib=50,
        swap_gib=4,
    )


def _assert_secret_absent_from_agentworks_exception_graph(exc: BaseException, secret: str) -> None:
    pending = [exc]
    seen: set[int] = set()
    found_frames: set[tuple[str, str]] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert secret not in repr(current)
        traceback = current.__traceback__
        while traceback is not None:
            module = str(traceback.tb_frame.f_globals.get("__name__", ""))
            function = traceback.tb_frame.f_code.co_name
            if module.startswith("agentworks."):
                found_frames.add((module, function))
                assert secret not in repr(traceback.tb_frame.f_locals)
            traceback = traceback.tb_next
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    assert found_frames >= _REMOTE_JOIN_FRAMES


def _wire_real_remote_join(
    monkeypatch: pytest.MonkeyPatch,
    run: object,
) -> list[str]:
    events: list[str] = []
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(
        LimaPlatform,
        "_create_remote",
        lambda self, name, yaml, *, redactions: events.append(f"create:{name}"),
    )
    monkeypatch.setattr("agentworks.ssh.subprocess.run", run)
    monkeypatch.setattr(
        LimaPlatform,
        "_cleanup_partial_create",
        lambda self, name: events.append(f"cleanup:{name}"),
    )
    return events


def test_remote_join_failure_scrubs_every_agentworks_traceback_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "tskey-remote-failure-sentinel"

    def _fail_join(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("input") == f"{secret}\n"
        return subprocess.CompletedProcess(args, 1, stdout=f"reflected {secret}", stderr=f"rejected {secret}")

    events = _wire_real_remote_join(monkeypatch, _fail_join)
    request = _request(secret)

    with pytest.raises(SSHError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}}).create(
            request,
            RunContext(),
        )

    assert str(caught.value).startswith("SSH stdin command failed (exit 1):")
    assert request.tailscale_auth_key is None
    assert events == ["create:myvm", "cleanup:myvm"]
    _assert_secret_absent_from_agentworks_exception_graph(caught.value, secret)


@pytest.mark.parametrize(
    "control_flow",
    [
        KeyboardInterrupt("operator interrupt"),
        SystemExit(7),
        GeneratorExit(),
    ],
    ids=("keyboard-interrupt", "system-exit", "generator-exit"),
)
def test_remote_join_control_flow_scrubs_every_agentworks_traceback_frame(
    monkeypatch: pytest.MonkeyPatch,
    control_flow: BaseException,
) -> None:
    secret = "tskey-remote-control-flow-sentinel"

    def _interrupt_join(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args
        assert kwargs["input"] == f"{secret}\n"
        raise control_flow

    events = _wire_real_remote_join(monkeypatch, _interrupt_join)
    request = _request(secret)

    with pytest.raises(type(control_flow)) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}}).create(
            request,
            RunContext(),
        )

    assert caught.value is control_flow
    assert request.tailscale_auth_key is None
    assert events == ["create:myvm", "cleanup:myvm"]
    _assert_secret_absent_from_agentworks_exception_graph(caught.value, secret)


def _traceback_functions(exc: BaseException) -> set[str]:
    functions: set[str] = set()
    traceback = exc.__traceback__
    while traceback is not None:
        functions.add(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    return functions


@pytest.mark.parametrize(
    "native_failure",
    [KeyboardInterrupt("stop"), SystemExit(12), GeneratorExit()],
    ids=("keyboard-interrupt", "system-exit", "generator-exit"),
)
def test_local_sensitive_lima_native_failure_strips_downstream_graph_and_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    native_failure: BaseException,
) -> None:
    secret = "local-lima-native-sentinel"

    def native_run(*args: object, **kwargs: object) -> None:
        del args
        retained_input = kwargs["input"]
        assert retained_input == secret
        raise native_failure

    monkeypatch.setattr(subprocess, "run", native_run)
    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})

    with pytest.raises(type(native_failure)) as caught:
        platform._run_lima("limactl create --tty=false -", input_text=secret)  # noqa: SLF001

    assert caught.value is native_failure
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "native_run" not in _traceback_functions(caught.value)
    traceback = caught.value.__traceback__
    while traceback is not None:
        module = str(traceback.tb_frame.f_globals.get("__name__", ""))
        if module.startswith("agentworks."):
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


def test_local_sensitive_lima_ordinary_native_failure_strips_graph_and_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "local-lima-ordinary-native-sentinel"
    native_failure = OSError("native failure")

    def native_run(*args: object, **kwargs: object) -> None:
        del args
        retained_input = kwargs["input"]
        assert retained_input == secret
        raise native_failure

    monkeypatch.setattr(subprocess, "run", native_run)
    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})

    with pytest.raises(OSError) as caught:
        platform._run_lima("limactl create --tty=false -", input_text=secret)  # noqa: SLF001

    assert caught.value is native_failure
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in repr(caught.value)
    assert "native_run" not in _traceback_functions(caught.value)


def test_local_non_sensitive_lima_native_error_keeps_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_failure = OSError("native failure")

    def native_run(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise native_failure

    monkeypatch.setattr(subprocess, "run", native_run)

    with pytest.raises(OSError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "local"}})._run_lima("limactl list")  # noqa: SLF001

    assert caught.value is native_failure
    assert "native_run" in _traceback_functions(caught.value)
