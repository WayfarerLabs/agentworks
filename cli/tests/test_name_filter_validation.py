"""Unknown names in the list/batch commands' name filters are hard errors.

Issue #304: ``session restart --all-stopped --vm wf-test`` with no VM by
that name reported "no sessions to restart" instead of failing. Every
service-layer function that accepts name filters (``--vm`` /
``--workspace`` / ``--agent``) now validates them against the state
database via ``validate_name_filters`` and raises ``NotFoundError`` for
unknown names, while a valid filter that matches nothing stays an empty
result. These tests pin both halves of that contract across the whole
filter surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.agents.manager import list_agents
from agentworks.db import SessionMode
from agentworks.errors import NotFoundError
from agentworks.name_filters import validate_name_filters
from agentworks.sessions import manager as session_manager
from agentworks.sessions.multi_console import list_consoles
from agentworks.workspaces.manager import list_workspaces

if TYPE_CHECKING:
    from agentworks.db import Database

    from .conftest import CapturedOutput


def _seed(db: Database) -> None:
    """One VM, one workspace, one agent; deliberately no sessions, so a
    valid filter always produces an empty result rather than a match."""
    db.insert_vm("dev-vm", site="lima", hostname="lima--dev-vm")
    db.insert_workspace(
        "ws-1",
        workspace_path="/home/agentworks/workspaces/ws-1",
        vm_name="dev-vm",
        linux_group="ws-ws-1",
    )
    db.insert_agent("a1", vm_name="dev-vm", linux_user="agent-a1")


# ---------------------------------------------------------------------------
# validate_name_filters unit behavior
# ---------------------------------------------------------------------------


def test_unknown_vm_error_shape(db: Database) -> None:
    """Single unknown VM: message names the kind and the name, and the
    entity_kind/entity_name attributes carry the structured dimension."""
    _seed(db)
    with pytest.raises(NotFoundError) as excinfo:
        validate_name_filters(db, vm_name="wf-test")
    assert "unknown VM 'wf-test'" in str(excinfo.value)
    assert excinfo.value.entity_kind == "vm"
    assert excinfo.value.entity_name == "wf-test"
    assert "agw vm list" in (excinfo.value.hint or "")


def test_multiple_unknown_names_all_reported(db: Database) -> None:
    _seed(db)
    with pytest.raises(NotFoundError) as excinfo:
        validate_name_filters(db, workspace_name=["nope-1", "ws-1", "nope-2"])
    message = str(excinfo.value)
    assert "unknown workspaces" in message
    assert "'nope-1'" in message
    assert "'nope-2'" in message
    assert "'ws-1'" not in message
    assert excinfo.value.entity_kind == "workspace"
    assert excinfo.value.entity_name == "nope-1"


def test_repeated_unknown_name_reported_once(db: Database) -> None:
    """A repeated unknown element (``--vm foo,foo``) reports 'foo' once,
    with the singular message shape."""
    _seed(db)
    with pytest.raises(NotFoundError) as excinfo:
        validate_name_filters(db, vm_name=["wf-test", "wf-test"])
    message = str(excinfo.value)
    assert message.count("wf-test") == 1
    assert "unknown VM 'wf-test'" in message
    assert "unknown VMs" not in message


def test_no_filters_is_a_noop(db: Database) -> None:
    """No filters set: nothing to validate, even on an empty database."""
    validate_name_filters(db)


# ---------------------------------------------------------------------------
# session list: every entity kind rejects an unknown name
# ---------------------------------------------------------------------------


def test_session_list_rejects_unknown_vm(db: Database) -> None:
    _seed(db)
    with pytest.raises(NotFoundError, match="unknown VM 'wf-test'"):
        session_manager.list_sessions(db, None, vm_name="wf-test")  # type: ignore[arg-type]


def test_session_list_rejects_unknown_workspace(db: Database) -> None:
    _seed(db)
    with pytest.raises(NotFoundError, match="unknown workspace 'nope'"):
        session_manager.list_sessions(db, None, workspace_name="nope")  # type: ignore[arg-type]


def test_session_list_rejects_unknown_agent(db: Database) -> None:
    _seed(db)
    with pytest.raises(NotFoundError, match="unknown agent 'nope'"):
        session_manager.list_sessions(db, None, agent_name="nope")  # type: ignore[arg-type]


def test_session_list_csv_with_one_bad_element_rejects(db: Database) -> None:
    """A CSV filter validates every element: one good name does not
    excuse an unknown sibling."""
    _seed(db)
    with pytest.raises(NotFoundError, match="unknown VM 'wf-test'") as excinfo:
        session_manager.list_sessions(db, None, vm_name=["dev-vm", "wf-test"])  # type: ignore[arg-type]
    assert "'dev-vm'" not in str(excinfo.value)


def test_session_list_valid_filter_empty_result_succeeds(
    db: Database,
    captured_output: CapturedOutput,
) -> None:
    """A defined VM with no sessions is a valid filter: empty result,
    no error. This is the half of the contract that must NOT change."""
    _seed(db)
    session_manager.list_sessions(db, None, vm_name="dev-vm")  # type: ignore[arg-type]
    assert any("No sessions found" in m for m in captured_output.info)


# ---------------------------------------------------------------------------
# session stop --all / session restart --all-stopped
# ---------------------------------------------------------------------------


def test_stop_all_sessions_rejects_unknown_vm(db: Database) -> None:
    _seed(db)
    with pytest.raises(NotFoundError, match="unknown VM 'wf-test'"):
        session_manager.stop_all_sessions(db, None, vm_name="wf-test")  # type: ignore[arg-type]


def test_stop_all_sessions_valid_filter_empty_result_succeeds(
    db: Database,
    captured_output: CapturedOutput,
) -> None:
    _seed(db)
    session_manager.stop_all_sessions(db, None, vm_name="dev-vm")  # type: ignore[arg-type]
    assert any("No running sessions to stop" in m for m in captured_output.info)


def test_restart_all_sessions_rejects_unknown_vm(db: Database) -> None:
    """The issue #304 reproducer: restart --all-stopped with an unknown
    ``--vm`` must be a hard error, not "no sessions to restart"."""
    _seed(db)
    with pytest.raises(NotFoundError, match="unknown VM 'wf-test'"):
        session_manager.restart_all_sessions(db, None, vm_name="wf-test")  # type: ignore[arg-type]


def test_restart_all_sessions_valid_filter_empty_result_succeeds(
    db: Database,
    captured_output: CapturedOutput,
) -> None:
    _seed(db)
    session_manager.restart_all_sessions(db, None, vm_name="dev-vm")  # type: ignore[arg-type]
    assert any("No matching sessions to restart" in m for m in captured_output.info)


# ---------------------------------------------------------------------------
# agent list / workspace list / console list
# ---------------------------------------------------------------------------


def test_agent_list_rejects_unknown_vm(db: Database) -> None:
    _seed(db)
    with pytest.raises(NotFoundError, match="unknown VM 'wf-test'"):
        list_agents(db, vm_name="wf-test")


def test_workspace_list_rejects_unknown_vm(db: Database) -> None:
    _seed(db)
    with pytest.raises(NotFoundError, match="unknown VM 'wf-test'"):
        list_workspaces(db, vm_name="wf-test")


def test_console_list_rejects_unknown_vm(db: Database) -> None:
    _seed(db)
    with pytest.raises(NotFoundError, match="unknown VM 'wf-test'"):
        list_consoles(db, vm_name="wf-test")


def test_console_list_rejects_unknown_workspace_and_agent(db: Database) -> None:
    _seed(db)
    with pytest.raises(NotFoundError, match="unknown workspace 'nope'"):
        list_consoles(db, workspace_name="nope")
    with pytest.raises(NotFoundError, match="unknown agent 'nope'"):
        list_consoles(db, agent_name="nope")


def test_list_commands_valid_filter_empty_result_succeeds(
    db: Database,
    captured_output: CapturedOutput,
) -> None:
    """A second VM with nothing on it filters every list down to an
    empty (but successful) result."""
    _seed(db)
    db.insert_vm("bare-vm", site="lima", hostname="lima--bare-vm")
    list_agents(db, vm_name="bare-vm")
    list_workspaces(db, vm_name="bare-vm")
    list_consoles(db, vm_name="bare-vm")
    assert any("No agents found" in m for m in captured_output.info)
    assert any("No workspaces found" in m for m in captured_output.info)
    assert any("No consoles found" in m for m in captured_output.info)


# ---------------------------------------------------------------------------
# Existing entities with no matches stay valid filter values
# ---------------------------------------------------------------------------


def test_agent_filter_valid_for_agent_with_no_sessions(
    db: Database,
    captured_output: CapturedOutput,
) -> None:
    """An agent that exists but runs no sessions is a valid ``--agent``
    filter value; sessions belonging to other agents are filtered out
    without an error."""
    _seed(db)
    db.insert_agent("a2", vm_name="dev-vm", linux_user="agent-a2")
    db.insert_session(
        "sess-1",
        workspace_name="ws-1",
        template="default",
        mode=SessionMode.AGENT,
        agent_name="a2",
        socket_path="/tmp/agw-a2/tmux.sock",
    )
    session_manager.list_sessions(db, None, agent_name="a1")  # type: ignore[arg-type]
    assert any("No sessions found" in m for m in captured_output.info)
