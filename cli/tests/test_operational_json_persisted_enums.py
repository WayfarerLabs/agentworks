"""Closed persisted-enum facts and real operational CLI projections."""

from __future__ import annotations

import json
from enum import Enum
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.db import PID_STOPPED, InitStatus, ProvisioningStatus, SessionMode, SessionStatus, VMStatus
from agentworks.db.projections import (
    project_session_mode,
    project_vm_initialization_status,
    project_vm_provisioning_status,
)
from tests.instance_state_support import stub_instance_state

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import Config
    from agentworks.db import Database


_RAW_TEXT = "operator-private-invalid\n\x1b[31m"
_RAW_BYTES = _RAW_TEXT.encode()


def _seed_invalid_persisted_enums(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
    db.insert_session("session-a", "ws", "default", SessionMode.ADMIN)
    db.update_session_runtime(
        "session-a", socket_path="/tmp/session-a.sock", pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )
    db._conn.execute(
        "UPDATE vms SET provisioning_status = ?, init_status = ? WHERE name = 'box'",
        (_RAW_TEXT, _RAW_BYTES),
    )
    db._conn.execute("UPDATE sessions SET mode = ? WHERE name = 'session-a'", (_RAW_TEXT,))
    db._conn.commit()


def _wire_cli(monkeypatch: pytest.MonkeyPatch, db: Database, config: Config) -> None:
    from agentworks.cli.commands import session, vm, workspace

    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(vm, "get_db", lambda: db)
    monkeypatch.setattr(workspace, "get_db", lambda: db)
    monkeypatch.setattr(session, "get_db", lambda: db)


def _json_data(result: object) -> dict[str, object]:
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert result.stderr_bytes == b""  # type: ignore[attr-defined]
    document = cast("dict[str, object]", json.loads(result.stdout_bytes))  # type: ignore[attr-defined]
    return cast("dict[str, object]", document["data"])


def test_invalid_database_enums_are_closed_in_shared_facts(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.sessions.manager import session_listing
    from agentworks.vms.manager import vm_listing
    from agentworks.vms.manager.inspect import VMDetailFacts
    from agentworks.workspaces.manager import workspace_description

    _seed_invalid_persisted_enums(db)
    config = make_config()

    vm_row = db.get_vm("box")
    assert vm_row is not None
    assert VMDetailFacts.from_row(vm_row).provisioning_status == "unknown"
    assert VMDetailFacts.from_row(vm_row).initialization_status == "unknown"
    vm_fact = vm_listing(db).vms[0]
    assert (vm_fact.provisioning_status, vm_fact.initialization_status) == ("unknown", "unknown")
    assert workspace_description(db, config, "ws").sessions[0].mode == "unknown"
    session_fact = session_listing(
        db,
        config,
    ).sessions[0]
    assert session_fact.mode == "unknown"
    assert session_fact.status == "unavailable"


def test_real_list_and_describe_clis_never_echo_invalid_persisted_enums(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.sessions import manager as sessions
    from agentworks.vms import sites
    from agentworks.vms.manager import inspect as vm_inspection

    _seed_invalid_persisted_enums(db)
    config = make_config()
    _wire_cli(monkeypatch, db, config)

    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sessions, "check_session_status", lambda *_args, **_kwargs: SessionStatus.STOPPED)
    monkeypatch.setattr(sessions, "transport", lambda *_args, **_kwargs: object())
    platform = SimpleNamespace(
        name="fixture-platform",
        display_backend_name=lambda _vm: "fixture-backend",
        status=lambda _vm, _context: VMStatus.RUNNING,
    )
    site = SimpleNamespace(platform=platform)
    monkeypatch.setattr(sites, "lookup_site", lambda *_args, **_kwargs: site)
    monkeypatch.setattr("agentworks.vms.nodes.live_vm_node", lambda *_args, **_kwargs: SimpleNamespace(site=site))
    monkeypatch.setattr("agentworks.orchestration.walk.walk", lambda node: (node,))
    monkeypatch.setattr("agentworks.orchestration.secrets.secret_union", lambda _nodes: ())
    monkeypatch.setattr("agentworks.orchestration.readiness.preflight_all", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agentworks.secrets.resolver.Resolver.resolve", lambda _self: None)
    monkeypatch.setattr(vm_inspection, "_platform_ops_ctx", lambda *_args: object())

    runner = CliRunner()
    vm_list = runner.invoke(app, ["vm", "list", "--output", "json"])
    vm_describe = runner.invoke(app, ["vm", "describe", "box", "--output", "json"])
    workspace_describe = runner.invoke(app, ["workspace", "describe", "ws", "--output", "json"])
    session_list = runner.invoke(app, ["session", "list", "--output", "json"])
    session_describe = runner.invoke(app, ["session", "describe", "session-a", "--output", "json"])

    vm_data = _json_data(vm_list)
    vm = cast("list[dict[str, object]]", vm_data["vms"])[0]
    assert vm["provisioning_status"] == vm["initialization_status"] == "unknown"
    vm_describe_data = _json_data(vm_describe)
    described_vm = cast("dict[str, object]", vm_describe_data["vm"])
    assert described_vm["provisioning_status"] == described_vm["initialization_status"] == "unknown"
    described_workspaces = cast("list[dict[str, object]]", described_vm["workspaces"])
    described_sessions = cast("list[dict[str, object]]", described_workspaces[0]["sessions"])
    assert described_sessions[0]["mode"] == "unknown"
    assert "operator-private-invalid" not in vm_describe.stdout
    workspace_data = _json_data(workspace_describe)
    workspace = cast("dict[str, object]", workspace_data["workspace"])
    assert cast("list[dict[str, object]]", workspace["sessions"])[0]["mode"] == "unknown"
    session_list_data = _json_data(session_list)
    listed = cast("list[dict[str, object]]", session_list_data["sessions"])[0]
    assert listed["mode"] == "unknown"
    assert listed["status"] == "unavailable"
    session_describe_data = _json_data(session_describe)
    described = cast("dict[str, object]", session_describe_data["session"])
    assert described["mode"] == "unknown"
    assert described["status"] == "stopped"

    human_commands = (
        ["vm", "list", "--output", "human"],
        ["vm", "describe", "box", "--output", "human"],
        ["workspace", "describe", "ws", "--output", "human"],
        ["session", "list", "--output", "human"],
        ["session", "describe", "session-a", "--output", "human"],
    )
    all_output = b"".join(runner.invoke(app, command).stdout_bytes for command in human_commands)
    assert _RAW_TEXT.encode() not in all_output
    assert _RAW_BYTES not in all_output
    assert all_output.count(b"unknown") >= 4


def _members_lost_in_projection(persisted: type[Enum], project: Callable[[object], str]) -> list[object]:
    """Return the persisted members the projection would not render verbatim."""
    return [member.value for member in persisted if project(member.value) != member.value]


# Each persisted enum beside the frozen JSON v1 vocabulary that has to carry
# every one of its members. A member the output side lacks does not raise; it
# renders as the ``unknown`` sentinel, so the fact disappears silently.
_PERSISTED_PROJECTIONS: tuple[tuple[type[Enum], Callable[[object], str]], ...] = (
    (ProvisioningStatus, project_vm_provisioning_status),
    (InitStatus, project_vm_initialization_status),
    (SessionMode, project_session_mode),
)


@pytest.mark.parametrize(
    ("persisted", "project"),
    _PERSISTED_PROJECTIONS,
    ids=[persisted.__name__ for persisted, _ in _PERSISTED_PROJECTIONS],
)
def test_every_persisted_enum_member_survives_its_json_v1_projection(
    persisted: type[Enum],
    project: Callable[[object], str],
) -> None:
    assert _members_lost_in_projection(persisted, project) == []


def test_the_parity_check_catches_a_persisted_member_the_output_vocabulary_lacks() -> None:
    """Prove the check above can fail, using a member no output vocabulary has."""

    class FutureProvisioningStatus(Enum):
        PENDING = "pending"
        QUEUED = "queued"

    assert _members_lost_in_projection(FutureProvisioningStatus, project_vm_provisioning_status) == ["queued"]


def test_projection_boundaries_close_manual_invalid_facts() -> None:
    from agentworks.sessions.manager._queries import (
        SessionDescription,
        SessionListing,
        SessionListRow,
        session_description_data,
        session_listing_data,
    )
    from agentworks.vms.manager.inspect import VMListing, VMListRow, vm_listing_data
    from agentworks.workspaces.manager.create import (
        WorkspaceDescription,
        WorkspaceDetailFacts,
        WorkspaceSession,
        workspace_description_data,
    )

    vm = VMListRow("box", "site", None, _RAW_TEXT, _RAW_BYTES, 0, 0, 0, None, "now")  # type: ignore[arg-type]
    projected_vm = cast("list[dict[str, object]]", vm_listing_data(VMListing((vm,)))["vms"])[0]
    assert projected_vm["provisioning_status"] == projected_vm["initialization_status"] == "unknown"

    workspace = WorkspaceDescription(
        WorkspaceDetailFacts("ws", "box", None, "/srv/ws", "now"),
        (WorkspaceSession("s", "default", _RAW_TEXT, None),),
        (),
        stub_instance_state("workspace"),
    )
    projected_workspace = cast("dict[str, object]", workspace_description_data(workspace)["workspace"])
    assert cast("list[dict[str, object]]", projected_workspace["sessions"])[0]["mode"] == "unknown"

    listed = SessionListRow("s", "ws", "box", "default", None, _RAW_TEXT, None, "unavailable")
    projected_list = cast("list[dict[str, object]]", session_listing_data(SessionListing((listed,)))["sessions"])[0]
    assert projected_list["mode"] == "unknown"
    assert projected_list["status"] == "unavailable"
    description = SessionDescription(
        "s",
        "ws",
        "box",
        "default",
        None,
        _RAW_TEXT,
        None,
        _RAW_TEXT,
        None,
        "c",
        "u",
        (),
        stub_instance_state("session"),
    )
    projected_description = cast("dict[str, object]", session_description_data(description)["session"])
    assert projected_description["mode"] == projected_description["status"] == "unknown"
