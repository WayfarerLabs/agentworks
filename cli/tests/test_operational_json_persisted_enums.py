"""Closed persisted-enum facts and real operational CLI projections."""

from __future__ import annotations

import contextlib
import json
from enum import StrEnum
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus, VMStatus

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

    from agentworks.config import Config
    from agentworks.db import Database


_RAW_TEXT = "operator-private-invalid\n\x1b[31m"
_RAW_BYTES = _RAW_TEXT.encode()


def _seed_invalid_persisted_enums(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
    db.insert_session("session-a", "ws", "default", SessionMode.ADMIN)
    db.update_session_pid("session-a", PID_STOPPED, boot_id="boot")
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
    assert workspace_description(db, "ws").sessions[0].mode == "unknown"
    session_fact = session_listing(db, config, no_status=True).sessions[0]
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

    @contextlib.contextmanager
    def prepared_vm(*_args: object, **_kwargs: object) -> Iterator[object]:
        yield db.get_workspace("ws"), db.get_vm("box"), None, None, object()

    monkeypatch.setattr(sessions, "_prepare_vm", prepared_vm)
    monkeypatch.setattr(sessions, "_ensure_pid", lambda row, **_kwargs: row)
    monkeypatch.setattr(sessions, "check_session_status", lambda row, **_kwargs: SessionStatus.STOPPED)
    platform = SimpleNamespace(
        name="fixture-platform",
        display_backend_name=lambda _vm: "fixture-backend",
        status=lambda _vm, _context: VMStatus.RUNNING,
    )
    site = SimpleNamespace(platform=platform)
    monkeypatch.setattr(sites, "lookup_site", lambda *_args, **_kwargs: site)
    monkeypatch.setattr(
        vm_inspection,
        "_live_vm_boundary",
        lambda *_args, **_kwargs: (SimpleNamespace(site=site), object()),
    )

    runner = CliRunner()
    vm_list = runner.invoke(app, ["vm", "list", "--output", "json"])
    vm_describe = runner.invoke(app, ["vm", "describe", "box", "--output", "json"])
    workspace_describe = runner.invoke(app, ["workspace", "describe", "ws", "--output", "json"])
    session_list = runner.invoke(app, ["session", "list", "--no-status", "--output", "json"])
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
        ["session", "list", "--no-status", "--output", "human"],
        ["session", "describe", "session-a", "--output", "human"],
    )
    all_output = b"".join(runner.invoke(app, command).stdout_bytes for command in human_commands)
    assert _RAW_TEXT.encode() not in all_output
    assert _RAW_BYTES not in all_output
    assert all_output.count(b"unknown") >= 4


def test_future_domain_members_do_not_expand_frozen_json_v1_vocabularies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.db import models
    from agentworks.db.projections import (
        project_session_mode,
        project_vm_initialization_status,
        project_vm_provisioning_status,
    )

    class FutureProvisioningStatus(StrEnum):
        PENDING = "pending"
        QUEUED = "queued"

    class FutureInitializationStatus(StrEnum):
        PENDING = "pending"
        DEFERRED = "deferred"

    class FutureSessionMode(StrEnum):
        ADMIN = "admin"
        OBSERVER = "observer"

    monkeypatch.setattr(models, "ProvisioningStatus", FutureProvisioningStatus)
    monkeypatch.setattr(models, "InitStatus", FutureInitializationStatus)
    monkeypatch.setattr(models, "SessionMode", FutureSessionMode)

    assert project_vm_provisioning_status(FutureProvisioningStatus.PENDING.value) == "pending"
    assert project_vm_initialization_status(FutureInitializationStatus.PENDING.value) == "pending"
    assert project_session_mode(FutureSessionMode.ADMIN.value) == "admin"
    assert project_vm_provisioning_status(FutureProvisioningStatus.QUEUED.value) == "unknown"
    assert project_vm_initialization_status(FutureInitializationStatus.DEFERRED.value) == "unknown"
    assert project_session_mode(FutureSessionMode.OBSERVER.value) == "unknown"


def test_projection_boundaries_close_manual_invalid_facts() -> None:
    from agentworks.sessions.manager._queries import (
        SessionDescription,
        SessionListing,
        SessionListRow,
        session_description_data,
        session_listing_data,
    )
    from agentworks.vms.manager.power import VMListing, VMListRow, vm_listing_data
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
    )
    projected_workspace = cast("dict[str, object]", workspace_description_data(workspace)["workspace"])
    assert cast("list[dict[str, object]]", projected_workspace["sessions"])[0]["mode"] == "unknown"

    listed = SessionListRow("s", "ws", "box", "default", None, _RAW_TEXT, None, "unavailable")
    projected_list = cast("list[dict[str, object]]", session_listing_data(SessionListing((listed,)))["sessions"])[0]
    assert projected_list["mode"] == "unknown"
    assert projected_list["status"] == "unavailable"
    description = SessionDescription("s", "ws", "box", "default", None, _RAW_TEXT, None, _RAW_TEXT, None, "c", "u")
    projected_description = cast("dict[str, object]", session_description_data(description)["session"])
    assert projected_description["mode"] == projected_description["status"] == "unknown"
