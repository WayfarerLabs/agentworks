"""Shared runtime composition runs real domain helpers against bounded fakes."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agentworks.db import PID_STOPPED, DesiredRuntimeState, InitStatus, SessionMode, SessionStatus, VMStatus
from agentworks.errors import AgentworksError, ValidationError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.runtime_state import reconcile_session, reconcile_vm
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from agentworks.sessions import multi_console as mc
from agentworks.sessions.tmux import ProbeStatus, TmuxServerFingerprint
from tests.conftest import ManifestDoc, stub_vm_ssh_identity
from tests.console_helpers import create_console_record

REFUSE = TtyInteractionPolicy.REFUSE
BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def seed_vm(db, name, *, target=DesiredRuntimeState.RUNNING):
    row = db.insert_vm(name, "proxmox", name, admin_username="admin", desired_runtime_state=target)
    db.update_vm_tailscale(name, "100.64.0.5")
    db.update_vm_init_status(name, InitStatus.COMPLETE)
    db._conn.execute(
        "INSERT INTO workspaces (name,vm_name,workspace_path,linux_group) VALUES(?,?,?,?)",
        (f"ws-{name}", name, f"/srv/{name}", f"ws-{name}"),
    )
    db._conn.commit()
    return row


def seed_session(db, name, vm, *, template="default", target=DesiredRuntimeState.RUNNING):
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
    powers, statuses, events = {}, {}, []
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
    monkeypatch.setattr("agentworks.vms.manager._tailscale_rejoin_required", lambda *a, **k: False)
    monkeypatch.setattr(manager, "check_session_status", lambda row, **k: statuses.get(row.name, SessionStatus.STOPPED))
    monkeypatch.setattr(
        manager,
        "observe_session_statuses",
        lambda rows, **k: {row.name: statuses.get(row.name, SessionStatus.STOPPED) for row in rows},
    )

    class Target:
        def run(self, command, **kwargs):
            return SimpleNamespace(
                returncode=0,
                ok=True,
                stdout="AGW_RUNTIME_STARTUP=off\n" if "AGW_RUNTIME_STARTUP=" in command else "",
                stderr="",
            )

        def write_file(self, *args, **kwargs):
            pass

    target = Target()
    monkeypatch.setattr(manager, "transport", lambda *a, **k: target)
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: target)
    monkeypatch.setattr(manager, "_get_boot_id", lambda target: BOOT)
    monkeypatch.setattr(manager, "_regenerate_tmuxinator", lambda *a, **k: None)
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


def test_invalid_vm_selector_is_local(db, monkeypatch):
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *a, **k: pytest.fail("loaded registry"))
    with pytest.raises(ValidationError):
        reconcile_vm(db, object(), interaction=REFUSE)
    with pytest.raises(ValidationError):
        reconcile_vm(db, object(), name="x", all_vms=True, interaction=REFUSE)


def test_childless_running_vm_activates_without_changing_target(db, make_config, runtime):
    powers, _, events = runtime
    selected = seed_vm(db, "empty")
    powers["empty"] = VMStatus.STOPPED
    reconcile_vm(db, make_config(), name="empty", interaction=REFUSE)
    assert ("vm-start", "empty") in events
    assert db.get_vm("empty").vm_uuid == selected.vm_uuid
    assert db.get_vm("empty").desired_runtime_state is DesiredRuntimeState.RUNNING


def test_repeated_session_reconcile_preserves_healthy_runtime_identity(db, make_config, runtime):
    _, _, events = runtime
    seed_vm(db, "box", target=DesiredRuntimeState.ON_DEMAND)
    seed_session(db, "work", "box")
    config = make_config()
    reconcile_session(db, config, name="work", interaction=REFUSE)
    first = db.get_session("work")
    reconcile_session(db, config, name="work", interaction=REFUSE)
    assert db.get_session("work") == first
    assert events.count(("session-start", "work")) == 1
    assert db.get_vm("box").desired_runtime_state is DesiredRuntimeState.ON_DEMAND


def test_workload_secret_failure_blocks_all_tmux_and_provider_stops(db, make_config, runtime, monkeypatch):
    powers, _, events = runtime
    seed_vm(db, "launch")
    seed_session(db, "work", "launch", template="requires-token")
    seed_vm(db, "shutdown", target=DesiredRuntimeState.STOPPED)
    powers["shutdown"] = VMStatus.RUNNING
    create_console_record(db, name="view", vm_name="launch", session_specs=[], add_admin_shell=True)
    monkeypatch.setattr(mc, "_console_runtime_presence", lambda *a: (ProbeStatus.ABSENT, ProbeStatus.PRESENT))
    monkeypatch.setattr(
        mc, "_build_console_tmux", lambda *a, **k: pytest.fail("console built before complete secret boundary")
    )
    monkeypatch.setattr(
        "agentworks.sessions.multi_console.attach._teardown_console_staging", lambda *a: pytest.fail("staging cleaned")
    )
    config = make_config(
        manifests=[
            ManifestDoc(
                "session-template",
                "requires-token",
                {"harness_integration": {"name": "shell"}, "env": {"TOKEN": {"secret": "missing-workload-key"}}},
            )
        ]
    )
    with pytest.raises(AgentworksError):
        reconcile_vm(db, config, all_vms=True, interaction=REFUSE)
    assert not any(action in {"session-start", "vm-stop"} for action, _ in events)
    assert powers["shutdown"] is VMStatus.RUNNING


def test_off_child_stop_never_enters_guest_or_hold(db, make_config, runtime, monkeypatch):
    powers, _, events = runtime
    seed_vm(db, "box", target=DesiredRuntimeState.STOPPED)
    seed_session(db, "work", "box", target=DesiredRuntimeState.STOPPED)
    powers["box"] = VMStatus.STOPPED
    monkeypatch.setattr(manager, "transport", lambda *a, **k: pytest.fail("off guest transport"))
    reconcile_session(db, make_config(), name="work", interaction=REFUSE)
    assert not events


def test_dry_run_has_no_native_guard_or_workload_sources(db, make_config, runtime, monkeypatch):
    powers, _, events = runtime
    seed_vm(db, "box")
    seed_session(db, "work", "box")
    powers["box"] = VMStatus.STOPPED
    before = db.get_session("work")
    monkeypatch.setattr(
        "agentworks.harness_setup.locking.native_mutation_guard", lambda *a, **k: pytest.fail("native guard acquired")
    )
    monkeypatch.setattr(manager, "prepare_session_launch", lambda *a, **k: pytest.fail("guest readiness invoked"))
    reconcile_session(db, make_config(), name="work", dry_run=True, interaction=REFUSE)
    assert not events
    assert db.get_session("work") == before
    assert not (db.path.parent / f".{db.path.name}.native-locks").exists()
