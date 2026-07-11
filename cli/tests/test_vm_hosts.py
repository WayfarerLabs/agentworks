"""The vm_hosts.manager PHASE-2 BRIDGE: the registry is gone; every
service function raises the typed replaced-by-vm-sites error (the CLI
commands themselves are removed in the CLI-surface phase).
"""

from __future__ import annotations

import pytest

from agentworks.db import Database
from agentworks.errors import StateError
from agentworks.vm_hosts.manager import add_vm_host, list_vm_hosts, remove_vm_host


def test_add_vm_host_raises_replaced(db: Database) -> None:
    with pytest.raises(StateError, match="replaced by vm-site") as exc:
        add_vm_host(db, "host1", "host1.example.com")
    assert "name: host1" in (exc.value.hint or "")


def test_list_vm_hosts_raises_replaced(db: Database) -> None:
    with pytest.raises(StateError, match="replaced by vm-site"):
        list_vm_hosts(db)


def test_list_vm_hosts_names_only_is_quiet(db: Database) -> None:
    """Shell completion callers degrade to empty output, not an error."""
    list_vm_hosts(db, names_only=True)


def test_remove_vm_host_raises_replaced(db: Database) -> None:
    with pytest.raises(StateError, match="replaced by vm-site"):
        remove_vm_host(db, "host1", force=True, yes=True)
