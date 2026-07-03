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

Also pins parity between the two SecretTarget builders so the new
pre-create helper can't silently diverge from the existing post-create
one for the inputs they both handle (existing workspace + existing
agent or admin mode).

create_session accepts CLI-flag-shaped args (workspace / new_workspace /
workspace_name / workspace_template / agent / new_agent / agent_name /
agent_template / admin / vm_name) and runs all validation, prompts,
and orchestration in the service layer. The CLI handler is a pure
pass-through.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks import output
from agentworks.db import Database
from agentworks.errors import ValidationError

from .conftest import stub_build_registry


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """SimpleNamespace configs don't carry publish_to; Phase 2a's
    manager-entry hoist is no-op'd via the shared helper."""
    stub_build_registry(monkeypatch)


def _seed_two_vms(tmp_path: Path) -> Database:
    """Two VMs each with one workspace and one agent.

    vm-A hosts ws-A and agt-A; vm-B hosts ws-B and agt-B. Useful for
    cross-VM mismatch tests.
    """
    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host, init_status) VALUES "
        "('vm-A', 'lima', 'admin', '100.64.0.1', 'complete'),"
        "('vm-B', 'lima', 'admin', '100.64.0.2', 'complete')"
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
        "INSERT INTO vms (name, platform, admin_username, tailscale_host, init_status) "
        "VALUES ('vm1', 'lima', 'admin', '100.64.0.5', 'complete')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    db._conn.commit()
    return db


@pytest.fixture(autouse=True)
def _non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force non-interactive output for tests so missing-arg prompts raise
    ValidationError instead of hanging on a chooser. Individual tests that
    want to exercise prompting can override via monkeypatch on
    ``output.is_interactive`` or seed enough args to skip the prompt."""
    monkeypatch.setattr(output, "is_interactive", lambda: False)


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
            workspace="ws-A",
            agent="agt-B",
        )

    # No state mutated: no session row written.
    assert db.get_session("s1") is None
    db.close()


def test_explicit_vm_disagreeing_with_workspace_fails_upfront(tmp_path: Path) -> None:
    """--vm vm-B alongside --workspace ws-A (which lives on vm-A)
    fails the anchor cross-check before any mutation."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="VM mismatch"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws-A",
            vm_name="vm-B",
            admin=True,  # mode is now required; admin doesn't affect VM check
        )

    assert db.get_session("s1") is None
    db.close()


def test_explicit_vm_agreeing_with_workspace_passes_anchor_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--vm vm-A + --workspace ws-A agree, so the anchor check passes.
    The first downstream call (_ensure_vm_running) is what we use as a
    sentinel that we got past validation."""
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
            workspace="ws-A",
            agent="agt-A",  # also on vm-A; pins the mode so no mode prompt fires
            vm_name="vm-A",
        )
    assert called == ["ensure_vm_up"]
    db.close()


def test_no_vm_anchor_with_multiple_vms_raises_in_non_interactive(
    tmp_path: Path,
) -> None:
    """new_workspace + admin + no vm_name with multiple VMs would need to
    prompt; the autouse non-interactive fixture forces a clean
    ValidationError instead of hanging."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)  # vm-A and vm-B, both fully initialized
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="--vm is required in non-interactive mode"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=True,
            admin=True,
        )
    db.close()


def test_no_vm_anchor_with_zero_vms_raises(tmp_path: Path) -> None:
    """new_workspace + admin + no vm_name with zero VMs raises with
    actionable hint."""
    from agentworks.errors import NotFoundError
    from agentworks.sessions.manager import create_session

    db = Database(tmp_path / "test.db")  # no VMs at all
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(NotFoundError, match="no VMs available"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=True,
            admin=True,
        )
    db.close()


def test_no_vm_anchor_with_single_vm_auto_selects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """new_workspace + admin + no vm_name with exactly one VM auto-
    selects that VM without prompting (matches workspace/agent prompt
    helpers' single-item shortcut). Non-interactive mode is irrelevant
    here: no prompt is shown."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # exactly vm1
    # Drop the workspace so the workspace-prompt path isn't entered
    # (it'd otherwise auto-select ws1 and pin the VM via that anchor,
    # bypassing the path under test).
    db._conn.execute("DELETE FROM workspaces WHERE name = 'ws1'")
    db._conn.commit()

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)

    called: list[str] = []

    def _spy(*args: object, **kwargs: object) -> None:
        called.append("ensure_vm_up")
        raise RuntimeError("stop after VM resolution")

    monkeypatch.setattr("agentworks.workspaces.manager._ensure_vm_running", _spy)

    with pytest.raises(RuntimeError, match="stop after VM resolution"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=True,
            admin=True,
        )
    assert called == ["ensure_vm_up"]
    db.close()


def test_workspace_and_new_workspace_mutex(tmp_path: Path) -> None:
    """Cannot specify both --workspace (existing) and --new-workspace."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="--workspace or --new-workspace, not both"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            new_workspace=True,
        )
    db.close()


def test_workspace_template_requires_new_workspace(tmp_path: Path) -> None:
    """--workspace-template only makes sense with --new-workspace."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="require --new-workspace"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            workspace_template="some-template",
        )
    db.close()


def test_admin_and_agent_mutex(tmp_path: Path) -> None:
    """Cannot specify --admin alongside --agent (or --new-agent)."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="at most one of --agent, --new-agent, or --admin"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            admin=True,
            agent="something",
        )
    db.close()


def test_agent_template_requires_new_agent(tmp_path: Path) -> None:
    """--agent-template only makes sense with --new-agent."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="require --new-agent"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            agent_template="some-template",
        )
    db.close()


def test_new_agent_with_explicit_agent_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--new-agent --agent-name X`` creates a new agent named X (not
    a lookup of existing). Regression for the bogus agent_name/new_agent
    mutex check that briefly existed."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)
    monkeypatch.setattr(
        session_manager, "_session_secret_target_pre_create", lambda *a, **k: None
    )
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})
    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running", lambda *a, **k: None
    )

    create_agent_calls: list[dict[str, object]] = []

    def _spy(db: object, config: object, **kwargs: object) -> None:
        create_agent_calls.append(dict(kwargs))
        raise RuntimeError("stop after agent create")

    monkeypatch.setattr("agentworks.agents.manager.create_agent", _spy)

    with pytest.raises(RuntimeError, match="stop after agent create"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            new_agent=True,
            agent_name="my-named-agent",
        )
    assert create_agent_calls == [
        {"name": "my-named-agent", "vm_name": "vm1", "template": None}
    ]
    db.close()


def test_ephemeral_agent_name_defaults_to_session_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``new_agent=True`` is set without ``agent_name``, the new
    agent's name defaults to the session name. The service layer owns
    this default; the CLI just forwards None when --agent-name wasn't
    supplied."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host) "
        "VALUES ('bbvm1', 'azure', 'admin', '100.64.0.5')"
    )
    db._conn.commit()
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)
    monkeypatch.setattr(
        session_manager, "_session_secret_target_pre_create", lambda *a, **k: None
    )
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})
    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running", lambda *a, **k: None
    )

    workspace_calls: list[dict[str, object]] = []
    agent_calls: list[dict[str, object]] = []

    def _ws_spy(db: object, config: object, **kwargs: object) -> None:
        workspace_calls.append(dict(kwargs))

    def _ag_spy(db: object, config: object, **kwargs: object) -> None:
        agent_calls.append(dict(kwargs))
        raise RuntimeError("stop after agent create")

    monkeypatch.setattr("agentworks.workspaces.manager.create_workspace", _ws_spy)
    monkeypatch.setattr("agentworks.agents.manager.create_agent", _ag_spy)

    with pytest.raises(RuntimeError, match="stop after"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="bbs3",
            new_workspace=True,
            new_agent=True,
            vm_name="bbvm1",
        )

    # Both ephemerals defaulted to the session name.
    assert workspace_calls[0]["name"] == "bbs3"
    assert agent_calls[0]["name"] == "bbs3"
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
            new_workspace=True,
            workspace_name="ws1",  # collides
            vm_name="vm1",
            admin=True,  # mode is required
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
    """``new_workspace=True + new_agent=True`` must prompt at most once
    for the union of all secrets across the three creations. Asserts the
    orchestrator calls resolve_for_command exactly once and that the
    call happens BEFORE create_workspace / create_agent run."""
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
            new_workspace=True,
            new_agent=True,
            vm_name="vm1",
        )

    assert sequence.count("resolve_for_command") == 1
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

    def _explode(*a: object, **k: object) -> None:
        raise RuntimeError("simulated session-internal failure")

    monkeypatch.setattr("agentworks.sessions.manager._prepare_vm", _explode)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(RuntimeError, match="simulated"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=True,
            new_agent=True,
            vm_name="vm1",
        )

    assert deletes == ["agent:s1", "workspace:s1"]
    db.close()


def test_new_agent_inherits_vm_from_existing_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``new_agent=True`` against an existing workspace pins the VM via
    the workspace anchor; no ``vm_name`` is required."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # vm1 + ws1
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)
    monkeypatch.setattr(
        session_manager, "_session_secret_target_pre_create", lambda *a, **k: None
    )
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})
    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running", lambda *a, **k: None
    )

    create_agent_calls: list[dict[str, object]] = []

    def _spy(db: object, config: object, **kwargs: object) -> None:
        create_agent_calls.append(dict(kwargs))
        raise RuntimeError("stop after agent create")

    monkeypatch.setattr("agentworks.agents.manager.create_agent", _spy)

    with pytest.raises(RuntimeError, match="stop after agent create"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            new_agent=True,
        )
    assert len(create_agent_calls) == 1
    assert create_agent_calls[0]["vm_name"] == "vm1"
    db.close()


def test_validation_failure_does_not_trigger_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-VM mismatch fails during validation, BEFORE any create_*
    is called. Rollback must not run."""
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
            workspace="ws-A",
            agent="agt-B",
        )

    assert deletes == [], "no rollback should run when nothing was created"
    db.close()


# ---------------------------------------------------------------------------
# Prompt behavior
# ---------------------------------------------------------------------------


def test_admin_non_interactive_on_vm_with_agents_does_not_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``admin=True`` must bypass the mode prompt even when the
    target VM has agents that would otherwise be offered as options.
    Regression: an earlier shape erased ``admin`` during canonicalization,
    and the mode-prompt gate couldn't distinguish "operator chose admin"
    from "operator didn't say", causing a spurious ValidationError in
    non-interactive mode."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    # vm1 with ws1 AND an existing agent on it.
    db = _seed_one_vm(tmp_path)
    db.insert_agent("agt1", "vm1", "aw-agt1")
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    # Stub template resolution so the SimpleNamespace doesn't need
    # session_templates; the call we want to land at is _ensure_vm_running.
    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)

    called: list[str] = []

    def _spy(*args: object, **kwargs: object) -> None:
        called.append("ensure_vm_up")
        raise RuntimeError("stop after mode-prompt gate")

    monkeypatch.setattr("agentworks.workspaces.manager._ensure_vm_running", _spy)

    # If admin is honored, we reach _ensure_vm_running. If admin is
    # erased and the prompt fires, the autouse non-interactive fixture
    # makes it raise ValidationError first.
    with pytest.raises(RuntimeError, match="stop after mode-prompt gate"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            admin=True,
        )
    assert called == ["ensure_vm_up"]
    db.close()


def test_mode_required_in_non_interactive(tmp_path: Path) -> None:
    """admin and agent mode are materially different (different Linux
    user, env, security boundary). The service prompts in interactive
    mode (with ``admin`` / existing agents / ``[Create new agent]`` as
    options) but never auto-selects -- omitting the flag in
    non-interactive mode raises."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # vm1 + ws1, no agents
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="session mode is required in non-interactive mode"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            # No --admin, no --agent, no --new-agent.
        )
    db.close()


def test_workspace_required_in_non_interactive(tmp_path: Path) -> None:
    """Workspace is part of the session's identity; sessions don't
    auto-select it even when there's exactly one workspace on disk.
    The service prompts in interactive mode (with existing workspaces
    + ``[Create new workspace]`` as options) but raises in
    non-interactive mode."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # vm1 + ws1, exactly one workspace
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="workspace is required in non-interactive mode"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            admin=True,  # mode is specified; workspace is what's under test
        )
    db.close()


# ---------------------------------------------------------------------------
# Interactive prompt behavior
# ---------------------------------------------------------------------------


def _stub_for_post_prompt_flow(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the downstream flow so a prompt-driven test can exit cleanly
    once the prompt has returned. Returns the call-log list the test can
    inspect."""
    from agentworks.sessions import manager as session_manager

    called: list[str] = []
    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)

    def _spy(*a: object, **k: object) -> None:
        called.append("ensure_vm_up")
        raise RuntimeError("stop after prompt")

    monkeypatch.setattr("agentworks.workspaces.manager._ensure_vm_running", _spy)
    return called


def test_workspace_prompt_picks_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In interactive mode, the workspace prompt offers existing
    workspaces + ``[Create new]``. Picking an existing one continues
    the flow with that workspace as the anchor."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # vm1 + ws1
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "choose", lambda msg, opts: 0)  # ws1
    called = _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            admin=True,
        )
    assert called == ["ensure_vm_up"]
    db.close()


def test_workspace_prompt_picks_create_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking the ``[Create new workspace]`` option (last in the list)
    sets ``new_workspace=True``, so interactive mode is functionally
    equivalent to passing ``--new-workspace`` on the CLI."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    # One workspace + [Create new] = 2 options; index 1 is [Create new].
    monkeypatch.setattr(output, "choose", lambda msg, opts: len(opts) - 1)

    # Stub the path up through eager-resolve so we land on the
    # ephemeral-create step cleanly.
    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)
    monkeypatch.setattr(
        session_manager, "_session_secret_target_pre_create", lambda *a, **k: None
    )
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})
    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running", lambda *a, **k: None
    )

    create_workspace_calls: list[dict[str, object]] = []

    def _ws_spy(db: object, config: object, **kwargs: object) -> None:
        create_workspace_calls.append(dict(kwargs))
        raise RuntimeError("stop after create_workspace")

    monkeypatch.setattr("agentworks.workspaces.manager.create_workspace", _ws_spy)

    with pytest.raises(RuntimeError, match="stop after create_workspace"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            admin=True,
        )
    # The flow reached create_workspace, which means new_workspace=True was set
    # by the prompt-driven path.
    assert create_workspace_calls == [{"name": "s1", "vm_name": "vm1", "template_name": None}]
    db.close()


def test_mode_prompt_picks_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In interactive mode, the mode prompt offers ``admin`` + existing
    agents on the resolved VM + ``[Create new agent]``. Picking
    ``admin`` continues in admin mode."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    db.insert_agent("agt1", "vm1", "aw-agt1")
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "choose", lambda msg, opts: 0)  # admin
    called = _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
        )
    assert called == ["ensure_vm_up"]
    db.close()


def test_mode_prompt_picks_existing_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking an existing agent (option > 0, not [Create new]) sets
    ``agent_name`` to that agent."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    db.insert_agent("agt1", "vm1", "aw-agt1")
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(output, "choose", lambda msg, opts: 1)  # agt1
    called = _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
        )
    assert called == ["ensure_vm_up"]
    db.close()


def test_mode_prompt_picks_create_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking the ``[Create new agent]`` option (last in the list)
    sets ``new_agent=True`` AND defaults ``agent_name`` to the session
    name. Regression: a prior shape sat the mode prompt after the
    agent-default/validation/existence block, so ``[Create new agent]``
    landed at the ephemeral-create step with ``agent_name=None`` and
    crashed on an internal assertion."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # vm1 + ws1, no agents
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)
    # No agents on VM: options are [admin, [Create new agent]] = 2 entries.
    # Last index is [Create new agent].
    monkeypatch.setattr(output, "choose", lambda msg, opts: len(opts) - 1)

    # Stub the path so we land on create_agent cleanly.
    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)
    monkeypatch.setattr(
        session_manager, "_session_secret_target_pre_create", lambda *a, **k: None
    )
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})
    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running", lambda *a, **k: None
    )

    create_agent_calls: list[dict[str, object]] = []

    def _ag_spy(db: object, config: object, **kwargs: object) -> None:
        create_agent_calls.append(dict(kwargs))
        raise RuntimeError("stop after create_agent")

    monkeypatch.setattr("agentworks.agents.manager.create_agent", _ag_spy)

    with pytest.raises(RuntimeError, match="stop after create_agent"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
        )
    # The flow reached create_agent with the session name defaulted in.
    assert create_agent_calls == [{"name": "s1", "vm_name": "vm1", "template": None}]
    db.close()


# ---------------------------------------------------------------------------
# Prompt filtering by known VM anchors
# ---------------------------------------------------------------------------


def test_workspace_prompt_filters_by_vm_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``--vm vm-A`` set, the workspace chooser only shows
    workspaces on vm-A. Workspaces on other VMs are filtered out so the
    operator can't pick one that the cross-check would reject."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)  # vm-A has ws-A; vm-B has ws-B
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)

    captured_choose: list[tuple[str, list[str]]] = []

    def _choose_spy(msg: str, opts: list[str]) -> int:
        captured_choose.append((msg, list(opts)))
        return 0  # pick the (one) filtered workspace

    info_messages: list[str] = []
    monkeypatch.setattr(output, "info", lambda m: info_messages.append(m))
    monkeypatch.setattr(output, "choose", _choose_spy)

    _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            vm_name="vm-A",  # pins VM upfront
            admin=True,
        )
    # Exactly one choose call: the workspace prompt.
    assert len(captured_choose) == 1
    _msg, opts = captured_choose[0]
    # The chooser saw only ws-A + [Create new], not ws-B.
    assert any("ws-A" in o for o in opts)
    assert not any("ws-B" in o for o in opts)
    assert opts[-1] == "[Create new workspace]"
    # And the operator got told why the list is short.
    assert any("Only showing workspaces on VM 'vm-A'" in m for m in info_messages)
    db.close()


def test_workspace_prompt_filters_by_existing_agent_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``--agent agt-A`` (on vm-A) set, the workspace chooser
    filters to workspaces on vm-A even when ``--vm`` was not passed.
    The agent's VM is the anchor."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)

    captured_choose: list[list[str]] = []

    def _choose_spy(msg: str, opts: list[str]) -> int:
        captured_choose.append(list(opts))
        return 0

    info_messages: list[str] = []
    monkeypatch.setattr(output, "info", lambda m: info_messages.append(m))
    monkeypatch.setattr(output, "choose", _choose_spy)

    _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            agent="agt-A",  # agt-A lives on vm-A
        )
    opts = captured_choose[0]
    assert any("ws-A" in o for o in opts)
    assert not any("ws-B" in o for o in opts)
    assert any("Only showing workspaces on VM 'vm-A'" in m for m in info_messages)
    db.close()


def test_mode_prompt_filters_by_resolved_vm_with_info_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mode prompt lists only agents on the resolved VM, and prints the
    'Only showing agents on VM X' info line when other-VM agents are
    being omitted."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)  # agt-A on vm-A, agt-B on vm-B
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)

    captured_choose: list[list[str]] = []
    info_messages: list[str] = []

    def _choose_spy(msg: str, opts: list[str]) -> int:
        captured_choose.append(list(opts))
        return 0  # admin

    monkeypatch.setattr(output, "info", lambda m: info_messages.append(m))
    monkeypatch.setattr(output, "choose", _choose_spy)
    _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws-A",  # pins VM to vm-A
            # No mode flag → mode prompt fires.
        )
    opts = captured_choose[0]
    assert opts[0] == "admin"
    assert any("agt-A" in o for o in opts)
    assert not any("agt-B" in o for o in opts)
    assert opts[-1] == "[Create new agent]"
    assert any("Only showing agents on VM 'vm-A'" in m for m in info_messages)
    db.close()


def test_vm_and_existing_agent_mismatch_fails_before_workspace_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--vm vm-A --agent agt-B`` (where agt-B lives on vm-B) is
    internally inconsistent; the service has to catch this before any
    prompt fires (no point asking for a workspace when we already know
    the command is impossible)."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    # If a prompt fires, this raises — proves the validation came first.
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(
        output, "choose", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt should fire"))
    )

    with pytest.raises(ValidationError, match="VM mismatch"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            vm_name="vm-A",
            agent="agt-B",  # lives on vm-B
        )
    db.close()


def test_mode_prompt_picks_existing_agent_pins_vm_no_vm_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--new-workspace`` + no ``--vm`` + no mode flag: workspace
    doesn't pin a VM, so the mode prompt lists agents across all VMs.
    Picking an existing agent pins the VM via that agent -- the VM
    prompt should NOT fire afterwards."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)  # agt-A on vm-A, agt-B on vm-B
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)

    captured_choose: list[tuple[str, list[str]]] = []

    def _choose_spy(msg: str, opts: list[str]) -> int:
        captured_choose.append((msg, list(opts)))
        # The only chooser call should be the mode prompt; pick agt-A
        # (index 1: 0=admin, 1=agt-A, 2=agt-B, 3=[Create new agent]).
        return 1

    monkeypatch.setattr(output, "choose", _choose_spy)

    # If _prompt_vm fires, fail loudly -- it should not, because the
    # mode prompt's pick already pinned the VM.
    def _vm_prompt_should_not_fire(*a: object, **k: object) -> object:
        raise AssertionError("VM prompt fired but mode prompt should have pinned the VM")

    monkeypatch.setattr(session_manager, "_prompt_vm", _vm_prompt_should_not_fire)

    _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=True,
            # No --vm, no mode flag.
        )
    # Exactly one chooser call -- the mode prompt, listing agents across
    # both VMs (with VM labels).
    assert len(captured_choose) == 1
    msg, opts = captured_choose[0]
    assert msg == "Run session as:"
    # The agent labels should include the VM since the prompt was
    # cross-VM.
    assert any("agt-A" in o and "vm: vm-A" in o for o in opts)
    assert any("agt-B" in o and "vm: vm-B" in o for o in opts)
    db.close()


def test_mode_prompt_picks_admin_then_vm_prompt_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--new-workspace`` + no ``--vm`` + no mode flag: if the mode
    prompt picks ``admin``, the VM is still unresolved, so the VM
    prompt MUST fire afterwards."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    monkeypatch.setattr(output, "is_interactive", lambda: True)

    # First chooser call: mode prompt → admin (index 0).
    # Second chooser call: VM prompt → pick vm-A (index 0).
    call_count = [0]

    def _choose_spy(msg: str, opts: list[str]) -> int:
        call_count[0] += 1
        if call_count[0] == 1:
            assert msg == "Run session as:"
            return 0  # admin
        if call_count[0] == 2:
            assert msg == "Select a VM:"
            return 0  # vm-A
        raise AssertionError(f"unexpected third chooser call: {msg}")

    monkeypatch.setattr(output, "choose", _choose_spy)
    _stub_for_post_prompt_flow(monkeypatch)

    with pytest.raises(RuntimeError, match="stop after prompt"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=True,
        )
    assert call_count[0] == 2  # both prompts fired in order
    db.close()


# ---------------------------------------------------------------------------
# SecretTarget builder parity
# ---------------------------------------------------------------------------


def _write_parity_config(tmp_path: Path) -> Path:
    """Config with secrets referenced at every env scope so the parity
    test exercises vm / workspace / admin / agent / session scopes."""
    from textwrap import dedent

    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [vm_templates.default]
        env = {{ VM_TOKEN = {{ secret = "vm-secret" }} }}

        [workspace_templates.default]
        env = {{ WS_TOKEN = {{ secret = "ws-secret" }} }}

        [agent_templates.default]
        env = {{ AGENT_TOKEN = {{ secret = "agent-secret" }} }}

        [admin.config]
        shell = "zsh"

        [admin.env]
        ADMIN_TOKEN = {{ secret = "admin-secret" }}

        [session_templates.default]
        env = {{ SESSION_TOKEN = {{ secret = "session-secret" }} }}

        [secrets.vm-secret]
        description = "vm-scope secret"
        [secrets.ws-secret]
        description = "workspace-scope secret"
        [secrets.agent-secret]
        description = "agent-scope secret"
        [secrets.admin-secret]
        description = "admin-scope secret"
        [secrets.session-secret]
        description = "session-scope secret"
        """)
    )
    return cfg


def _seed_parity_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "parity.db")
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host, template) "
        "VALUES ('vm1', 'lima', 'admin', '100.64.0.5', 'default')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group, template) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1', 'default')"
    )
    db._conn.commit()
    db.insert_agent("agt1", "vm1", "aw-agt1", template="default")
    return db


@pytest.mark.parametrize("mode", ["admin", "agent"])
def test_secret_target_pre_create_parity_with_session_secret_target(
    tmp_path: Path, mode: str
) -> None:
    """For existing workspace + (existing agent | admin mode), the two
    SecretTarget builders must produce equal targets so
    ``compute_needed_secrets`` is invariant across the two helpers."""
    from agentworks.config import load_config
    from agentworks.db import SessionMode
    from agentworks.secrets import compute_needed_secrets
    from agentworks.sessions.manager import (
        _resolve_template,
        _session_secret_target,
        _session_secret_target_pre_create,
    )

    config = load_config(_write_parity_config(tmp_path), warn_issues=False)
    db = _seed_parity_db(tmp_path)
    vm = db.get_vm("vm1")
    ws = db.get_workspace("ws1")
    assert vm is not None
    assert ws is not None
    agent = db.get_agent("agt1") if mode == "agent" else None
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    session_template = _resolve_template(registry, None)

    post = _session_secret_target(
        registry,
        db=db,
        vm=vm,
        ws=ws,
        session_name="s1",
        session_template=session_template,
        mode=SessionMode.AGENT if mode == "agent" else SessionMode.ADMIN,
        agent_name="agt1" if mode == "agent" else None,
    )
    pre = _session_secret_target_pre_create(
        registry,
        name="s1",
        workspace_name="ws1",
        vm=vm,
        session_template=session_template,
        new_workspace=False,
        workspace_template=None,
        existing_workspace=ws,
        new_agent=False,
        agent_template=None,
        existing_agent=agent,
        is_admin_mode=(mode == "admin"),
    )

    assert pre == post
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    assert compute_needed_secrets([pre], registry) == compute_needed_secrets(
        [post], registry
    )

    db.close()
