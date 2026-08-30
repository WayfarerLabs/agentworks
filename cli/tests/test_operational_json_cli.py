"""High-fidelity Typer coverage for operational JSON v1 projections."""

from __future__ import annotations

import contextlib
import json
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING, cast

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus, VMRow, WorkspaceRow
from agentworks.debian import DebianRelease
from agentworks.secrets.policy import TtyInteractionPolicy
from tests.conftest import stub_vm_ssh_identity

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.config import Config
    from agentworks.db import Database, SessionRow


@pytest.fixture(autouse=True)
def _stub_ssh_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_vm_ssh_identity(monkeypatch)


def _document_bytes(command: str, data: object) -> bytes:
    return (
        json.dumps(
            {"schema_version": 1, "command": command, "data": data},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _invoke_twice(argv: list[str], expected: bytes) -> None:
    first = CliRunner().invoke(app, argv)
    second = CliRunner().invoke(app, argv)
    assert first.exit_code == second.exit_code == 0, first.output
    assert first.stdout_bytes == second.stdout_bytes == expected
    assert first.stderr_bytes == second.stderr_bytes == b""
    assert b"\x1b" not in first.stdout_bytes


def test_nonempty_operational_lists_have_exact_ordered_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every operational list uses its closed projection, order, and nulls."""
    from agentworks.agents import manager as agents
    from agentworks.agents.manager.inspect import AgentGrant, AgentListing, AgentListRow
    from agentworks.cli.commands import agent, console, session, vm, workspace
    from agentworks.sessions import manager as sessions
    from agentworks.sessions import multi_console
    from agentworks.sessions.manager._queries import SessionListing, SessionListRow
    from agentworks.sessions.multi_console.attach import ConsoleListing, ConsoleListRow
    from agentworks.vms import manager as vms
    from agentworks.vms.manager.inspect import VMListing, VMListRow
    from agentworks.workspaces import manager as workspaces
    from agentworks.workspaces.manager.create import WorkspaceListing, WorkspaceListRow

    workspace_rows = (
        WorkspaceListRow("alpha", "vm-a", None, "2026-01-01"),
        WorkspaceListRow("zeta", "vm-z", "python", "2026-01-02"),
    )
    vm_rows = tuple(
        VMListRow(
            name=f"vm-{index}",
            site="site-a",
            template=None if index == 0 else "small",
            provisioning_status=provisioning,
            initialization_status=initialization,
            workspace_count=index,
            agent_count=index + 1,
            session_count=index + 2,
            tailscale_host=None if index == 0 else f"100.64.0.{index}",
            created_at=f"2026-01-0{index + 1}",
        )
        for index, (provisioning, initialization) in enumerate(
            (
                ("pending", "pending"),
                ("in_progress", "in_progress"),
                ("complete", "complete"),
                ("failed", "partial"),
                ("complete", "failed"),
            )
        )
    )
    agent_rows = (
        AgentListRow(
            "agent-a",
            "vm-a",
            None,
            False,
            (
                AgentGrant("alpha", "explicit"),
                AgentGrant("beta", "implicit"),
                AgentGrant("gamma", "both"),
            ),
        ),
        AgentListRow("agent-z", "vm-z", "large", True, ()),
    )
    session_rows = tuple(
        SessionListRow(
            name=f"session-{status}",
            workspace_name="alpha",
            vm_name="vm-a",
            template="default",
            harness_integration=None if status == "unavailable" else "shell",
            mode="agent" if index % 2 else "admin",
            agent_name="agent-a" if index % 2 else None,
            status=status,
        )
        for index, status in enumerate(("running", "stopped", "broken", "unknown", "unavailable"))
    )
    console_rows = (
        ConsoleListRow("console-a", "vm-a", 2),
        ConsoleListRow("console-z", "vm-z", 0),
    )

    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    for module in (agent, console, session, vm, workspace):
        monkeypatch.setattr(module, "get_db", lambda: object())
    monkeypatch.setattr(vms, "vm_listing", lambda _db: VMListing(vm_rows))
    monkeypatch.setattr(workspaces, "workspace_listing", lambda _db, **_kwargs: WorkspaceListing(workspace_rows))
    monkeypatch.setattr(agents, "agent_listing", lambda _db, **_kwargs: AgentListing(agent_rows))
    monkeypatch.setattr(sessions, "session_listing", lambda _db, _config, **_kwargs: SessionListing(session_rows))
    monkeypatch.setattr(multi_console, "console_listing", lambda _db, **_kwargs: ConsoleListing(console_rows))

    expected_vms = {
        "vms": [
            {
                "name": row.name,
                "site": row.site,
                "template": row.template,
                "provisioning_status": row.provisioning_status,
                "initialization_status": row.initialization_status,
                "workspace_count": row.workspace_count,
                "agent_count": row.agent_count,
                "session_count": row.session_count,
                "tailscale_host": row.tailscale_host,
                "created_at": row.created_at,
                "debian_release": row.debian_release,
                "debian_release_observed_at": row.debian_release_observed_at,
            }
            for row in vm_rows
        ]
    }
    expected_workspaces = {
        "workspaces": [
            {"name": row.name, "vm_name": row.vm_name, "template": row.template, "created_at": row.created_at}
            for row in workspace_rows
        ]
    }
    expected_agents = {
        "agents": [
            {
                "name": row.name,
                "vm_name": row.vm_name,
                "template": row.template,
                "grant_all": row.grant_all,
                "grants": [
                    {"workspace_name": grant.workspace_name, "grant_type": grant.grant_type} for grant in row.grants
                ],
            }
            for row in agent_rows
        ]
    }
    expected_sessions = {
        "sessions": [
            {
                "name": row.name,
                "workspace_name": row.workspace_name,
                "vm_name": row.vm_name,
                "template": row.template,
                "harness_integration": row.harness_integration,
                "mode": row.mode,
                "agent_name": row.agent_name,
                "status": row.status,
            }
            for row in session_rows
        ]
    }
    expected_consoles = {
        "consoles": [
            {"name": row.name, "vm_name": row.vm_name, "session_count": row.session_count} for row in console_rows
        ]
    }
    for argv, command, data in (
        (["vm", "list", "--output", "json"], "vm.list", expected_vms),
        (["workspace", "list", "--output", "json"], "workspace.list", expected_workspaces),
        (["agent", "list", "--output", "json"], "agent.list", expected_agents),
        (["session", "list", "--output", "json"], "session.list", expected_sessions),
        (["console", "list", "--output", "json"], "console.list", expected_consoles),
    ):
        _invoke_twice(argv, _document_bytes(command, data))


def test_nonempty_operational_describes_have_exact_safe_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nested arrays retain service order and unsafe stored state stays excluded."""
    from agentworks.agents import manager as agents
    from agentworks.agents.manager.inspect import AgentDescription, AgentSession
    from agentworks.cli.commands import agent, console, session, vm, workspace
    from agentworks.sessions import manager as sessions
    from agentworks.sessions import multi_console
    from agentworks.sessions.manager._queries import SessionConsole, SessionDescription
    from agentworks.sessions.multi_console.attach import ConsoleDescription, ConsoleMember, ConsoleShell
    from agentworks.vms import manager as vms
    from agentworks.vms.manager.inspect import (
        VMDescription,
        VMDetailAgent,
        VMDetailEvent,
        VMDetailFacts,
        VMDetailSession,
        VMDetailWorkspace,
        VMInspectionIssueSource,
        VMIssue,
        VMLiveResources,
    )
    from agentworks.workspaces import manager as workspaces
    from agentworks.workspaces.manager.create import (
        WorkspaceAgent,
        WorkspaceDescription,
        WorkspaceDetailFacts,
        WorkspaceSession,
    )

    unsafe = "raw-config secret-value opaque-platform harness-state socket-path boot-id"
    vm_row = VMRow(
        name="box",
        site="site-a",
        template="vm-template",
        admin_template="admin-template",
        extra_packages=[],
        provisioning_status="complete",
        init_status="partial",
        tailscale_host="100.64.0.9",
        cpus=8,
        memory_gib=32,
        disk_gib=256,
        swap_gib=None,
        admin_username="operator",
        hostname="lab-box",
        created_at="2026-01-01",
        last_seen_at="2026-01-02",
        debian_release=DebianRelease.TRIXIE,
        debian_release_observed_at="2026-01-02T12:00:00Z",
        platform_metadata={"unsafe": unsafe},
        operator_stopped=False,
    )
    workspace_row = WorkspaceRow("workspace-a", "box", None, "/srv/workspace-a", "2026-01-03", "ws-a")
    vm_description = VMDescription(
        vm=VMDetailFacts.from_row(vm_row),
        platform="proxmox",
        backend="node/pve1",
        observed_status="running",
        status_disposition=None,
        system_slug="lab",
        system_slug_state="set",
        live_resources=VMLiveResources(
            "8",
            "0.1 0.2 0.3",
            "32 GiB",
            "4 GiB",
            "12%",
            "0 B",
            "0 B",
            "0%",
            "256 GiB",
            "64 GiB",
            "25%",
        ),
        agents=(VMDetailAgent("agent-b", "aw-b", True, 2), VMDetailAgent("agent-a", "aw-a", False, 1)),
        workspaces=(
            VMDetailWorkspace(
                "workspace-b",
                "/srv/b",
                (
                    VMDetailSession("admin", "shell", "admin", None),
                    VMDetailSession("worker", "codex", "agent", "agent-a"),
                ),
            ),
        ),
        events=(
            VMDetailEvent("2026-01-04", "provisioning_started", None),
            VMDetailEvent("2026-01-05", "init_complete", "ok"),
        ),
        issues=tuple(VMIssue(VMInspectionIssueSource(source)) for source in VMInspectionIssueSource),
        diagnostics=(),
    )
    workspace_description = WorkspaceDescription(
        WorkspaceDetailFacts.from_row(workspace_row),
        (
            WorkspaceSession("admin", "shell", "admin", None),
            WorkspaceSession("worker", "codex", "agent", "agent-a"),
        ),
        (WorkspaceAgent("agent-b", "aw-b"), WorkspaceAgent("agent-a", "aw-a")),
    )
    agent_description = AgentDescription(
        "agent-a",
        "box",
        "aw-a",
        None,
        False,
        "2026-01-06",
        ("workspace-b", "workspace-a"),
        (AgentSession("worker-b", "codex", "workspace-b"), AgentSession("worker-a", "shell", "workspace-a")),
    )
    session_description = SessionDescription(
        "worker",
        "workspace-a",
        "box",
        "codex",
        "claude-code",
        "agent",
        "agent-a",
        "running",
        4242,
        "2026-01-07",
        "2026-01-08",
        (SessionConsole("console-z", 5), SessionConsole("console-a", 2)),
    )
    console_description = ConsoleDescription(
        "console-a",
        "box",
        True,
        "2026-01-09",
        "2026-01-10",
        (
            ConsoleMember(2, "worker", (ConsoleShell("/srv/a", False), ConsoleShell(None, True))),
            ConsoleMember(5, "admin", ()),
        ),
    )

    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: {"unsafe": unsafe})
    for module in (agent, console, session, vm, workspace):
        monkeypatch.setattr(module, "get_db", lambda: object())
    monkeypatch.setattr(vms, "vm_description", lambda *_args, **_kwargs: vm_description)
    monkeypatch.setattr(workspaces, "workspace_description", lambda *_args, **_kwargs: workspace_description)
    monkeypatch.setattr(agents, "agent_description", lambda *_args, **_kwargs: agent_description)
    monkeypatch.setattr(sessions, "session_description", lambda *_args, **_kwargs: session_description)
    monkeypatch.setattr(multi_console, "console_description", lambda *_args, **_kwargs: console_description)

    expected_vm = {
        "vm": {
            "name": "box",
            "created_at": "2026-01-01",
            "site": "site-a",
            "platform": "proxmox",
            "backend": "node/pve1",
            "observed_status": "running",
            "status_disposition": None,
            "operator_stopped": False,
            "hostname": "lab-box",
            "system_slug": "lab",
            "system_slug_state": "set",
            "template": "vm-template",
            "admin_template": "admin-template",
            "admin_username": "operator",
            "provisioning_status": "complete",
            "initialization_status": "partial",
            "tailscale_host": "100.64.0.9",
            "last_seen_at": "2026-01-02",
            "debian_release": "trixie",
            "debian_release_observed_at": "2026-01-02T12:00:00Z",
            "provisioned_resources": {"cpus": 8, "memory_gib": 32, "disk_gib": 256, "swap_gib": None},
            "live_resources": {
                "cpus": "8",
                "load_average": "0.1 0.2 0.3",
                "memory_total": "32 GiB",
                "memory_used": "4 GiB",
                "memory_percent": "12%",
                "swap_total": "0 B",
                "swap_used": "0 B",
                "swap_percent": "0%",
                "disk_total": "256 GiB",
                "disk_used": "64 GiB",
                "disk_percent": "25%",
            },
            "agents": [
                {"name": "agent-b", "linux_user": "aw-b", "grant_all": True, "grant_count": 2},
                {"name": "agent-a", "linux_user": "aw-a", "grant_all": False, "grant_count": 1},
            ],
            "workspaces": [
                {
                    "name": "workspace-b",
                    "path": "/srv/b",
                    "sessions": [
                        {"name": "admin", "template": "shell", "mode": "admin", "agent_name": None},
                        {"name": "worker", "template": "codex", "mode": "agent", "agent_name": "agent-a"},
                    ],
                }
            ],
            "events": [
                {"created_at": "2026-01-04", "event": "provisioning_started", "detail": None},
                {"created_at": "2026-01-05", "event": "init_complete", "detail": None},
            ],
        },
        "issues": [
            {"source": source, "code": "unavailable"}
            for source in ("site_lookup", "preflight", "secret_resolution", "platform_status")
        ],
    }
    expected_workspace = {
        "workspace": {
            "name": "workspace-a",
            "vm_name": "box",
            "template": None,
            "path": "/srv/workspace-a",
            "created_at": "2026-01-03",
            "sessions": [
                {"name": "admin", "template": "shell", "mode": "admin", "agent_name": None},
                {"name": "worker", "template": "codex", "mode": "agent", "agent_name": "agent-a"},
            ],
            "agents": [{"name": "agent-b", "linux_user": "aw-b"}, {"name": "agent-a", "linux_user": "aw-a"}],
        }
    }
    expected_agent = {
        "agent": {
            "name": "agent-a",
            "vm_name": "box",
            "linux_user": "aw-a",
            "template": None,
            "grant_all": False,
            "created_at": "2026-01-06",
            "explicit_grants": ["workspace-b", "workspace-a"],
            "sessions": [
                {"name": "worker-b", "template": "codex", "workspace_name": "workspace-b"},
                {"name": "worker-a", "template": "shell", "workspace_name": "workspace-a"},
            ],
        }
    }
    expected_session = {
        "session": {
            "name": "worker",
            "workspace_name": "workspace-a",
            "vm_name": "box",
            "template": "codex",
            "harness_integration": "claude-code",
            "mode": "agent",
            "agent_name": "agent-a",
            "status": "running",
            "pid": 4242,
            "created_at": "2026-01-07",
            "updated_at": "2026-01-08",
            "consoles": [
                {"console_name": "console-z", "position": 5},
                {"console_name": "console-a", "position": 2},
            ],
        }
    }
    expected_console = {
        "console": {
            "name": "console-a",
            "vm_name": "box",
            "admin_shell": True,
            "created_at": "2026-01-09",
            "updated_at": "2026-01-10",
            "sessions": [
                {
                    "position": 2,
                    "session_name": "worker",
                    "shells": [{"cwd": "/srv/a", "admin": False}, {"cwd": None, "admin": True}],
                },
                {"position": 5, "session_name": "admin", "shells": []},
            ],
        }
    }
    for argv, command, data in (
        (["vm", "describe", "box", "--output", "json"], "vm.describe", expected_vm),
        (["workspace", "describe", "workspace-a", "--output", "json"], "workspace.describe", expected_workspace),
        (["agent", "describe", "agent-a", "--output", "json"], "agent.describe", expected_agent),
        (["session", "describe", "worker", "--output", "json"], "session.describe", expected_session),
        (["console", "describe", "console-a", "--output", "json"], "console.describe", expected_console),
    ):
        expected = _document_bytes(command, data)
        _invoke_twice(argv, expected)
        assert unsafe.encode() not in expected


def _seed_session_rows(db: Database) -> None:
    db.insert_vm("box", site="site", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
    db.insert_session("live", "ws", "missing-template", SessionMode.ADMIN)
    db.insert_session("stopped", "ws", "missing-template", SessionMode.ADMIN)
    db.update_session_pid("stopped", PID_STOPPED, boot_id="secret-boot-stopped")


def test_session_json_status_repairs_and_no_status_are_preserved(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Actual session Typer paths retain PID repair writes and sentinel rules."""
    from agentworks.cli.commands import session as command
    from agentworks.sessions import manager

    _seed_session_rows(db)
    config = object()
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(command, "get_db", lambda: db)
    monkeypatch.setattr(manager, "_display_registry", lambda _config: None)
    monkeypatch.setattr(manager, "_display_harness_integration", lambda _registry, _template: "-")

    boundary_calls = 0

    @contextlib.contextmanager
    def boundary(
        _db: object,
        _config: object,
        _vms: object,
        *,
        interaction: TtyInteractionPolicy,
    ) -> Iterator[frozenset[str]]:
        assert interaction is TtyInteractionPolicy.ALLOW
        nonlocal boundary_calls
        boundary_calls += 1
        yield frozenset({"box"})

    repairs = 0

    def repair(rows: list[SessionRow], *, db: Database, config: object) -> list[SessionRow]:
        nonlocal repairs
        del config
        repairs += 1
        db.update_session_pid("live", 4321, boot_id="secret-boot-live")
        return db.list_sessions()

    monkeypatch.setattr(manager, "_best_effort_batch_vm_boundary", boundary)
    monkeypatch.setattr(manager, "ensure_pids_batch", repair)
    monkeypatch.setattr(
        manager,
        "batch_check_all_sessions",
        lambda rows, *, db, config: {"live": SessionStatus.OK},
    )

    status_result = CliRunner().invoke(app, ["session", "list", "--output", "json"])
    assert status_result.exit_code == 0, status_result.output
    status_document = cast("dict[str, object]", json.loads(status_result.stdout_bytes))
    rows = cast("list[dict[str, object]]", cast("dict[str, object]", status_document["data"])["sessions"])
    assert [(row["name"], row["status"], row["harness_integration"]) for row in rows] == [
        ("live", "running", None),
        ("stopped", "stopped", None),
    ]
    repaired = db.get_session("live")
    assert repaired is not None and (repaired.pid, repaired.boot_id) == (4321, "secret-boot-live")
    assert repairs == 1
    assert boundary_calls == 1
    for excluded in (b"secret-boot-live", b"secret-boot-stopped"):
        assert excluded not in status_result.stdout_bytes

    db.update_session_pid("live", None)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("--no-status must not repair or probe")

    monkeypatch.setattr(manager, "ensure_pids_batch", forbidden)
    monkeypatch.setattr(manager, "batch_check_all_sessions", forbidden)
    no_status = CliRunner().invoke(app, ["session", "list", "--no-status", "--output", "json"])
    assert no_status.exit_code == 0, no_status.output
    no_status_rows = json.loads(no_status.stdout_bytes)["data"]["sessions"]
    assert [row["status"] for row in no_status_rows] == ["unavailable", "unavailable"]
    unrepaired = db.get_session("live")
    assert unrepaired is not None and unrepaired.pid is None


def test_session_describe_json_positive_and_stopped_pid_with_degraded_template(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Describe repairs a positive PID, maps PID_STOPPED to null, and leaks no fallback error."""
    from agentworks.cli.commands import session as command
    from agentworks.sessions import manager

    _seed_session_rows(db)
    db.insert_console("zeta", "box")
    db.insert_console("alpha", "box")
    db.add_console_session("zeta", "stopped", [])
    db.add_console_session("zeta", "live", [])
    db.add_console_session("alpha", "live", [])
    config = object()
    marker = "unresolvable-template-sensitive-error"
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(command, "get_db", lambda: db)
    monkeypatch.setattr(manager, "_display_registry", lambda _config: None)
    monkeypatch.setattr(manager, "_display_harness_integration", lambda _registry, _template: "-")

    @contextlib.contextmanager
    def prepare(
        db: Database,
        config: object,
        row: SessionRow,
        *,
        operation: str | None,
        interaction: TtyInteractionPolicy,
    ) -> Iterator[tuple[object, object, object, object, object]]:
        del config, operation
        assert interaction is TtyInteractionPolicy.ALLOW
        workspace = db.get_workspace(row.workspace_name)
        vm = db.get_vm("box")
        assert workspace is not None and vm is not None
        yield workspace, vm, object(), object(), object()

    def repair(row: SessionRow, *, target: object, db: Database) -> SessionRow:
        del target
        db.update_session_pid(row.name, 7777, boot_id="secret-boot-describe")
        repaired = db.get_session(row.name)
        assert repaired is not None
        return repaired

    monkeypatch.setattr(manager, "_prepare_vm", prepare)
    monkeypatch.setattr(manager, "_ensure_pid", repair)
    monkeypatch.setattr(manager, "check_session_status", lambda row, *, target: SessionStatus.OK)

    live = CliRunner().invoke(app, ["session", "describe", "live", "--output", "json"])
    assert live.exit_code == 0, live.output
    live_record = json.loads(live.stdout_bytes)["data"]["session"]
    assert (live_record["pid"], live_record["status"], live_record["harness_integration"]) == (
        7777,
        "running",
        None,
    )
    assert marker.encode() not in live.stdout_bytes
    assert b"secret-boot-describe" not in live.stdout_bytes
    assert live_record["consoles"] == [
        {"console_name": "alpha", "position": 0},
        {"console_name": "zeta", "position": 1},
    ]

    monkeypatch.setattr(manager, "_ensure_pid", lambda row, *, target, db: row)
    monkeypatch.setattr(manager, "check_session_status", lambda row, *, target: SessionStatus.STOPPED)
    stopped = CliRunner().invoke(app, ["session", "describe", "stopped", "--output", "json"])
    assert stopped.exit_code == 0, stopped.output
    stopped_record = json.loads(stopped.stdout_bytes)["data"]["session"]
    assert stopped_record["pid"] is None
    assert stopped_record["status"] == "stopped"
    assert stopped_record["harness_integration"] is None
    assert stopped_record["consoles"] == [{"console_name": "zeta", "position": 0}]


@pytest.mark.parametrize("failure", ["broken-registry", "unresolvable-template"])
def test_session_list_and_describe_degrade_harness_integration_without_error_text(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Both actual Typer projections use null for display-only config failures."""
    from agentworks.cli.commands import session as command
    from agentworks.errors import ConfigError
    from agentworks.sessions import manager

    _seed_session_rows(db)
    marker = f"{failure}-sensitive-detail"
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    monkeypatch.setattr(command, "get_db", lambda: db)
    if failure == "broken-registry":
        monkeypatch.setattr(manager, "_display_registry", lambda _config: None)
    else:
        monkeypatch.setattr(manager, "_display_registry", lambda _config: object())

        def cannot_resolve(*_args: object, **_kwargs: object) -> object:
            raise ConfigError(marker)

        monkeypatch.setattr("agentworks.sessions.templates.resolve_template", cannot_resolve)

    @contextlib.contextmanager
    def batch_boundary(*_args: object, **_kwargs: object) -> Iterator[None]:
        yield

    @contextlib.contextmanager
    def prepare(
        db: Database,
        config: object,
        row: SessionRow,
        *,
        operation: str | None,
        interaction: TtyInteractionPolicy,
    ) -> Iterator[tuple[object, object, object, object, object]]:
        del config, operation
        assert interaction is TtyInteractionPolicy.ALLOW
        workspace = db.get_workspace(row.workspace_name)
        vm = db.get_vm("box")
        assert workspace is not None and vm is not None
        yield workspace, vm, object(), object(), object()

    monkeypatch.setattr(manager, "_batch_vm_boundary", batch_boundary)
    monkeypatch.setattr(manager, "_prepare_vm", prepare)
    monkeypatch.setattr(manager, "_ensure_pid", lambda row, *, target, db: row)
    monkeypatch.setattr(manager, "check_session_status", lambda row, *, target: SessionStatus.STOPPED)

    listing = CliRunner().invoke(app, ["session", "list", "--no-status", "--output", "json"])
    description = CliRunner().invoke(app, ["session", "describe", "stopped", "--output", "json"])

    assert listing.exit_code == description.exit_code == 0
    assert all(row["harness_integration"] is None for row in json.loads(listing.stdout_bytes)["data"]["sessions"])
    assert json.loads(description.stdout_bytes)["data"]["session"]["harness_integration"] is None
    for result in (listing, description):
        assert marker.encode() not in result.stdout_bytes
        assert marker.encode() not in result.stderr_bytes


@pytest.mark.parametrize(
    ("observed", "disposition", "slug", "slug_state"),
    [
        ("running", None, "lab", "set"),
        ("stopped", "manual", None, "declined"),
        ("deallocated", "idle", None, "unset"),
        ("unknown", None, "lab", "set"),
    ],
)
def test_vm_describe_closed_status_and_slug_enums_reach_typer_json(
    monkeypatch: pytest.MonkeyPatch,
    observed: str,
    disposition: str | None,
    slug: str | None,
    slug_state: str,
) -> None:
    from agentworks.cli.commands import vm
    from agentworks.vms import manager
    from agentworks.vms.manager.inspect import VMDescription, VMDetailFacts

    row = VMRow(
        name="box",
        site="site",
        template=None,
        admin_template=None,
        extra_packages=[],
        provisioning_status="complete",
        init_status="complete",
        tailscale_host=None,
        cpus=None,
        memory_gib=None,
        disk_gib=None,
        swap_gib=None,
        admin_username="admin",
        hostname="box",
        created_at="2026-01-01",
        last_seen_at=None,
    )
    description = VMDescription(
        VMDetailFacts.from_row(row), None, None, observed, disposition, slug, slug_state, None, (), (), (), (), ()
    )
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    monkeypatch.setattr(vm, "get_db", lambda: object())
    monkeypatch.setattr(manager, "vm_description", lambda *_args, **_kwargs: description)

    result = CliRunner().invoke(app, ["vm", "describe", "box", "--output", "json"])

    assert result.exit_code == 0, result.output
    projected = json.loads(result.stdout_bytes)["data"]["vm"]
    assert (projected["observed_status"], projected["status_disposition"]) == (observed, disposition)
    assert (projected["system_slug"], projected["system_slug_state"]) == (slug, slug_state)


def test_vm_and_workspace_descriptions_snapshot_only_frozen_safe_scalars() -> None:
    """Mutable database rows and live dictionaries cannot alter collected facts."""
    from agentworks.vms.manager.inspect import (
        VMDescription,
        VMDetailFacts,
        VMLiveResources,
        vm_description_data,
    )
    from agentworks.workspaces.manager.create import (
        WorkspaceDescription,
        WorkspaceDetailFacts,
        WorkspaceListing,
        WorkspaceListRow,
        workspace_description_data,
        workspace_listing_data,
    )

    marker = "OPAQUE_PLATFORM_SECRET"
    vm_row = VMRow(
        name="box",
        site="site",
        template=None,
        admin_template=None,
        extra_packages=[],
        provisioning_status="complete",
        init_status="partial",
        tailscale_host=None,
        cpus=4,
        memory_gib=8,
        disk_gib=64,
        swap_gib=1,
        admin_username="admin",
        hostname="box",
        created_at="2026-01-01",
        last_seen_at=None,
        platform_metadata={"unsafe": marker},
    )
    live_mapping = {
        "cpus": "4",
        "load_avg": "0.1",
        "mem_total": "8 GiB",
        "mem_used": "1 GiB",
        "mem_pct": "12%",
        "swap_total": "1 GiB",
        "swap_used": "0 B",
        "swap_pct": "0%",
        "disk_total": "64 GiB",
        "disk_used": "8 GiB",
        "disk_pct": "12%",
    }
    vm_facts = VMDetailFacts.from_row(vm_row)
    live_facts = VMLiveResources.from_mapping(live_mapping)
    vm_description = VMDescription(vm_facts, None, None, None, None, None, "unset", live_facts, (), (), (), (), ())
    vm_before = vm_description_data(vm_description)

    workspace_row = WorkspaceRow("ws", "box", None, "/srv/ws", "2026-01-02", "ws-ws")
    workspace_facts = WorkspaceDetailFacts.from_row(workspace_row)
    workspace_list_row = WorkspaceListRow.from_row(workspace_row)
    detail = WorkspaceDescription(workspace_facts, (), ())
    listing = WorkspaceListing((workspace_list_row,))
    workspace_before = (workspace_description_data(detail), workspace_listing_data(listing))

    vm_row.name = "mutated"
    vm_row.platform_metadata["unsafe"] = "mutated-secret"
    live_mapping["cpus"] = "999"
    workspace_row.name = "mutated"
    workspace_row.workspace_path = "/unsafe"

    assert vm_description_data(vm_description) == vm_before
    assert workspace_description_data(detail) == workspace_before[0]
    assert workspace_listing_data(listing) == workspace_before[1]
    assert marker not in json.dumps(vm_before)
    assert "platform_metadata" not in vars(vm_facts)
    assert "linux_group" not in vars(workspace_facts)
    with pytest.raises(FrozenInstanceError):
        vm_facts.name = "forbidden"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        live_facts.cpus = "forbidden"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        workspace_facts.name = "forbidden"  # type: ignore[misc]


def test_session_human_listing_uses_one_fact_path_and_names_only_stays_lightweight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary status/repair is single-sourced; completion avoids it entirely."""
    from agentworks import output
    from agentworks.db import SessionRow
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import _queries
    from agentworks.sessions.manager._queries import SessionListing

    calls: list[str] = []
    listing = SessionListing(())

    def collect(*_args: object, **_kwargs: object) -> SessionListing:
        calls.append("collect")
        return listing

    monkeypatch.setattr(session_manager, "session_listing", collect)
    monkeypatch.setattr(session_manager, "filter_sessions", lambda *_args, **_kwargs: pytest.fail("duplicate filter"))
    monkeypatch.setattr(
        _queries,
        "render_session_listing",
        lambda facts: calls.append("render") if facts is listing else pytest.fail("wrong facts"),
    )

    _queries.list_sessions(
        cast("Database", object()),
        cast("Config", object()),
        interaction=TtyInteractionPolicy.REFUSE,
    )
    assert calls == ["collect", "render"]

    rows = [
        SessionRow("second", "zeta", "default", "admin", "created", "updated"),
        SessionRow("first", "alpha", "default", "admin", "created", "updated"),
    ]
    emitted: list[str] = []
    monkeypatch.setattr(session_manager, "filter_sessions", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(session_manager, "session_listing", lambda *_args, **_kwargs: pytest.fail("status path"))
    monkeypatch.setattr(output, "info", emitted.append)

    _queries.list_sessions(
        cast("Database", object()),
        cast("Config", object()),
        names_only=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )
    assert emitted == ["first", "second"]
