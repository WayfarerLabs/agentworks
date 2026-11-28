"""The shared runtime composition harness: seeded VMs and sessions, and the ``runtime`` fixture's bounded fakes.

Tests run real domain helpers against these fakes: the provider's power
operations, the guest's answers, and the tmux launch.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from agentworks.db import (
    PID_STOPPED,
    DesiredRuntimeState,
    InitStatus,
    ProvisioningStatus,
    SessionMode,
    SessionStatus,
    VMStatus,
)
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from agentworks.sessions.tmux import ProbeStatus, TmuxServerFingerprint
from tests.conftest import seed_vm_trust, stub_vm_ssh_identity
from tests.vm_access_support import Call, command_line, guest_result, is_command_check, route_vm_access_to_guest

if TYPE_CHECKING:
    from agentworks.db import Database, VMRow
    from agentworks.execution.results import Result


REFUSE = TtyInteractionPolicy.REFUSE
# Bounds the waits on worker threads only so a broken run fails instead of hanging. Healthy runs end
# the waits in milliseconds; a loaded host can still delay a thread by seconds.
HANG_GUARD_SECONDS = 30
BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def seed_vm(
    db: Database, name: str, *, target: DesiredRuntimeState = DesiredRuntimeState.RUNNING, site: str = "proxmox"
) -> VMRow:
    row = db.insert_vm(name, site, name, admin_username="admin", desired_runtime_state=target)
    db.update_vm_provisioning_status(name, ProvisioningStatus.COMPLETE)
    db.update_vm_tailscale(name, "100.64.0.5")
    seed_vm_trust(name)
    db.update_vm_init_status(name, InitStatus.COMPLETE)
    db._conn.execute(
        "INSERT INTO workspaces (name,vm_name,workspace_path,linux_group) VALUES(?,?,?,?)",
        (f"ws-{name}", name, f"/srv/{name}", f"ws-{name}"),
    )
    db._conn.commit()
    return row


def seed_session(
    db: Database,
    name: str,
    vm: str,
    *,
    template: str = "default",
    target: DesiredRuntimeState = DesiredRuntimeState.RUNNING,
) -> None:
    db.insert_session(
        name,
        f"ws-{vm}",
        template,
        SessionMode.ADMIN,
        socket_path=f"/run/agentworks/admin-tmux-sockets/admin/{name}.sock",
        desired_runtime_state=target,
    )
    db.update_session_runtime(
        name,
        socket_path=f"/run/agentworks/admin-tmux-sockets/admin/{name}.sock",
        pid=PID_STOPPED,
        boot_id=None,
        tmux_server_start_ticks=None,
    )


@pytest.fixture
def runtime(monkeypatch):
    powers: dict[str, VMStatus] = {}
    statuses: dict[str, SessionStatus] = {}
    events: list[tuple[str, str]] = []
    stub_vm_ssh_identity(monkeypatch)
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: powers.get(row.name, VMStatus.RUNNING))

    def start(self, row, ctx):
        events.append(("vm-start", row.name))
        powers[row.name] = VMStatus.RUNNING

    def stop(self, row, ctx):
        events.append(("vm-stop", row.name))
        powers[row.name] = VMStatus.STOPPED

    @contextmanager
    def hold(self, row, **kwargs):
        events.append(("hold", row.name))
        yield

    monkeypatch.setattr(ProxmoxPlatform, "start", start)
    monkeypatch.setattr(ProxmoxPlatform, "stop", stop)
    monkeypatch.setattr(ProxmoxPlatform, "vm_active", hold)
    monkeypatch.setattr("agentworks.vms.manager._await_tailnet", lambda *a, **k: None)
    # The workstation's own Tailscale check: CI runners have no tailscale binary.
    monkeypatch.setattr("agentworks.vms.manager.verify_tailscale_available", lambda: None)
    monkeypatch.setattr(manager, "check_session_status", lambda row, **k: statuses.get(row.name, SessionStatus.STOPPED))
    monkeypatch.setattr(
        manager,
        "observe_session_statuses",
        lambda rows, **k: {row.name: statuses.get(row.name, SessionStatus.STOPPED) for row in rows},
    )

    def guest(call: Call) -> Result | None:
        command = command_line(call.request)
        if command is None or is_command_check(call.request):
            return None
        return guest_result(stdout="AGW_RUNTIME_STARTUP=off\n" if "AGW_RUNTIME_STARTUP=" in command else "")

    route_vm_access_to_guest(monkeypatch, guest)
    monkeypatch.setattr(manager, "_get_boot_id", lambda run_command: BOOT)
    monkeypatch.setattr(tmux, "deploy_restricted_config", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "finalize_session_start", lambda *a, **k: None)

    def spawn(name, *args, **kwargs):
        kwargs["before_spawn"]()
        events.append(("session-start", name))
        statuses[name] = SessionStatus.RUNNING
        return f"/run/agentworks/admin-tmux-sockets/admin/{name}.sock", 4243

    monkeypatch.setattr(tmux, "create_session", spawn)
    monkeypatch.setattr(
        tmux,
        "capture_tmux_server_fingerprint",
        lambda **k: tmux.FingerprintProbe(ProbeStatus.PRESENT, TmuxServerFingerprint(4243, BOOT, 1)),
    )
    return powers, statuses, events


def running_session(db, statuses, name, vm, *, agent=False) -> None:
    """A session on ``vm`` that reads as running: an admin one, or with ``agent`` one of the VM's agent."""
    if agent:
        agent_name = f"agent-{vm}"
        if db.get_agent(agent_name) is None:
            db.insert_agent(agent_name, vm, f"agt-{vm}")
        socket = f"/run/agentworks/agent-tmux-sockets/agt-{vm}/{name}.sock"
        db.insert_session(name, f"ws-{vm}", "default", SessionMode.AGENT, agent_name=agent_name, socket_path=socket)
    else:
        seed_session(db, name, vm)
        socket = db.get_session(name).socket_path
    ordinal = len(statuses) + 1
    db.update_session_runtime(
        name, socket_path=socket, pid=100 + ordinal, boot_id=BOOT, tmux_server_start_ticks=200 + ordinal
    )
    statuses[name] = SessionStatus.RUNNING
