"""Tests for the state database."""

from __future__ import annotations

from pathlib import Path

from agentworks.db import Database, InitStatus


def test_roundtrip_vm_host(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        db.insert_vm_host("mac-studio", "192.168.1.10", os="darwin")
        host = db.get_vm_host("mac-studio")
        assert host is not None
        assert host.ssh_host == "192.168.1.10"
        assert host.os == "darwin"

        hosts = db.list_vm_hosts()
        assert len(hosts) == 1

        db.delete_vm_host("mac-studio")
        assert db.get_vm_host("mac-studio") is None
    finally:
        db.close()


def test_roundtrip_vm(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        db.insert_vm_host("mac-studio", "192.168.1.10")
        db.insert_vm(
            "dev-vm",
            platform="lima",
            vm_host_name="mac-studio",
            extra_packages=["nodejs", "python3"],
            cpus=4,
            memory_gib=8,
            disk_gib=50,
        )
        vm = db.get_vm("dev-vm")
        assert vm is not None
        assert vm.platform == "lima"
        assert vm.extra_packages == ["nodejs", "python3"]
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
    finally:
        db.close()


def test_vm_resources_nullable(tmp_path: Path) -> None:
    """Resource columns are nullable for VMs created before v2 migration."""
    db = Database(tmp_path / "test.db")
    try:
        db.insert_vm("wsl-vm", platform="wsl2")
        vm = db.get_vm("wsl-vm")
        assert vm is not None
        assert vm.cpus is None
        assert vm.memory_gib is None
        assert vm.disk_gib is None
    finally:
        db.close()


def test_roundtrip_workspace(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    try:
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
    finally:
        db.close()


def test_roundtrip_git_host_keys(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        db.insert_vm_host("mac-studio", "192.168.1.10")
        db.insert_vm("dev-vm", platform="lima", vm_host_name="mac-studio")

        key = db.insert_vm_git_host_key("dev-vm", "github", "key-123")
        assert key.remote_key_id == "key-123"

        keys = db.list_vm_git_host_keys("dev-vm")
        assert len(keys) == 1

        db.delete_vm_git_host_key(key.id)
        assert len(db.list_vm_git_host_keys("dev-vm")) == 0
    finally:
        db.close()


def test_vm_delete_cascades(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        db.insert_vm_host("mac-studio", "192.168.1.10")
        db.insert_vm("dev-vm", platform="lima", vm_host_name="mac-studio")
        db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="dev-vm")
        db.insert_vm_git_host_key("dev-vm", "github", "key-123")

        db.delete_vm("dev-vm")

        assert db.get_vm("dev-vm") is None
        assert len(db.list_workspaces(vm_name="dev-vm")) == 0
        assert len(db.list_vm_git_host_keys("dev-vm")) == 0
    finally:
        db.close()


def test_count_helpers(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        db.insert_vm_host("mac-studio", "192.168.1.10")
        db.insert_vm("vm1", platform="lima", vm_host_name="mac-studio")
        db.insert_workspace("ws-1", ws_type="vm", workspace_path="/tmp/ws-1", vm_name="vm1")

        assert db.count_vms_on_host("mac-studio") == 1
        assert db.count_workspaces_on_vm("vm1") == 1
    finally:
        db.close()
