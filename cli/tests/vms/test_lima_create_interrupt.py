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

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.capabilities.vm_platform import lima as lima_mod
from agentworks.capabilities.vm_platform.bootstrap_script import REBOOT_SENTINEL_PATH
from agentworks.capabilities.vm_platform.lima import _REBOOT_CLEAR_MARKER, LimaPlatform
from agentworks.errors import StateError
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


def _request() -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="myvm",
        hostname="lima--myvm",
        system_slug=None,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        tailscale_auth_key="tskey-test",
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

    with pytest.raises(SSHError, match="provision exploded"):
        LimaPlatform("lima", {}).create(_request(), RunContext())

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
        LimaPlatform("lima", {}).create(_request(), RunContext())

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
        LimaPlatform("lima", {}).create(_request(), RunContext())

    assert exc.value is interrupt
    assert _deletes(ran) == ["limactl delete --force myvm"]


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
        LimaPlatform("lima", {}).create(_request(), RunContext())

    assert exc.value is interrupt
    assert len(_deletes(ran)) == 1
    (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
    assert "'limactl delete --force myvm'" in abandoned


class _FakeHostTransport:
    """Stands in for the vm_host SSHTransport (run_detached's target and
    the rollback's kill target). Records every call into the shared
    event log; every ``run`` reports rc 1 (so run_detached sees no
    prior pid/status files and starts fresh; its launch calls are all
    ``check=False``)."""

    def __init__(self, events: list[tuple[str, str]]) -> None:
        self._events = events

    def write_file(self, path: str, content: str) -> None:
        self._events.append(("host-write", path))

    def run(self, cmd: str, **_kw: object) -> SimpleNamespace:
        self._events.append(("host", cmd))
        return SimpleNamespace(returncode=1, stdout="", stderr="", ok=False)


def _wire_remote_host(monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, str]]) -> None:
    """Point the vm_host transport seam at the recording fake (the
    rollback's kill must never open a real SSH connection in tests)."""
    monkeypatch.setattr(
        LimaPlatform,
        "_host_transport",
        lambda self, logger=None: _FakeHostTransport(events),
    )


def test_remote_abandon_warning_names_the_vm_host(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """On a remote-Lima site the manual removal runs on the vm_host, so
    the abandon warning says where."""
    interrupt = KeyboardInterrupt("first")
    _wire(monkeypatch, errors={"limactl delete": KeyboardInterrupt("second")})
    _wire_remote_host(monkeypatch, [])
    monkeypatch.setattr(
        LimaPlatform,
        "_create_remote",
        lambda self, name, yaml: (_ for _ in ()).throw(interrupt),
    )

    with pytest.raises(KeyboardInterrupt) as exc:
        LimaPlatform("lima", {"vm_host": "user@host"}).create(_request(), RunContext())

    assert exc.value is interrupt
    (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
    assert "on 'user@host'" in abandoned


def test_remote_interrupt_kills_the_detached_limactl_before_deleting(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The real remote path (#340 review): _create_remote drives limactl
    via run_detached, which nohups the remote command precisely so it
    SURVIVES this process; a Ctrl-C during the poll sleep stops nothing
    on the vm_host. The rollback must therefore kill the detached
    process (via its PID file, run_detached's own mechanism) BEFORE
    running `limactl delete`, or the delete races a create/start still
    mutating the same instance. Exercises the real _create_remote /
    run_detached / _poll_until_done code, faking only the transport,
    logger, and sleep seams as the remote_exec tests do."""
    import time as real_time

    from agentworks import remote_exec

    events: list[tuple[str, str]] = []
    _wire_remote_host(monkeypatch, events)

    # Seams around _create_remote's edges: the local-to-host template
    # copy, the host-side rm of it, and the SSH log file.
    monkeypatch.setattr(lima_mod, "copy_to", lambda target, src, dst: None)
    monkeypatch.setattr(
        lima_mod, "ssh_run", lambda target, cmd, **kw: events.append(("ssh", cmd)) or SimpleNamespace(stdout="")
    )

    class _StubLogger:
        path = "/dev/null"

        def __init__(self, *a: object, **kw: object) -> None:
            pass

        def log_error(self, msg: str) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("agentworks.ssh.SSHLogger", _StubLogger)

    # The interrupt lands in _poll_until_done's sleep (run_detached's
    # 0.5s launch pause passes; the poll-interval sleep raises).
    interrupt = KeyboardInterrupt("first")

    def _sleep(secs: float) -> None:
        if secs >= 1:
            raise interrupt

    monkeypatch.setattr(remote_exec, "time", SimpleNamespace(sleep=_sleep, monotonic=real_time.monotonic))

    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)

    def _fake_run_lima(self: LimaPlatform, cmd: str, **_kw: object) -> str:
        events.append(("lima", cmd))
        return ""

    monkeypatch.setattr(LimaPlatform, "_run_lima", _fake_run_lima)

    with pytest.raises(KeyboardInterrupt) as exc:
        LimaPlatform("lima", {"vm_host": "user@host"}).create(_request(), RunContext())

    assert exc.value is interrupt
    # The kill goes through run_detached's PID-file mechanism, and it
    # PRECEDES the delete.
    kill_cmd = "kill $(cat /tmp/agentworks-lima-myvm.pid)"
    (kill_index,) = [i for i, (kind, cmd) in enumerate(events) if kind == "host" and kill_cmd in cmd]
    (delete_index,) = [
        i for i, (kind, cmd) in enumerate(events) if kind == "lima" and cmd == "limactl delete --force myvm"
    ]
    assert kill_index < delete_index
    # The interrupt no longer skips the remote template cleanup (the
    # widened finally): the host-side rm of the copied YAML still runs.
    assert ("ssh", "rm -f /tmp/agentworks-myvm.yaml") in events
    assert any("Ctrl-C again to abandon" in w for w in captured_output.warnings)


def test_cleanup_failure_warns_and_does_not_mask_the_original(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The rollback is best-effort: a broken teardown warns with the
    manual removal command and the ORIGINAL failure still propagates."""
    ran = _wire(monkeypatch, errors={"limactl delete": SSHError("cleanup broke")})
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: (_ for _ in ()).throw(SSHError("original failure")),
    )

    with pytest.raises(SSHError, match="original failure"):
        LimaPlatform("lima", {}).create(_request(), RunContext())

    assert len(_deletes(ran)) == 1
    warned = "\n".join(captured_output.warnings)
    assert "could not clean up the partial Lima instance 'myvm'" in warned
    assert "'limactl delete --force myvm'" in warned


def test_pre_mutation_failure_makes_no_cleanup_calls(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A failure before anything is created (the name-collision
    pre-flight) must not fire any teardown: there is nothing of ours to
    delete, and the colliding instance is NOT ours to touch."""
    ran = _wire(monkeypatch)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: True)

    with pytest.raises(StateError, match="already exists"):
        LimaPlatform("lima", {}).create(_request(), RunContext())

    assert _deletes(ran) == []
    # The cleanup announcement goes to the detail stream; its absence
    # there pins that neither rollback arm even started.
    assert not any("Cleaning up" in d for d in captured_output.detail)


def test_delete_op_issues_the_shared_teardown_command(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """delete() is unchanged by the dedup: exactly the forced delete the
    rollback also uses."""
    ran = _wire(monkeypatch)
    vm = SimpleNamespace(name="myvm", platform_metadata={"instance_name": "myvm"})

    LimaPlatform("lima", {}).delete(vm, RunContext())  # type: ignore[arg-type]

    assert ran == ["limactl delete --force myvm"]
