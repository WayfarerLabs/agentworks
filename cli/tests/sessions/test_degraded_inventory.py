"""Recovery inventory behavior for structurally incomplete sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.db import SessionMode, SessionStatus
from agentworks.errors import NotFoundError
from agentworks.output import Role
from agentworks.sessions import manager as session_manager
from agentworks.sessions.manager._queries import (
    SessionListing,
    SessionListRow,
    session_listing_data,
)

if TYPE_CHECKING:
    from agentworks.db import Database, SessionRow


def _seed_session(db: Database, name: str, workspace_name: str) -> None:
    db.insert_session(
        name,
        workspace_name,
        "default",
        SessionMode.ADMIN,
        socket_path=f"/tmp/{name}.sock",
    )


def _seed_recovery_inventory(db: Database) -> None:
    db.insert_vm("healthy-vm", site="site", hostname="healthy-vm")
    db.insert_workspace("healthy-ws", "/srv/healthy", "healthy-vm", "healthy")
    _seed_session(db, "healthy", "healthy-ws")

    db.insert_vm("removed-vm", site="site", hostname="removed-vm")
    db.insert_workspace("stale-vm-ws", "/srv/stale-vm", "removed-vm", "stale-vm")
    _seed_session(db, "stale-vm", "stale-vm-ws")

    db.insert_workspace("removed-ws", "/srv/removed", "healthy-vm", "removed")
    _seed_session(db, "stale-workspace", "removed-ws")

    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM vms WHERE name = 'removed-vm'")
    db._conn.execute("DELETE FROM workspaces WHERE name = 'removed-ws'")
    db._conn.commit()
    db._conn.execute("PRAGMA foreign_keys = ON")


def _stub_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_manager, "_display_registry", lambda _config: None)
    monkeypatch.setattr(
        session_manager,
        "_display_harness_integration",
        lambda _registry, _template: "-",
    )


def test_human_session_status_inventory_isolates_structural_orphans(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    _seed_recovery_inventory(db)
    _stub_display(monkeypatch)
    observed: list[tuple[str, ...]] = []

    def observe(
        sessions: list[SessionRow],
        *,
        db: Database,
        config: object,
    ) -> dict[str, SessionStatus]:
        observed.append(tuple(session.name for session in sessions))
        return {"healthy": SessionStatus.RUNNING}

    monkeypatch.setattr(session_manager, "observe_session_statuses", observe)
    changes_before = db._conn.total_changes

    listing = session_manager.session_listing(
        db,
        object(),  # type: ignore[arg-type]
        include_status=True,
    )

    assert db._conn.total_changes == changes_before
    assert [(row.name, row.workspace_name, row.vm_name, row.status) for row in listing.sessions] == [
        ("healthy", "healthy-ws", "healthy-vm", "running"),
        ("stale-workspace", "removed-ws", None, "unknown"),
        ("stale-vm", "stale-vm-ws", "removed-vm", "unknown"),
    ]
    assert observed == [("healthy",)]

    session_manager.render_session_listing(listing, include_status=True)
    assert sum(role is Role.WARNING for role, _level, _message in captured_output.lines) == 2
    table_rows = [line for line in captured_output.info if line.startswith(("healthy", "stale-"))]
    stale_workspace_cells = next(line.split() for line in table_rows if line.startswith("stale-workspace"))
    assert stale_workspace_cells[2] == "-"


def test_plain_session_inventory_is_local_and_preserves_orphans(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_recovery_inventory(db)
    _stub_display(monkeypatch)
    monkeypatch.setattr(
        session_manager,
        "observe_session_statuses",
        lambda *_args, **_kwargs: pytest.fail("plain inventory attempted observation"),
    )
    changes_before = db._conn.total_changes

    listing = session_manager.session_listing(db, object())  # type: ignore[arg-type]

    assert db._conn.total_changes == changes_before
    assert [(row.name, row.status) for row in listing.sessions] == [
        ("healthy", "unavailable"),
        ("stale-workspace", "unavailable"),
        ("stale-vm", "unavailable"),
    ]


def test_required_vm_names_reject_missing_workspace_before_observation(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_recovery_inventory(db)
    _stub_display(monkeypatch)
    monkeypatch.setattr(
        session_manager,
        "observe_session_statuses",
        lambda *_args, **_kwargs: pytest.fail("observation ran before local projection refusal"),
    )

    with pytest.raises(NotFoundError) as caught:
        session_manager.session_listing(
            db,
            object(),  # type: ignore[arg-type]
            include_status=True,
            require_vm_names=True,
        )

    assert caught.value.entity_kind == "workspace"
    assert caught.value.entity_name == "removed-ws"


def test_required_vm_names_preserve_stored_vm_when_vm_row_is_missing(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("removed-vm", site="site", hostname="removed-vm")
    db.insert_workspace("workspace", "/srv/workspace", "removed-vm", "workspace")
    _seed_session(db, "session", "workspace")
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM vms WHERE name = 'removed-vm'")
    db._conn.commit()
    db._conn.execute("PRAGMA foreign_keys = ON")
    _stub_display(monkeypatch)

    listing = session_manager.session_listing(
        db,
        object(),  # type: ignore[arg-type]
        include_status=True,
        require_vm_names=True,
    )

    assert listing.sessions[0].vm_name == "removed-vm"
    assert listing.sessions[0].status == "unknown"
    rows = cast("list[dict[str, object]]", session_listing_data(listing)["sessions"])
    assert rows[0]["vm_name"] == "removed-vm"


def test_session_json_projection_refuses_nullable_internal_vm_name() -> None:
    listing = SessionListing(
        sessions=(
            SessionListRow(
                name="session",
                workspace_name="removed-ws",
                vm_name=None,
                template="default",
                harness_integration=None,
                mode="admin",
                agent_name=None,
                status="unavailable",
            ),
        )
    )

    with pytest.raises(AssertionError):
        session_listing_data(listing)


def test_session_json_cli_requests_required_vm_names(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_recovery_inventory(db)
    _stub_display(monkeypatch)
    from agentworks.cli.commands import session as command

    monkeypatch.setattr(command, "get_db", lambda: db)
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    monkeypatch.setattr(
        session_manager,
        "observe_session_statuses",
        lambda *_args, **_kwargs: pytest.fail("JSON observed before local projection refusal"),
    )

    result = CliRunner().invoke(app, ["session", "list", "--status", "--output", "json"])

    assert result.exit_code != 0
    assert result.stdout_bytes == b""


def test_names_only_retains_orphan_inventory(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    _seed_recovery_inventory(db)
    monkeypatch.setattr(
        session_manager,
        "_display_registry",
        lambda _config: pytest.fail("names-only loaded display registry"),
    )

    session_manager.list_sessions(
        db,
        object(),  # type: ignore[arg-type]
        names_only=True,
    )

    assert captured_output.info == ["healthy", "stale-workspace", "stale-vm"]


def test_session_filters_keep_their_existing_relationship_boundaries(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("box", site="site", hostname="box")
    db.insert_workspace("healthy-ws", "/srv/healthy", "box", "healthy")
    _seed_session(db, "healthy", "healthy-ws")
    db.insert_agent("operator", "box", "agt-operator")
    db.insert_workspace("removed-ws", "/srv/removed", "box", "removed")
    _seed_session(db, "orphan-admin", "removed-ws")
    db.insert_session(
        "orphan-agent",
        "removed-ws",
        "default",
        SessionMode.AGENT,
        agent_name="operator",
        socket_path="/tmp/orphan-agent.sock",
    )
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM workspaces WHERE name = 'removed-ws'")
    db._conn.commit()
    db._conn.execute("PRAGMA foreign_keys = ON")
    _stub_display(monkeypatch)

    agent_listing = session_manager.session_listing(
        db,
        object(),  # type: ignore[arg-type]
        agent_name="operator",
    )
    admin_listing = session_manager.session_listing(
        db,
        object(),  # type: ignore[arg-type]
        admin_only=True,
    )
    vm_listing = session_manager.session_listing(
        db,
        object(),  # type: ignore[arg-type]
        vm_name="box",
    )

    assert [(row.name, row.vm_name) for row in agent_listing.sessions] == [("orphan-agent", None)]
    assert [(row.name, row.vm_name) for row in admin_listing.sessions] == [
        ("healthy", "box"),
        ("orphan-admin", None),
    ]
    assert [row.name for row in vm_listing.sessions] == ["healthy"]
