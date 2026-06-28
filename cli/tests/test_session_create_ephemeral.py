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
            new_workspace=True,
            admin=True,
        )
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


def test_no_workspace_specified_raises_in_non_interactive(tmp_path: Path) -> None:
    """When neither --workspace nor --new-workspace is specified and
    there's more than one workspace on disk, the service has to prompt
    to pick. In non-interactive mode (the test default), the prompt
    raises rather than hanging."""
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)  # 2 workspaces → prompt would fire
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match="--workspace or --new-workspace"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
        )
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
    session_template = _resolve_template(config, None)

    post = _session_secret_target(
        config,
        db=db,
        vm=vm,
        ws=ws,
        session_name="s1",
        session_template=session_template,
        mode=SessionMode.AGENT if mode == "agent" else SessionMode.ADMIN,
        agent_name="agt1" if mode == "agent" else None,
    )
    pre = _session_secret_target_pre_create(
        config,
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
    assert compute_needed_secrets([pre], config) == compute_needed_secrets([post], config)

    db.close()
