"""``agw vm recover``: the boundary, the refusals, host-key re-establishment, the repair, the address and the wait.

Real config, registry, resolver and backend loop (env-var backend); the
platform's status read and its root channel (a fake guest) are the fakes, and
so is the final wait.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform import tailscale_join
from agentworks.db import DesiredRuntimeState, InitStatus, ProvisioningStatus, VMStatus
from agentworks.db.instance_state import AppliedStateKey
from agentworks.errors import ConnectivityError, StateError, UserAbort, ValidationError
from agentworks.execution import reachability, trust
from agentworks.execution.root_script import RawRun
from agentworks.plugins.proxmox.api import ProxmoxAPIError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.ssh_identity import VerifiedSSHIdentity
from agentworks.vms import manager as vm_manager
from agentworks.vms.applied_state import VMSSHIdentityState, compare_vm_ssh_identity, encode_ssh_identity
from agentworks.vms.manager.boundary import require_vm_ssh_boundary
from tests._root_channel_fakes import ADDRESS, Answer, FakeGuest
from tests.conftest import seed_vm_trust
from tests.ssh_fixtures import fake_host_key

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import Database
    from tests.conftest import CapturedOutput
    from tests.vm_access_support import AccessRecorder

KEY = "tskey-recover"
NEW_ADDRESS = "100.64.0.77"
HOST_KEY = fake_host_key()


@pytest.fixture(autouse=True)
def _world(monkeypatch: pytest.MonkeyPatch, vm_access_recorder: AccessRecorder) -> list[str]:
    """The workstation is on Tailscale, the SSH config is untouched, retries do not sleep, the key is in the env."""
    synced: list[str] = []
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", KEY)
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda config, db, **kwargs: synced.append("sync"))
    monkeypatch.setattr("agentworks.capabilities.vm_platform.tailscale_join.time.sleep", lambda seconds: None)
    return synced


def _seed(db: Database, *, record: bool = True) -> None:
    db.insert_vm("box", site="proxmox", hostname="box-node")
    db.update_vm_tailscale("box", ADDRESS)
    db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
    if record:
        seed_vm_trust("box")


def _is_repair(payload: str) -> bool:
    return "BackendState" in payload


def _guest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: VMStatus = VMStatus.RUNNING,
    repair: Answer | None = None,
    address: Answer | None = None,
    events: list[str] | None = None,
) -> FakeGuest:
    repair = repair if repair is not None else Answer()
    address = address if address is not None else Answer(stdout=f"{NEW_ADDRESS}\n".encode())
    host_keys = Answer(stdout=f"{HOST_KEY.key_type} {HOST_KEY.blob} root@box\n".encode())

    def respond(payload: str, seen: int) -> Answer:
        if _is_repair(payload):
            return repair
        if payload == "tailscale ip -4":
            return address
        if payload.startswith("cat /etc/ssh/"):
            return host_keys
        return Answer()

    guest = FakeGuest(respond=respond, events=events)
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: status)
    monkeypatch.setattr(
        ProxmoxPlatform, "run_as_root", lambda self, vm, ctx, script, deadline: guest.run_as_root(script, deadline)
    )
    return guest


def _waits(monkeypatch: pytest.MonkeyPatch, *, answers: bool = True) -> list[reachability.Wording]:
    waits: list[reachability.Wording] = []

    def _wait(access: object, *, wording: reachability.Wording) -> bool:
        waits.append(wording)
        return answers

    monkeypatch.setattr(reachability, "wait", _wait)
    return waits


def _recording_hold(events: list[str]) -> Callable[..., AbstractContextManager[None]]:
    @contextlib.contextmanager
    def hold(platform: object, vm: object, *, config: object = None) -> Iterator[None]:
        events.append("hold-open")
        try:
            yield
        finally:
            events.append("hold-close")

    return hold


def _recover(db: Database, config: Config) -> None:
    vm_manager.recover_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)


def test_a_healthy_node_is_repaired_read_and_verified_with_the_key_resolved_once(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """One boundary burst holds the auth key beside the site's secret; the key travels in the repair script alone;
    the address is read, recorded when it changed, and verified once."""
    _seed(db)
    guest = _guest(monkeypatch)
    waits = _waits(monkeypatch)

    _recover(db, make_config())

    assert [sorted(names) for names in resolve_counter] == [["proxmox-token", "tailscale-auth-key"]]
    assert [_is_repair(payload) for payload in guest.payloads] == [True, False]
    assert guest.payloads[1] == "tailscale ip -4"
    assert [KEY.encode() in script for script in guest.scripts] == [True, False]
    assert waits == [reachability.Wording.VERIFY]
    row = db.get_vm("box")
    assert row is not None and row.tailscale_host == NEW_ADDRESS


def test_a_failed_initialization_warns_once(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Recover exists to repair such a VM, so it goes on, and the recheck under the guard does not warn again."""
    _seed(db)
    db.update_vm_init_status("box", InitStatus.FAILED)
    guest = _guest(monkeypatch)
    _waits(monkeypatch)

    _recover(db, make_config())

    assert len(captured_output.warnings) == 1
    assert [_is_repair(payload) for payload in guest.payloads] == [True, False]


def test_a_vm_whose_provisioning_changed_before_the_guard_is_refused(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row read under the guard decides: a VM whose provisioning failed meanwhile is not repaired."""
    from agentworks.harness_setup import locking

    _seed(db)
    guest = _guest(monkeypatch)
    real_guard = locking.native_mutation_guard

    @contextlib.contextmanager
    def guard_after_a_change(*args: object, **kwargs: object) -> Iterator[object]:
        db.update_vm_provisioning_status("box", ProvisioningStatus.FAILED)
        with real_guard(*args, **kwargs) as held:  # type: ignore[arg-type]
            yield held

    monkeypatch.setattr(locking, "native_mutation_guard", guard_after_a_change)

    with pytest.raises(StateError) as raised:
        _recover(db, make_config())

    assert raised.value.entity_name == "box"
    assert guest.payloads == []


def test_a_vm_replaced_before_the_guard_is_refused(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VM deleted and recreated under the same name before the guard is not the VM recover selected."""
    from agentworks.harness_setup import locking

    _seed(db)
    guest = _guest(monkeypatch)
    real_guard = locking.native_mutation_guard
    replacements = []

    @contextlib.contextmanager
    def guard_after_a_replacement(*args: object, **kwargs: object) -> Iterator[object]:
        db.delete_vm("box")
        _seed(db)
        replacements.append(db.get_vm("box"))
        with real_guard(*args, **kwargs) as held:  # type: ignore[arg-type]
            yield held

    monkeypatch.setattr(locking, "native_mutation_guard", guard_after_a_replacement)

    with pytest.raises(StateError) as raised:
        _recover(db, make_config())

    assert raised.value.entity_name == "box"
    assert resolve_counter == []
    assert guest.payloads == []
    assert db.get_vm("box") == replacements[0]


def test_the_ssh_config_is_synced_even_when_the_address_did_not_change(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    _world: list[str],
) -> None:
    """The aliases name the configured key, which the repair just installed: after a key change it is a new one."""
    _seed(db)
    _guest(monkeypatch, address=Answer(stdout=f"{ADDRESS}\n".encode()))
    _waits(monkeypatch)

    _recover(db, make_config())

    assert _world == ["sync"]


@pytest.mark.parametrize("agents", [[], ["coder", "reviewer"]], ids=["no-agents", "agents"])
def test_a_recovered_vm_s_agents_are_named_as_keeping_the_previous_key(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    agents: list[str],
) -> None:
    """The repair rewrites only the admin's keys, so after a key change the agents need their own reinit."""
    _seed(db)
    for agent in agents:
        db.insert_agent(agent, "box", f"agt-{agent}")
    _guest(monkeypatch)
    _waits(monkeypatch)

    _recover(db, make_config())

    # The note is the one line naming the agents; with none, no line names one.
    noted = [line for line in captured_output.info if any(agent in line for agent in ("coder", "reviewer"))]
    assert len(noted) == (1 if agents else 0)
    assert all(agent in noted[0] for agent in agents)


def test_the_operation_log_never_holds_the_key(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    tmp_path: Path,
) -> None:
    from agentworks import ssh

    # The suite's log directory is per worker, so this test's log needs one of its own.
    monkeypatch.setattr(ssh, "LOG_DIR", tmp_path / "logs")
    _seed(db)
    _guest(monkeypatch, repair=Answer(stdout=f"echoed {KEY}\njoined\n".encode()))
    _waits(monkeypatch)

    _recover(db, make_config())

    (log,) = ssh.LOG_DIR.glob("box-*-recover.log")
    text = log.read_text(encoding="utf-8")
    assert "BackendState" not in text  # the script's source is never written
    assert KEY not in text


def test_a_join_selects_the_settle_and_the_reconnect_wording(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """After a join the node is unusable without its address, so reads that run out log it out."""
    _seed(db)
    guest = _guest(monkeypatch, repair=Answer(stdout=b"joined\n"), address=Answer(status=1))
    waits = _waits(monkeypatch)

    with pytest.raises(ConnectivityError):
        _recover(db, make_config())

    assert guest.count(tailscale_join.LOGOUT_SCRIPT) == 1
    assert waits == []

    db.delete_vm("box")
    _seed(db)
    guest = _guest(monkeypatch, repair=Answer(stdout=b"joined\n"))
    _recover(db, make_config())
    assert waits == [reachability.Wording.RECONNECT]


def test_without_a_join_reads_that_run_out_never_log_the_node_out(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A flaky channel must not log out a node that was working."""
    _seed(db)
    guest = _guest(monkeypatch, address=Answer(status=1))
    _waits(monkeypatch)

    with pytest.raises(ConnectivityError):
        _recover(db, make_config())

    assert guest.count("tailscale ip -4") == tailscale_join.ADDRESS_READS
    assert guest.count(tailscale_join.LOGOUT_SCRIPT) == 0


def test_a_read_alone_that_runs_out_points_at_the_log_that_recorded_every_read(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    from contextlib import contextmanager

    from agentworks.vms.manager import recover

    records: list[object] = []

    class _Log:
        display_path = "log"

        def step(self, name: str) -> None: ...

        def output(self, text: str) -> None: ...

        def record(self, request: object, result: object) -> None:
            records.append(request)

    @contextmanager
    def _recording_log(*_args: object, **_kwargs: object):  # noqa: ANN202
        yield _Log()

    monkeypatch.setattr(recover, "_operation_log", _recording_log)
    _seed(db)
    guest = _guest(monkeypatch, address=Answer(status=1))
    _waits(monkeypatch)

    with pytest.raises(ConnectivityError) as caught:
        _recover(db, make_config())

    assert caught.value.hint is not None
    assert len(records) == len(guest.scripts) == 1 + tailscale_join.ADDRESS_READS


@pytest.mark.parametrize("repair", [Answer(status=1), Answer(status=None)], ids=["failed", "unknown"])
def test_a_repair_that_does_not_succeed_stops_before_the_address_read(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    repair: Answer,
) -> None:
    _seed(db)
    guest = _guest(monkeypatch, repair=repair)
    waits = _waits(monkeypatch)

    with pytest.raises((StateError, ConnectivityError)):
        _recover(db, make_config())

    assert len(guest.scripts) == 1
    assert waits == []


@pytest.mark.parametrize("platform_name", ["wsl2", "aws-ec2"])
@pytest.mark.parametrize("status", [1, None], ids=["failed", "unknown"])
def test_a_failed_repair_names_the_native_shell_only_when_the_channel_answered(
    platform_name: str, status: int | None
) -> None:
    """A repair that exited ran through the channel the native shell uses; one with no outcome may have lost it."""
    from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
    from agentworks.execution.root_script import RootScriptResult
    from agentworks.plugins.aws.platform import EC2Platform
    from agentworks.vms.manager.recover import _repair_hint

    platform = (
        WSL2Platform("wsl2", {})
        if platform_name == "wsl2"
        else EC2Platform("aws-site", {"region": "us-east-1", "auth": {"mode": "ambient"}})
    )

    hint = _repair_hint(platform, "box", RootScriptResult(status, b"", b"", ""))

    assert ("agw vm shell --platform box" in hint) is (status is not None)


def test_a_repaired_vm_that_does_not_answer_raises(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    _seed(db)
    _guest(monkeypatch)
    _waits(monkeypatch, answers=False)

    with pytest.raises(ConnectivityError):
        _recover(db, make_config())


@pytest.mark.parametrize("status", [VMStatus.STOPPED, VMStatus.DEALLOCATED])
@pytest.mark.parametrize("record", [True, False], ids=["record", "no-record"])
def test_a_stopped_vm_is_started_before_the_hold_then_repaired(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    status: VMStatus,
    record: bool,
) -> None:
    """A VM that stopped (a WSL2 distro that idled out after start, say) is started, with no wait, and one with no
    host-key record has its keys recorded, so recover is the way out of the start that refuses such a VM. The start
    precedes the hold, as an explicit start's does, and the hold covers everything after it."""
    _seed(db, record=record)
    seeded = db.get_vm("box")
    assert seeded is not None
    db.set_vm_desired_runtime_state("box", DesiredRuntimeState.STOPPED, expected_vm_uuid=seeded.vm_uuid)
    events: list[str] = []
    guest = _guest(monkeypatch, status=status, events=events)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, vm, ctx: events.append("start"))
    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _recording_hold(events))
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.confirm", lambda message, default: True)
    _waits(monkeypatch)

    _recover(db, make_config())

    assert events[:2] == ["start", "hold-open"]
    assert events[-1] == "hold-close"
    assert any(_is_repair(payload) for payload in guest.payloads)
    row = db.get_vm("box")
    assert row is not None and row.desired_runtime_state is DesiredRuntimeState.RUNNING
    assert row.last_started_at is not None
    if not record:
        assert trust.stored_keys("box") == (HOST_KEY,)


class _BootingGuestAgent:
    """The Proxmox API of a VM that recover just started: its guest agent answers only from the third ping."""

    def __init__(self, guest: FakeGuest) -> None:
        self.guest = guest
        self.pings = 0
        self.dispatches = 0
        self.finished: dict[int, RawRun] = {}

    def vm_status(self, node: str, vmid: int, *, timeout: float | None = None) -> dict[str, object]:
        """The guest at the row's VMID is the row's: its name is the hostname the clone set."""
        return {"status": "running", "name": "box-node"}

    def guest_agent_ping(self, node: str, vmid: int, *, timeout: float) -> None:
        self.pings += 1
        if self.pings < 3:
            raise ProxmoxAPIError("QEMU guest agent is not running")

    def guest_agent_exec(self, node: str, vmid: int, *, command: list[str], input_data: str, timeout: float) -> int:
        if self.pings < 3:
            raise ProxmoxAPIError("QEMU guest agent is not running")
        self.dispatches += 1
        self.finished[self.dispatches] = self.guest.run_as_root(input_data.encode(), timeout)
        return self.dispatches

    def guest_agent_exec_status(self, node: str, vmid: int, *, pid: int, timeout: float) -> dict[str, object]:
        raw = self.finished.pop(pid)
        return {"exited": True, "out-data": raw.stdout.decode(), "err-data": raw.stderr.decode()}


def test_a_stopped_proxmox_vm_recovers_in_one_run_through_its_real_channel(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Just after the start, the guest agent is not up yet; the channel waits for it rather than failing the
    first script."""
    _seed(db)
    db.update_vm_platform_metadata("box", {"vmid": "101", "node": "pve1"})
    guest = FakeGuest(
        respond=lambda payload, seen: (
            Answer(stdout=f"{NEW_ADDRESS}\n".encode()) if payload == "tailscale ip -4" else Answer()
        )
    )
    agent = _BootingGuestAgent(guest)
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.STOPPED)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, vm, ctx: None)
    monkeypatch.setattr(ProxmoxPlatform, "_api", lambda self, ctx: agent)
    monkeypatch.setattr("agentworks.plugins.proxmox.guest_agent.time.sleep", lambda seconds: None)
    _waits(monkeypatch)

    _recover(db, make_config())

    assert agent.pings >= 3
    assert [_is_repair(payload) for payload in guest.payloads] == [True, False]
    assert agent.dispatches == len(guest.scripts)


def test_a_vm_whose_power_state_is_unknown_is_refused_before_any_script(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(db)
    guest = _guest(monkeypatch, status=VMStatus.UNKNOWN)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, vm, ctx: pytest.fail("an unknown VM was started"))

    with pytest.raises(StateError):
        _recover(db, make_config())

    assert guest.scripts == []


@pytest.mark.parametrize("auth_key", ["", " \t "], ids=["empty", "whitespace"])
def test_a_blank_auth_key_is_refused_before_any_script(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    auth_key: str,
) -> None:
    """A repair that joins with a blank key would wait for an interactive login nobody sees."""
    _seed(db)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", auth_key)
    guest = _guest(monkeypatch)

    with pytest.raises(ValidationError):
        _recover(db, make_config())

    assert guest.scripts == []


@pytest.mark.parametrize("provisioning", [ProvisioningStatus.FAILED, ProvisioningStatus.IN_PROGRESS])
def test_a_vm_whose_provisioning_did_not_complete_is_refused_before_any_secret(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    provisioning: ProvisioningStatus,
) -> None:
    _seed(db)
    db.update_vm_provisioning_status("box", provisioning)
    guest = _guest(monkeypatch)

    with pytest.raises(StateError):
        _recover(db, make_config())

    assert resolve_counter == []
    assert guest.scripts == []


def test_a_missing_record_is_written_only_after_confirmation_then_synced(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    _world: list[str],
) -> None:
    _seed(db, record=False)
    events: list[str] = []
    _guest(monkeypatch, events=events)
    _waits(monkeypatch)

    def _confirm(message: str, default: bool = False) -> bool:
        events.append(f"confirm:{trust.has_record('box')}")
        return True

    monkeypatch.setattr("agentworks.output.confirm", _confirm)
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)

    _recover(db, make_config())

    assert trust.stored_keys("box") == (HOST_KEY,)
    assert events[:2] == ["root:cat /etc/ssh/ssh_host_*_key.pub", "confirm:False"]
    assert _is_repair(events[2])
    assert _world[0] == "sync"


def test_a_missing_record_without_a_terminal_is_refused_before_any_secret(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(db, record=False)
    guest = _guest(monkeypatch)
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: False)

    with pytest.raises(StateError) as caught:
        _recover(db, make_config())

    assert caught.value.hint is not None
    assert resolve_counter == []
    assert guest.scripts == []


def test_a_refused_record_writes_nothing_and_stops(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    _seed(db, record=False)
    guest = _guest(monkeypatch)
    monkeypatch.setattr("agentworks.output.confirm", lambda message, default=False: False)
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)

    with pytest.raises(UserAbort):
        _recover(db, make_config())

    assert not trust.has_record("box")
    assert not any(_is_repair(payload) for payload in guest.payloads)


# -- The SSH identity -------------------------------------------------------------------------


def _identity_state(db: Database, config: Config) -> VMSSHIdentityState:
    return compare_vm_ssh_identity(db, "box", config.operator.ssh_private_key).state


def _record_another_identity(db: Database) -> None:
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-reinit",
        {AppliedStateKey.SSH_IDENTITY: encode_ssh_identity("/old/key", VerifiedSSHIdentity("SHA256:" + "A" * 43))},
    )


@pytest.mark.parametrize("recorded", [True, False], ids=["another-key-recorded", "none-recorded"])
def test_a_recovered_vm_records_the_configured_identity_it_answered_with(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    recorded: bool,
) -> None:
    """After an operator key change the repair installs the new key, so every SSH command must then accept it."""
    _seed(db)
    if recorded:
        _record_another_identity(db)
    config = make_config()
    _guest(monkeypatch)
    _waits(monkeypatch)

    _recover(db, config)

    assert _identity_state(db, config) is VMSSHIdentityState.MATCH
    require_vm_ssh_boundary(db, config, db.get_vm("box"))  # type: ignore[arg-type]


def test_a_vm_that_does_not_answer_keeps_its_recorded_identity(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    _seed(db)
    _record_another_identity(db)
    config = make_config()
    _guest(monkeypatch)
    _waits(monkeypatch, answers=False)

    with pytest.raises(ConnectivityError):
        _recover(db, config)

    assert _identity_state(db, config) is VMSSHIdentityState.DRIFT


def test_a_key_replaced_while_recover_ran_is_not_recorded(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    from agentworks.ssh_identity import read_private_ssh_identity

    _seed(db)
    _record_another_identity(db)
    config = make_config()
    _guest(monkeypatch)
    waits: list[str] = []

    def wait_then_replace_the_key(access: object, *, wording: reachability.Wording) -> bool:
        waits.append("wait")
        monkeypatch.setattr(
            "agentworks.vms.applied_state.read_private_ssh_identity",
            lambda path: VerifiedSSHIdentity("SHA256:" + "B" * 43),
        )
        return True

    monkeypatch.setattr(reachability, "wait", wait_then_replace_the_key)

    with pytest.raises(StateError) as caught:
        _recover(db, config)

    assert waits == ["wait"]
    assert "agw vm recover box" in (caught.value.hint or "")
    monkeypatch.setattr("agentworks.vms.applied_state.read_private_ssh_identity", read_private_ssh_identity)
    assert _identity_state(db, config) is VMSSHIdentityState.DRIFT
