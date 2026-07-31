"""Eager-resolve atomicity, phase framing, and rollback for
``sessions.manager.create_session``.

Split out of ``test_session_create_ephemeral.py`` (see
``_session_ephemeral_support.py`` for the full background on issue #124's
guarantees). This file covers the "Eager-resolve atomicity" slice: the
single boundary-resolve pass across the ephemeral workspace/agent/session
creations, the operator-facing phase framing, the realize-body seam
contract, and rollback of ephemerals on downstream failure.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks import output
from agentworks.db import Database
from agentworks.errors import ValidationError
from agentworks.secrets.resolver import Resolver

from ._session_ephemeral_support import (
    _install_session_prep_stubs,
    _non_interactive,
    _seed_one_vm,
    _seed_two_vms,
    _stub_build_registry,
)
from .conftest import CapturedOutput

__all__ = ["_non_interactive", "_stub_build_registry"]


# ---------------------------------------------------------------------------
# Eager-resolve atomicity
# ---------------------------------------------------------------------------


def test_eager_resolve_fires_exactly_once_for_new_workspace_and_new_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``new_workspace=True + new_agent=True`` must prompt at most once
    for the union of all secrets across the three creations. Asserts the
    orchestrator runs exactly one boundary resolve pass (env chain +
    graph union) and that it happens BEFORE the workspace / agent
    realization bodies run."""
    from agentworks.secrets.resolver import Resolver as _RealResolver
    from agentworks.sessions.manager import create_session

    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host, init_status) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5', 'complete')"
    )
    db._conn.commit()
    _install_session_prep_stubs(monkeypatch)

    sequence: list[str] = []

    real_resolve = _RealResolver.resolve

    def _counting_resolve(self: Resolver) -> None:
        sequence.append("boundary_resolve")
        real_resolve(self)  # empty set: never touches real backends

    monkeypatch.setattr(_RealResolver, "resolve", _counting_resolve)

    def _ws_spy(db: object, config: object, registry: object, **kwargs: object) -> None:
        sequence.append("realize_workspace")
        db._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
            (kwargs["name"], kwargs["vm"].name, "/tmp/ws", f"ws-{kwargs['name']}"),  # type: ignore[attr-defined]
        )
        db._conn.commit()  # type: ignore[attr-defined]

    def _ag_spy(db: object, config: object, registry: object, **kwargs: object) -> None:
        sequence.append("realize_agent")
        db.insert_agent(kwargs["name"], kwargs["vm"].name, f"aw-{kwargs['name']}")  # type: ignore[attr-defined,union-attr]

    monkeypatch.setattr("agentworks.workspaces.realize.realize_workspace", _ws_spy)
    monkeypatch.setattr("agentworks.agents.realize.realize_agent", _ag_spy)

    class _Stop(Exception):
        pass

    def _stop_at_session_slice(*a: object, **k: object) -> None:
        sequence.append("session_slice")
        raise _Stop

    monkeypatch.setattr("agentworks.sessions.manager._require_workspace", _stop_at_session_slice)

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

    assert sequence.count("boundary_resolve") == 1
    assert sequence == [
        "boundary_resolve",
        "realize_workspace",
        "realize_agent",
        "session_slice",
    ]
    db.close()


def test_session_create_frames_phases_like_a_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Session create reads like a plan executing (the vm-create model):
    a Preflight phase that names each resource it checks, a Resolving
    Secrets phase, then the ephemeral workspace and agent realized as
    distinct, announced stages. Pins the operator-facing framing only;
    the graph, realization order, and secrets are unchanged (the ordering
    tests above pin those)."""
    from agentworks.sessions.manager import create_session

    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host, init_status) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5', 'complete')"
    )
    db._conn.commit()
    _install_session_prep_stubs(monkeypatch)

    def _ws_spy(db: object, config: object, registry: object, **kwargs: object) -> None:
        db._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
            (kwargs["name"], kwargs["vm"].name, "/tmp/ws", f"ws-{kwargs['name']}"),  # type: ignore[attr-defined]
        )
        db._conn.commit()  # type: ignore[attr-defined]

    def _ag_spy(db: object, config: object, registry: object, **kwargs: object) -> None:
        db.insert_agent(kwargs["name"], kwargs["vm"].name, f"aw-{kwargs['name']}")  # type: ignore[attr-defined,union-attr]

    monkeypatch.setattr("agentworks.workspaces.realize.realize_workspace", _ws_spy)
    monkeypatch.setattr("agentworks.agents.realize.realize_agent", _ag_spy)

    class _Stop(Exception):
        pass

    # Stop at the session's own realizing slice: the ephemeral stages
    # (Creating Workspace / Creating Agent) have already been framed and
    # run by this point, which is what this test pins.
    monkeypatch.setattr(
        "agentworks.sessions.manager._require_workspace",
        lambda *a, **k: (_ for _ in ()).throw(_Stop()),
    )

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

    assert "=== Preflight ===" in captured_output.info
    assert "=== Resolving Secrets ===" in captured_output.info
    assert "=== Creating Workspace ===" in captured_output.info
    assert "=== Creating Agent ===" in captured_output.info
    # Preflight names each resource in the <kind>/<name> form vm/agent
    # create use.
    assert any(m.startswith("Checking session-template/") for m in captured_output.info)
    assert any(m.startswith("Checking workspace-template/") for m in captured_output.info)
    assert any(m.startswith("Checking agent-template/") for m in captured_output.info)

    # Nesting (not just substrings): each phase header sits at level 0 and
    # its body renders one level deeper. The Preflight "Checking ..." lines
    # are primary steps, so they render as Role.BODY at level 1 (2 spaces)
    # under the header, and the ephemeral "Creating agent" announce,
    # likewise BODY so it matches its workspace sibling, renders at level 1
    # under its own header.
    assert (output.Role.HEADER, 0, "Preflight") in captured_output.lines
    assert (output.Role.HEADER, 0, "Creating Workspace") in captured_output.lines
    assert (output.Role.HEADER, 0, "Creating Agent") in captured_output.lines
    assert any(
        role is output.Role.BODY and level == 1 and msg.startswith("Checking session-template/")
        for role, level, msg in captured_output.lines
    )
    assert any(
        role is output.Role.BODY and level == 1 and msg.startswith("Creating agent 's1'")
        for role, level, msg in captured_output.lines
    )
    db.close()


def test_realize_bodies_take_domain_shaped_kwargs_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the realization-body seam contract: the bodies are
    phase-free domain code and receive domain-shaped kwargs ONLY. Everything power-shaped arrives already
    prepared by the orchestrator: the agent body's ``git_tokens`` are
    pre-resolved (read through scoped delivery at the one boundary),
    and NO resolver, values mapping, or platform threads through, so a
    body structurally cannot re-run a resolve or re-frame phases. If
    someone widens this seam to "save" a resolve, this test trips."""
    from agentworks.sessions.manager import create_session

    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host, init_status) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5', 'complete')"
    )
    db._conn.commit()
    _install_session_prep_stubs(monkeypatch)

    seam_kwargs: dict[str, set[str]] = {}

    def _ws_spy(db: object, config: object, registry: object, **kwargs: object) -> None:
        seam_kwargs["realize_workspace"] = set(kwargs)
        db._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
            (kwargs["name"], kwargs["vm"].name, "/tmp/ws", f"ws-{kwargs['name']}"),  # type: ignore[attr-defined]
        )
        db._conn.commit()  # type: ignore[attr-defined]

    def _ag_spy(db: object, config: object, registry: object, **kwargs: object) -> None:
        seam_kwargs["realize_agent"] = set(kwargs)
        db.insert_agent(kwargs["name"], kwargs["vm"].name, f"aw-{kwargs['name']}")  # type: ignore[attr-defined,union-attr]

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})
    monkeypatch.setattr("agentworks.workspaces.realize.realize_workspace", _ws_spy)
    monkeypatch.setattr("agentworks.agents.realize.realize_agent", _ag_spy)

    class _Stop(Exception):
        pass

    monkeypatch.setattr(
        "agentworks.sessions.manager._require_workspace",
        lambda *a, **k: (_ for _ in ()).throw(_Stop()),
    )

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

    # Allowlist, not denylist: the seam contract is domain-shaped args
    # and NOTHING else, so any smuggled kwarg (values, resolver,
    # platform, ...) trips this regardless of its name.
    assert seam_kwargs["realize_workspace"] == {"name", "vm", "template"}
    assert seam_kwargs["realize_agent"] == {"name", "vm", "template", "git_tokens"}
    db.close()


def test_failure_after_ephemeral_create_rolls_back_ephemerals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If anything fails after create_workspace / create_agent have run,
    the orchestrator must call delete_agent and delete_workspace to undo
    them. Unused ephemerals must not survive a failed session create."""
    from agentworks.sessions.manager import create_session

    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host, init_status) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5', 'complete')"
    )
    db._conn.commit()
    _install_session_prep_stubs(monkeypatch)

    deletes: list[str] = []

    def _ws_create(db: object, config: object, registry: object, **kwargs: object) -> None:
        db._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
            (kwargs["name"], kwargs["vm"].name, "/tmp/ws", f"ws-{kwargs['name']}"),  # type: ignore[attr-defined]
        )
        db._conn.commit()  # type: ignore[attr-defined]

    def _ag_create(db: object, config: object, registry: object, **kwargs: object) -> None:
        db.insert_agent(kwargs["name"], kwargs["vm"].name, f"aw-{kwargs['name']}")  # type: ignore[attr-defined,union-attr]

    def _ws_delete(db: object, config: object, name: str, **kwargs: object) -> None:
        deletes.append(f"workspace:{name}")

    def _ag_delete(db: object, config: object, *, name: str, **kwargs: object) -> None:
        deletes.append(f"agent:{name}")

    monkeypatch.setattr("agentworks.workspaces.realize.realize_workspace", _ws_create)
    monkeypatch.setattr("agentworks.agents.realize.realize_agent", _ag_create)
    monkeypatch.setattr("agentworks.workspaces.manager.delete_workspace", _ws_delete)
    monkeypatch.setattr("agentworks.agents.manager.delete_agent", _ag_delete)

    def _explode(*a: object, **k: object) -> None:
        raise RuntimeError("simulated session-internal failure")

    monkeypatch.setattr("agentworks.sessions.manager._require_workspace", _explode)

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


def test_new_agent_inherits_vm_from_existing_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``new_agent=True`` against an existing workspace pins the VM via
    the workspace anchor; no ``vm_name`` is required."""
    from agentworks.sessions.manager import create_session

    db = _seed_one_vm(tmp_path)  # vm1 + ws1
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
        )
    assert len(realize_agent_calls) == 1
    assert realize_agent_calls[0]["vm"].name == "vm1"  # type: ignore[attr-defined]
    db.close()


def test_validation_failure_does_not_trigger_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
