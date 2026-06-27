"""Tests for the ephemeral workspace/agent orchestration in
``sessions.manager.create_session``.

Pins issue #124's two operator-facing guarantees:

1. **VM-anchor cross-check happens upfront.** When an existing
   workspace and an existing agent are on different VMs, the failure
   raises before any state mutation -- no orphan workspace gets created.

2. **Eager-resolve runs once, atomically, before any state mutation.**
   ``--new-workspace --new-agent`` resolves the union of secret needs
   across all three creations in one call; a Ctrl-C at the prompt
   leaves no orphan workspace or agent. Any failure after state
   mutation begins rolls back every ephemeral resource that was
   created.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import ValidationError
from agentworks.sessions.manager import NewAgentArgs, NewWorkspaceArgs

if TYPE_CHECKING:
    pass


def _seed_two_vms(tmp_path: Path) -> Database:
    """Two VMs each with one workspace and one agent.

    vm-A hosts ws-A and agt-A; vm-B hosts ws-B and agt-B. Useful for
    cross-VM mismatch tests.
    """
    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host) VALUES "
        "('vm-A', 'lima', 'admin', '100.64.0.1'),"
        "('vm-B', 'lima', 'admin', '100.64.0.2')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES "
        "('ws-A', 'vm-A', '/home/me/ws-A', 'ws-ws-A'),"
        "('ws-B', 'vm-B', '/home/me/ws-B', 'ws-ws-B')"
    )
    db._conn.commit()
    db.insert_agent("agt-A", "vm-A", "aw-agt-A")
    db.insert_agent("agt-B", "vm-B", "aw-agt-B")
    return db


def _seed_one_vm(tmp_path: Path) -> Database:
    """Single VM with one workspace."""
    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host) "
        "VALUES ('vm1', 'lima', 'admin', '100.64.0.5')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    db._conn.commit()
    return db


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_cross_vm_existing_workspace_and_agent_fails_upfront(tmp_path: Path) -> None:
    """Existing workspace on vm-A + existing agent on vm-B raises
    ValidationError before any state mutation. The pre-#124 behavior was
    to create the workspace, THEN bail with the mismatch, leaving an
    orphan."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="VM mismatch"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace_name="ws-A",
            agent_name="agt-B",
        )

    # No state mutated: no session row written.
    assert db.get_session("s1") is None
    db.close()


def test_explicit_vm_disagreeing_with_workspace_fails_upfront(tmp_path: Path) -> None:
    """vm_name="vm-B" alongside workspace_name="ws-A" (which lives on vm-A)
    fails the anchor cross-check before any mutation."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="VM mismatch"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace_name="ws-A",
            vm_name="vm-B",
        )

    assert db.get_session("s1") is None
    db.close()


def test_explicit_vm_agreeing_with_workspace_passes_anchor_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vm_name="vm-A" plus workspace_name="ws-A" agree, so the anchor
    check passes. The first downstream call (_ensure_vm_running) is what
    we use as a sentinel that we got past validation."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    # Template resolution is downstream of anchor cross-check but upstream
    # of _ensure_vm_running; stub it so the SimpleNamespace config doesn't
    # need session_templates.
    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)

    called: list[str] = []

    def _spy(*args: object, **kwargs: object) -> None:
        called.append("ensure_vm_up")
        raise RuntimeError("stop after anchor check")

    monkeypatch.setattr("agentworks.workspaces.manager._ensure_vm_running", _spy)

    with pytest.raises(RuntimeError, match="stop after anchor check"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace_name="ws-A",
            vm_name="vm-A",
        )
    assert called == ["ensure_vm_up"]
    db.close()


def test_no_vm_anchor_raises(tmp_path: Path) -> None:
    """new_workspace + admin mode + no vm_name = nothing pins the VM."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="VM anchor required"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=NewWorkspaceArgs(),
            # admin mode (no agent_name, no new_agent), no vm_name
        )
    db.close()


def test_workspace_and_new_workspace_both_none_raises(tmp_path: Path) -> None:
    """Operator must specify which workspace to use."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="workspace_name or new_workspace"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
        )
    db.close()


def test_agent_name_and_new_agent_mutually_exclusive(tmp_path: Path) -> None:
    """Cannot say "use existing agent X" AND "create new agent" at once."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="mutually exclusive"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace_name="ws1",
            agent_name="some-existing",
            new_agent=NewAgentArgs(),
        )
    db.close()


def test_ephemeral_workspace_name_collision_raises(tmp_path: Path) -> None:
    """If a workspace by the target name already exists, the create flow
    must fail before any mutation rather than partially overwriting state."""
    from agentworks.errors import AlreadyExistsError
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # already has ws1
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(AlreadyExistsError, match="workspace 'ws1' already exists"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace_name="ws1",  # collides
            new_workspace=NewWorkspaceArgs(),
            vm_name="vm1",
        )
    db.close()


# ---------------------------------------------------------------------------
# Eager-resolve atomicity
# ---------------------------------------------------------------------------


def _install_session_prep_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stubs that let create_session run end-to-end with a SimpleNamespace
    config -- no real VM, no real SSH, no real templates."""
    from tests.conftest import stub_session_resolvers

    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running",
        lambda *a, **k: None,
    )

    class _Result:
        ok = True
        returncode = 0
        stdout = ""
        stderr = ""

    class _Target:
        def run(self, *a: object, **k: object) -> _Result:
            return _Result()

    factory = lambda *a, **k: _Target()  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", factory)
    monkeypatch.setattr("agentworks.transports.agent_transport", factory)
    stub_session_resolvers(monkeypatch)


def test_eager_resolve_fires_exactly_once_for_new_workspace_and_new_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--new-workspace --new-agent`` must prompt at most once for the union
    of all secrets across the three creations. Asserts the orchestrator
    calls resolve_for_command exactly once and that the call happens BEFORE
    create_workspace / create_agent run."""
    from agentworks.sessions.manager import create_session

    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host) "
        "VALUES ('vm1', 'lima', 'admin', '100.64.0.5')"
    )
    db._conn.commit()
    _install_session_prep_stubs(monkeypatch)

    sequence: list[str] = []

    def _resolve_spy(*a: object, **k: object) -> dict[str, str]:
        sequence.append("resolve_for_command")
        return {}

    def _ws_spy(db: object, config: object, **kwargs: object) -> None:
        sequence.append("create_workspace")
        # Pretend the workspace was created by inserting a stub row.
        db._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
            "VALUES (?, ?, ?, ?)",
            (kwargs["name"], kwargs["vm_name"], "/tmp/ws", f"ws-{kwargs['name']}"),
        )
        db._conn.commit()  # type: ignore[attr-defined]

    def _ag_spy(db: object, config: object, **kwargs: object) -> None:
        sequence.append("create_agent")
        db.insert_agent(kwargs["name"], kwargs["vm_name"], f"aw-{kwargs['name']}")  # type: ignore[attr-defined]

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _resolve_spy)
    monkeypatch.setattr("agentworks.workspaces.manager.create_workspace", _ws_spy)
    monkeypatch.setattr("agentworks.agents.manager.create_agent", _ag_spy)

    # Stop the flow right after the ephemeral creates so we don't have to
    # stub the entire inner state-mutation block.
    class _Stop(Exception):
        pass

    def _stop_at_prepare_vm(*a: object, **k: object) -> None:
        sequence.append("prepare_vm")
        raise _Stop

    monkeypatch.setattr("agentworks.sessions.manager._prepare_vm", _stop_at_prepare_vm)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(_Stop):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=NewWorkspaceArgs(),
            new_agent=NewAgentArgs(),
            vm_name="vm1",
        )

    # Exactly one eager-resolve call, before the creates.
    assert sequence.count("resolve_for_command") == 1
    # Order: resolve, then create_workspace, then create_agent, then prepare_vm.
    assert sequence == [
        "resolve_for_command",
        "create_workspace",
        "create_agent",
        "prepare_vm",
    ]
    db.close()


def test_failure_after_ephemeral_create_rolls_back_ephemerals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If anything fails after create_workspace / create_agent have run,
    the orchestrator must call delete_agent and delete_workspace to undo
    them. Unused ephemerals must not survive a failed session create."""
    from agentworks.sessions.manager import create_session

    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host) "
        "VALUES ('vm1', 'lima', 'admin', '100.64.0.5')"
    )
    db._conn.commit()
    _install_session_prep_stubs(monkeypatch)

    deletes: list[str] = []

    def _ws_create(db: object, config: object, **kwargs: object) -> None:
        db._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
            "VALUES (?, ?, ?, ?)",
            (kwargs["name"], kwargs["vm_name"], "/tmp/ws", f"ws-{kwargs['name']}"),
        )
        db._conn.commit()  # type: ignore[attr-defined]

    def _ag_create(db: object, config: object, **kwargs: object) -> None:
        db.insert_agent(kwargs["name"], kwargs["vm_name"], f"aw-{kwargs['name']}")  # type: ignore[attr-defined]

    def _ws_delete(db: object, config: object, name: str, **kwargs: object) -> None:
        deletes.append(f"workspace:{name}")

    def _ag_delete(db: object, config: object, *, name: str, **kwargs: object) -> None:
        deletes.append(f"agent:{name}")

    monkeypatch.setattr("agentworks.workspaces.manager.create_workspace", _ws_create)
    monkeypatch.setattr("agentworks.agents.manager.create_agent", _ag_create)
    monkeypatch.setattr("agentworks.workspaces.manager.delete_workspace", _ws_delete)
    monkeypatch.setattr("agentworks.agents.manager.delete_agent", _ag_delete)

    # Make the session-internal block fail (after both ephemerals are created).
    def _explode(*a: object, **k: object) -> None:
        raise RuntimeError("simulated session-internal failure")

    monkeypatch.setattr("agentworks.sessions.manager._prepare_vm", _explode)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(RuntimeError, match="simulated"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=NewWorkspaceArgs(),
            new_agent=NewAgentArgs(),
            vm_name="vm1",
        )

    # Rollback ran for both. Order is reverse-of-create: agent then workspace.
    assert deletes == ["agent:s1", "workspace:s1"]
    db.close()


def test_validation_failure_does_not_trigger_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-VM mismatch fails during validation, BEFORE any create_*
    is called. Rollback must not run -- there's nothing to undo, and
    triggering it would attempt to delete resources that don't exist."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    deletes: list[str] = []
    monkeypatch.setattr(
        "agentworks.workspaces.manager.delete_workspace",
        lambda *a, **k: deletes.append("workspace"),
    )
    monkeypatch.setattr(
        "agentworks.agents.manager.delete_agent",
        lambda *a, **k: deletes.append("agent"),
    )

    with pytest.raises(ValidationError, match="VM mismatch"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace_name="ws-A",
            agent_name="agt-B",
        )

    assert deletes == [], "no rollback should run when nothing was created"
    db.close()
