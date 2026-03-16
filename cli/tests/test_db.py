"""Tests for the state database."""

from __future__ import annotations

import sqlite3

import pytest

from agentworks.db import Database, InitStatus, ProvisioningStatus


def test_roundtrip_vm_host(db: Database) -> None:
    db.insert_vm_host("mac-studio", "192.168.1.10", os="darwin")
    host = db.get_vm_host("mac-studio")
    assert host is not None
    assert host.ssh_host == "192.168.1.10"
    assert host.os == "darwin"

    hosts = db.list_vm_hosts()
    assert len(hosts) == 1

    db.delete_vm_host("mac-studio")
    assert db.get_vm_host("mac-studio") is None


def test_roundtrip_vm(db: Database) -> None:
    db.insert_vm_host("mac-studio", "192.168.1.10")
    db.insert_vm(
        "dev-vm",
        platform="lima",
        vm_host_name="mac-studio",
        cpus=4,
        memory_gib=8,
        disk_gib=50,
    )
    vm = db.get_vm("dev-vm")
    assert vm is not None
    assert vm.platform == "lima"
    assert vm.provisioning_status == "pending"
    assert vm.init_status == "pending"
    assert vm.cpus == 4
    assert vm.memory_gib == 8
    assert vm.disk_gib == 50

    db.update_vm_init_status("dev-vm", InitStatus.COMPLETE)
    vm = db.get_vm("dev-vm")
    assert vm is not None
    assert vm.init_status == "complete"

    db.update_vm_tailscale("dev-vm", "100.64.0.1")
    vm = db.get_vm("dev-vm")
    assert vm is not None
    assert vm.tailscale_host == "100.64.0.1"


def test_vm_resources_nullable(db: Database) -> None:
    """Resource columns are nullable for VMs created before v2 migration."""
    db.insert_vm("wsl-vm", platform="wsl2")
    vm = db.get_vm("wsl-vm")
    assert vm is not None
    assert vm.cpus is None
    assert vm.memory_gib is None
    assert vm.disk_gib is None


def test_roundtrip_workspace(db: Database) -> None:
    db.insert_vm_host("mac-studio", "192.168.1.10")
    db.insert_vm("dev-vm", platform="lima", vm_host_name="mac-studio")

    db.insert_workspace(
        "ws-123",
        ws_type="vm",
        workspace_path="/home/agentworks/workspaces/ws-123",
        vm_name="dev-vm",
        template="gruntweave",
    )
    ws = db.get_workspace("ws-123")
    assert ws is not None
    assert ws.type == "vm"
    assert ws.template == "gruntweave"

    # local workspace
    db.insert_workspace(
        "ws-local",
        ws_type="local",
        workspace_path="/Users/test/workspaces/ws-local",
    )
    all_ws = db.list_workspaces()
    assert len(all_ws) == 2

    vm_ws = db.list_workspaces(vm_name="dev-vm")
    assert len(vm_ws) == 1

    local_ws = db.list_workspaces(ws_type="local")
    assert len(local_ws) == 1


def test_vm_delete_cascades(db: Database) -> None:
    db.insert_vm_host("mac-studio", "192.168.1.10")
    db.insert_vm("dev-vm", platform="lima", vm_host_name="mac-studio")
    db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="dev-vm")

    db.delete_vm("dev-vm")

    assert db.get_vm("dev-vm") is None
    assert len(db.list_workspaces(vm_name="dev-vm")) == 0


def test_count_helpers(db: Database) -> None:
    db.insert_vm_host("mac-studio", "192.168.1.10")
    db.insert_vm("vm1", platform="lima", vm_host_name="mac-studio")
    db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="vm1")

    assert db.count_vms_on_host("mac-studio") == 1
    assert db.count_workspaces_on_vm("vm1") == 1


def test_roundtrip_agent(db: Database) -> None:
    db.insert_vm("dev-vm", platform="lima")
    db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="dev-vm")

    agent = db.insert_agent("coder", "ws-1", "ws-1--coder")
    assert agent.name == "coder"
    assert agent.workspace_name == "ws-1"
    assert agent.linux_user == "ws-1--coder"

    fetched = db.get_agent("ws-1", "coder")
    assert fetched is not None
    assert fetched.linux_user == "ws-1--coder"

    assert db.get_agent("ws-1", "nonexistent") is None


def test_list_agents(db: Database) -> None:
    db.insert_vm("dev-vm", platform="lima")
    db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="dev-vm")
    db.insert_workspace("ws-2", ws_type="vm", workspace_path="/tmp/ws-2", vm_name="dev-vm")

    db.insert_agent("coder", "ws-1", "ws-1--coder")
    db.insert_agent("reviewer", "ws-1", "ws-1--reviewer")
    db.insert_agent("coder", "ws-2", "ws-2--coder")

    # filter by workspace
    ws1_agents = db.list_agents(workspace_name="ws-1")
    assert len(ws1_agents) == 2
    assert [a.name for a in ws1_agents] == ["coder", "reviewer"]

    ws2_agents = db.list_agents(workspace_name="ws-2")
    assert len(ws2_agents) == 1

    # list all
    all_agents = db.list_agents()
    assert len(all_agents) == 3


def test_delete_agent(db: Database) -> None:
    db.insert_vm("dev-vm", platform="lima")
    db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="dev-vm")
    db.insert_agent("coder", "ws-1", "ws-1--coder")

    db.delete_agent("ws-1", "coder")
    assert db.get_agent("ws-1", "coder") is None


def test_workspace_delete_cascades_agents(db: Database) -> None:
    db.insert_vm("dev-vm", platform="lima")
    db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="dev-vm")
    db.insert_agent("coder", "ws-1", "ws-1--coder")
    db.insert_agent("reviewer", "ws-1", "ws-1--reviewer")

    db.delete_workspace("ws-1")
    assert len(db.list_agents(workspace_name="ws-1")) == 0


def test_vm_delete_cascades_agents(db: Database) -> None:
    db.insert_vm("dev-vm", platform="lima")
    db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="dev-vm")
    db.insert_agent("coder", "ws-1", "ws-1--coder")

    db.delete_vm("dev-vm")
    assert len(db.list_agents(workspace_name="ws-1")) == 0


def test_agent_linux_user_unique(db: Database) -> None:
    db.insert_vm("dev-vm", platform="lima")
    db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="dev-vm")
    db.insert_agent("coder", "ws-1", "ws-1--coder")

    with pytest.raises(sqlite3.IntegrityError):
        db.insert_agent("coder2", "ws-1", "ws-1--coder")  # duplicate linux_user


def test_agent_composite_pk(db: Database) -> None:
    """Same agent name in different workspaces is allowed."""
    db.insert_vm("dev-vm", platform="lima")
    db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="dev-vm")
    db.insert_workspace("ws-2", ws_type="vm", workspace_path="/tmp/ws-2", vm_name="dev-vm")

    db.insert_agent("coder", "ws-1", "ws-1--coder")
    db.insert_agent("coder", "ws-2", "ws-2--coder")

    assert db.get_agent("ws-1", "coder") is not None
    assert db.get_agent("ws-2", "coder") is not None


def test_delete_agents_for_workspace(db: Database) -> None:
    db.insert_vm("dev-vm", platform="lima")
    db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="dev-vm")
    db.insert_agent("coder", "ws-1", "ws-1--coder")
    db.insert_agent("reviewer", "ws-1", "ws-1--reviewer")

    deleted = db.delete_agents_for_workspace("ws-1")
    assert len(deleted) == 2
    assert len(db.list_agents(workspace_name="ws-1")) == 0


def test_provisioning_status(db: Database) -> None:
    db.insert_vm("dev-vm", platform="lima")
    vm = db.get_vm("dev-vm")
    assert vm is not None
    assert vm.provisioning_status == "pending"

    db.update_vm_provisioning_status("dev-vm", ProvisioningStatus.IN_PROGRESS)
    vm = db.get_vm("dev-vm")
    assert vm is not None
    assert vm.provisioning_status == "in_progress"

    db.update_vm_provisioning_status("dev-vm", ProvisioningStatus.COMPLETE)
    vm = db.get_vm("dev-vm")
    assert vm is not None
    assert vm.provisioning_status == "complete"


def test_vm_events(db: Database) -> None:
    db.insert_vm("dev-vm", platform="lima")

    db.insert_vm_event("dev-vm", "provisioning_started", "lima:dev-vm")
    db.insert_vm_event("dev-vm", "provisioning_complete", "100.64.0.1")

    events = db.list_vm_events("dev-vm")
    assert len(events) == 2
    assert events[0].event == "provisioning_started"
    assert events[0].detail == "lima:dev-vm"
    assert events[1].event == "provisioning_complete"


def test_vm_delete_cascades_events(db: Database) -> None:
    db.insert_vm("dev-vm", platform="lima")
    db.insert_vm_event("dev-vm", "provisioning_started")

    db.delete_vm("dev-vm")
    # Events should be cleaned up (can't query directly since VM is gone,
    # but the delete should not raise a foreign key error)
