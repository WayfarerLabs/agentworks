"""Session lifecycle selection, observation, and shared runtime composition."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.db import PID_STOPPED, ProvisioningStatus, SessionMode, SessionStatus, VMStatus
from agentworks.errors import NotFoundError, StateError
from agentworks.execution.errors import TrustMissing
from agentworks.output import Role
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager as session_manager
from agentworks.sessions.manager._queries import session_listing_data
from agentworks.vms import manager as vm_manager
from agentworks.vms.applied_state import VMSSHIdentityPolicyError
from tests._runtime_probes import framed_runtime_result
from tests.conftest import seed_vm_trust, stub_vm_ssh_identity

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database

BOOT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def _stub_ssh_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_vm_ssh_identity(monkeypatch)


def _seed_vm(db: Database, name: str, host: str | None, *, site: str = "proxmox") -> None:
    db.insert_vm(name, site=site, hostname=name)
    db.update_vm_provisioning_status(name, ProvisioningStatus.COMPLETE)
    if host is not None:
        db.update_vm_tailscale(name, host)
    seed_vm_trust(name)
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
        (f"ws-{name}", name, f"/srv/ws-{name}", f"ws-ws-{name}"),
    )
    db._conn.commit()


def _seed_session(db: Database, name: str, ws: str, *, agent: str | None = None) -> None:
    db.insert_session(
        name,
        ws,
        "default",
        SessionMode.AGENT if agent else SessionMode.ADMIN,
        agent_name=agent,
        socket_path=f"/tmp/{name}.sock",
    )
    db.update_session_runtime(
        name, socket_path=f"/tmp/{name}.sock", pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _stop_the_vms(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Backend fakes for the stopped-VM gate path, recording per-VM."""
    _reachable(monkeypatch, False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append(f"status:{row.name}") or VMStatus.STOPPED,
    )
    monkeypatch.setattr(
        ProxmoxPlatform,
        "start",
        lambda self, row, ctx: events.append(f"start:{row.name}"),
    )
    monkeypatch.setattr(
        "agentworks.vms.manager._await_tailnet",
        lambda db, config, vm, **k: events.append(f"tailscale:{vm.name}"),
    )


def _record_holds(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    @contextlib.contextmanager
    def _recording_hold(self: ProxmoxPlatform, row: object, *, config: object = None):  # noqa: ANN202
        events.append(f"hold-open:{row.name}")  # type: ignore[attr-defined]
        try:
            yield
        finally:
            events.append(f"hold-close:{row.name}")  # type: ignore[attr-defined]

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _recording_hold)


def test_best_effort_status_and_pid_repair_skip_identity_refusal_before_transport(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_vm(db, "vm-a", "100.64.0.11")
    db.insert_session("s-a", "ws-vm-a", "default", SessionMode.ADMIN, socket_path="/tmp/s-a.sock")
    session = db.get_session("s-a")
    assert session is not None

    def refuse(*args: object, **kwargs: object) -> None:
        raise VMSSHIdentityPolicyError("SSH identity drift")

    monkeypatch.setattr(vm_manager, "require_vm_ssh_boundary", refuse)
    monkeypatch.setattr(
        session_manager,
        "transport",
        lambda *args, **kwargs: pytest.fail("transport constructed after identity refusal"),
    )

    assert session_manager.observe_session_statuses([session], db=db, config=SimpleNamespace()) == {
        "s-a": SessionStatus.UNKNOWN
    }
    assert session_manager.ensure_pids_batch([session], db=db, config=SimpleNamespace()) == [session]


def test_list_status_reports_identity_refusal_as_unknown_without_transport(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    _seed_vm(db, "vm-a", "100.64.0.11")
    db.insert_session("s-a", "ws-vm-a", "default", SessionMode.ADMIN, socket_path="/tmp/s-a.sock")
    db.update_session_runtime("s-a", socket_path="/tmp/s-a.sock", pid=4321, boot_id=BOOT_ID, tmux_server_start_ticks=77)

    def refuse(*args: object, **kwargs: object) -> None:
        raise VMSSHIdentityPolicyError("SSH identity drift")

    monkeypatch.setattr(vm_manager, "require_vm_ssh_boundary", refuse)
    monkeypatch.setattr(
        session_manager,
        "transport",
        lambda *args, **kwargs: pytest.fail("transport constructed after identity refusal"),
    )

    listing = session_manager.session_listing(
        db,
        make_config(),
        include_status=True,
    )

    assert listing.sessions[0].status == "unknown"
    json_rows = cast("list[dict[str, object]]", session_listing_data(listing)["sessions"])
    assert json_rows[0]["status"] == "unknown"

    session_manager.render_session_listing(listing, include_status=True)
    assert sum(role is Role.WARNING for role, _level, _message in captured_output.lines) == 1


class _FakeTarget:
    """Transport double for the probe / kill / regenerate helpers."""

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        return framed_runtime_result(cmd, SimpleNamespace(ok=True, returncode=0, stdout="", stderr=""))

    def interactive(self, cmd: str, **kwargs: object) -> int:
        return 0

    def write_file(self, path: str, content: str, **kwargs: object) -> None:
        return None


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> _FakeTarget:
    fake = _FakeTarget()
    factory = lambda vm, config, **kwargs: fake  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", factory)
    # sessions.manager imports ``transport`` eagerly at module load.
    monkeypatch.setattr(session_manager, "transport", factory)
    return fake


# -- the batch graph (the shared site-node memo) ------------------------------


def test_batch_graph_two_vms_share_one_site_node(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    """Two live VM nodes on one site MUST share one ``VMSiteNode``
    object (the walk raises otherwise); the ``site_nodes`` memo is the
    sharing mechanism, and the batch union is the site's config secret
    once."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import VMSiteNode, live_vm_node

    config = make_config()
    _seed_vm(db, "vm-a", "100.64.0.11")
    _seed_vm(db, "vm-b", "100.64.0.12")
    vm_a, vm_b = db.get_vm("vm-a"), db.get_vm("vm-b")
    assert vm_a is not None and vm_b is not None
    registry = build_registry(config)

    site_nodes: dict[str, VMSiteNode] = {}
    node_a = live_vm_node(db, config, registry, vm_a, site_nodes=site_nodes)
    node_b = live_vm_node(db, config, registry, vm_b, site_nodes=site_nodes)
    nodes = walk(node_a, node_b)

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/vm-a", "vm/vm-b"]
    assert nodes[0] is node_a.deps()[0]
    assert nodes[0] is node_b.deps()[0]
    assert secret_union(nodes) == ("proxmox-token",)


# -- the batch composition ----------------------------------------------------


def test_stop_all_accepts_absent_matches_without_runtime_activation(
    db, make_config, resolve_counter, target, monkeypatch
):
    from agentworks.db import DesiredRuntimeState

    _seed_vm(db, "vm-a", "100.64.0.11")
    _seed_vm(db, "vm-b", "100.64.0.12")
    _seed_session(db, "s-a", "ws-vm-a")
    _seed_session(db, "s-b", "ws-vm-b")
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda *a: VMStatus.RUNNING)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda *a: pytest.fail("stop activated VM"))
    monkeypatch.setattr(ProxmoxPlatform, "vm_active", lambda *a, **k: pytest.fail("stop opened active hold"))
    monkeypatch.setattr(session_manager, "check_session_status", lambda *a, **k: SessionStatus.STOPPED)
    session_manager.stop_all_sessions(db, make_config(), interaction=TtyInteractionPolicy.REFUSE)
    assert resolve_counter == [["proxmox-token"]]
    assert all(db.get_session(name).desired_runtime_state is DesiredRuntimeState.STOPPED for name in ("s-a", "s-b"))


def test_stop_all_off_parents_never_start_or_open_holds(db, make_config, resolve_counter, monkeypatch):
    from agentworks.db import DesiredRuntimeState

    for name in ("vm-a", "vm-b"):
        _seed_vm(db, name, "100.64.0.11")
        _seed_session(db, f"s-{name}", f"ws-{name}")
    events: list[str] = []
    _stop_the_vms(monkeypatch, events)
    _record_holds(monkeypatch, events)
    session_manager.stop_all_sessions(db, make_config(), interaction=TtyInteractionPolicy.REFUSE)
    assert all(event.startswith("status:") for event in events)
    assert resolve_counter == [["proxmox-token"]]
    assert all(
        db.get_session(f"s-{name}").desired_runtime_state is DesiredRuntimeState.STOPPED for name in ("vm-a", "vm-b")
    )


def test_batch_empty_vm_set_is_a_complete_noop(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """An empty VM set costs zero registry builds, zero resolver work,
    and zero gate activity (the imperative lazy-bind property)."""
    import agentworks.bootstrap as bootstrap

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("build_registry must not run for an empty VM set")

    monkeypatch.setattr(bootstrap, "build_registry", _boom)

    session_manager.stop_all_sessions(db, make_config(), interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert any("No running sessions to stop" in m for m in captured_output.info)


@pytest.mark.parametrize("operation_name", ["start_all_sessions", "restart_all_sessions"])
def test_batch_launch_delegates_all_selected_matches(db, make_config, monkeypatch, operation_name):
    _seed_vm(db, "box", "100.64.0.9")
    for name in ("running", "stopped", "residual", "broken"):
        _seed_session(db, name, "ws-box")
    selected: list[str] = []
    monkeypatch.setattr(
        session_manager, "observe_session_statuses", lambda rows, **k: {row.name: SessionStatus.STOPPED for row in rows}
    )
    monkeypatch.setattr(
        "agentworks.runtime_state.run_session_batch",
        lambda db_, config, rows, **k: selected.extend(row.name for row in rows),
    )
    getattr(session_manager, operation_name)(db, make_config(), interaction=TtyInteractionPolicy.REFUSE)
    assert selected == ["broken", "residual", "running", "stopped"]


def _stub_batch_restart(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    statuses: dict[str, SessionStatus],
) -> list[tuple[str, bool | None]]:
    _seed_vm(db, "box", "100.64.0.9")
    for name in statuses:
        _seed_session(db, name, "ws-box")
    launched: list[tuple[str, bool | None]] = []
    monkeypatch.setattr(session_manager, "observe_session_statuses", lambda selected, **kwargs: statuses)

    def run_batch(db_, config, rows, *, running_restart_authorized, **kwargs):
        launched.extend((row.name, row.name in running_restart_authorized) for row in rows)

    monkeypatch.setattr("agentworks.runtime_state.run_session_batch", run_batch)
    return launched


def test_batch_restart_confirms_once_and_authorizes_only_observed_running_names(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = _stub_batch_restart(
        db,
        monkeypatch,
        {"running": SessionStatus.RUNNING, "stopped": SessionStatus.STOPPED},
    )
    confirmations: list[None] = []
    monkeypatch.setattr(
        "agentworks.sessions.manager._lifecycle.output.confirm",
        lambda *_args, **_kwargs: confirmations.append(None) is None,
    )

    session_manager.restart_all_sessions(
        db,
        make_config(),
        interaction=TtyInteractionPolicy.ALLOW,
    )

    assert len(confirmations) == 1
    assert launched == [("running", True), ("stopped", False)]


def test_batch_restart_refuses_a_recreated_session_before_preparation(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_vm(db, "box", "100.64.0.9")
    _seed_session(db, "running", "ws-box")
    selected = db.get_session("running")
    assert selected is not None
    replacement_rows = []

    monkeypatch.setattr(
        session_manager,
        "observe_session_statuses",
        lambda sessions, **kwargs: {"running": SessionStatus.RUNNING},
    )

    def _replace_and_confirm(*args: object, **kwargs: object) -> bool:
        db.delete_session("running")
        _seed_session(db, "running", "ws-box")
        replacement = db.get_session("running")
        assert replacement is not None
        replacement_rows.append(replacement)
        return True

    monkeypatch.setattr(
        "agentworks.sessions.manager._lifecycle.output.confirm",
        _replace_and_confirm,
    )
    monkeypatch.setattr(
        session_manager,
        "ensure_pids_batch",
        lambda *args, **kwargs: pytest.fail("identity refusal must precede PID repair"),
    )
    monkeypatch.setattr(
        "agentworks.sessions.manager._teardown_session",
        lambda *args, **kwargs: pytest.fail("identity refusal must precede teardown"),
    )

    with pytest.raises(StateError):
        session_manager.restart_all_sessions(
            db,
            make_config(),
            interaction=TtyInteractionPolicy.ALLOW,
        )

    replacement = db.get_session("running")
    assert replacement is not None
    assert replacement.session_uuid != selected.session_uuid
    assert replacement_rows == [replacement]


def test_batch_restart_noninteractive_refusal_precedes_every_launch(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = _stub_batch_restart(db, monkeypatch, {"running": SessionStatus.RUNNING})
    monkeypatch.setattr(
        session_manager,
        "ensure_pids_batch",
        lambda *args, **kwargs: pytest.fail("refusal must precede PID repair"),
    )

    with pytest.raises(StateError):
        session_manager.restart_all_sessions(
            db,
            make_config(),
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert launched == []


def test_declined_batch_restart_precedes_every_launch(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.errors import UserAbort

    launched = _stub_batch_restart(db, monkeypatch, {"running": SessionStatus.RUNNING})
    monkeypatch.setattr(
        session_manager,
        "ensure_pids_batch",
        lambda *args, **kwargs: pytest.fail("decline must precede PID repair"),
    )
    monkeypatch.setattr(
        "agentworks.sessions.manager._lifecycle.output.confirm",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(UserAbort):
        session_manager.restart_all_sessions(
            db,
            make_config(),
            interaction=TtyInteractionPolicy.ALLOW,
        )

    assert launched == []


@pytest.mark.parametrize("yes", [False, True])
def test_batch_restart_with_only_non_running_sessions_does_not_confirm_or_authorize_running_race(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    yes: bool,
) -> None:
    launched = _stub_batch_restart(db, monkeypatch, {"stopped": SessionStatus.STOPPED})
    monkeypatch.setattr(
        "agentworks.sessions.manager._lifecycle.output.confirm",
        lambda *_args, **_kwargs: pytest.fail("non-running batch must not confirm"),
    )

    session_manager.restart_all_sessions(
        db,
        make_config(),
        yes=yes,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert launched == [("stopped", False)]


def test_batch_stop_under_stopped_parent_accepts_without_guest_probes(db, make_config, monkeypatch):
    from agentworks.db import DesiredRuntimeState

    _seed_vm(db, "vm-a", "100.64.0.11")
    _seed_session(db, "s-a", "ws-vm-a")
    row = db.get_vm("vm-a")
    db.set_vm_desired_runtime_state(row.name, DesiredRuntimeState.STOPPED, expected_vm_uuid=row.vm_uuid)
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda *a: VMStatus.STOPPED)
    monkeypatch.setattr(session_manager, "transport", lambda *a, **k: pytest.fail("off guest probed"))
    session_manager.stop_all_sessions(db, make_config(), interaction=TtyInteractionPolicy.REFUSE)
    assert db.get_session("s-a").desired_runtime_state is DesiredRuntimeState.STOPPED
    assert db.get_vm("vm-a").desired_runtime_state is DesiredRuntimeState.STOPPED


_SECOND_SITE_DOC = """\
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: proxmox-b
  description: Second proxmox site with its own token secret
spec:
  platform:
    name: proxmox
    api_url: "https://pve-b:8006"
    node: pve1
    token_id: "agw@pam!agw"
    template_vmid: 9000
    token_secret: proxmox-token-b
"""


# -- the singular ops (_prepare_vm as a gate span) ----------------------------


def _seed_singular(db: Database, *, agent: str | None = None) -> None:
    _seed_vm(db, "box", "100.64.0.9")
    if agent is not None:
        db.insert_agent(agent, "box", f"aw-{agent}")
    _seed_session(db, "s1", "ws-box", agent=agent)


def test_stop_session_records_stopped_for_absent_runtime(db, make_config, resolve_counter, target, monkeypatch):
    from agentworks.db import DesiredRuntimeState

    _seed_singular(db)
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda *a: VMStatus.RUNNING)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *a, **k: SessionStatus.STOPPED)
    session_manager.stop_session(db, make_config(), name="s1", interaction=TtyInteractionPolicy.REFUSE)
    assert resolve_counter == [["proxmox-token"]]
    assert db.get_session("s1").desired_runtime_state is DesiredRuntimeState.STOPPED


def test_stop_session_off_parent_has_only_provider_observation(db, make_config, resolve_counter, monkeypatch):
    _seed_singular(db)
    events: list[str] = []
    _stop_the_vms(monkeypatch, events)
    _record_holds(monkeypatch, events)
    session_manager.stop_session(db, make_config(), name="s1", interaction=TtyInteractionPolicy.REFUSE)
    assert events and all(event == "status:box" for event in events)
    assert resolve_counter == [["proxmox-token"]]


def test_single_stop_uses_prepared_teardown_and_preserves_runtime_identity(db, make_config, target, monkeypatch):
    from agentworks.db import DesiredRuntimeState

    _seed_singular(db)
    selected = db.get_session("s1")
    assert selected is not None
    stopped: list[str] = []
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda *a: VMStatus.RUNNING)
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **k: session)
    monkeypatch.setattr(
        session_manager,
        "check_session_status",
        lambda *a, **k: SessionStatus.RUNNING if not stopped else SessionStatus.STOPPED,
    )
    monkeypatch.setattr(session_manager, "_build_session_target", lambda *a, **k: target)
    monkeypatch.setattr(session_manager, "_teardown_session", lambda session, **k: stopped.append(session.session_uuid))
    session_manager.stop_session(db, make_config(), name="s1", interaction=TtyInteractionPolicy.REFUSE)
    assert stopped == [selected.session_uuid]
    assert db.get_session("s1").desired_runtime_state is DesiredRuntimeState.STOPPED


def test_delete_session_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *_args, **_kwargs: SessionStatus.STOPPED)

    session_manager.delete_session(db, config, name="s1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert db.get_session("s1") is None


def test_attach_session_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *_args, **_kwargs: SessionStatus.STOPPED)

    with pytest.raises(StateError):
        session_manager.attach_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]


def test_session_logs_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *_args, **_kwargs: SessionStatus.STOPPED)

    with pytest.raises(StateError):
        session_manager.session_logs(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]


def test_residual_session_refuses_attach_without_interactive_entry(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.db import SessionStatus

    _seed_singular(db)
    _reachable(monkeypatch, True)
    session = db.get_session("s1")
    assert session is not None
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda *args, **kwargs: session)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *args, **kwargs: SessionStatus.RESIDUAL)
    monkeypatch.setattr(
        target,
        "interactive",
        lambda *args, **kwargs: pytest.fail("residual runtime must not be attached"),
    )

    with pytest.raises(StateError):
        session_manager.attach_session(
            db,
            make_config(),
            name="s1",
            interaction=TtyInteractionPolicy.REFUSE,
        )


def test_residual_session_refuses_logs_without_capture(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.db import SessionStatus

    _seed_singular(db)
    _reachable(monkeypatch, True)
    session = db.get_session("s1")
    assert session is not None
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda *args, **kwargs: session)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *args, **kwargs: SessionStatus.RESIDUAL)
    monkeypatch.setattr(
        "agentworks.sessions.tmux.capture_output",
        lambda *args, **kwargs: pytest.fail("residual runtime has no canonical pane to capture"),
    )

    with pytest.raises(StateError):
        session_manager.session_logs(
            db,
            make_config(),
            name="s1",
            interaction=TtyInteractionPolicy.REFUSE,
        )


def test_session_logs_writes_exact_utf8_bytes_on_a_legacy_console(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session scrollback is the raw data pipe ``_logs.py``'s module
    docstring describes, not a structured message: it can carry arbitrary
    Unicode captured verbatim from a workload. The CLI entrypoint's
    legacy-console policy degrades unencodable output to a literal ``?``
    for human display; routing the raw capture through that same
    text-mode stream would silently alias a genuine non-ASCII character
    onto the same byte as a literal ``?``, corrupting a redirected
    capture. The raw boundary instead writes exact UTF-8 bytes no matter
    what codepage the console reports."""
    import io
    import sys

    from agentworks.db import SessionStatus

    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, *, target, db: session)
    monkeypatch.setattr(
        session_manager,
        "check_session_status",
        lambda session, *, target: SessionStatus.RUNNING,
    )
    payload = "result=雪\n"
    monkeypatch.setattr("agentworks.sessions.tmux.capture_output", lambda *a, **k: payload)

    # A Windows-style legacy console: cp1252, strict, CRLF (matches the
    # stand-in ``cli/tests/test_console_streams.py`` uses for the same
    # field shape). ``errors="strict"`` makes any accidental fall-through
    # to the text path raise instead of silently degrading the assertion.
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\r\n")
    monkeypatch.setattr(sys, "stdout", console)

    session_manager.session_logs(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
    console.flush()

    assert raw.getvalue() == payload.encode("utf-8")
    # Through a text-mode legacy console, the genuine character and a
    # literal "?" would both degrade to this same byte string. The raw
    # boundary keeps them distinguishable.
    assert raw.getvalue() != b"result=?\n"


def test_session_logs_writes_the_whole_capture_through_a_short_writing_pipe(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under ``python -u`` stdout's buffer is a raw ``FileIO`` whose
    ``write`` can accept only part of a large capture against a
    non-blocking pipe and report the short count instead of raising.
    The raw boundary must loop until every byte is out (the
    ``write_all`` contract shared with the machine-output writer), or a
    redirected ``session logs`` silently truncates at exit code 0."""
    import sys

    from agentworks.db import SessionStatus

    class _ShortWritePipe:
        """A stdout stand-in whose buffer accepts a few bytes per call."""

        def __init__(self) -> None:
            self.raw = bytearray()

        @property
        def buffer(self):  # noqa: ANN202
            return self

        def write(self, data: bytes) -> int:
            accepted = min(7, len(data))
            self.raw.extend(data[:accepted])
            return accepted

        def flush(self) -> None:
            pass

    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, *, target, db: session)
    monkeypatch.setattr(
        session_manager,
        "check_session_status",
        lambda session, *, target: SessionStatus.RUNNING,
    )
    payload = ("line=雪\n" * 500)[:-1] + "\n"
    monkeypatch.setattr("agentworks.sessions.tmux.capture_output", lambda *a, **k: payload)

    pipe = _ShortWritePipe()
    monkeypatch.setattr(sys, "stdout", pipe)

    session_manager.session_logs(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    assert bytes(pipe.raw) == payload.encode("utf-8")


def test_describe_session_observes_without_a_vm_hold(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Describe observes outside the activation gate and any VM hold."""
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    events: list[str] = []
    _record_holds(monkeypatch, events)

    def _probe(session: object, *, target: object) -> object:
        del session, target
        assert db._tx_depth == 0  # noqa: SLF001
        events.append("probe")
        from agentworks.db import SessionStatus

        return SessionStatus.STOPPED

    monkeypatch.setattr(session_manager, "check_session_status", _probe)

    session_manager.describe_session(db, config, name="s1")

    assert events == ["probe"]
    assert resolve_counter == []
    assert any("Status:         stopped" in m for m in captured_output.info)


def test_describe_session_derives_vm_from_its_structural_snapshot(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_singular(db)
    db._conn.execute(
        "UPDATE sessions SET last_started_at = ? WHERE name = ?",
        ("2020-01-01T00:00:00Z", "s1"),
    )
    db._conn.commit()
    _seed_vm(db, "moved", "100.64.0.10")
    _reachable(monkeypatch, True)

    def move_before_snapshot(*_args: object, **_kwargs: object) -> object:
        del _kwargs
        assert db._tx_depth == 0  # noqa: SLF001
        db._conn.execute("UPDATE workspaces SET vm_name = 'moved' WHERE name = 'ws-box'")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
        from agentworks.db import SessionStatus

        return SessionStatus.STOPPED

    monkeypatch.setattr(session_manager, "check_session_status", move_before_snapshot)

    description = session_manager.session_description(
        db,
        config,
        name="s1",
    )

    assert description.vm_name == "moved"
    assert description.last_started_at == "2020-01-01T00:00:00Z"
    assert description.uptime_seconds is None
    assert resolve_counter == []


def test_session_description_warns_which_vm_has_no_host_key_record_and_degrades(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_singular(db)

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise TrustMissing("box")

    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", _refuse)

    description = session_manager.session_description(db, config, name="s1")

    assert description.status == SessionStatus.UNKNOWN.value
    assert len([w for w in captured_output.warnings if "'box'" in w]) == 1


def test_running_session_description_derives_uptime(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    db._conn.execute(
        "UPDATE sessions SET last_started_at = ? WHERE name = ?",
        ("2020-01-01T00:00:00Z", "s1"),
    )
    db._conn.commit()
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **kwargs: session)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *args, **kwargs: SessionStatus.RUNNING)

    description = session_manager.session_description(db, config, name="s1")

    assert description.last_started_at == "2020-01-01T00:00:00Z"
    assert description.uptime_seconds is not None and description.uptime_seconds > 0


def test_describe_session_preserves_live_legacy_structural_error(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_singular(db)
    db.update_session_runtime(
        "s1",
        socket_path=None,
        pid=42,
        boot_id=BOOT_ID,
        tmux_server_start_ticks=100,
    )

    with pytest.raises(StateError) as caught:
        session_manager.session_description(db, config, name="s1")

    assert caught.value.entity_kind == "session"
    assert caught.value.entity_name == "s1"


def test_describe_session_preserves_malformed_stored_boot_error(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    db.update_session_runtime(
        "s1",
        socket_path="/tmp/s1.sock",
        pid=42,
        boot_id="malformed",
        tmux_server_start_ticks=100,
    )

    class _AbsentTarget(_FakeTarget):
        def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
            del kwargs
            if "has-session" in cmd:
                return framed_runtime_result(
                    cmd, SimpleNamespace(ok=False, returncode=1, stdout="", stderr="can't find session: s1")
                )
            if "list-sessions" in cmd:
                return SimpleNamespace(
                    ok=False,
                    returncode=1,
                    stdout="AGW_RUNTIME_SERVER=",
                    stderr="no server running on /tmp/s1.sock",
                )
            if "/proc/sys/kernel/random/boot_id" in cmd:
                return SimpleNamespace(ok=True, returncode=0, stdout=f"AGW_RUNTIME_BOOT={BOOT_ID}\n", stderr="")
            raise AssertionError(cmd)

    monkeypatch.setattr(session_manager, "transport", lambda *_args, **_kwargs: _AbsentTarget())

    with pytest.raises(StateError) as caught:
        session_manager.session_description(db, config, name="s1")

    assert caught.value.entity_kind == "session"
    assert caught.value.entity_name == "s1"


def test_describe_session_shows_harness_integration_line(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Describe carries the resolved harness integration between Template and Mode."""
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)

    from agentworks.db import SessionStatus

    monkeypatch.setattr(
        session_manager,
        "check_session_status",
        lambda *_args, **_kwargs: SessionStatus.STOPPED,
    )

    session_manager.describe_session(db, config, name="s1")

    info = captured_output.info
    assert "Harness integration: shell" in info
    # The Harness integration line sits after Template and before Mode.
    template_idx = next(i for i, m in enumerate(info) if m.startswith("Template:"))
    harness_integration_idx = next(i for i, m in enumerate(info) if m.startswith("Harness integration:"))
    mode_idx = next(i for i, m in enumerate(info) if m.startswith("Mode:"))
    assert template_idx < harness_integration_idx < mode_idx


def test_describe_session_stopped_vm_does_not_open_the_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_singular(db)
    events: list[str] = []
    _stop_the_vms(monkeypatch, events)

    session_manager.describe_session(db, config, name="s1")

    assert events == []
    assert resolve_counter == []


def test_unknown_session_refuses_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_vm(db, "box", "100.64.0.9")
    _reachable(monkeypatch, False)

    def _status_must_not_run(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for an unknown session")

    monkeypatch.setattr(ProxmoxPlatform, "status", _status_must_not_run)

    with pytest.raises(NotFoundError, match="session 'ghost' not found"):
        session_manager.stop_session(db, config, name="ghost", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []


def test_unknown_workspace_refuses_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    # An orphan session row (its workspace is gone): the FK is relaxed
    # for the insert to reproduce the dangling-reference state.
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db.insert_session("orphan", "ghost-ws", "default", SessionMode.ADMIN, socket_path="/tmp/orphan.sock")
    db._conn.commit()
    db._conn.execute("PRAGMA foreign_keys = ON")
    _reachable(monkeypatch, False)

    def _status_must_not_run(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for an unknown workspace")

    monkeypatch.setattr(ProxmoxPlatform, "status", _status_must_not_run)

    with pytest.raises(NotFoundError):
        session_manager.stop_session(db, config, name="orphan", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []


def test_no_tailscale_off_parent_stop_is_satisfied_without_guest_transport(
    db, make_config, resolve_counter, monkeypatch
):
    from agentworks.db import DesiredRuntimeState

    _seed_vm(db, "box", None)
    _seed_session(db, "s1", "ws-box")
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda *a: VMStatus.STOPPED)
    monkeypatch.setattr(session_manager, "transport", lambda *a, **k: pytest.fail("off guest transport"))
    session_manager.stop_session(db, make_config(), name="s1", interaction=TtyInteractionPolicy.REFUSE)
    assert resolve_counter == [["proxmox-token"]]
    assert db.get_session("s1").desired_runtime_state is DesiredRuntimeState.STOPPED


def test_session_describe_does_not_reach_provider_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Session status observation does not construct a provider scope."""

    config = make_config()
    _seed_singular(db, agent="a1")
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    session_manager.describe_session(db, config, name="s1")

    assert scopes == []
