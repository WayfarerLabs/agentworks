"""Shared runtime composition runs real domain helpers against bounded fakes."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import PID_STOPPED, DesiredRuntimeState, InitStatus, SessionMode, SessionStatus, VMStatus
from agentworks.errors import AgentworksError, ValidationError
from agentworks.harness_setup.locking import native_mutation_guard
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.runtime_state import reconcile_console, reconcile_session, reconcile_vm
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from agentworks.sessions import multi_console as mc
from agentworks.sessions.tmux import ProbeStatus, TmuxServerFingerprint
from tests.conftest import ManifestDoc, stub_vm_ssh_identity
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from agentworks.db import Database, VMRow


REFUSE = TtyInteractionPolicy.REFUSE
BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def seed_vm(
    db: Database, name: str, *, target: DesiredRuntimeState = DesiredRuntimeState.RUNNING, site: str = "proxmox"
) -> VMRow:
    row = db.insert_vm(name, site, name, admin_username="admin", desired_runtime_state=target)
    db.update_vm_tailscale(name, "100.64.0.5")
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


def unavailable_site() -> ManifestDoc:
    return ManifestDoc(
        "vm-site",
        "unavailable",
        {
            "platform": {
                "name": "proxmox",
                "api_url": "https://unavailable:8006",
                "node": "pve1",
                "token_id": "agw@pam!agw",
                "token_secret": "shared-unavailable",
                "template_vmids": {"trixie": 9001},
            }
        },
    )


def test_shared_failed_gate_is_attempted_once_and_isolates_unrelated_family(db, make_config, runtime, resolve_counter):
    seed_vm(db, "bad-a", site="unavailable")
    seed_vm(db, "bad-b", site="unavailable")
    seed_vm(db, "good")
    seed_session(db, "work", "good")
    config = make_config(manifests=[unavailable_site()])
    with pytest.raises(AgentworksError):
        reconcile_vm(db, config, all_vms=True, interaction=REFUSE)
    assert sum(call == ["shared-unavailable"] for call in resolve_counter) == 1
    assert ("session-start", "work") in runtime[2]


def test_failed_gate_overlap_blocks_complete_union_without_retry_or_other_lookup(
    db, make_config, runtime, resolve_counter
):
    seed_vm(db, "bad", site="unavailable")
    seed_vm(db, "good")
    seed_session(db, "work", "good", template="overlap")
    config = make_config(
        manifests=[
            unavailable_site(),
            ManifestDoc(
                "session-template",
                "overlap",
                {
                    "harness_integration": {"name": "shell"},
                    "env": {"TOKEN": {"secret": "shared-unavailable"}, "OTHER": {"secret": "unused-lookup"}},
                },
            ),
        ]
    )
    with pytest.raises(AgentworksError):
        reconcile_vm(db, config, all_vms=True, interaction=REFUSE)
    assert sum(call == ["shared-unavailable"] for call in resolve_counter) == 1
    assert not any("unused-lookup" in call for call in resolve_counter)
    assert not any(action == "session-start" for action, _ in runtime[2])


def test_shared_conditional_repair_key_resolves_once(db, make_config, runtime, resolve_counter, monkeypatch):
    seed_vm(db, "a")
    seed_vm(db, "b")
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "shared-rejoin-value")
    monkeypatch.setattr("agentworks.vms.manager._tailscale_rejoin_required", lambda *a, **k: True)
    repaired = []

    def repair(db_, config, row, *args, **kwargs):
        assert kwargs["auth_keys"].get(kwargs["auth_key_name"]) == "shared-rejoin-value"
        repaired.append(row.name)

    monkeypatch.setattr("agentworks.vms.manager._ensure_tailscale", repair)
    reconcile_vm(db, make_config(), all_vms=True, interaction=REFUSE)
    assert repaired == ["a", "b"]
    assert sum(call == ["tailscale-auth-key"] for call in resolve_counter) == 1


def test_activation_precedes_unrelated_missing_workload_and_preserves_completed_effects(db, make_config, runtime):
    powers, _, events = runtime
    seed_vm(db, "empty")
    seed_vm(db, "all-stopped")
    seed_session(db, "idle", "all-stopped", target=DesiredRuntimeState.STOPPED)
    seed_vm(db, "launch")
    seed_session(db, "work", "launch", template="requires-token")
    powers.update({"empty": VMStatus.STOPPED, "all-stopped": VMStatus.STOPPED})
    config = make_config(
        manifests=[
            ManifestDoc(
                "session-template",
                "requires-token",
                {"harness_integration": {"name": "shell"}, "env": {"TOKEN": {"secret": "missing-workload"}}},
            )
        ]
    )
    with pytest.raises(AgentworksError):
        reconcile_vm(db, config, all_vms=True, interaction=REFUSE)
    assert ("vm-start", "empty") in events and ("vm-start", "all-stopped") in events
    assert not any(action == "session-start" for action, _ in events)


def test_console_focus_never_expands_member_sessions_or_changes_member_intent(db, make_config, runtime, monkeypatch):
    seed_vm(db, "box", target=DesiredRuntimeState.ON_DEMAND)
    seed_session(db, "member", "box", target=DesiredRuntimeState.STOPPED)
    create_console_record(db, name="view", vm_name="box", session_specs=["member"])
    selected = db.get_session("member")
    monkeypatch.setattr(
        manager, "prepare_session_definition", lambda *a, **k: pytest.fail("selected Console member session")
    )
    monkeypatch.setattr(mc, "_console_runtime_presence", lambda *a: (ProbeStatus.PRESENT, ProbeStatus.ABSENT))
    reconcile_console(db, make_config(), name="view", interaction=REFUSE)
    assert db.get_session("member") == selected


def test_busy_family_does_not_prevent_independent_start(db, make_config, runtime):
    seed_vm(db, "busy")
    seed_vm(db, "good")
    seed_session(db, "work", "good")
    with native_mutation_guard(db.path, "busy"), pytest.raises(AgentworksError):
        reconcile_vm(db, make_config(), all_vms=True, interaction=REFUSE)
    assert ("session-start", "work") in runtime[2]
