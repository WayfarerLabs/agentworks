"""Tests for the vm_hosts.manager service layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import StateError, UserAbort
from agentworks.vm_hosts.manager import remove_vm_host

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


def _seed_host_with_vm(db: Database, host_name: str = "host1", vm_name: str = "vm1") -> None:
    db.insert_vm_host(host_name, "host1.example.com", platform="lima", os="linux")
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, vm_host_name) "
        "VALUES (?, 'lima', 'admin', ?)",
        (vm_name, host_name),
    )
    db._conn.commit()


def test_remove_vm_host_prompts_by_default(db: Database, captured_output: CapturedOutput) -> None:
    """No --yes, no --force, no VMs: prompts (and proceeds when confirmed)."""
    db.insert_vm_host("host1", "host1.example.com", platform="lima", os="linux")
    captured_output.confirm_response = True

    remove_vm_host(db, "host1")

    assert db.get_vm_host("host1") is None


def test_remove_vm_host_user_abort_when_prompt_declined(
    db: Database, captured_output: CapturedOutput
) -> None:
    """Prompt declined: raise UserAbort and leave the host in place."""
    db.insert_vm_host("host1", "host1.example.com", platform="lima", os="linux")
    captured_output.confirm_response = False

    with pytest.raises(UserAbort):
        remove_vm_host(db, "host1")

    assert db.get_vm_host("host1") is not None


def test_remove_vm_host_yes_skips_prompt(db: Database, captured_output: CapturedOutput) -> None:
    """--yes: no prompt, host removed."""
    db.insert_vm_host("host1", "host1.example.com", platform="lima", os="linux")
    # If the code prompted, confirm_response=False would block the removal.
    captured_output.confirm_response = False

    remove_vm_host(db, "host1", yes=True)

    assert db.get_vm_host("host1") is None


def test_remove_vm_host_refuses_when_vms_exist_without_force(db: Database) -> None:
    """Default path: VMs reference the host -> StateError, no prompt."""
    _seed_host_with_vm(db)

    with pytest.raises(StateError, match="1 VM"):
        remove_vm_host(db, "host1")

    assert db.get_vm_host("host1") is not None


def test_remove_vm_host_force_unlinks_vms_and_skips_prompt(
    db: Database, captured_output: CapturedOutput
) -> None:
    """--force: bypass the safety check, unlink VMs, no prompt."""
    _seed_host_with_vm(db)
    captured_output.confirm_response = False  # would block if prompted

    remove_vm_host(db, "host1", force=True)

    assert db.get_vm_host("host1") is None
    vm = db.get_vm("vm1")
    assert vm is not None
    assert vm.vm_host_name is None
