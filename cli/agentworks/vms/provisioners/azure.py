"""Azure VM provisioner -- creates VMs via az cli."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from agentworks.db import VMStatus
from agentworks.ssh import ExecTarget, SSHTarget
from agentworks.vms.base import ProvisionResult, VMProvisioner

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import VMRow

CLOUD_INIT_TEMPLATE = """\
#cloud-config
package_update: true
packages:
  - openssh-server
users:
  - name: agentworks
    ssh_authorized_keys:
      - {ssh_public_key}
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
"""


def _az(args: list[str], *, check: bool = True) -> str:
    """Run an az cli command and return stdout."""
    result = subprocess.run(["az", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"az command failed: {result.stderr.strip()}")
    return result.stdout


class AzureProvisioner(VMProvisioner):
    """Provisions Azure VMs via az cli with cloud-init."""

    def create(self, vm_name: str, config: Config, extra_packages: list[str] | None = None) -> ProvisionResult:
        assert config.azure is not None, "Azure config is required"
        az = config.azure

        typer.echo(f"Creating Azure VM '{vm_name}' in {az.region}...")

        # Read user's SSH public key
        ssh_pub_key = config.user.ssh_public_key.read_text().strip()

        # Write cloud-init userdata
        cloud_init = CLOUD_INIT_TEMPLATE.format(ssh_public_key=ssh_pub_key)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(cloud_init)
            cloud_init_path = f.name

        try:
            output = _az([
                "vm", "create",
                "--resource-group", az.resource_group,
                "--name", vm_name,
                "--image", "Debian:debian-12:12-gen2:latest",
                "--size", "Standard_D4s_v5",
                "--admin-username", "agentworks",
                "--ssh-key-values", ssh_pub_key,
                "--custom-data", cloud_init_path,
                "--public-ip-sku", "Standard",
                "--nsg-rule", "SSH",
                "--tags", "owner=agentworks",
                "--output", "json",
            ])
        finally:
            Path(cloud_init_path).unlink()

        vm_info = json.loads(output)
        public_ip = vm_info.get("publicIpAddress", "")
        resource_id = vm_info.get("id", "")

        typer.echo(f"Azure VM '{vm_name}' created (IP: {public_ip})")

        return ProvisionResult(
            exec_target=ExecTarget(
                ssh=SSHTarget(
                    host=public_ip,
                    user="agentworks",
                    identity_file=config.user.ssh_private_key,
                )
            ),
            azure_resource_id=resource_id or None,
        )

    def start(self, vm: VMRow) -> None:
        typer.echo(f"Starting Azure VM '{vm.name}'...")
        assert vm.azure_resource_id is not None
        rg, name = _parse_resource_id(vm.azure_resource_id)
        _az(["vm", "start", "--resource-group", rg, "--name", name])
        typer.echo(f"Azure VM '{vm.name}' started")

    def stop(self, vm: VMRow) -> None:
        typer.echo(f"Deallocating Azure VM '{vm.name}'...")
        assert vm.azure_resource_id is not None
        rg, name = _parse_resource_id(vm.azure_resource_id)
        _az(["vm", "deallocate", "--resource-group", rg, "--name", name])
        typer.echo(f"Azure VM '{vm.name}' deallocated")

    def delete(self, vm: VMRow) -> None:
        typer.echo(f"Deleting Azure VM '{vm.name}'...")
        if vm.azure_resource_id is None:
            typer.echo("Warning: no Azure resource ID, skipping Azure cleanup")
            return
        rg, name = _parse_resource_id(vm.azure_resource_id)
        _az(["vm", "delete", "--resource-group", rg, "--name", name, "--yes"], check=False)
        # Clean up associated resources
        for resource_type in ["nic", "disk", "nsg", "public-ip"]:
            _az(
                ["network" if resource_type in ("nic", "nsg", "public-ip") else "disk",
                 resource_type if resource_type != "disk" else "delete",
                 "delete" if resource_type != "disk" else "",
                 "--resource-group", rg,
                 "--name", f"{name}*",
                 "--yes"],
                check=False,
            )
        typer.echo(f"Azure VM '{vm.name}' deleted")

    def status(self, vm: VMRow) -> VMStatus:
        if vm.azure_resource_id is None:
            return VMStatus.UNKNOWN
        rg, name = _parse_resource_id(vm.azure_resource_id)
        try:
            output = _az([
                "vm", "get-instance-view",
                "--resource-group", rg,
                "--name", name,
                "--output", "json",
            ])
        except RuntimeError:
            return VMStatus.UNKNOWN

        info = json.loads(output)
        statuses = info.get("instanceView", {}).get("statuses", [])
        for s in statuses:
            code = s.get("code", "")
            if code == "PowerState/running":
                return VMStatus.RUNNING
            if code == "PowerState/stopped":
                return VMStatus.STOPPED
            if code == "PowerState/deallocated":
                return VMStatus.DEALLOCATED
        return VMStatus.UNKNOWN


def _parse_resource_id(resource_id: str) -> tuple[str, str]:
    """Extract resource group and name from an Azure resource ID."""
    parts = resource_id.split("/")
    rg_idx = next(i for i, p in enumerate(parts) if p.lower() == "resourcegroups")
    name_idx = next(i for i, p in enumerate(parts) if p.lower() == "virtualmachines")
    return parts[rg_idx + 1], parts[name_idx + 1]
