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

from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.db import VMStatus
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database


def _seed_vm(db: Database, *, operator_stopped: bool = False) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    if operator_stopped:
        db.set_operator_stopped("box", True)


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _fake_status(
    monkeypatch: pytest.MonkeyPatch, status: VMStatus
) -> list[str]:
    """Fake the platform's backend status read (recording the op order);
    ``start`` records too, so never-gates pins can assert its absence."""
    events: list[str] = []
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or status,
    )
    monkeypatch.setattr(
        ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start")
    )
    monkeypatch.setattr(
        vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale")
    )
    return events


# == describe_vm: composition without a gate ==================================


@pytest.fixture
def _quiet_backend_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub describe's non-status backend reads (the display name is a
    backend API render; the live-resource query SSHes to the VM)."""
    monkeypatch.setattr(
        ProxmoxPlatform, "display_backend_name", lambda self, row: "vmid 100"
    )
    monkeypatch.setattr(
        vm_manager, "_query_live_resources", lambda vm, config: None
    )


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

    vm_manager.describe_vm(db, config, "box")

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

    def _no_live(vm: object, config: object) -> None:
        raise AssertionError("live SSH read must be skipped on a stopped VM")

    monkeypatch.setattr(vm_manager, "_query_live_resources", _no_live)

    vm_manager.describe_vm(db, config, "box")

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

    vm_manager.describe_vm(db, config, "box")

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"


# == rekey_vm: the template node beside the live VM; gate after boundary =====


@pytest.fixture
def _ts_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey-new")


def test_rekey_graph_roots_the_template_beside_the_live_vm(
    db: Database, make_config, _ts_key: None  # noqa: ANN001
) -> None:
    """The rekey graph: the vm-template node roots FIRST (HEAD's
    template-readiness-before-platform-preflight precedence), the live
    VM beside it, and the union is the auth key plus the site's config
    secret: the auth key IS this command's planned op, the contrast
    with reinit's deliberate template exclusion."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node, vm_template_node
    from agentworks.vms.templates import resolve_template

    config = make_config()
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    resolver = Resolver(config, registry)

    tmpl_node = vm_template_node(resolve_template(registry, vm.template), resolver)
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
        vm_manager.rekey_vm(db, config, "box")

    # The boundary (preflight, then the one resolve pass) fully
    # precedes the status op; nothing re-resolves afterwards.
    assert order == ["preflight", "resolve", "status"]


def test_rekey_unpredictable_key_fails_before_any_resolve_or_status(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The template node's relocated readiness check runs in the
    preflight sweep: an unresolvable auth key bails BEFORE the resolve
    pass and before any backend op (zero prompts, zero status reads),
    exactly the delegate's old promise."""
    from agentworks.errors import ConfigError

    config = make_config('[secret_config]\nbackends = ["env-var"]\n')
    _seed_vm(db)
    monkeypatch.delenv("AW_SECRET_TAILSCALE_AUTH_KEY", raising=False)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)

    with pytest.raises(ConfigError, match="not resolvable"):
        vm_manager.rekey_vm(db, config, "box")

    assert resolve_counter == []
    assert events == []


def _fake_rekey_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Fake the rekey body's out-of-band transport work: the native
    transport records the tailscale commands, the Tailscale-side
    verification succeeds, and the per-step stabilization sleeps cost
    nothing."""
    from types import SimpleNamespace

    from agentworks.transports import SSHTransport

    commands: list[str] = []

    def _run(cmd: str, **kwargs: object) -> object:
        commands.append(cmd)
        if cmd == "tailscale ip -4":
            return SimpleNamespace(stdout="100.64.0.77\n", ok=True)
        return SimpleNamespace(stdout="", ok=True)

    native = SimpleNamespace(run=_run)
    monkeypatch.setattr(
        "agentworks.transports.native_transport",
        lambda vm, platform, config, *, stack: native,
    )
    monkeypatch.setattr(
        "agentworks.transports.transport",
        lambda vm, config, **kwargs: SSHTransport(host="100.64.0.9"),
    )
    monkeypatch.setattr(
        "agentworks.transports.wait_for_reconnect", lambda target: True
    )
    monkeypatch.setattr(
        "agentworks.ssh_config.sync_ssh_config", lambda config, db: None
    )
    import time

    monkeypatch.setattr(time, "sleep", lambda secs: None)
    return commands


def test_rekey_one_boundary_burst_covers_key_and_site_secret(
    db: Database,
    make_config,  # noqa: ANN001
    _ts_key: None,
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """End to end on a running, reachable VM: ONE boundary burst covers
    the new auth key AND the site's config secret (HEAD's single prompt
    session), the gate's fast path costs nothing, the new key reaches
    `tailscale up`, and the row records the new IP."""
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)
    commands = _fake_rekey_transports(monkeypatch)

    vm_manager.rekey_vm(db, config, "box")

    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == ["proxmox-token", "tailscale-auth-key"]
    assert events == ["status"]
    assert "tailscale up --auth-key tskey-new" in commands
    row = db.get_vm("box")
    assert row is not None and row.tailscale_host == "100.64.0.77"
    assert any("rekeyed successfully" in m for m in captured_output.info)


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
    _fake_rekey_transports(monkeypatch)

    vm_manager.rekey_vm(db, config, "box")

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
    _fake_rekey_transports(monkeypatch)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    vm_manager.rekey_vm(db, config, "box")

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
    """The rekey step sequence renders inside a 'Rekeying' section: the
    tailnet steps are primary (BODY) lines one level deep, the read-back
    IP is a subordinate DETAIL, and the terminal is a column-0 RESULT."""
    from agentworks.output import Role

    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    _fake_status(monkeypatch, VMStatus.RUNNING)
    _fake_rekey_transports(monkeypatch)

    vm_manager.rekey_vm(db, config, "box")

    assert any(
        role is Role.HEADER and lvl == 0 and msg == "Rekeying 'box'"
        for role, lvl, msg in captured_output.lines
    )
    body_l1 = [
        msg for role, lvl, msg in captured_output.lines if role is Role.BODY and lvl == 1
    ]
    assert "Joining new tailnet..." in body_l1
    assert "Reading new Tailscale IP..." in body_l1
    # The read-back IP is a subordinate detail of the read step, not a step.
    assert any(
        role is Role.DETAIL and msg.startswith("Tailscale IP:")
        for role, lvl, msg in captured_output.lines
    )
    assert any(
        role is Role.RESULT and lvl == 0 and "rekeyed successfully" in msg
        for role, lvl, msg in captured_output.lines
    )


# == port_forward_vm: a gated command =========================================


class _FakeTunnel:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.terminated = False

    def __call__(self, cmd: list[str]) -> _FakeTunnel:
        self.argv.append(cmd)
        return self

    def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        self.terminated = True


@pytest.fixture
def tunnel(monkeypatch: pytest.MonkeyPatch) -> _FakeTunnel:
    fake = _FakeTunnel()
    monkeypatch.setattr("subprocess.Popen", fake)
    return fake


def test_port_forward_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    tunnel: _FakeTunnel,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """port-forward on a reachable VM: the gate's fast path costs
    nothing, the site secret rides ONE boundary burst, and the SSH
    tunnel carries the -L specs inside the held span."""
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)

    # The service returns the SSH exit code; the CLI layer owns the
    # translation to process exit (check 9: no sys.exit in the service).
    assert vm_manager.port_forward_vm(db, config, "box", ["8080", "9000:3000"]) == 0

    assert resolve_counter == [["proxmox-token"]]
    (argv,) = tunnel.argv
    assert "localhost:8080:localhost:8080" in " ".join(argv)
    assert "localhost:9000:localhost:3000" in " ".join(argv)


def test_port_forward_stopped_vm_gates_then_forwards(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    tunnel: _FakeTunnel,
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
    monkeypatch.setattr(
        ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start")
    )
    monkeypatch.setattr(
        vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale")
    )

    assert vm_manager.port_forward_vm(db, config, "box", ["8080"]) == 0

    assert events == ["status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"]]
    assert len(tunnel.argv) == 1


def test_port_forward_bad_spec_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    tunnel: _FakeTunnel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.errors import ValidationError

    config = make_config()
    _seed_vm(db)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)
    _reachable(monkeypatch, False)

    with pytest.raises(ValidationError, match="invalid port"):
        vm_manager.port_forward_vm(db, config, "box", ["nope"])
    with pytest.raises(ValidationError, match="out of range"):
        vm_manager.port_forward_vm(db, config, "box", ["70000"])

    assert resolve_counter == []
    assert events == []
    assert tunnel.argv == []


def test_port_forward_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    tunnel: _FakeTunnel,
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

    assert vm_manager.port_forward_vm(db, config, "box", ["8080"]) == 0

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
    target = SSHTransport(host="100.64.0.9")
    target.run = lambda *a, **k: SimpleNamespace(stdout="", ok=True)  # type: ignore[method-assign, assignment]
    monkeypatch.setattr(
        "agentworks.transports.transport", lambda vm, config, **kwargs: target
    )
    return make_config(f'[paths]\nbackups = "{tmp_path}/backups"\n')


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

    backup_dir = vm_backup.backup_vm(db, backup_env, "box")

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
    monkeypatch.setattr(
        ProxmoxPlatform, "start", lambda self, row, ctx: gate_events.append("start")
    )
    monkeypatch.setattr(
        vm_manager,
        "_ensure_tailscale",
        lambda *a, **k: gate_events.append("tailscale"),
    )

    backup_dir = vm_backup.backup_vm(db, backup_env, "box")

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

    vm_backup.backup_vm(db, backup_env, "box")

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

    vm_backup.backup_vm(db, backup_env, "box")

    assert any(
        role is Role.HEADER and lvl == 0 and msg.startswith("Backing up VM 'box'")
        for role, lvl, msg in captured_output.lines
    )
    body_l1 = [
        msg for role, lvl, msg in captured_output.lines if role is Role.BODY and lvl == 1
    ]
    assert "Reading database (consistent snapshot)..." in body_l1
    assert "Exporting VM metadata..." in body_l1
    assert any(
        role is Role.RESULT and lvl == 0 and msg.startswith("Backup complete:")
        for role, lvl, msg in captured_output.lines
    )
