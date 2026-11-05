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

Backend seams (``LimaHost.run``, ``_create_local`` / ``_create_remote``)
are mocked as in test_lima_create_flow.py; no test runs limactl.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest, RetainedProvisioningError
from agentworks.capabilities.vm_platform.bootstrap_script import REBOOT_SENTINEL_PATH
from agentworks.capabilities.vm_platform.lima import _REBOOT_CLEAR_MARKER, LimaPlatform
from agentworks.capabilities.vm_platform.lima_host import LimaCommandFailed, LimaHost
from agentworks.capabilities.vm_platform.tailscale_join import JOIN_FRAGMENT
from agentworks.debian import DebianRelease
from agentworks.errors import ProvisioningError
from agentworks.execution.client import SSHClient
from tests._root_channel_fakes import Answer, FakeGuest, answer_by_payload, install_at_run_process
from tests.conftest import provisioning_trust_for_tests

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


class _FakeProcess:
    """What ``ssh_process.spawn`` returns: ``communicate`` answers, or raises ``failure``."""

    def __init__(
        self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"", failure: BaseException | None = None
    ):
        self.returncode = returncode
        self._streams = (stdout, stderr)
        self._failure = failure
        self.inputs: list[bytes | None] = []

    def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
        self.inputs.append(input)
        if self._failure is not None:
            raise self._failure
        return self._streams

    def kill(self) -> None:
        pass

    def wait(self) -> int:
        return self.returncode


def _spawn_returning(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> list[list[str]]:
    argvs: list[list[str]] = []

    def spawn(argv: list[str], **kwargs: object) -> _FakeProcess:
        argvs.append(list(argv))
        return process

    monkeypatch.setattr("agentworks.capabilities.vm_platform.lima_host.ssh_process.spawn", spawn)
    monkeypatch.setattr("agentworks.execution.resolve_ssh", lambda: SSHClient("/usr/bin/ssh", (9, 2)))
    return argvs


def _request(*, tailscale_auth_key: str = "tskey-test") -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="myvm",
        debian_release=DebianRelease.TRIXIE,
        hostname="lima--myvm",
        system_slug=None,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        provisioning_trust=provisioning_trust_for_tests("myvm"),
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
    guest: FakeGuest | None = None,
) -> list[str]:
    """Mock the backend seams; return the ``LimaHost.run`` commands issued.

    ``errors`` maps a command substring to the exception ``LimaHost.run``
    raises when it sees it (the command is still recorded first, so
    tests can pin exactly-once attempts). ``guest`` answers the root
    channel, ``limactl shell`` through ``root_script.run_process``.
    """
    install_at_run_process(monkeypatch, guest or FakeGuest())
    ran: list[str] = []

    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(LimaPlatform, "_create_local", lambda self, name, yaml: None)

    def _fake_run(self: LimaHost, cmd: str, **_kw: object) -> str:
        ran.append(cmd)
        for needle, exc in (errors or {}).items():
            if needle in cmd:
                raise exc
        if REBOOT_SENTINEL_PATH in cmd:
            return f"{_REBOOT_CLEAR_MARKER}\n"
        return ""

    monkeypatch.setattr(LimaHost, "run", _fake_run)
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

    def _fake_run(self: LimaHost, command: str, **kwargs: object) -> str:
        calls.append((command, kwargs))
        return ""

    monkeypatch.setattr(LimaHost, "run", _fake_run)

    LimaPlatform("lima", {})._create_local("myvm", _PROVIDER_YAML)

    assert calls == [
        ("limactl create --name myvm --tty=false -", {"input": _PROVIDER_YAML}),
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
    process = _FakeProcess(failure=failure)
    commands = _spawn_returning(monkeypatch, process)

    with pytest.raises(OSError) as caught:
        LimaPlatform("lima", {})._create_local("myvm", _PROVIDER_YAML)

    assert caught.value is failure
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert process.inputs == [_PROVIDER_YAML.encode()]
    assert commands == [["limactl", "create", "--name", "myvm", "--tty=false", "-"]]


def test_local_lima_sensitive_stdin_failure_omits_reflected_key(monkeypatch: pytest.MonkeyPatch) -> None:
    actual_auth_key = "tskey-local-stdin-sentinel"
    process = _FakeProcess(
        returncode=1, stdout=f"reflected {actual_auth_key}".encode(), stderr=f"rejected {actual_auth_key}".encode()
    )
    _spawn_returning(monkeypatch, process)

    with pytest.raises(LimaCommandFailed) as caught:
        LimaHost(None).run("limactl shell myvm cat", input=actual_auth_key)

    assert process.inputs == [actual_auth_key.encode()]
    _assert_secret_absent_from_exception_graph(caught.value, actual_auth_key)


def test_remote_lima_failure_omits_sensitive_input_and_raw_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_auth_key = "tskey-remote-stdin-sentinel"
    process = _FakeProcess(
        returncode=1, stdout=f"reflected {actual_auth_key}".encode(), stderr=f"rejected {actual_auth_key}".encode()
    )
    argvs = _spawn_returning(monkeypatch, process)

    with pytest.raises(LimaCommandFailed) as caught:
        LimaHost("user@host").run("limactl shell myvm cat", input=actual_auth_key)

    assert process.inputs == [actual_auth_key.encode()]
    assert not any(actual_auth_key in argument for argument in argvs[0])
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
        lambda self, name, yaml: (_ for _ in ()).throw(LimaCommandFailed("provision exploded")),
    )

    with pytest.raises(LimaCommandFailed, match="provision exploded"):
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


def test_interrupt_during_post_start_steps_cleans_up_too(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The rollback spans the FULL create, not just limactl create/start:
    a Ctrl-C in the post-start Tailscale IP read still tears down."""
    interrupt = KeyboardInterrupt("first")
    ran = _wire(monkeypatch, guest=FakeGuest(answer_by_payload(failures={"tailscale ip -4": [interrupt]})))

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
    guest = FakeGuest(answer_by_payload(failures={JOIN_FRAGMENT: [interrupt]}))
    ran = _wire(monkeypatch, guest=guest)

    with pytest.raises(KeyboardInterrupt) as caught:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(
            _request(tailscale_auth_key=secret),
            RunContext(),
        )

    assert caught.value is interrupt
    assert guest.joins == 1
    assert _deletes(ran) == ["limactl delete --force myvm"]
    assert secret not in repr((ran, guest.argvs))
    assert secret not in "\n".join([*captured_output.detail, *captured_output.warnings])


def test_ephemeral_tailscale_join_failure_cleans_up_without_key_in_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "tskey-failure-sentinel"
    guest = FakeGuest(answer_by_payload(answers={JOIN_FRAGMENT: Answer(status=1)}))
    ran = _wire(monkeypatch, guest=guest)

    with pytest.raises(ProvisioningError) as caught:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(
            _request(tailscale_auth_key=secret),
            RunContext(),
        )

    assert guest.payloads[-1] == JOIN_FRAGMENT
    assert guest.joins == 1
    assert _deletes(ran) == ["limactl delete --force myvm"]
    _assert_secret_absent_from_exception_graph(caught.value, secret)
    assert secret not in repr((ran, guest.argvs))


def test_second_interrupt_abandons_cleanup_and_retains_the_row(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A second Ctrl-C during the cleanup abandons it instead of
    wedging: the delete is attempted exactly once, the warning names the
    exact removal command, and the instance is handed to core so the row
    survives for ``vm delete``."""
    interrupt = KeyboardInterrupt("first")
    ran = _wire(monkeypatch, errors={"limactl delete": KeyboardInterrupt("second")})
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: (_ for _ in ()).throw(interrupt),
    )

    with pytest.raises(RetainedProvisioningError) as exc:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert exc.value.platform_metadata == {"instance_name": "myvm"}
    assert len(_deletes(ran)) == 1
    (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
    assert "'limactl delete --force myvm'" in abandoned
