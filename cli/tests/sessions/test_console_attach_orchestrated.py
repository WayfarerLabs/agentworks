"""The named-console attach path through the orchestrated model.

Pins the lazily-decided console-node ruling
(there is NO console node: attach provisions nothing, so the graph is
the live VM alone), the gate-prompt parity carries, the gate's
held-active span covering the interactive attach (keep-active parity
with the imperative hold), the pre-gate no-Tailscale bail, and the VM
scope reaching node readiness.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops, the reachability probe, and the VM's
accesses (recording accesses) are the fakes.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.db import DesiredRuntimeState, ProvisioningStatus, SessionMode, VMStatus
from agentworks.errors import StateError, ValidationError
from agentworks.execution.access import Admin
from agentworks.execution.requests import TerminalCommand
from agentworks.execution.results import Completed, Result
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import multi_console
from agentworks.sessions.tmux import ProbeStatus
from agentworks.vms import manager as vm_manager
from tests.conftest import ManifestDoc, seed_vm_trust, stub_vm_ssh_identity
from tests.vm_access_support import AccessRecorder, Call, RecordingVMAccess, command_line, install_access_recorder

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database

ATTACH = TerminalCommand(("tmux", "attach", "-t", "=aw-console-c1"))


@pytest.fixture(autouse=True)
def _outside_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    stub_vm_ssh_identity(monkeypatch)


def _seed_vm(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
    db.update_vm_tailscale("box", "100.64.0.9")
    seed_vm_trust("box")


def _seed_named_console(db: Database) -> None:
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'box')")
    db._conn.commit()


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _stop_the_vm(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    _reachable(monkeypatch, False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or VMStatus.STOPPED,
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start"))
    monkeypatch.setattr("agentworks.vms.manager._await_tailnet", lambda *a, **k: events.append("tailscale"))


class _Guest:
    """The VM's recording accesses: the console tmux session already exists (the plain-attach path)."""

    def __init__(self, recorder: AccessRecorder) -> None:
        self.recorder = recorder

    @property
    def admin(self) -> RecordingVMAccess:
        return self.recorder.access(Admin())

    def attaches(self) -> list[object]:
        return [(call.identity, call.request) for call in self.recorder.calls if call.operation == "attach"]


def _console_running(call: Call) -> Result:
    if "aw-console-build+c1" in (command_line(call.request) or ""):
        return Result(Completed(1), b"", b"can't find session: aw-console-build+c1")
    return Result(Completed(0))


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> _Guest:
    recorder = install_access_recorder(monkeypatch)
    recorder.answer = _console_running
    return _Guest(recorder)


# -- the derived graph (the console-node ruling) ------------------------------


def test_attach_graph_is_the_live_vm_alone(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    """The lazily-decided console-node ruling: attach provisions
    nothing console-shaped, so no console node exists and the graph is
    the live VM (vm-site + vm), union = the site's config secret."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)

    nodes = walk(live_vm_node(db, config, registry, vm))

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box"]
    assert secret_union(nodes) == ("proxmox-token",)


def test_named_console_attach_holds_across_the_interactive_attach(
    db: Database,
    make_config,  # noqa: ANN001
    target: _Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Hold parity with the imperative caller-opened ``vm_active``
    span: the gate's held-active span covers the tmux probe and the
    interactive attach, closing after them."""
    config = make_config()
    _seed_vm(db)
    _seed_named_console(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.RUNNING)
    monkeypatch.setattr(vm_manager, "_await_tailnet", lambda *a, **kw: None)
    events: list[str] = []

    @contextlib.contextmanager
    def _recording_hold(self: ProxmoxPlatform, row: object, *, config: object = None):  # noqa: ANN202
        events.append("hold-open")
        try:
            yield
        finally:
            events.append("hold-close")

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _recording_hold)
    admin = target.admin
    attach = admin.attach

    def _tracking(request: TerminalCommand, **kwargs: bool) -> int:
        events.append("interactive")
        return attach(request, **kwargs)

    admin.attach = _tracking  # type: ignore[method-assign,assignment]

    multi_console.attach_console(db, config, name="c1", interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["hold-open", "hold-close", "hold-open", "interactive", "hold-close"]
    assert target.attaches() == [(Admin(), ATTACH)]


def test_console_restart_multiline_environment_secret_refuses_before_rebuild(
    db: Database,
    make_config,  # noqa: ANN001
    target: _Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_value = "console-restart-sentinel\nsecond\n"
    monkeypatch.setenv("AW_SECRET_CONSOLE_ENV", environment_value)
    config = make_config(
        manifests=[
            ManifestDoc(
                "vm-template",
                "default",
                {"env": {"CONSOLE_ENV": {"secret": "console-env"}}},
            ),
            ManifestDoc("secret", "console-env", description="console environment"),
        ]
    )
    _seed_vm(db)
    db.insert_console("c1", "box", admin_shell=True)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.RUNNING)
    monkeypatch.setattr(vm_manager, "_await_tailnet", lambda *a, **kw: None)
    monkeypatch.setattr(multi_console, "_console_tmux_presence", lambda *args: ProbeStatus.PRESENT)
    rebuilds: list[str] = []
    monkeypatch.setattr(
        "agentworks.sessions.multi_console._build_console_tmux",
        lambda *args, **kwargs: rebuilds.append("rebuild"),
    )

    with pytest.raises(ValidationError) as caught:
        multi_console.restart_console(
            db,
            config,
            name="c1",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert rebuilds == []
    assert target.attaches() == []
    assert db.get_console("c1") is not None
    assert "console-restart-sentinel" not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_named_console_stopped_vm_gate_burst_seeds_the_boundary(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _Guest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_vm(db)
    _seed_named_console(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    assert multi_console.attach_console(db, config, name="c1", interaction=TtyInteractionPolicy.REFUSE) == 0

    assert events == ["status", "status", "start", "tailscale"]
    assert resolve_counter == [["proxmox-token"]]
    assert target.attaches() == [(Admin(), ATTACH)]


def test_named_console_no_tailscale_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    db.insert_vm("box", site="proxmox", hostname="box")  # no tailscale host
    db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
    _seed_named_console(db)
    _reachable(monkeypatch, False)

    def _status_must_not_run(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for an unattachable VM")

    monkeypatch.setattr(ProxmoxPlatform, "status", _status_must_not_run)

    with pytest.raises(StateError, match="no Tailscale address"):
        multi_console.attach_console(db, config, name="c1", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []


def _restore_guest(commands: list[str]) -> Callable[[Call], Result]:
    """The restore path's guest: the console tmux session exists, the session window is present, and
    the window carries only its session pane (no shell panes), so a zero-shell member is already intact."""

    def answer(call: Call) -> Result:
        cmd = command_line(call.request) or ""
        commands.append(cmd)
        if "window_index" in cmd and "window_name" in cmd:
            return Result(Completed(0), b"0|s1\n")
        if "list-windows" in cmd:
            return Result(Completed(0), b"s1\n")
        if "list-panes" in cmd:
            # Pane 0 is the session pane; no tagged shell panes.
            return Result(Completed(0), b"%0|0|\n")
        return Result(Completed(0))

    return answer


def test_restore_session_stopped_vm_drives_the_real_gated_composition(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """restore_session end to end through the real orchestrated helper
    on a stopped VM: the gate's just-in-time token resolve fully seeds
    the union (one backend pass total), the gate starts the VM, and
    the tmux reconciliation (probe, window check, pane check, landing
    focus) runs inside the gate span; the already-intact member is a
    reported no-op."""
    config = make_config()
    _seed_vm(db)
    _seed_named_console(db)
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'box', '/srv/ws1', 'ws-ws1')"
    )
    db.insert_session("s1", "ws1", "default", SessionMode.ADMIN, socket_path="/tmp/s1.sock")
    db._conn.execute(
        "INSERT INTO console_sessions (console_name, session_name, shells, position) VALUES ('c1', 's1', '[]', 0)"
    )
    db._conn.commit()
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)
    commands: list[str] = []
    install_access_recorder(monkeypatch).answer = _restore_guest(commands)

    multi_console.restore_session(
        db, config, console_name="c1", session_name="s1", interaction=TtyInteractionPolicy.REFUSE
    )

    assert events == ["status", "status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"]]
    assert any("has-session -t '=aw-console-c1'" in c for c in commands)
    assert any("list-windows -t '=aw-console-c1'" in c for c in commands)
    # The no-op landing focus still fires (post-restore focus parity).
    assert any("select-pane -t '=aw-console-c1':s1.0" in c for c in commands)
    assert any("already matches config" in m for m in captured_output.info)


@pytest.mark.windows
@pytest.mark.parametrize("status", [VMStatus.RUNNING, VMStatus.STOPPED, VMStatus.DEALLOCATED, VMStatus.UNKNOWN])
def test_console_attach_with_stopped_parent_intent_is_live_diagnostics_only(
    db, make_config, target, monkeypatch, resolve_counter, status
):
    from agentworks.harness_setup.locking import native_mutation_guard

    config = make_config()
    _seed_vm(db)
    _seed_named_console(db)
    vm = db.get_vm("box")
    assert vm is not None
    db.set_vm_desired_runtime_state("box", DesiredRuntimeState.STOPPED, expected_vm_uuid=vm.vm_uuid)
    before_vm = db.get_vm("box")
    before_console = db.get_console("c1")
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda *a: status)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda *a: pytest.fail("diagnostic attach started its parent"))
    monkeypatch.setattr(ProxmoxPlatform, "vm_active", lambda *a, **k: pytest.fail("stopped intent acquired idle hold"))
    monkeypatch.setattr(vm_manager, "_await_tailnet", lambda *a, **k: pytest.fail("diagnostic attach waited"))
    admin = target.admin
    attach = admin.attach

    def unlocked_attach(request, **kwargs):
        with native_mutation_guard(db.path, "box"):
            return attach(request, **kwargs)

    admin.attach = unlocked_attach
    if status is VMStatus.RUNNING:
        assert multi_console.attach_console(db, config, name="c1", interaction=TtyInteractionPolicy.REFUSE) == 0
        assert len(target.attaches()) == 1
    else:
        with pytest.raises(StateError):
            multi_console.attach_console(db, config, name="c1", interaction=TtyInteractionPolicy.REFUSE)
        assert target.attaches() == []
    assert resolve_counter == [["proxmox-token"]]
    assert db.get_vm("box") == before_vm
    assert db.get_console("c1") == before_console
