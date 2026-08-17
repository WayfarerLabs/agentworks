"""Lima ``create`` rollback on failure and interrupt (#340).

``create`` runs ``limactl create`` + ``limactl start`` (a minutes-long
window where a Ctrl-C is likeliest) and then post-start steps (the
restart-sentinel probe, the Tailscale IP read). The caller
(``create_vm``) deletes only the DB row on failure or interrupt, so an
instance left behind would be orphaned with nothing left to target it.
``create`` therefore tears down the instance it made (``limactl delete
--force``, the delete op's exact command via the shared
``_delete_instance``) on plain failure AND on interrupt, re-raising
the original; a SECOND interrupt during the cleanup abandons it loudly,
naming the removal command. The azure precedent is
test_azure_create_interrupt.py (#338).

Backend seams (``_run_lima``, ``_create_local`` / ``_create_remote``)
are mocked as in test_lima_create_flow.py; no test runs limactl.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.capabilities.vm_platform.bootstrap_script import REBOOT_SENTINEL_PATH
from agentworks.capabilities.vm_platform.lima import _REBOOT_CLEAR_MARKER, LimaPlatform
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


_REMOTE_TEMPLATE_DIR = "/tmp/agentworks-lima-template.A1b2C3d4E5"
_PROVIDER_YAML = "arch: default\nmounts: []\n"


def _assert_secret_absent_from_exception_graph(
    exc: BaseException,
    secret: str,
) -> None:
    """Inspect rendered linked exception objects, including groups."""
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert secret not in repr(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)


def _remote_ssh_success(command: str) -> SimpleNamespace:
    stdout = f"{_REMOTE_TEMPLATE_DIR}\n" if "mktemp -d" in command else ""
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="", ok=True)


def _request(*, tailscale_auth_key: str = "tskey-test") -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="myvm",
        hostname="lima--myvm",
        system_slug=None,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        tailscale_auth_key=tailscale_auth_key,
        progress=MagicMock(),
        # The vm-template layer's resolved defaults, which is the only
        # shape a platform ever sees (the hardware fields are required).
        cpus=4,
        memory_gib=8,
        disk_gib=50,
        swap_gib=4,
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    errors: dict[str, BaseException] | None = None,
) -> list[str]:
    """Mock the backend seams; return the ``_run_lima`` commands issued.

    ``errors`` maps a command substring to the exception ``_run_lima``
    raises when it sees it (the command is still recorded first, so
    tests can pin exactly-once attempts).
    """
    ran: list[str] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(LimaPlatform, "_create_local", lambda self, name, yaml: None)
    monkeypatch.setattr(LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace())

    def _fake_run(self: LimaPlatform, cmd: str, **_kw: object) -> str:
        ran.append(cmd)
        for needle, exc in (errors or {}).items():
            if needle in cmd:
                raise exc
        if REBOOT_SENTINEL_PATH in cmd:
            return f"{_REBOOT_CLEAR_MARKER}\n"
        if "tailscale ip" in cmd:
            return "100.64.0.1"
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)
    return ran


def _deletes(ran: list[str]) -> list[str]:
    return [cmd for cmd in ran if cmd.startswith("limactl delete")]


def _forbid_persistent_tempfiles(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("persistent local template file was attempted")

    monkeypatch.setattr("tempfile.NamedTemporaryFile", _forbidden)


def test_local_create_streams_template_without_persistent_file(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    _forbid_persistent_tempfiles(monkeypatch)

    def _fake_run(self: LimaPlatform, command: str, **kwargs: object) -> str:
        calls.append((command, kwargs))
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run)

    LimaPlatform("lima", {})._create_local("myvm", _PROVIDER_YAML)

    assert calls == [
        ("limactl create --name myvm --tty=false -", {"input_text": _PROVIDER_YAML}),
        ("limactl start myvm", {}),
    ]


@pytest.mark.parametrize(
    "failure",
    [
        BrokenPipeError("stdin write failed"),
        OSError("stdin flush failed"),
        OSError("stdin pre-close failed"),
    ],
    ids=("write", "flush", "pre-close"),
)
def test_local_stdin_io_failure_refuses_start_without_persistent_file(
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    commands: list[list[str]] = []

    def _fail_stdin(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        assert kwargs["input"] == _PROVIDER_YAML
        raise failure

    monkeypatch.setattr("subprocess.run", _fail_stdin)

    with pytest.raises(OSError) as caught:
        LimaPlatform("lima", {})._create_local("myvm", _PROVIDER_YAML)

    assert caught.value is failure
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert commands == [["limactl", "create", "--name", "myvm", "--tty=false", "-"]]


def test_local_lima_sensitive_stdin_failure_omits_reflected_key(monkeypatch: pytest.MonkeyPatch) -> None:
    actual_auth_key = "tskey-local-stdin-sentinel"

    def _reflect_stdin(*args: object, **kwargs: object) -> SimpleNamespace:
        del args
        assert kwargs["input"] == actual_auth_key
        return SimpleNamespace(
            returncode=1,
            stdout=f"reflected {actual_auth_key}",
            stderr=f"rejected {actual_auth_key}",
        )

    monkeypatch.setattr("subprocess.run", _reflect_stdin)

    with pytest.raises(SSHError) as caught:
        LimaPlatform("lima", {})._run_lima(
            "limactl shell myvm cat",
            input_text=actual_auth_key,
        )

    assert str(caught.value) == "limactl stdin command failed (exit 1): limactl shell myvm cat"
    _assert_secret_absent_from_exception_graph(caught.value, actual_auth_key)


def test_remote_lima_failure_omits_sensitive_input_and_raw_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_auth_key = "tskey-remote-stdin-sentinel"

    def _reflect_stdin(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["input"] == actual_auth_key
        return subprocess.CompletedProcess(
            args[0],
            1,
            stdout=f"reflected {actual_auth_key}",
            stderr=f"rejected {actual_auth_key}",
        )

    monkeypatch.setattr("agentworks.ssh.subprocess.run", _reflect_stdin)

    with pytest.raises(SSHError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "ssh", "host": "user@host"}})._run_lima(
            "limactl shell myvm cat",
            input_text=actual_auth_key,
        )

    assert str(caught.value) == "SSH stdin command failed (exit 1): limactl shell myvm cat"
    _assert_secret_absent_from_exception_graph(caught.value, actual_auth_key)


def test_failure_mid_create_cleans_up_and_reraises(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A plain backend failure (here: limactl start) tears down the
    instance and re-raises unwrapped, per lima's error convention; the
    interrupt messaging never appears."""
    ran = _wire(monkeypatch)
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: (_ for _ in ()).throw(SSHError("provision exploded")),
    )

    with pytest.raises(SSHError):
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert _deletes(ran) == ["limactl delete --force myvm"]
    assert not any("Interrupted" in w for w in captured_output.warnings)
    assert not any("Cleanup abandoned" in w for w in captured_output.warnings)


def test_interrupt_during_start_cleans_up_and_reraises_the_original(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The likeliest scenario: Ctrl-C during the minutes-long limactl
    start. The rollback deletes the instance and the ORIGINAL interrupt
    propagates for the caller's row unwind (identity pin)."""
    ran = _wire(monkeypatch)
    interrupt = KeyboardInterrupt("first")
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: (_ for _ in ()).throw(interrupt),
    )

    with pytest.raises(KeyboardInterrupt) as exc:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert exc.value is interrupt
    assert _deletes(ran) == ["limactl delete --force myvm"]
    assert any("Ctrl-C again to abandon" in w for w in captured_output.warnings)


def test_interrupt_during_post_start_steps_cleans_up_too(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The rollback spans the FULL create, not just limactl create/start:
    a Ctrl-C in the post-start Tailscale IP read still tears down."""
    interrupt = KeyboardInterrupt("first")
    ran = _wire(monkeypatch, errors={"tailscale ip": interrupt})

    with pytest.raises(KeyboardInterrupt) as exc:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert exc.value is interrupt
    assert _deletes(ran) == ["limactl delete --force myvm"]


def test_interrupt_during_ephemeral_tailscale_join_cleans_up_and_does_not_render_key(
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    secret = "tskey-interrupt-sentinel"
    interrupt = KeyboardInterrupt("first")
    ran = _wire(monkeypatch, errors={"tailscale up --auth-key": interrupt})

    with pytest.raises(KeyboardInterrupt) as caught:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(
            _request(tailscale_auth_key=secret),
            RunContext(),
        )

    assert caught.value is interrupt
    assert _deletes(ran) == ["limactl delete --force myvm"]
    assert secret not in repr(ran)
    assert secret not in "\n".join([*captured_output.detail, *captured_output.warnings])


def test_ephemeral_tailscale_join_failure_cleans_up_without_key_in_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "tskey-failure-sentinel"
    failure = SSHError("fixed command failed")
    ran = _wire(monkeypatch, errors={"tailscale up --auth-key": failure})

    with pytest.raises(SSHError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(
            _request(tailscale_auth_key=secret),
            RunContext(),
        )

    assert caught.value is failure
    assert _deletes(ran) == ["limactl delete --force myvm"]
    assert secret not in repr(caught.value)
    assert secret not in repr(ran)


def test_second_interrupt_abandons_cleanup_loudly(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A second Ctrl-C during the cleanup abandons it instead of
    wedging: the delete is attempted exactly once, the warning names the
    exact removal command, and the ORIGINAL interrupt still propagates."""
    interrupt = KeyboardInterrupt("first")
    ran = _wire(monkeypatch, errors={"limactl delete": KeyboardInterrupt("second")})
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: (_ for _ in ()).throw(interrupt),
    )

    with pytest.raises(KeyboardInterrupt) as exc:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert exc.value is interrupt
    assert len(_deletes(ran)) == 1
    (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
    assert "'limactl delete --force myvm'" in abandoned
