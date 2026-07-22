"""Shared fixtures and helpers for the ``test_session_create_ephemeral_*``
test-file family.

This module backs the sibling files that were split out of the original
(oversized) ``test_session_create_ephemeral.py``, which pinned issue #124's
two operator-facing guarantees:

1. **VM-anchor cross-check happens upfront.** When an existing workspace
   and an existing agent are on different VMs, the failure raises before
   any state mutation -- no orphan workspace gets created.

2. **Eager-resolve runs once, atomically, before any state mutation.**
   ``--new-workspace --new-agent`` resolves the union of secret needs
   across all three creations in one call; a Ctrl-C at the prompt leaves
   no orphan workspace or agent. Any failure after state mutation begins
   rolls back every ephemeral resource that was created.

Also pins parity between the two SecretTarget builders so the pre-create
helper can't silently diverge from the existing post-create one for the
inputs they both handle (existing workspace + existing agent or admin
mode).

create_session accepts CLI-flag-shaped args (workspace / new_workspace /
workspace_name / workspace_template / agent / new_agent / agent_name /
agent_template / admin / vm_name) and runs all validation, prompts, and
orchestration in the service layer. The CLI handler is a pure
pass-through.

Each sibling test file imports the fixtures/helpers it needs from here
(fixtures are picked up by pytest via the imported name, same as
importing a fixture from ``conftest``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks import output
from agentworks.db import Database

from .conftest import stub_build_registry, stub_vm_gates


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
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host, init_status) VALUES "
        "('vm-A', 'lima', 'h', 'admin', '100.64.0.1', 'complete'),"
        "('vm-B', 'lima', 'h', 'admin', '100.64.0.2', 'complete')"
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
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host, init_status) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5', 'complete')"
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


def _install_session_prep_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stubs that let create_session run end-to-end with a SimpleNamespace
    config -- no real VM, no real SSH, no real templates."""
    from tests.conftest import stub_session_resolvers

    stub_vm_gates(monkeypatch)

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


def _stub_for_post_prompt_flow(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the downstream flow so a prompt-driven test can exit cleanly
    once the prompt has returned. Returns the call-log list the test can
    inspect."""
    from agentworks.sessions import manager as session_manager

    from .conftest import empty_secret_target

    called: list[str] = []
    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: None)
    # The pre-create SecretTarget joins the resolver's boundary
    # registration and reads template env; the stub above keeps the
    # SimpleNamespace config out of template resolution.
    monkeypatch.setattr(
        session_manager,
        "_session_secret_target_pre_create",
        lambda *a, **k: empty_secret_target(),
    )

    def _spy(*a: object, **k: object) -> None:
        called.append("build_graph")
        raise RuntimeError("stop after prompt")

    stub_vm_gates(monkeypatch)
    monkeypatch.setattr("agentworks.vms.nodes.live_vm_node", _spy)
    return called
