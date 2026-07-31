"""Shared fixtures and helpers for the ``test_secrets_eager_resolve_*``
test-file family.

This module backs the sibling files that were split out of the original
(oversized) ``test_secrets_eager_resolve.py``, which pins the operator-facing
guarantee from FRD R4: every shell-opening command resolves secrets up
front, BEFORE any state mutation. If resolution fails (e.g. non-interactive
+ no AW_SECRET_<NAME> in env), the failure surfaces as
``SecretUnavailableError`` with no DB or VM side-effects.

The tests work by patching the boundary (``resolve_for_command`` for the
paths that still call it directly, ``Resolver.resolve`` or
``Resolver.register_targets`` for the roots whose env chain rides the
operation's one resolve pass) to raise; if the manager reaches it AFTER
mutating state, the DB inspection at the end of the test catches the leak.

Each sibling test file imports the fixtures/helpers it needs from here
(fixtures are picked up by pytest via the imported name, same as importing
a fixture from ``conftest``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.db import Database

from .conftest import stub_build_registry, stub_vm_gates


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """SimpleNamespace configs don't carry publish_to; Phase 2a's
    manager-entry hoist is no-op'd via the shared helper."""
    stub_build_registry(monkeypatch)


class _NullCM:
    """No-op context manager used to stub context-manager seams."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_a: object) -> None:
        return None


def _stub_target() -> object:
    class _Result:
        ok = True
        returncode = 0
        stdout = ""
        stderr = ""

    class _Target:
        def run(self, *args: object, **kwargs: object) -> _Result:
            return _Result()

    return _Target()


def _seed_basic_db(tmp_path: Path) -> Database:
    """VM + workspace seeded; no agent. Enough for an admin-mode session."""
    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    db._conn.commit()
    return db


def _stub_session_prep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ssh / vm probes that would otherwise need a real VM."""
    stub_vm_gates(monkeypatch)
    factory = lambda *a, **k: _stub_target()  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", factory)
