"""VM-anchor resolution and mutex/validation guards for
``sessions.manager.create_session``.

Split out of ``test_session_create_ephemeral.py`` (see
``_session_ephemeral_support.py`` for the full background on issue #124's
guarantees). This file covers the "Validation" slice: the VM-anchor
cross-check happening upfront, the flag-mutex guards, and the ephemeral
naming/collision checks that also run before any state mutation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.db import Database
from agentworks.errors import ValidationError

from ._session_ephemeral_support import (
    _install_session_prep_stubs,
    _non_interactive,
    _seed_one_vm,
    _seed_two_vms,
    _stub_build_registry,
)
from .conftest import empty_secret_target, stub_vm_gates

__all__ = ["_non_interactive", "_stub_build_registry"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_ephemeral_workspace_name_uses_workspace_cap(tmp_path: Path) -> None:
    """An ephemeral (``--new-workspace``) name is validated against the
    WORKSPACE cap (29, the Linux-group budget), NOT the freeform session cap:
    a 30-char name that fits a session name is rejected because it would
    overflow the ``ws-<name>`` group."""
    from agentworks.agents.grants import MAX_WORKSPACE_NAME_LENGTH
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match=f"max {MAX_WORKSPACE_NAME_LENGTH}") as excinfo:
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=True,
            workspace_name="w" * (MAX_WORKSPACE_NAME_LENGTH + 1),
            vm_name="vm1",
        )
    # Explicitly-passed name: plain error, no override hint (the operator named
    # it directly, so pointing at --workspace-name would be noise).
    assert excinfo.value.hint is None
    assert db.get_session("s1") is None
    db.close()


def test_ephemeral_agent_name_uses_agent_cap(tmp_path: Path) -> None:
    """An ephemeral (``--new-agent``) name is validated against the AGENT cap
    (28, the Linux-username budget), NOT the freeform session cap: a 29-char
    name that fits a session name is rejected because it would overflow the
    ``agt-<name>`` username."""
    from agentworks.agents.manager import MAX_AGENT_NAME_LENGTH
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match=f"max {MAX_AGENT_NAME_LENGTH}") as excinfo:
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            new_agent=True,
            agent_name="a" * (MAX_AGENT_NAME_LENGTH + 1),
        )
    # Explicitly-passed name: plain error, no override hint.
    assert excinfo.value.hint is None
    assert db.get_session("s1") is None
    db.close()


def test_defaulted_ephemeral_agent_name_overflow_hints_override_flag(tmp_path: Path) -> None:
    """When ``--new-agent`` is given with NO ``--agent-name``, the agent name
    defaults to the session name. A session name that is itself valid (<= 64)
    but overflows the 28-char agent cap must be rejected WITH a hint that names
    the derived default and points at ``--agent-name`` to override it."""
    from agentworks.agents.manager import MAX_AGENT_NAME_LENGTH
    from agentworks.config import MAX_FREEFORM_NAME_LENGTH
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    # 40 chars: a valid session name, but over the agent cap.
    long_session = "s" * 40
    assert MAX_AGENT_NAME_LENGTH < len(long_session) <= MAX_FREEFORM_NAME_LENGTH

    with pytest.raises(ValidationError, match=f"max {MAX_AGENT_NAME_LENGTH}") as excinfo:
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name=long_session,
            workspace="ws1",
            new_agent=True,
        )
    hint = excinfo.value.hint
    assert hint is not None, "defaulted-overflow must carry a hint"
    assert "--agent-name" in hint
    assert "defaulted to the session name" in hint
    assert db.get_session(long_session) is None
    db.close()


def test_defaulted_ephemeral_workspace_name_overflow_hints_override_flag(tmp_path: Path) -> None:
    """When ``--new-workspace`` is given with NO ``--workspace-name``, the
    workspace name defaults to the session name. A session name that overflows
    the 29-char workspace cap is rejected WITH a hint pointing at
    ``--workspace-name``."""
    from agentworks.agents.grants import MAX_WORKSPACE_NAME_LENGTH
    from agentworks.config import MAX_FREEFORM_NAME_LENGTH
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    long_session = "s" * 40
    assert MAX_WORKSPACE_NAME_LENGTH < len(long_session) <= MAX_FREEFORM_NAME_LENGTH

    with pytest.raises(ValidationError, match=f"max {MAX_WORKSPACE_NAME_LENGTH}") as excinfo:
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name=long_session,
            new_workspace=True,
            vm_name="vm1",
        )
    hint = excinfo.value.hint
    assert hint is not None, "defaulted-overflow must carry a hint"
    assert "--workspace-name" in hint
    assert "defaulted to the session name" in hint
    assert db.get_session(long_session) is None
    db.close()


def test_session_name_uses_freeform_cap(tmp_path: Path) -> None:
    """The SESSION name itself is freeform (tmux/display only), so it takes the
    generous freeform cap (64), not a username/group-derived one: a 40-char
    session name that would be rejected as an agent/workspace name is accepted
    (it fails later only on the freeform boundary)."""
    from agentworks.config import MAX_FREEFORM_NAME_LENGTH
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(ValidationError, match=f"max {MAX_FREEFORM_NAME_LENGTH}"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s" * (MAX_FREEFORM_NAME_LENGTH + 1),
            workspace="ws1",
            admin=True,
        )
    assert db.get_session("s" * (MAX_FREEFORM_NAME_LENGTH + 1)) is None
    db.close()


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
    The first downstream call (ensure_active) is what we use as a
    sentinel that we got past validation."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import create_session

    db = _seed_two_vms(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    # Template resolution is downstream of anchor cross-check but upstream
    # of ensure_active; stub it so the SimpleNamespace config doesn't
    # need session_templates.
    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)
    # The pre-create SecretTarget joins the resolver's boundary
    # registration and reads template env; the stub above keeps the
    # SimpleNamespace config out of template resolution.
    monkeypatch.setattr(
        session_manager,
        "_session_secret_target_pre_create",
        lambda *a, **k: empty_secret_target(),
    )

    called: list[str] = []

    def _spy(*args: object, **kwargs: object) -> None:
        called.append("build_graph")
        raise RuntimeError("stop after anchor check")

    stub_vm_gates(monkeypatch)
    monkeypatch.setattr("agentworks.vms.nodes.live_vm_node", _spy)

    with pytest.raises(RuntimeError, match="stop after anchor check"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws-A",
            agent="agt-A",  # also on vm-A; pins the mode so no mode prompt fires
            vm_name="vm-A",
        )
    assert called == ["build_graph"]
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


def test_no_vm_anchor_with_single_vm_auto_selects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    # The pre-create SecretTarget joins the resolver's boundary
    # registration and reads template env; the stub above keeps the
    # SimpleNamespace config out of template resolution.
    monkeypatch.setattr(
        session_manager,
        "_session_secret_target_pre_create",
        lambda *a, **k: empty_secret_target(),
    )

    called: list[str] = []

    def _spy(*args: object, **kwargs: object) -> None:
        called.append("build_graph")
        raise RuntimeError("stop after VM resolution")

    stub_vm_gates(monkeypatch)
    monkeypatch.setattr("agentworks.vms.nodes.live_vm_node", _spy)

    with pytest.raises(RuntimeError, match="stop after VM resolution"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            new_workspace=True,
            admin=True,
        )
    assert called == ["build_graph"]
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


def test_new_agent_with_explicit_agent_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--new-agent --agent-name X`` creates a new agent named X (not
    a lookup of existing). Regression for the bogus agent_name/new_agent
    mutex check that briefly existed."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    _install_session_prep_stubs(monkeypatch)

    realize_agent_calls: list[dict[str, object]] = []

    def _spy(db: object, config: object, registry: object, **kwargs: object) -> None:
        realize_agent_calls.append(dict(kwargs))
        raise RuntimeError("stop after agent realize")

    monkeypatch.setattr("agentworks.agents.realize.realize_agent", _spy)

    with pytest.raises(RuntimeError, match="stop after agent realize"):
        create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            new_agent=True,
            agent_name="my-named-agent",
        )
    (call,) = realize_agent_calls
    assert call["name"] == "my-named-agent"
    assert call["vm"].name == "vm1"  # type: ignore[attr-defined]
    db.close()


def test_ephemeral_agent_name_defaults_to_session_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``new_agent=True`` is set without ``agent_name``, the new
    agent's name defaults to the session name. The service layer owns
    this default; the CLI just forwards None when --agent-name wasn't
    supplied."""
    from agentworks.sessions.manager import create_session

    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host, init_status) "
        "VALUES ('bbvm1', 'lima', 'h', 'admin', '100.64.0.5', 'complete')"
    )
    db._conn.commit()
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    _install_session_prep_stubs(monkeypatch)

    workspace_calls: list[dict[str, object]] = []
    agent_calls: list[dict[str, object]] = []

    def _ws_spy(db: object, config: object, registry: object, **kwargs: object) -> None:
        workspace_calls.append(dict(kwargs))

    def _ag_spy(db: object, config: object, registry: object, **kwargs: object) -> None:
        agent_calls.append(dict(kwargs))
        raise RuntimeError("stop after agent realize")

    monkeypatch.setattr("agentworks.workspaces.realize.realize_workspace", _ws_spy)
    monkeypatch.setattr("agentworks.agents.realize.realize_agent", _ag_spy)

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
