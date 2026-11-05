"""The last straggler commands through the orchestrated model:
``describe_vm``, ``rekey_vm``, ``port_forward_vm``, and ``backup_vm``
(the seam that finished the still-open caller catalog).

Per-command shape: describe composes without gating
(``_live_vm_boundary``: one boundary burst, a status read as its op,
NEVER a gate or a start); rekey composes the vm-template node beside
the live VM (the new auth key IS its planned op) and gates AFTER the
boundary, exactly where HEAD held ``keep_active``; port-forward and
backup are gated commands (``gated_vm_boundary``) with their
validation pre-gate.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops, the reachability probe, and the
SSH/subprocess transports are the fakes.
"""

from __future__ import annotations

import os
import signal
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform.tailscale_join import JOIN_FRAGMENT
from agentworks.db import ProvisioningStatus, VMStatus
from agentworks.execution.requests import Forward
from agentworks.execution.trust import HOST_TRUST
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from agentworks.vms.manager.tailscale import VMNotAnswering
from tests._root_channel_fakes import Answer, FakeGuest
from tests.conftest import seed_vm_trust, stub_vm_ssh_identity

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database
    from tests.vm_access_support import AccessRecorder


@pytest.fixture(autouse=True)
def _stub_ssh_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_vm_ssh_identity(monkeypatch)


@pytest.fixture(autouse=True)
def _on_tailscale(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workstation is on Tailscale, whatever the test host is: rekey checks it first."""
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)


def _seed_vm(db: Database, *, operator_stopped: bool = False) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
    seed_vm_trust("box")
    if operator_stopped:
        db.set_operator_stopped("box", True)


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _fake_status(monkeypatch: pytest.MonkeyPatch, status: VMStatus) -> list[str]:
    """Fake the platform's backend status read (recording the op order);
    ``start`` records too, so never-gates pins can assert its absence."""
    events: list[str] = []
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or status,
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start"))
    monkeypatch.setattr("agentworks.vms.manager._await_tailnet", lambda *a, **k: events.append("tailscale"))
    return events


# == describe_vm: composition without a gate ==================================


@pytest.fixture
def _quiet_backend_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub describe's non-status backend reads (the display name is a
    backend API render; the live-resource query SSHes to the VM)."""
    monkeypatch.setattr(ProxmoxPlatform, "display_backend_name", lambda self, row: "vmid 100")
    monkeypatch.setattr(vm_manager, "_query_live_resources", lambda db, vm, config: None)


def test_describe_running_vm_is_one_boundary_burst_and_reads_only(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    _quiet_backend_reads: None,
) -> None:
    """The no-gate mirror of the tracer's parity shape: exactly ONE
    boundary burst covering the union, then the status read (describe's
    op), and nothing else; the report renders the observed status."""
    config = make_config()
    _seed_vm(db)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)

    vm_manager.describe_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert events == ["status"]
    assert any("Status:         running" in m for m in captured_output.info)


def test_describe_operator_stopped_vm_never_gates(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    _quiet_backend_reads: None,
) -> None:
    """The never-gates pin: on an operator-stopped VM describe behaves
    exactly as HEAD: one status read, NO start, the "(manual)" label,
    and the live SSH read skipped (the stopped-VM short-circuit)."""
    config = make_config()
    _seed_vm(db, operator_stopped=True)
    events = _fake_status(monkeypatch, VMStatus.STOPPED)

    def _no_live(db: object, vm: object, config: object) -> None:
        raise AssertionError("live SSH read must be skipped on a stopped VM")

    monkeypatch.setattr(vm_manager, "_query_live_resources", _no_live)

    vm_manager.describe_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert events == ["status"]  # never "start"
    assert any("stopped (manual)" in m for m in captured_output.info)


def test_describe_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    _quiet_backend_reads: None,
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed_vm(db)
    _fake_status(monkeypatch, VMStatus.RUNNING)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    vm_manager.describe_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"


# == rekey_vm: the template node beside the live VM; gate after boundary =====


@pytest.fixture
def _ts_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey-new")


def test_rekey_graph_roots_the_template_beside_the_live_vm(
    db: Database,
    make_config,
    _ts_key: None,  # noqa: ANN001
) -> None:
    """The rekey graph: the vm-template node roots FIRST (HEAD's
    template-readiness-before-platform-preflight precedence), the live
    VM beside it, and the union is the auth key plus the site's config
    secret: the auth key IS this command's planned op, the contrast
    with reinit's deliberate template exclusion."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import live_vm_node, vm_template_node
    from agentworks.vms.templates import resolve_template

    config = make_config()
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)

    tmpl_node = vm_template_node(resolve_template(registry, vm.template))
    vm_node = live_vm_node(db, config, registry, vm)
    nodes = walk(tmpl_node, vm_node)

    assert [n.key for n in nodes] == [
        "vm-template/default",
        "vm-site/proxmox",
        "vm/box",
    ]
    assert secret_union(nodes) == ("tailscale-auth-key", "proxmox-token")


def test_rekey_running_check_runs_after_the_resolve_boundary(
    db: Database,
    make_config,  # noqa: ANN001
    _ts_key: None,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The is-it-running check is an op (a backend status read; on
    proxmox it needs the token), so it runs PAST the preflight
    boundary: after the one resolve pass, never before. The trade (a
    stopped-VM error lands after the prompt session) was ruled
    preferable to a second prompt session, which the contract forbids.
    A not-running VM errors HERE, before any gate: rekey never
    auto-starts one."""
    from agentworks.errors import StateError
    from agentworks.secrets.resolver import Resolver

    config = make_config()
    _seed_vm(db)
    order: list[str] = []

    real_preflight = ProxmoxPlatform.preflight

    def _spying_preflight(self: ProxmoxPlatform, ctx: RunContext) -> None:
        order.append("preflight")
        real_preflight(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _spying_preflight)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: order.append("status") or VMStatus.STOPPED,
    )
    real_resolve = Resolver.resolve

    def _spying_resolve(self: Resolver) -> None:
        order.append("resolve")
        real_resolve(self)

    monkeypatch.setattr(Resolver, "resolve", _spying_resolve)

    with pytest.raises(StateError, match="is not running"):
        vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    # The boundary (preflight, then the one resolve pass) fully
    # precedes the status op; nothing re-resolves afterwards.
    assert order == ["preflight", "resolve", "status"]


def test_rekey_without_a_host_key_record_raises_before_resolving_or_touching_the_vm(
    db: Database,
    make_config,  # noqa: ANN001
    _ts_key: None,
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rekeying would spend the new key and record the new address before its
    reconnect check raised."""
    from agentworks.execution import trust
    from agentworks.execution.errors import TrustMissing

    config = make_config()
    _seed_vm(db)
    trust.remove("box")
    events = _fake_status(monkeypatch, VMStatus.RUNNING)

    with pytest.raises(TrustMissing):
        vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert events == []
    row = db.get_vm("box")
    assert row is not None and row.tailscale_host == "100.64.0.9"


def test_a_workstation_off_tailscale_refuses_rekey_before_resolving_or_touching_the_vm(
    db: Database,
    make_config,  # noqa: ANN001
    _ts_key: None,
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final wait reaches the VM over the tailnet, so the new key must not be spent first."""
    from agentworks.errors import ConnectivityError

    config = make_config()
    _seed_vm(db)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)

    def _off_tailscale() -> None:
        raise ConnectivityError("not on Tailscale")

    monkeypatch.setattr(vm_manager, "verify_tailscale_available", _off_tailscale)

    with pytest.raises(ConnectivityError, match="not on Tailscale"):
        vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert events == []


def test_an_unsupported_client_refuses_rekey_before_resolving_or_touching_the_vm(
    db: Database,
    make_config,  # noqa: ANN001
    _ts_key: None,
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.execution.errors import ClientUnsupported

    config = make_config()
    _seed_vm(db)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)

    def _unsupported() -> object:
        raise ClientUnsupported("no supported OpenSSH client")

    monkeypatch.setattr("agentworks.execution.resolve_ssh", _unsupported)

    with pytest.raises(ClientUnsupported):
        vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert events == []


def test_rekey_missing_key_fails_at_the_one_resolve_before_status(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider-aware preflight detects absence before resolution or status."""
    from agentworks.errors import ConfigError

    config = make_config('[secret_config]\nsources = ["env-var"]\n')
    _seed_vm(db)
    monkeypatch.delenv("AW_SECRET_TAILSCALE_AUTH_KEY", raising=False)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)

    with pytest.raises(ConfigError):
        vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert events == []


def test_rekey_rejects_multiline_key_before_status_or_daemon_action(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.errors import ValidationError

    auth_key = "tskey-prefix\ntskey-suffix\n"
    config = make_config()
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", auth_key)
    _seed_vm(db)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda *args, **kwargs: pytest.fail("status reached with a line-unsafe rekey secret"),
    )

    with pytest.raises(ValidationError) as caught:
        vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert auth_key not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


NEW_ADDRESS = "100.64.0.77"


def _fake_rekey_channel(
    monkeypatch: pytest.MonkeyPatch, *, rekey_status: int | None = 0, answers: bool = True
) -> FakeGuest:
    """Answer the platform's root channel from a fake guest (the rekey script, then the address read), let the
    wait answer as ``answers`` says, and keep the SSH config untouched."""
    from agentworks.execution import reachability

    def respond(payload: str, seen: int) -> Answer:
        if JOIN_FRAGMENT in payload:
            return Answer(status=rekey_status)
        if payload == "tailscale ip -4":
            return Answer(stdout=f"{NEW_ADDRESS}\n".encode())
        return Answer()

    guest = FakeGuest(respond=respond)
    monkeypatch.setattr(
        ProxmoxPlatform, "run_as_root", lambda self, vm, ctx, script, deadline: guest.run_as_root(script, deadline)
    )
    monkeypatch.setattr(reachability, "wait", lambda access, *, wording: answers)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda config, db: None)
    return guest


def test_rekey_one_boundary_burst_covers_key_and_site_secret(
    db: Database,
    make_config,  # noqa: ANN001
    _ts_key: None,
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """End to end on a running, reachable VM: ONE boundary burst covers
    the new auth key AND the site's config secret (one prompt session),
    the gate's fast path costs nothing, one rekey script carries the new
    key, the settle reads the address after it, and the row records it."""
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)
    guest = _fake_rekey_channel(monkeypatch)

    vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == ["proxmox-token", "tailscale-auth-key"]
    assert events == ["status"]
    assert [JOIN_FRAGMENT in payload for payload in guest.payloads] == [True, False]
    assert guest.payloads[1] == "tailscale ip -4"
    assert [b"tskey-new" in script for script in guest.scripts] == [True, False]
    row = db.get_vm("box")
    assert row is not None and row.tailscale_host == NEW_ADDRESS


@pytest.mark.parametrize("status", [1, None], ids=["failed", "unknown"])
def test_a_rekey_script_that_does_not_succeed_records_nothing_and_reads_no_address(
    db: Database,
    make_config,  # noqa: ANN001
    _ts_key: None,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    status: int | None,
) -> None:
    from agentworks.errors import AgentworksError, ConnectivityError, StateError

    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    _fake_status(monkeypatch, VMStatus.RUNNING)
    guest = _fake_rekey_channel(monkeypatch, rekey_status=status)

    with pytest.raises(AgentworksError) as caught:
        vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert type(caught.value) is (StateError if status is not None else ConnectivityError)
    assert len(guest.scripts) == 1
    row = db.get_vm("box")
    assert row is not None and row.tailscale_host == "100.64.0.9"


def test_a_rekeyed_vm_that_does_not_answer_raises_and_keeps_the_new_address(
    db: Database,
    make_config,  # noqa: ANN001
    _ts_key: None,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    _fake_status(monkeypatch, VMStatus.RUNNING)
    _fake_rekey_channel(monkeypatch, answers=False)

    with pytest.raises(VMNotAnswering):
        vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    row = db.get_vm("box")
    assert row is not None and row.tailscale_host == NEW_ADDRESS


def test_rekey_gate_serves_the_boundary_cache(
    db: Database,
    make_config,  # noqa: ANN001
    _ts_key: None,
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Boundary-then-gate (HEAD's check-then-keep_active order): on a
    running but unreachable VM the gate probes status again (as HEAD's
    ensure_active did) but resolves NOTHING new: its callback serves
    the boundary's cached values, so the whole command stays one
    backend burst."""
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, False)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)
    _fake_rekey_channel(monkeypatch)

    vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert len(resolve_counter) == 1
    # The pre-gate running check, then the gate's own observation.
    assert events == ["status", "status"]


def test_rekey_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    _ts_key: None,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    _fake_status(monkeypatch, VMStatus.RUNNING)
    _fake_rekey_channel(monkeypatch)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"


def test_rekey_wraps_steps_in_a_section(
    db: Database,
    make_config,  # noqa: ANN001
    _ts_key: None,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The rekey renders inside a section: its progress is a primary
    (BODY) line one level deep, the read-back IP a subordinate DETAIL, and
    the terminal a column-0 RESULT."""
    from agentworks.output import Role

    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    _fake_status(monkeypatch, VMStatus.RUNNING)
    _fake_rekey_channel(monkeypatch)

    vm_manager.rekey_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert any(role is Role.HEADER and lvl == 0 for role, lvl, _msg in captured_output.lines)
    assert any(role is Role.BODY and lvl == 1 for role, lvl, _msg in captured_output.lines)
    assert any(role is Role.DETAIL and msg.startswith("Tailscale IP:") for role, lvl, msg in captured_output.lines)
    assert any(role is Role.RESULT and lvl == 0 for role, lvl, _msg in captured_output.lines)


# == port_forward_vm: a gated command =========================================


def test_port_forward_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """port-forward on a reachable VM: the gate's fast path costs
    nothing, the site secret rides ONE boundary burst, and the forward
    carries the port specs inside the held span."""
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)

    # The service returns the SSH exit code; the CLI layer owns the
    # translation to process exit (check 9: no sys.exit in the service).
    assert (
        vm_manager.port_forward_vm(db, config, "box", ["8080", "9000:3000"], interaction=TtyInteractionPolicy.REFUSE)
        == 0
    )

    assert resolve_counter == [["proxmox-token"]]
    (forwards,) = vm_access_recorder.requests("forward")
    assert forwards == (Forward(8080, 8080, "localhost"), Forward(9000, 3000, "localhost"))


def test_port_forward_stopped_vm_gates_then_forwards(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The gate-prompt parity mirror on a stopped VM: the gate's
    just-in-time token resolve seeds the boundary (nothing resolves
    twice), the VM starts, and the tunnel still opens."""
    config = make_config()
    _seed_vm(db)
    events: list[str] = []
    _reachable(monkeypatch, False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or VMStatus.STOPPED,
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start"))
    monkeypatch.setattr("agentworks.vms.manager._await_tailnet", lambda *a, **k: events.append("tailscale"))

    assert vm_manager.port_forward_vm(db, config, "box", ["8080"], interaction=TtyInteractionPolicy.REFUSE) == 0

    assert events == ["status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"]]
    assert len(vm_access_recorder.requests("forward")) == 1


def test_port_forward_bad_spec_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.errors import ValidationError

    config = make_config()
    _seed_vm(db)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)
    _reachable(monkeypatch, False)

    with pytest.raises(ValidationError, match="invalid port"):
        vm_manager.port_forward_vm(db, config, "box", ["nope"], interaction=TtyInteractionPolicy.REFUSE)
    with pytest.raises(ValidationError, match="out of range"):
        vm_manager.port_forward_vm(db, config, "box", ["70000"], interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert events == []
    assert vm_access_recorder.requests("forward") == []


def test_port_forward_returns_zero_when_the_operator_stops_it(
    db: Database,
    make_config,  # noqa: ANN001
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    vm_access_recorder.access().forward_status = KeyboardInterrupt()

    assert vm_manager.port_forward_vm(db, config, "box", ["8080"], interaction=TtyInteractionPolicy.REFUSE) == 0


def test_port_forward_returns_the_clients_status_for_any_other_end(
    db: Database,
    make_config,  # noqa: ANN001
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    vm_access_recorder.access().forward_status = 255

    assert vm_manager.port_forward_vm(db, config, "box", ["8080"], interaction=TtyInteractionPolicy.REFUSE) == 255


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal handlers")
def test_port_forward_turns_sigterm_into_an_operator_stop_and_restores_the_handler(
    db: Database,
    make_config,  # noqa: ANN001
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    before = signal.getsignal(signal.SIGTERM)
    access = vm_access_recorder.access()

    def terminated(forwards, *, verbose=False) -> int:  # noqa: ANN001
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM was not delivered as an interrupt")

    access.forward = terminated  # type: ignore[method-assign]

    assert vm_manager.port_forward_vm(db, config, "box", ["8080"], interaction=TtyInteractionPolicy.REFUSE) == 0
    assert signal.getsignal(signal.SIGTERM) == before

    access.forward = lambda forwards, *, verbose=False: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="boom"):
        vm_manager.port_forward_vm(db, config, "box", ["8080"], interaction=TtyInteractionPolicy.REFUSE)
    assert signal.getsignal(signal.SIGTERM) == before


def test_port_forward_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    assert vm_manager.port_forward_vm(db, config, "box", ["8080"], interaction=TtyInteractionPolicy.REFUSE) == 0

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"


# == backup_vm: a gated command ===============================================


@pytest.fixture
def backup_env(
    tmp_path,  # noqa: ANN001
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
):  # noqa: ANN201
    """A config whose backups land under tmp, the SSH log dir isolated,
    and the admin transport faked (an SSHTransport whose run is a
    stub: the body asserts the SSH-specific type)."""
    from types import SimpleNamespace

    from agentworks.transports import SSHTransport

    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path / "logs")
    target = SSHTransport(trust=HOST_TRUST, host="100.64.0.9")
    target.run = lambda *a, **k: SimpleNamespace(stdout="", ok=True)  # type: ignore[method-assign, assignment]
    monkeypatch.setattr("agentworks.transports.transport", lambda vm, config, **kwargs: target)
    return make_config(f'[paths]\nbackups = "{tmp_path.as_posix()}/backups"\n')


def test_backup_reachable_vm_is_one_boundary_burst(
    db: Database,
    backup_env,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """backup on a reachable VM: the gate's fast path costs nothing,
    the site secret rides ONE boundary burst, and the metadata backup
    completes end to end (manifest written, completion event logged)."""
    import json

    from agentworks.vms import backup as vm_backup

    _seed_vm(db)
    _reachable(monkeypatch, True)

    backup_dir = vm_backup.backup_vm(db, backup_env, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    manifest = json.loads((backup_dir / "manifest.json").read_text())
    assert manifest["vm_name"] == "box"
    events = [e.event for e in db.list_vm_events("box")]
    assert "backup_started" in events and "backup_completed" in events


def test_backup_stopped_vm_gates_then_backs_up(
    db: Database,
    backup_env,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The gate-prompt parity mirror on a stopped VM: the gate's
    just-in-time token resolve seeds the boundary (nothing resolves
    twice), the VM starts, and the backup still completes."""
    from agentworks.vms import backup as vm_backup

    _seed_vm(db)
    gate_events: list[str] = []
    _reachable(monkeypatch, False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: gate_events.append("status") or VMStatus.STOPPED,
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: gate_events.append("start"))
    monkeypatch.setattr("agentworks.vms.manager._await_tailnet", lambda *a, **k: gate_events.append("tailscale"))

    backup_dir = vm_backup.backup_vm(db, backup_env, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert gate_events == ["status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"]]
    assert (backup_dir / "manifest.json").exists()


def test_backup_scope_reaches_node_readiness(
    db: Database,
    backup_env,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel
    from agentworks.vms import backup as vm_backup

    _seed_vm(db)
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    vm_backup.backup_vm(db, backup_env, "box", interaction=TtyInteractionPolicy.REFUSE)

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"


def test_backup_wraps_phases_in_a_section(
    db: Database,
    backup_env,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The backup phases render inside a section: a level-0 header, the
    export phases as primary (BODY) steps one level deeper (not orphaned
    detail), and the terminal 'Backup complete' as a column-0 RESULT."""
    from agentworks.output import Role
    from agentworks.vms import backup as vm_backup

    _seed_vm(db)
    _reachable(monkeypatch, True)

    vm_backup.backup_vm(db, backup_env, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert any(
        role is Role.HEADER and lvl == 0 and msg.startswith("Backing up VM 'box'")
        for role, lvl, msg in captured_output.lines
    )
    body_l1 = [msg for role, lvl, msg in captured_output.lines if role is Role.BODY and lvl == 1]
    assert "Reading database (consistent snapshot)..." in body_l1
    assert "Exporting VM metadata..." in body_l1
    assert any(
        role is Role.RESULT and lvl == 0 and msg.startswith("Backup complete:")
        for role, lvl, msg in captured_output.lines
    )
