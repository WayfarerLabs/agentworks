"""Shared fixtures and stubs for the test_consoles_* shard modules.

`test_consoles.py` grew past the file-size guideline in `.claude/rules/code-style.md`
and was split into sibling shard modules (`test_consoles_specs_db.py`,
`test_consoles_orchestration.py`, `test_consoles_attach.py`,
`test_consoles_live_sync.py`, `test_consoles_shell_panes.py`). This module holds the
seed helpers and stub Config classes those shards all depend on, plus the
`_stub_build_registry` autouse fixture.

Note: pytest only auto-applies autouse fixtures that are visible in a test module's
own namespace, so each shard re-imports `_stub_build_registry` by name (even though
it's never called directly) purely to bring it into scope:

    from tests._consoles_support import _stub_build_registry

This module is intentionally not named `test_*` so pytest does not try to collect it
as a test module in its own right.
"""

from __future__ import annotations

import pytest

from agentworks.db import Database
from tests.conftest import stub_build_registry


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve Registry reads from the module's namespace configs."""
    stub_build_registry(monkeypatch)


def _seed_vm(db: Database, vm_name: str = "vm1", *, with_tailscale: bool = False) -> None:
    """Insert a VM and a workspace. No tailscale host -> live-sync skips."""
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username) VALUES (?, 'lima', 'h', 'admin')",
        (vm_name,),
    )
    if with_tailscale:
        db._conn.execute(
            "UPDATE vms SET tailscale_host = ? WHERE name = ?",
            (f"100.64.0.{hash(vm_name) % 250}", vm_name),
        )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
        (f"ws-{vm_name}", vm_name, f"/home/me/{vm_name}", f"ws-ws-{vm_name}"),
    )
    db._conn.commit()


def _seed_sessions(db: Database, names: list[str], *, workspace_name: str = "ws-vm1") -> None:
    for n in names:
        # Per the env-and-secrets SDD, all sessions (admin and agent) carry
        # a per-session socket path. Tests that only need the row's existence
        # use a sentinel path; tests that actually probe tmux replace it.
        db._conn.execute(
            "INSERT INTO sessions (name, workspace_name, template, mode, socket_path) "
            "VALUES (?, ?, 'default', 'admin', ?)",
            (n, workspace_name, f"/run/agentworks/admin-tmux-sockets/admin/{n}.sock"),
        )
    db._conn.commit()


class _StubNamedConsoleConfig:
    tmux_layout: str = "tiled"


class _StubAdminConfig:
    env: dict[str, object] = {}  # noqa: RUF012 - stub class attr


class _StubConfig:
    """A no-op Config stand-in.

    Tests that don't install the ``fake_target`` fixture also use VMs seeded
    with ``with_tailscale=False`` so ``_live_target`` returns None up front
    and the SSH layer is never entered. If you set ``with_tailscale=True``
    without monkey-patching ``transports.transport`` you will hit an
    AttributeError on this stub -- prefer the ``fake_target`` fixture.

    ``named_console`` provides only what multi_console reads from Config;
    extend here as new fields are added to NamedConsoleConfig.

    ``vm_templates``, ``agent_templates``, ``workspace_templates``,
    ``session_templates``, and ``admin`` carry empty defaults so
    ``_resolve_pane_env`` and related env-resolution helpers in
    multi_console don't crash on stub inputs; tests that probe env flow
    should use real Config rather than this stub.
    """

    named_console = _StubNamedConsoleConfig()
    vm_templates: dict[str, object] = {}  # noqa: RUF012
    agent_templates: dict[str, object] = {}  # noqa: RUF012
    workspace_templates: dict[str, object] = {}  # noqa: RUF012
    session_templates: dict[str, object] = {}  # noqa: RUF012
    admin: _StubAdminConfig = _StubAdminConfig()
