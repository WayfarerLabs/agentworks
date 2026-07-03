"""Proxmox VE VM provisioner -- creates and manages VMs via the Proxmox REST API."""

from __future__ import annotations

import os
import time
import urllib.parse
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import VMStatus
from agentworks.transports import SSHTransport
from agentworks.vms.base import ProvisionResult, VMProvisioner
from agentworks.vms.bootstrap_script import generate_bootstrap_script, vm_hostname
from agentworks.vms.cloud_init import PROVISIONING_PACKAGES
from agentworks.vms.provisioners.proxmox_api import ProxmoxAPI, ProxmoxAPIError

if TYPE_CHECKING:
    from agentworks.config import Config, ProxmoxConfig
    from agentworks.db import VMRow
    from agentworks.transports import Transport


class ProxmoxProvisioner(VMProvisioner):
    """Provisions VMs on Proxmox VE via clone + cloud-init + guest agent bootstrap."""

    def __init__(self, proxmox_config: ProxmoxConfig) -> None:
        self._cfg = proxmox_config
        token_secret = os.environ.get("PROXMOX_TOKEN_SECRET", "")
        if not token_secret:
            raise RuntimeError(
                "PROXMOX_TOKEN_SECRET environment variable is required"
            )
        self._api = ProxmoxAPI(
            api_url=proxmox_config.api_url,
            token_id=proxmox_config.token_id,
            token_secret=token_secret,
            verify_ssl=proxmox_config.verify_ssl,
        )

    def create(
        self,
        vm_name: str,
        config: Config,
        *,
        cpus: int | None = None,
        memory: int | None = None,
        disk: int | None = None,
        swap: int = 4,
        admin_username: str = "agentworks",
        tailscale_auth_key: str | None = None,
    ) -> ProvisionResult:
        node = self._cfg.node
        template_vmid = self._cfg.template_vmid

        output.info(f"Provisioning Proxmox VM '{vm_name}' on node {node}...")

        # 1. Get next VMID
        newid = self._api.next_id()
        output.detail(f"Allocated VMID: {newid}")

        # 2. Clone template into the agentworks pool
        output.detail(f"Cloning template {template_vmid}...")
        upid = self._api.clone_vm(
            node, template_vmid, newid, vm_name,
            storage=self._cfg.storage,
            pool=self._cfg.pool,
        )
        self._api.wait_for_task(node, upid)
        output.detail("Clone complete")

        # 3. Configure VM resources
        vm_config: dict[str, object] = {}
        if cpus is not None:
            vm_config["cores"] = cpus
        if memory is not None:
            vm_config["memory"] = memory * 1024  # GiB -> MiB

        # Cloud-init: user, SSH key, network
        ssh_pub_key = config.operator.ssh_public_key.read_text().strip()
        vm_config["ciuser"] = admin_username
        vm_config["sshkeys"] = urllib.parse.quote(ssh_pub_key, safe="")
        vm_config["ipconfig0"] = "ip=dhcp"

        # Boot order, guest agent, and CPU type (host passthrough exposes
        # AVX/AVX2 which tools like Bun require)
        vm_config["boot"] = "order=scsi0"
        vm_config["agent"] = "enabled=1"
        vm_config["cpu"] = "host"

        output.detail("Configuring VM...")
        self._api.configure_vm(node, newid, **vm_config)

        # 4. Resize disk if requested
        if disk is not None:
            output.detail(f"Resizing disk to {disk}G...")
            self._api.resize_disk(node, newid, "scsi0", f"{disk}G")

        # 5. Start VM
        output.detail("Starting VM...")
        upid = self._api.start_vm(node, newid)
        self._api.wait_for_task(node, upid)

        # 6. Wait for guest agent and get VM IP
        output.detail("Waiting for guest agent...")
        ip = self._wait_for_guest_ip(node, newid)
        output.detail(f"VM IP: {ip}")

        # 7. Wait for cloud-init to finish (releases apt lock)
        output.detail("Waiting for cloud-init...")
        self._wait_for_cloud_init(node, newid)

        # 8. Run bootstrap script via guest agent
        bootstrap_complete = False
        tailscale_ip: str | None = None
        if tailscale_auth_key:
            output.detail("Running bootstrap via guest agent...")
            bootstrap = generate_bootstrap_script(
                admin_username=admin_username,
                ssh_public_key=ssh_pub_key,
                provisioning_packages=PROVISIONING_PACKAGES,
                tailscale_auth_key=tailscale_auth_key,
                hostname=vm_hostname("proxmox", vm_name),
                swap=swap,
            )
            tailscale_ip = self._run_bootstrap_via_agent(node, newid, bootstrap)
            bootstrap_complete = tailscale_ip is not None
            if tailscale_ip:
                output.detail(f"Tailscale IP: {tailscale_ip}")

        host = tailscale_ip or ip
        target = SSHTransport(
            host=host,
            user=admin_username,
            identity_file=config.operator.ssh_private_key,
        )

        return ProvisionResult(
            provisioner_transport=target,
            proxmox_vmid=str(newid),
            bootstrap_complete=bootstrap_complete,
            tailscale_ip=tailscale_ip,
        )

    def start(self, vm: VMRow) -> None:
        vmid = self._vmid(vm)
        upid = self._api.start_vm(self._cfg.node, vmid)
        self._api.wait_for_task(self._cfg.node, upid)

    def stop(self, vm: VMRow) -> None:
        vmid = self._vmid(vm)
        upid = self._api.stop_vm(self._cfg.node, vmid)
        self._api.wait_for_task(self._cfg.node, upid)

    def delete(self, vm: VMRow) -> None:
        vmid = self._vmid(vm)
        node = self._cfg.node

        # Stop if running
        try:
            status = self._api.vm_status(node, vmid)
            if status.get("status") == "running":
                upid = self._api.stop_vm(node, vmid)
                self._api.wait_for_task(node, upid)
        except ProxmoxAPIError:
            pass  # VM may already be gone

        # Delete VM
        try:
            upid = self._api.delete_vm(node, vmid)
            self._api.wait_for_task(node, upid)
        except ProxmoxAPIError:
            pass  # best-effort

    def status(self, vm: VMRow) -> VMStatus:
        vmid = self._vmid(vm)
        try:
            result = self._api.vm_status(self._cfg.node, vmid)
        except ProxmoxAPIError:
            return VMStatus.UNKNOWN
        pve_status = result.get("status", "")
        if pve_status == "running":
            return VMStatus.RUNNING
        if pve_status == "stopped":
            return VMStatus.STOPPED
        return VMStatus.UNKNOWN

    def provisioner_transport(
        self, vm: VMRow, *, config: object | None = None,
    ) -> Transport:
        raise NotImplementedError(
            "Proxmox provisioning transport not yet implemented. "
            "Requires QEMU guest agent exec integration. "
            "See docs/sdd/2026-04-27-exec-target-cleanup/hla.md for details."
        )

    # -- Helpers ---------------------------------------------------------------

    def _vmid(self, vm: VMRow) -> int:
        if not vm.proxmox_vmid:
            raise RuntimeError(f"VM '{vm.name}' has no proxmox_vmid")
        return int(vm.proxmox_vmid)

    def _wait_for_cloud_init(
        self, node: str, vmid: int, *, timeout: int = 300
    ) -> None:
        """Wait for cloud-init to finish inside the VM."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = self._api.guest_agent_exec_wait(
                    node, vmid, "/usr/bin/cloud-init", ["status", "--wait"],
                    timeout=60,
                )
                if result is not None and result.get("exitcode", -1) == 0:
                    return
            except ProxmoxAPIError:
                pass
            time.sleep(5)
        # Don't fail -- cloud-init may not be installed or may have already finished

    def _wait_for_guest_ip(
        self, node: str, vmid: int, *, timeout: int = 120
    ) -> str:
        """Poll the guest agent until it reports a non-loopback IPv4 address."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                interfaces = self._api.guest_agent_network(node, vmid)
                for iface in interfaces:
                    if iface.get("name") == "lo":
                        continue
                    for addr in iface.get("ip-addresses", []):
                        if addr.get("ip-address-type") == "ipv4":
                            ip = addr["ip-address"]
                            if not ip.startswith("127."):
                                return str(ip)
            except ProxmoxAPIError:
                pass  # guest agent not ready yet
            time.sleep(3)
        raise RuntimeError(
            f"Timed out waiting for guest agent IP on VMID {vmid}"
        )

    def _run_bootstrap_via_agent(
        self, node: str, vmid: int, script: str
    ) -> str | None:
        """Write and run the bootstrap script via the guest agent.

        Returns the Tailscale IP if bootstrap succeeds, None otherwise.
        """
        from agentworks.vms.bootstrap_script import parse_bootstrap_output

        # Write script to VM via guest agent file-write
        self._api.guest_agent_file_write(
            node, vmid, "/tmp/agentworks-bootstrap.sh", script
        )

        # Run bootstrap (long-running -- installs packages, joins tailscale)
        # bash is invoked explicitly so the script doesn't need +x
        result = self._api.guest_agent_exec_wait(
            node, vmid, "/bin/bash", ["/tmp/agentworks-bootstrap.sh"],
            timeout=600,
        )

        if result is None:
            output.warn("bootstrap timed out")
            return None

        exit_code = result.get("exitcode", -1)
        stdout = result.get("out-data", "")
        parsed = parse_bootstrap_output(stdout, exit_code)

        if parsed.ok:
            return parsed.tailscale_ip

        stderr = result.get("err-data", "")
        if stderr:
            output.warn(f"Bootstrap stderr: {stderr[:500]}")

        return None
