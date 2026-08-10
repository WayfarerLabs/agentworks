"""Google Compute Engine contract-v2 VM platform."""

from __future__ import annotations

import contextlib
import sys
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from agentworks import output
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.capabilities.vm_platform.bootstrap_script import generate_bootstrap_script
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.capabilities.vm_platform.ssh_exposure import config_allow_cidrs, operator_ssh_prefixes
from agentworks.capabilities.vm_platform.tailscale_join import EphemeralTailscaleBootstrap
from agentworks.db import VMStatus
from agentworks.errors import AgentworksError, ConnectivityError, StateError
from agentworks.plugins.gcp.auth import GcpClientCache
from agentworks.plugins.gcp.bootstrap import (
    GCE_READINESS_COMMAND,
    GCE_READINESS_LABEL,
    build_startup_script,
)
from agentworks.plugins.gcp.cleanup import (
    CleanupCoordinates,
    InstanceOwnership,
    InstanceState,
    RollbackReport,
    manual_cleanup_guidance,
    rollback_after_interrupt,
    rollback_partial_create,
    rollback_then_raise,
)
from agentworks.plugins.gcp.compute import (
    EXTERNAL_ACCESS_CONFIG_NAME,
    get_project,
    get_zone,
    live_external_ipv4,
    provider_resource_id,
    require_instance_name_available,
    resolve_balanced_disk_type,
    resolve_debian_image,
    verify_live_machine_type,
    verify_zonal_operation,
)
from agentworks.plugins.gcp.config import GcpGCEConfig, machine_catalog, select_machine_type
from agentworks.plugins.gcp.errors import GCEError, GCEOperationError, call_google, call_google_optional
from agentworks.plugins.gcp.instance import (
    InstanceInsertAttempt,
    build_instance_resource,
    insert_instance_reconciled,
    read_owned_instance,
    verify_instance_network,
    wait_for_instance_status,
)
from agentworks.plugins.gcp.names import derive_names, transient_route_name
from agentworks.plugins.gcp.network import (
    FirewallInsertAttempt,
    FirewallOwnership,
    FirewallState,
    build_deny_firewall,
    build_ssh_allow_firewall,
    delete_matching_firewall,
    get_network,
    insert_firewall_reconciled,
    list_firewalls,
    reject_priority_zero_conflicts,
    require_classic_first,
    require_firewall_name_available,
    resolve_network,
)
from agentworks.topics import TopicProse
from agentworks.transports import SSHTransport

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.transports import Transport


_OPERATION_TIMEOUT_SECONDS = 300.0
_RUNNING_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class _VMIdentity:
    project_id: str
    zone: str
    instance_name: str
    instance_id: str
    network_url: str
    subnet_url: str | None
    network_tag: str
    allow_rule: str
    allow_rule_id: str
    deny_rule: str
    deny_rule_id: str
    access_config_name: str

    @classmethod
    def from_row(cls, vm: VMRow) -> _VMIdentity:
        metadata = vm.platform_metadata
        required = (
            "project_id",
            "zone",
            "instance_name",
            "instance_id",
            "network_url",
            "network_tag",
            "allow_rule",
            "allow_rule_id",
            "deny_rule",
            "deny_rule_id",
            "access_config_name",
        )
        missing = [key for key in required if not metadata.get(key)]
        if missing:
            raise StateError(
                f"VM '{vm.name}' has incomplete GCE platform metadata (missing {', '.join(missing)})",
                entity_kind="vm",
                entity_name=vm.name,
                hint="restore the persisted provider identities before retrying lifecycle operations",
            )
        return cls(
            project_id=str(metadata["project_id"]),
            zone=str(metadata["zone"]),
            instance_name=str(metadata["instance_name"]),
            instance_id=str(metadata["instance_id"]),
            network_url=str(metadata["network_url"]),
            subnet_url=str(metadata["subnet_url"]) if metadata.get("subnet_url") else None,
            network_tag=str(metadata["network_tag"]),
            allow_rule=str(metadata["allow_rule"]),
            allow_rule_id=str(metadata["allow_rule_id"]),
            deny_rule=str(metadata["deny_rule"]),
            deny_rule_id=str(metadata["deny_rule_id"]),
            access_config_name=str(metadata["access_config_name"]),
        )

    @property
    def coordinates(self) -> CleanupCoordinates:
        return CleanupCoordinates(
            project_id=self.project_id,
            zone=self.zone,
            instance_name=self.instance_name,
            allow_rule=self.allow_rule,
            deny_rule=self.deny_rule,
        )


class GCEPlatform(VMPlatform):
    """Runs VMs on Google Compute Engine with provider-ID-owned cleanup."""

    contract_version: ClassVar[int] = 2
    name: ClassVar[str] = "gcp-gce"
    description: ClassVar[str] = "Google Compute Engine (project + zone)"
    config_model: ClassVar[type[GcpGCEConfig]] = GcpGCEConfig
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Google Compute Engine VMs",
        overview="""
        Creates a Debian 12 Compute Engine instance in one project and zone.
        The site uses Application Default Credentials unless its tagged `auth`
        selects one complete service-account JSON secret. A scoped priority-0
        SSH allow exists only while bootstrap or a native route needs it; an
        owned priority-1 all-ingress deny remains for the VM lifetime.

        The startup metadata is credential-free. Agentworks waits for its
        durable success marker, then sends the Tailscale auth key once through
        fixed-command stdin. The external IPv4 remains attached for outbound
        access but is read live and never persisted.

        Ships as the opt-in `gcp` system plugin. Enable it explicitly before a
        `gcp-gce` vm-site becomes ready.
        """,
    )
    probe_failure_hint: ClassVar[str | None] = (
        "The transient GCE SSH allow is scoped to the detected public IP. "
        "Set operator.ssh_allow_cidrs when SSH exits through another VPN, proxy, or NAT address."
    )

    def __init__(self, owner_name: str, config: Mapping[str, object]) -> None:
        super().__init__(owner_name, config)
        self._clients = GcpClientCache(owner_name, self.config)

    @property
    def config(self) -> GcpGCEConfig:
        return self._config_as(GcpGCEConfig)

    def runup(self, ctx: RunContext) -> None:
        """Authenticate and verify the address and inspectable firewall boundary."""
        output.info(f"Performing runup test for vm-site/{self.site_name}...")
        get_project(self._clients, ctx, self.config.project_id)
        get_zone(self._clients, ctx, self.config.project_id, self.config.zone)
        network = resolve_network(self._clients, ctx, self.config)
        live_network = get_network(
            self._clients,
            ctx,
            project_id=self.config.project_id,
            network_url=network.network_url,
        )
        require_classic_first(live_network, project_id=self.config.project_id)
        prefixes = operator_ssh_prefixes(config_allow_cidrs(ctx.config))
        reject_priority_zero_conflicts(
            list_firewalls(self._clients, ctx, self.config.project_id),
            network_url=network.network_url,
            operator_prefixes=prefixes,
            target_tag=None,
        )

    def create(self, request: ProvisionRequest, ctx: RunContext) -> ProvisionResult:
        names = derive_names(request.hostname)
        selected = select_machine_type(machine_catalog(self.config), cpus=request.cpus, memory_gib=request.memory_gib)
        prefixes = operator_ssh_prefixes(config_allow_cidrs(ctx.config))
        request.progress.step("Validate Google Compute target")

        # P0: every definitive lookup, collision, and final request build occurs
        # before the first insert.
        get_project(self._clients, ctx, self.config.project_id)
        get_zone(self._clients, ctx, self.config.project_id, self.config.zone)
        network = resolve_network(self._clients, ctx, self.config)
        live_network = get_network(
            self._clients,
            ctx,
            project_id=self.config.project_id,
            network_url=network.network_url,
        )
        require_classic_first(live_network, project_id=self.config.project_id)
        firewalls = self._clients.client("firewalls", ctx)
        all_firewalls = list_firewalls(self._clients, ctx, self.config.project_id)
        reject_priority_zero_conflicts(
            all_firewalls,
            network_url=network.network_url,
            operator_prefixes=prefixes,
            target_tag=names.network_tag,
        )
        instances = self._clients.client("instances", ctx)
        require_instance_name_available(
            self._clients,
            ctx,
            project_id=self.config.project_id,
            zone=self.config.zone,
            instance_name=names.backend_name,
        )
        require_firewall_name_available(firewalls, project_id=self.config.project_id, rule_name=names.deny_rule)
        require_firewall_name_available(firewalls, project_id=self.config.project_id, rule_name=names.allow_rule)

        machine = verify_live_machine_type(self._clients, ctx, self.config, selected)
        image = resolve_debian_image(self._clients, ctx, selected.arch)
        disk_type = resolve_balanced_disk_type(self._clients, ctx, self.config)
        machine_url = str(machine.self_link)
        if not machine_url:
            raise GCEError(f"GCE machine type '{selected.type}' returned no self link")

        bootstrap = generate_bootstrap_script(
            admin_username=request.admin_username,
            ssh_public_key="",
            provisioning_packages=PROVISIONING_PACKAGES,
            tailscale_auth_key=None,
            hostname=request.hostname,
            swap=request.swap_gib,
        )
        startup_script = build_startup_script(bootstrap, instance_name=names.backend_name)
        deny = build_deny_firewall(
            rule_name=names.deny_rule,
            network_url=network.network_url,
            network_tag=names.network_tag,
        )
        allow = build_ssh_allow_firewall(
            rule_name=names.allow_rule,
            network_url=network.network_url,
            network_tag=names.network_tag,
            operator_prefixes=prefixes,
        )
        instance = build_instance_resource(
            instance_name=names.backend_name,
            machine_type_url=machine_url,
            image_url=str(image.self_link),
            disk_type_url=str(disk_type.self_link),
            disk_gib=request.disk_gib,
            network=network,
            network_tag=names.network_tag,
            admin_username=request.admin_username,
            ssh_public_key=request.ssh_public_key,
            startup_script=startup_script,
        )

        coordinates = CleanupCoordinates(
            project_id=self.config.project_id,
            zone=self.config.zone,
            instance_name=names.backend_name,
            allow_rule=names.allow_rule,
            deny_rule=names.deny_rule,
        )
        deny_attempt = FirewallInsertAttempt.create(names.deny_rule)
        allow_attempt = FirewallInsertAttempt.create(names.allow_rule)
        instance_attempt = InstanceInsertAttempt.create(names.backend_name)
        instance_possible = False
        transport: Transport | None = None
        tailscale_ip: str | None = None

        def rollback() -> RollbackReport:
            return rollback_partial_create(
                instances=instances,
                firewalls=firewalls,
                coordinates=coordinates,
                expected_allow=allow,
                expected_deny=deny,
                allow_ownership=allow_attempt.ownership,
                deny_ownership=deny_attempt.ownership,
                instance_ownership=instance_attempt.ownership,
                instance_possible=instance_possible,
                timeout=_OPERATION_TIMEOUT_SECONDS,
            )

        try:
            request.progress.step("Create GCE ingress deny")
            deny_ownership = insert_firewall_reconciled(
                firewalls,
                project_id=self.config.project_id,
                firewall=deny,
                attempt=deny_attempt,
                timeout=_OPERATION_TIMEOUT_SECONDS,
            )
            request.progress.output(f"Created deny rule '{names.deny_rule}'")

            request.progress.step("Open scoped bootstrap SSH")
            allow_ownership = insert_firewall_reconciled(
                firewalls,
                project_id=self.config.project_id,
                firewall=allow,
                attempt=allow_attempt,
                timeout=_OPERATION_TIMEOUT_SECONDS,
            )
            request.progress.output(f"Created scoped allow rule '{names.allow_rule}'")

            request.progress.step("Insert GCE instance")
            instance_possible = True
            _realized, instance_ownership = insert_instance_reconciled(
                instances,
                project_id=self.config.project_id,
                zone=self.config.zone,
                instance=instance,
                attempt=instance_attempt,
                timeout=_OPERATION_TIMEOUT_SECONDS,
            )
            running = wait_for_instance_status(
                instances,
                project_id=self.config.project_id,
                zone=self.config.zone,
                instance_name=names.backend_name,
                resource_id=instance_ownership.resource_id,
                accepted=("RUNNING",),
                timeout=_RUNNING_TIMEOUT_SECONDS,
            )
            verify_instance_network(running, network_url=network.network_url, subnet_url=network.subnet_url)
            external_ip = live_external_ipv4(running)
            request.progress.output(f"GCE instance '{names.backend_name}' is running")
            transport = SSHTransport(
                host=external_ip,
                user=request.admin_username,
                identity_file=request.ssh_private_key,
                force_tty=sys.platform == "win32",
            )

            request.progress.step("Wait for GCE startup marker")
            request.progress.step("Join Tailscale through fixed stdin")
            tailscale_ip = EphemeralTailscaleBootstrap(
                transport,
                readiness_command=GCE_READINESS_COMMAND,
                readiness_label=GCE_READINESS_LABEL,
            ).complete(request.tailscale_auth_key)
            request.progress.output("GCE credential-free bootstrap and Tailscale join completed")
        except KeyboardInterrupt as primary:
            rollback_after_interrupt(
                primary,
                rollback,
                coordinates,
                instance_ownership=instance_attempt.ownership,
                allow_ownership=allow_attempt.ownership,
                deny_ownership=deny_attempt.ownership,
            )
        except Exception as primary:
            request.progress.log_error("GCE create failed; attempting provider-ID-owned rollback")
            rollback_then_raise(
                primary,
                rollback,
                coordinates,
                instance_ownership=instance_attempt.ownership,
                allow_ownership=allow_attempt.ownership,
                deny_ownership=deny_attempt.ownership,
            )

        if transport is None:  # pragma: no cover - both failure arms re-raise
            raise AssertionError("successful GCE create has no native transport")
        metadata = {
            "project_id": self.config.project_id,
            "zone": self.config.zone,
            "instance_name": names.backend_name,
            "instance_id": instance_ownership.resource_id,
            "network_url": network.network_url,
            "network_tag": names.network_tag,
            "allow_rule": names.allow_rule,
            "allow_rule_id": allow_ownership.resource_id,
            "deny_rule": names.deny_rule,
            "deny_rule_id": deny_ownership.resource_id,
            "access_config_name": EXTERNAL_ACCESS_CONFIG_NAME,
        }
        if network.subnet_url is not None:
            metadata["subnet_url"] = network.subnet_url
        return ProvisionResult(native_transport=transport, platform_metadata=metadata, tailscale_ip=tailscale_ip)

    def start(self, vm: VMRow, ctx: RunContext) -> None:
        identity = _VMIdentity.from_row(vm)
        instances = self._clients.client("instances", ctx)
        current = self._owned_instance(instances, identity)
        if current is None:
            raise StateError(f"GCE instance '{identity.instance_name}' no longer exists")
        if str(current.status).upper() == "RUNNING":
            return
        self._power(instances, identity, action="start")
        wait_for_instance_status(
            instances,
            project_id=identity.project_id,
            zone=identity.zone,
            instance_name=identity.instance_name,
            resource_id=identity.instance_id,
            accepted=("RUNNING",),
            timeout=_RUNNING_TIMEOUT_SECONDS,
        )

    def stop(self, vm: VMRow, ctx: RunContext) -> None:
        identity = _VMIdentity.from_row(vm)
        instances = self._clients.client("instances", ctx)
        current = self._owned_instance(instances, identity)
        if current is None:
            raise StateError(f"GCE instance '{identity.instance_name}' no longer exists")
        if str(current.status).upper() in {"TERMINATED", "SUSPENDED"}:
            return
        self._power(instances, identity, action="stop")
        wait_for_instance_status(
            instances,
            project_id=identity.project_id,
            zone=identity.zone,
            instance_name=identity.instance_name,
            resource_id=identity.instance_id,
            accepted=("TERMINATED", "SUSPENDED"),
            timeout=_RUNNING_TIMEOUT_SECONDS,
        )

    def delete(self, vm: VMRow, ctx: RunContext) -> None:
        identity = _VMIdentity.from_row(vm)
        instances = self._clients.client("instances", ctx)
        firewalls = self._clients.client("firewalls", ctx)
        expected_allow = self._owned_firewall_or_absence_shape(firewalls, identity, role="allow")
        expected_deny = build_deny_firewall(
            rule_name=identity.deny_rule,
            network_url=identity.network_url,
            network_tag=identity.network_tag,
        )
        report = rollback_partial_create(
            instances=instances,
            firewalls=firewalls,
            coordinates=identity.coordinates,
            expected_allow=expected_allow,
            expected_deny=expected_deny,
            allow_ownership=FirewallOwnership(identity.allow_rule, identity.allow_rule_id),
            deny_ownership=FirewallOwnership(identity.deny_rule, identity.deny_rule_id),
            instance_ownership=InstanceOwnership(identity.instance_id),
            instance_possible=True,
            timeout=_OPERATION_TIMEOUT_SECONDS,
        )
        guidance = manual_cleanup_guidance(
            identity.coordinates,
            report,
            instance_ownership=InstanceOwnership(identity.instance_id),
            allow_ownership=FirewallOwnership(identity.allow_rule, identity.allow_rule_id),
            deny_ownership=FirewallOwnership(identity.deny_rule, identity.deny_rule_id),
        )
        if report.instance is not InstanceState.ABSENT:
            raise GCEOperationError(
                f"GCE instance '{identity.instance_name}' was not proven absent; its database row is retained",
                entity_kind="gcp-instance",
                entity_name=identity.instance_name,
                hint=guidance,
            )
        if not report.clean:
            output.warn(guidance)

    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        identity = _VMIdentity.from_row(vm)
        instances = self._clients.client("instances", ctx)
        try:
            current = self._owned_instance(instances, identity)
        except (ConnectivityError, GCEError):
            return VMStatus.UNKNOWN
        if current is None:
            return VMStatus.UNKNOWN
        state = str(current.status).upper()
        if state == "RUNNING":
            return VMStatus.RUNNING
        if state in {"STOPPING", "TERMINATED", "SUSPENDING", "SUSPENDED"}:
            return VMStatus.STOPPED
        return VMStatus.UNKNOWN

    def display_backend_name(self, vm: VMRow) -> str:
        identity = _VMIdentity.from_row(vm)
        return f"{identity.instance_name}@{identity.zone}"

    def native_transport(
        self,
        vm: VMRow,
        ctx: RunContext,
        *,
        config: Config | None = None,
    ) -> Transport | None:
        identity = _VMIdentity.from_row(vm)
        instances = self._clients.client("instances", ctx)
        current = self._owned_instance(instances, identity)
        if current is None:
            return None
        verify_instance_network(current, network_url=identity.network_url, subnet_url=identity.subnet_url)
        identity_file = None
        if config is not None:
            identity_file = getattr(getattr(config, "operator", None), "ssh_private_key", None)
        return SSHTransport(
            host=live_external_ipv4(current, access_config_name=identity.access_config_name),
            user=vm.admin_username,
            identity_file=identity_file,
            force_tty=sys.platform == "win32",
        )

    def post_tailscale_ready(self, vm: VMRow, ctx: RunContext) -> None:
        self._close_stable_allow(vm, ctx)

    def secure_failed_vm(self, vm: VMRow, ctx: RunContext) -> None:
        self._close_stable_allow(vm, ctx)

    @contextlib.contextmanager
    def transient_route(self, vm: VMRow, ctx: RunContext, *, config: Config | None = None) -> Iterator[None]:
        identity = _VMIdentity.from_row(vm)
        instances = self._clients.client("instances", ctx)
        current = self._owned_instance(instances, identity)
        if current is None:
            raise StateError(f"GCE instance '{identity.instance_name}' no longer exists")
        verify_instance_network(current, network_url=identity.network_url, subnet_url=identity.subnet_url)

        live_network = get_network(
            self._clients,
            ctx,
            project_id=identity.project_id,
            network_url=identity.network_url,
        )
        require_classic_first(live_network, project_id=identity.project_id)
        prefixes = operator_ssh_prefixes(config_allow_cidrs(config))
        reject_priority_zero_conflicts(
            list_firewalls(self._clients, ctx, identity.project_id),
            network_url=identity.network_url,
            operator_prefixes=prefixes,
            target_tag=identity.network_tag,
        )
        firewalls = self._clients.client("firewalls", ctx)
        route_name = transient_route_name(vm.hostname)
        require_firewall_name_available(firewalls, project_id=identity.project_id, rule_name=route_name)
        route = build_ssh_allow_firewall(
            rule_name=route_name,
            network_url=identity.network_url,
            network_tag=identity.network_tag,
            operator_prefixes=prefixes,
        )
        attempt = FirewallInsertAttempt.create(route_name)
        try:
            insert_firewall_reconciled(
                firewalls,
                project_id=identity.project_id,
                firewall=route,
                attempt=attempt,
                timeout=_OPERATION_TIMEOUT_SECONDS,
            )
            yield
        finally:
            result = delete_matching_firewall(
                firewalls,
                project_id=identity.project_id,
                expected=route,
                ownership=attempt.ownership,
                timeout=_OPERATION_TIMEOUT_SECONDS,
            )
            if result.state is not FirewallState.ABSENT:
                output.warn(
                    f"GCE transient SSH rule '{route_name}' was not proven absent in project "
                    f"'{identity.project_id}'; inspect provider ID "
                    f"'{result.observed_resource_id or 'unknown'}' and do not delete a mismatched rule by name"
                )

    def _owned_instance(self, instances: Any, identity: _VMIdentity) -> Any | None:
        return read_owned_instance(
            instances,
            project_id=identity.project_id,
            zone=identity.zone,
            instance_name=identity.instance_name,
            resource_id=identity.instance_id,
        )

    def _power(self, instances: Any, identity: _VMIdentity, *, action: str) -> None:
        from google.cloud import compute_v1

        request_id = str(uuid.uuid4())
        if action == "start":
            request: Any = compute_v1.StartInstanceRequest(
                project=identity.project_id,
                zone=identity.zone,
                instance=identity.instance_name,
                request_id=request_id,
            )
            operation = call_google(
                lambda: instances.start(request=request, retry=None),
                operation="requesting instance start",
                resource=f"instance {identity.project_id}/{identity.zone}/{identity.instance_name}",
            )
        elif action == "stop":
            request = compute_v1.StopInstanceRequest(
                project=identity.project_id,
                zone=identity.zone,
                instance=identity.instance_name,
                request_id=request_id,
            )
            operation = call_google(
                lambda: instances.stop(request=request, retry=None),
                operation="requesting instance stop",
                resource=f"instance {identity.project_id}/{identity.zone}/{identity.instance_name}",
            )
        else:  # pragma: no cover - internal closed vocabulary
            raise ValueError(action)
        verify_zonal_operation(
            operation,
            request_id=request_id,
            operation_type=action,
            project_id=identity.project_id,
            zone=identity.zone,
            instance_name=identity.instance_name,
            expected_resource_id=identity.instance_id,
        )
        from agentworks.plugins.gcp.errors import wait_for_extended_operation

        wait_for_extended_operation(operation, label=f"instance {identity.instance_name} {action}", timeout=300.0)

    def _owned_firewall_or_absence_shape(self, firewalls: Any, identity: _VMIdentity, *, role: str) -> Any:
        name = identity.allow_rule if role == "allow" else identity.deny_rule
        expected_id = identity.allow_rule_id if role == "allow" else identity.deny_rule_id
        actual = call_google_optional(
            lambda: firewalls.get(project=identity.project_id, firewall=name),
            operation="reading an owned firewall rule",
            resource=f"firewall rule {identity.project_id}/{name}",
        )
        if actual is not None and provider_resource_id(actual.id) == expected_id:
            return actual
        if role == "deny":
            return build_deny_firewall(
                rule_name=name,
                network_url=identity.network_url,
                network_tag=identity.network_tag,
            )
        # Only used to classify absence or a provider-ID collision. A matching
        # live incarnation returned above supplies its complete retained shape.
        return build_ssh_allow_firewall(
            rule_name=name,
            network_url=identity.network_url,
            network_tag=identity.network_tag,
            operator_prefixes=("127.255.255.255/32",),
        )

    def _close_stable_allow(self, vm: VMRow, ctx: RunContext) -> None:
        try:
            identity = _VMIdentity.from_row(vm)
            firewalls = self._clients.client("firewalls", ctx)
            expected = self._owned_firewall_or_absence_shape(firewalls, identity, role="allow")
            result = delete_matching_firewall(
                firewalls,
                project_id=identity.project_id,
                expected=expected,
                ownership=FirewallOwnership(identity.allow_rule, identity.allow_rule_id),
                timeout=_OPERATION_TIMEOUT_SECONDS,
            )
            if result.state is not FirewallState.ABSENT:
                output.warn(
                    f"GCE provisioning allow '{identity.allow_rule}' was not proven absent in project "
                    f"'{identity.project_id}'; inspect expected provider ID '{identity.allow_rule_id}' and "
                    "do not delete a mismatched rule by name"
                )
        except AgentworksError:
            output.warn("Could not verify closure of the provider-ID-owned GCE provisioning allow; inspect it")
