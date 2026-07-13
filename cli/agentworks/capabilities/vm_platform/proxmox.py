"""The Proxmox VE VM platform -- clone + cloud-init + guest-agent bootstrap
via the Proxmox REST API."""

from __future__ import annotations

import time
import urllib.parse
from typing import TYPE_CHECKING, Any, ClassVar

from agentworks import output
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.capabilities.vm_platform.bootstrap_script import generate_bootstrap_script
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.capabilities.vm_platform.proxmox_api import ProxmoxAPI, ProxmoxAPIError
from agentworks.db import VMStatus
from agentworks.errors import ConfigError, StateError
from agentworks.transports import SSHTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.db import VMRow
    from agentworks.resources.reference import ConfigReference


# The well-known secret name the token_secret field defaults to. The
# site emits the reference (auto-declared at finalize); the default
# env-var backend convention reads AW_SECRET_PROXMOX_TOKEN. Not
# "proxmox-token-secret": the kind is already secret/ and the env-var
# prefix already says SECRET, so the suffix was pure redundancy.
DEFAULT_TOKEN_SECRET = "proxmox-token"

_REQUIRED_KEYS = ("api_url", "node", "token_id", "template_vmid")
_OPTIONAL_KEYS = ("storage", "bridge", "pool", "verify_ssl", "token_secret")


class ProxmoxPlatform(VMPlatform):
    """Runs VMs on a Proxmox VE cluster."""

    name: ClassVar[str] = "proxmox"
    description: ClassVar[str] = "Proxmox VE cluster VMs (clone + cloud-init)"
    no_native_transport_hint: ClassVar[str] = (
        "The QEMU guest agent exec interface is one-shot and "
        "non-interactive, so use the Proxmox web UI's serial console "
        "(VM > Console in the Proxmox VE web UI) as the equivalent "
        "escape hatch."
    )

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        for key in _REQUIRED_KEYS:
            if key not in config:
                raise ConfigError(
                    f"{owner}.{key} is required for the proxmox platform"
                )
        try:
            int(str(config["template_vmid"]))
        except ValueError:
            raise ConfigError(
                f"{owner}.template_vmid must be an integer, "
                f"got {config['template_vmid']!r}"
            ) from None
        token_secret = config.get("token_secret", DEFAULT_TOKEN_SECRET)
        if not isinstance(token_secret, str) or not token_secret:
            raise ConfigError(
                f"{owner}.token_secret must be a bare secret name (string); "
                f"omit the key to use the default '{DEFAULT_TOKEN_SECRET}'"
            )
        unknown = sorted(set(config) - set(_REQUIRED_KEYS) - set(_OPTIONAL_KEYS))
        if unknown:
            raise ConfigError(
                f"{owner}: unknown proxmox platform field(s): {', '.join(unknown)}"
            )
        # Capability-implied reference: the API token is an ordinary
        # secret reference; the owning site attaches itself as source
        # (whoever hosts the config that names the secret emits the
        # reference).
        from agentworks.resources.reference import ConfigReference

        return (
            ConfigReference(
                kind="secret",
                name=str(token_secret),
                usage="the Proxmox API token",
            ),
        )

    @classmethod
    def shared_backend(cls, platform_config: Mapping[str, object]) -> bool:
        return True

    @classmethod
    def legacy_platform_metadata(
        cls, row: Mapping[str, Any], legacy: Mapping[str, Any]
    ) -> dict[str, str]:
        metadata: dict[str, str] = {}
        if row["proxmox_vmid"]:
            metadata["vmid"] = str(row["proxmox_vmid"])
        # Best-effort: the node comes from the legacy [proxmox] section
        # when the migration context could parse one. When absent, ops
        # fall back to the bound site's platform_config node.
        proxmox_section = legacy.get("proxmox")
        if isinstance(proxmox_section, dict) and proxmox_section.get("node"):
            metadata["node"] = str(proxmox_section["node"])
        return metadata

    def _cfg(self, key: str, default: object | None = None) -> Any:
        return self.platform_config.get(key, default)

    @property
    def _api(self) -> ProxmoxAPI:
        api: ProxmoxAPI | None = getattr(self, "_api_cached", None)
        if api is None:
            token_secret = str(self._cfg("token_secret", DEFAULT_TOKEN_SECRET))
            if self.resolver is None:
                # The composition root constructs the platform against
                # the operation's resolver; reaching here means a caller
                # constructed directly (inspection?) and then invoked an
                # op that needs the API token.
                raise StateError(
                    f"the Proxmox platform for site '{self.site_name}' was "
                    f"constructed without a resolver; ops need the "
                    f"'{token_secret}' API token"
                )
            # From the operation's boundary resolve pass; the resolver
            # raises a typed error if the pass hasn't run (an op must
            # never trigger a prompt).
            token_value = self.resolver.get(token_secret)
            api = ProxmoxAPI(
                api_url=str(self._cfg("api_url")),
                token_id=str(self._cfg("token_id")),
                token_secret=token_value,
                verify_ssl=bool(self._cfg("verify_ssl", True)),
            )
            self._api_cached = api
        return api

    def _vm_node(self, vm: VMRow) -> str:
        # Prefer the recorded node (decouples existing VMs from config
        # edits); fall back to the site's node for rows migrated without
        # a parseable legacy [proxmox] section.
        node = vm.platform_metadata.get("node") or self._cfg("node")
        if not node:
            raise StateError(
                f"VM '{vm.name}' has no proxmox node in its platform "
                f"metadata or site configuration",
                entity_kind="vm",
                entity_name=vm.name,
            )
        return str(node)

    def _vmid(self, vm: VMRow) -> int:
        vmid = vm.platform_metadata.get("vmid")
        if not vmid:
            raise StateError(
                f"VM '{vm.name}' has no proxmox vmid in its platform "
                f"metadata; the DB row is incomplete",
                entity_kind="vm",
                entity_name=vm.name,
            )
        return int(vmid)

    def create(self, request: ProvisionRequest) -> ProvisionResult:
        node = str(self._cfg("node"))
        template_vmid = int(str(self._cfg("template_vmid")))
        pool = str(self._cfg("pool", "agentworks"))
        storage = str(self._cfg("storage", "local-lvm"))

        # The platform owns the backend-side name. PVE names are
        # soft (the vmid identifies), but a duplicate name is operator
        # confusion worth surfacing.
        backend_name = (
            f"{request.system_slug}-{request.vm_name}"
            if request.system_slug
            else request.vm_name
        )
        if self._name_exists(node, backend_name):
            raise StateError(
                f"a Proxmox VM named '{backend_name}' already exists on "
                f"node {node}",
                entity_kind="vm",
                entity_name=request.vm_name,
                hint="delete it first or pick a different VM name",
            )

        output.info(f"Provisioning Proxmox VM '{backend_name}' on node {node}...")

        # 1. Get next VMID
        newid = self._api.next_id()
        output.detail(f"Allocated VMID: {newid}")

        # 2. Clone template into the agentworks pool
        output.detail(f"Cloning template {template_vmid}...")
        upid = self._api.clone_vm(
            node, template_vmid, newid, backend_name,
            storage=storage,
            pool=pool,
        )
        self._api.wait_for_task(node, upid)
        output.detail("Clone complete")

        # 3. Configure VM resources
        vm_config: dict[str, object] = {}
        if request.cpus is not None:
            vm_config["cores"] = request.cpus
        if request.memory_gib is not None:
            vm_config["memory"] = request.memory_gib * 1024  # GiB -> MiB

        # Cloud-init: user, SSH key, network
        vm_config["ciuser"] = request.admin_username
        vm_config["sshkeys"] = urllib.parse.quote(request.ssh_public_key, safe="")
        vm_config["ipconfig0"] = "ip=dhcp"

        # Boot order, guest agent, and CPU type (host passthrough exposes
        # AVX/AVX2 which tools like Bun require)
        vm_config["boot"] = "order=scsi0"
        vm_config["agent"] = "enabled=1"
        vm_config["cpu"] = "host"

        output.detail("Configuring VM...")
        self._api.configure_vm(node, newid, **vm_config)

        # 4. Resize disk if requested
        if request.disk_gib is not None:
            output.detail(f"Resizing disk to {request.disk_gib}G...")
            self._api.resize_disk(node, newid, "scsi0", f"{request.disk_gib}G")

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
        if request.tailscale_auth_key:
            output.detail("Running bootstrap via guest agent...")
            bootstrap = generate_bootstrap_script(
                admin_username=request.admin_username,
                ssh_public_key=request.ssh_public_key,
                provisioning_packages=PROVISIONING_PACKAGES,
                tailscale_auth_key=request.tailscale_auth_key,
                hostname=request.hostname,
                swap=request.swap_gib if request.swap_gib is not None else 0,
            )
            tailscale_ip = self._run_bootstrap_via_agent(node, newid, bootstrap)
            bootstrap_complete = tailscale_ip is not None
            if tailscale_ip:
                output.detail(f"Tailscale IP: {tailscale_ip}")

        host = tailscale_ip or ip
        target = SSHTransport(
            host=host,
            user=request.admin_username,
            identity_file=request.ssh_private_key,
        )

        return ProvisionResult(
            native_transport=target,
            platform_metadata={"vmid": str(newid), "node": node},
            bootstrap_complete=bootstrap_complete,
            tailscale_ip=tailscale_ip,
        )

    def _name_exists(self, node: str, backend_name: str) -> bool:
        """Pre-flight: does a VM with this name exist on the node?"""
        try:
            existing = self._api.list_vms(node)
        except ProxmoxAPIError:
            return False
        return any(entry.get("name") == backend_name for entry in existing)

    def start(self, vm: VMRow) -> None:
        # Idempotency guard (the ABC flags start): the Proxmox
        # status/start endpoint errors on an already-running VM.
        if self.status(vm) == VMStatus.RUNNING:
            output.detail(f"Proxmox VM '{vm.name}' is already running")
            return
        node = self._vm_node(vm)
        upid = self._api.start_vm(node, self._vmid(vm))
        self._api.wait_for_task(node, upid)

    def stop(self, vm: VMRow) -> None:
        # Idempotency guard (the ABC flags stop): stopping an
        # already-stopped VM must land in the stopped state, not error.
        if self.status(vm) == VMStatus.STOPPED:
            output.detail(f"Proxmox VM '{vm.name}' is already stopped")
            return
        node = self._vm_node(vm)
        upid = self._api.stop_vm(node, self._vmid(vm))
        self._api.wait_for_task(node, upid)

    def delete(self, vm: VMRow) -> None:
        vmid = self._vmid(vm)
        node = self._vm_node(vm)

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
        try:
            result = self._api.vm_status(self._vm_node(vm), self._vmid(vm))
        except ProxmoxAPIError:
            return VMStatus.UNKNOWN
        pve_status = result.get("status", "")
        if pve_status == "running":
            return VMStatus.RUNNING
        if pve_status == "stopped":
            return VMStatus.STOPPED
        return VMStatus.UNKNOWN

    def display_backend_name(self, vm: VMRow) -> str:
        vmid = vm.platform_metadata.get("vmid", "?")
        node = vm.platform_metadata.get("node") or self._cfg("node", "?")
        return f"{vmid}@{node}"

    # native_transport: inherited None default. One-shot QEMU guest-agent
    # exec can't host an interactive shell; the transports factory raises
    # the typed StateError with the web-console hint.

    # -- Helpers ---------------------------------------------------------------

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
        from agentworks.capabilities.vm_platform.bootstrap_script import parse_bootstrap_output

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
