"""Independent AWS creation under the caller's original exact VM owner."""

from __future__ import annotations

import re
import time
from dataclasses import replace
from threading import TIMEOUT_MAX, Lock
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.capabilities.vm_platform.base import provider_locator_remaining
from agentworks.capabilities.vm_platform.debian_release import code_owned_release_value
from agentworks.capabilities.vm_platform.ssh_exposure import config_allow_cidrs
from agentworks.db import OperationResourceKind, VMStatus
from agentworks.errors import StateError, ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.binding import NativeExecutionBinding, _EarlyGuestFactsRoute, _IndependentJobAvailability
from agentworks.execution.carriers.ssh import SSHCarrier
from agentworks.plugins.aws._creation import EC2Creation, identifier
from agentworks.plugins.aws._creation_bootstrap import early_user_data
from agentworks.plugins.aws._creation_rollback import observe_attachments, rollback
from agentworks.plugins.aws._native_route import EC2NativeRoute
from agentworks.plugins.aws._owned_auth import _OwnedRoleSession
from agentworks.plugins.aws.auth import _build_ambient_session
from agentworks.plugins.aws.config import AwsAmbientAuth, _instance_catalog, _select_instance_type
from agentworks.vms._creation_access import VMCreationResult
from agentworks.vms._creation_ssh import CLOUD_INIT_CHECK, SSHReadiness, observe_early_guest
from agentworks.vms._creation_trust import CreationSSHTrust
from agentworks.vms._ssh_native_binding import vm_ssh_connection
from agentworks.vms._ssh_native_exposure import OwnedSSHExposure
from agentworks.vms.identity import validate_vm_instance_marker

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.base import ProviderLocator, ProvisionRequest
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationOwner
    from agentworks.plugins.aws.platform import EC2Platform


class EC2OwnedCreationAccess:
    """Passive construction; one launch and a separately selected rollback window.

    The outer owner serializes this access with guest work. Fresh settlement
    budgets observe the original resource and requests, never provision again.
    Public SDK close is not a physical socket-drain certificate.
    """

    def __init__(
        self,
        request: ProvisionRequest,
        platform: EC2Platform,
        ctx: RunContext,
        owner: OperationOwner,
        custody: LocalDeliveryCustody,
    ) -> None:
        from agentworks.plugins.aws.platform import _DEBIAN_SSM_RELEASES

        if (
            owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != request.vm_name
        ):
            raise ValidationError("EC2 creation requires exact VM ownership")
        self._name, self._admin = request.vm_name, request.admin_username
        self._marker = validate_vm_instance_marker(request.instance_marker)
        self._backend = f"{request.system_slug}-{self._name}" if request.system_slug else self._name
        self._platform, self._ctx, self._owner, self._custody = platform, ctx, owner, custody
        self._release = code_owned_release_value(
            _DEBIAN_SSM_RELEASES, request.debian_release, platform_name=platform.name
        )
        self._selected = _select_instance_type(
            _instance_catalog(platform.config), cpus=request.cpus, memory_gib=request.memory_gib
        )
        if type(request.disk_gib) is not int or request.disk_gib <= 0:
            raise ValidationError("EC2 creation requires a positive integer disk size")
        self._disk = request.disk_gib
        self._user_data = early_user_data(
            admin=self._admin, public_key=request.ssh_public_key, hostname=request.hostname, marker=self._marker
        )
        if ctx.config is None or ctx.config.operator.ssh is None:
            raise ValidationError("EC2 creation requires explicit operator SSH settings")
        self._settings = ctx.config.operator.ssh
        # Validate endpoint-independent policy without admitting local files.
        vm_ssh_connection(self._name, self._admin, "0.0.0.0", self._settings, instance_marker=self._marker)
        self._nonce = uuid4().hex
        self._lock = Lock()
        self._used = False
        self._closed = False
        self._rollback_requested = False
        self._role_auth: _OwnedRoleSession | None = None
        self.creation: EC2Creation | None = None
        self._route: EC2NativeRoute | None = None
        self._exposure = OwnedSSHExposure(self._name)
        self._readiness = SSHReadiness()
        self._trust: CreationSSHTrust | None = None
        self._locator: ProviderLocator | None = None
        self._endpoint: str | None = None
        self.binding: NativeExecutionBinding | None = None
        self._result: VMCreationResult | None = None

    @property
    def platform_metadata(self) -> Mapping[str, str]:
        creation = self.creation
        if creation is None or not creation.history or creation.intent.group_id is None:
            return MappingProxyType({})
        intent = creation.intent
        values = {
            "region": intent.region,
            "account_id": intent.account,
            "backend_name": intent.backend,
            "security_group_id": str(intent.group_id),
        }
        if intent.instance_id is not None:
            values["instance_id"] = intent.instance_id
        return MappingProxyType(values)

    @property
    def independent_availability(self) -> _IndependentJobAvailability:
        return _IndependentJobAvailability.NO_IDLE_STOP

    @property
    def guest_check(self) -> Callable[[VMGuestIdentity], None] | None:
        return None

    @property
    def route_check(self) -> Callable[[Deadline], None] | None:
        return self._check_route if self.binding is not None else None

    def _remaining(self, deadline: Deadline) -> float:
        return provider_locator_remaining(deadline, vm_name=self._name)

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("EC2 creation transition deadline expired")

    def _ordinary_admission(self) -> None:
        with self._owner._guard:  # noqa: SLF001
            self._owner._reconcile_transition_locked()  # noqa: SLF001
            self._owner._require_dispatch_admission_locked()  # noqa: SLF001
            self._owner._require_no_active_work_locked()  # noqa: SLF001
            if self._owner._obligations_sealed or self._owner._recovery_owner:  # noqa: SLF001
                raise StateError("EC2 creation requires an unsealed ordinary operation owner")

    def _connect(self, deadline: Deadline) -> EC2Creation:
        auth, region = self._platform.config.auth, self._platform.config.region
        self._remaining(deadline)
        if isinstance(auth, AwsAmbientAuth):
            session = _build_ambient_session(region)
        else:
            self._role_auth = _OwnedRoleSession(self._name)
            self._role_auth.begin(deadline)
            try:
                session = self._role_auth.build_session(
                    auth, self._ctx.secret(auth.access_key_secret), self._platform.site_name, region
                )
            finally:
                try:
                    self._role_auth.close_client()
                finally:
                    self._role_auth.end()
        self.creation = EC2Creation(self._owner, self._name, session, region, role_auth=self._role_auth)
        self._remaining(deadline)
        return self.creation

    def _prepare_launch(self, creation: EC2Creation, deadline: Deadline) -> tuple[str, str, str, str, str]:
        identity = creation.call("sts", "get_caller_identity", deadline)
        account = identity.get("Account")
        if type(account) is not str or re.fullmatch(r"[0-9]{12}", account) is None:
            raise ValidationError("EC2 creation requires one actual AWS account")
        selected = self._selected
        result = creation.call("ec2", "describe_instance_types", deadline, InstanceTypes=[selected.type])
        entries = result.get("InstanceTypes")
        if (
            type(entries) is not list
            or len(entries) != 1
            or type(entries[0]) is not dict
            or entries[0].get("InstanceType") != selected.type
            or type(entries[0].get("ProcessorInfo")) is not dict
            or selected.arch not in entries[0]["ProcessorInfo"].get("SupportedArchitectures", [])
        ):
            raise ValidationError("EC2 instance architecture does not match the requested catalog entry")
        segment = {"x86_64": "amd64", "arm64": "arm64"}[selected.arch]
        result = creation.call(
            "ssm", "get_parameter", deadline, Name=f"/aws/service/debian/release/{self._release}/latest/{segment}"
        )
        parameter = result.get("Parameter")
        if type(parameter) is not dict:
            raise ValidationError("EC2 Debian image observation is incomplete")
        image = identifier(parameter.get("Value"), "ami")
        result = creation.call("ec2", "describe_images", deadline, ImageIds=[image])
        images = result.get("Images")
        if type(images) is not list or len(images) != 1 or type(images[0]) is not dict:
            raise ValidationError("EC2 Debian root image observation is incomplete")
        observed = images[0]
        root = observed.get("RootDeviceName")
        mappings = observed.get("BlockDeviceMappings")
        if (
            observed.get("ImageId") != image
            or observed.get("Architecture") != selected.arch
            or observed.get("RootDeviceType") != "ebs"
            or type(root) is not str
            or not root.startswith("/dev/")
            or type(mappings) is not list
            or len(mappings) != 1
            or type(mappings[0]) is not dict
            or mappings[0].get("DeviceName") != root
            or type(mappings[0].get("Ebs")) is not dict
        ):
            raise StateError("EC2 creation requires one observed Debian EBS root device")
        floor = mappings[0]["Ebs"].get("VolumeSize")
        if type(floor) is not int or floor <= 0:
            raise ValidationError("EC2 Debian root image has no valid volume size")
        if self._disk < floor:
            raise ValidationError("EC2 requested root disk is smaller than the Debian image volume")
        configured = self._platform.config.subnet_id
        query = (
            {"SubnetIds": [configured]} if configured else {"Filters": [{"Name": "default-for-az", "Values": ["true"]}]}
        )
        result = creation.call("ec2", "describe_subnets", deadline, **query)
        subnets = result.get("Subnets")
        if (
            type(subnets) is not list
            or not subnets
            or result.get("NextToken")
            or any(type(subnet) is not dict for subnet in subnets)
        ):
            raise StateError("EC2 launch subnet observation is incomplete")
        if configured and (len(subnets) != 1 or subnets[0].get("SubnetId") != configured):
            raise StateError("EC2 launch subnet does not match the configured selection")
        chosen = min(
            subnets, key=lambda subnet: (str(subnet.get("AvailabilityZone", "")), str(subnet.get("SubnetId", "")))
        )
        subnet, vpc = identifier(chosen.get("SubnetId"), "subnet"), identifier(chosen.get("VpcId"), "vpc")
        if chosen.get("OwnerId") != account:
            raise StateError("EC2 launch subnet belongs to another account")
        result = creation.call(
            "ec2",
            "describe_instances",
            deadline,
            Filters=[
                {"Name": "tag:agentworks:vm", "Values": [self._backend]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
            ],
        )
        reservations = result.get("Reservations")
        if type(reservations) is not list or result.get("NextToken") or reservations:
            raise StateError("EC2 backend name is in use or its absence is unproved")
        return account, subnet, vpc, image, root

    def provision(self, deadline: Deadline) -> VMCreationResult:
        self._acquire(deadline)
        try:
            if self._used or self._closed or self._rollback_requested:
                raise StateError("EC2 creation cannot be admitted again")
            self._used = True
            self._ordinary_admission()
            prefixes = self._exposure.resolve(config_allow_cidrs(self._ctx.config), deadline)
            creation = self._connect(deadline)
            account, subnet, vpc, image, root = self._prepare_launch(creation, deadline)
            self._ordinary_admission()
            creation.launch(
                deadline,
                account=account,
                backend=self._backend,
                nonce=self._nonce,
                subnet=subnet,
                vpc=vpc,
                image=image,
                root_device=root,
                arguments={
                    "ImageId": image,
                    "InstanceType": self._selected.type,
                    "MinCount": 1,
                    "MaxCount": 1,
                    "ClientToken": self._nonce,
                    "UserData": self._user_data,
                    "BlockDeviceMappings": [
                        {
                            "DeviceName": root,
                            "Ebs": {
                                "VolumeSize": self._disk,
                                "VolumeType": "gp3",
                                "DeleteOnTermination": True,
                            },
                        }
                    ],
                    "TagSpecifications": [
                        {
                            "ResourceType": resource,
                            "Tags": [
                                {"Key": "agentworks:vm", "Value": self._backend},
                                {"Key": "agentworks:creation", "Value": self._nonce},
                            ],
                        }
                        for resource in ("instance", "volume")
                    ],
                },
            )
            while True:
                locator, instance = creation.instance(deadline)
                state = instance.get("State")
                if type(state) is not dict or state.get("Name") not in {"pending", "running"}:
                    raise StateError("EC2 created instance has an unexpected power state")
                if state.get("Name") == "running" and instance.get("PublicIpAddress"):
                    break
                time.sleep(min(0.1, self._remaining(deadline)))
            observe_attachments(creation, instance, deadline, record_initial=True)
            endpoint = instance["PublicIpAddress"]
            connection = vm_ssh_connection(
                self._name, self._admin, endpoint, self._settings, instance_marker=self._marker
            )
            self._locator, self._endpoint = locator, endpoint
            self._route = EC2NativeRoute(
                self._owner,
                self._name,
                creation.session,
                account,
                creation.region,
                str(creation.intent.group_id),
                self._backend,
                instance,
                prefixes,
                role_auth=self._role_auth,
            )
            self._ordinary_admission()
            self._route_transition(self._route.open, deadline)
            if self._route.cleanup_incomplete or creation.cleanup_incomplete:
                raise StateError("EC2 creation native route retains original clients")
            self._readiness.wait(endpoint, deadline)
            self._validate_route(deadline)
            self._ordinary_admission()
            self._trust = CreationSSHTrust(
                self._owner,
                vm_name=self._name,
                admin_username=self._admin,
                instance_marker=self._marker,
                endpoint=endpoint,
                settings=self._settings,
                deadline=deadline,
            )
            self._trust.prepare()
            self._validate_route(deadline)
            observe_early_guest(connection, self._marker, deadline, self._custody, bootstrap_check=CLOUD_INIT_CHECK)
            self._validate_route(deadline)
            carrier = SSHCarrier(connection)
            root_entry = IdentityPlan(
                IdentityExpectation(0, 0, (0,)),
                IdentityMode.DIRECT if self._admin == "root" else IdentityMode.SUDO_ROOT,
            )
            self.binding = NativeExecutionBinding(
                carrier,
                self._admin,
                RuntimeSelection(RuntimeTargetOS.LINUX),
                _early_guest_facts_route=_EarlyGuestFactsRoute(carrier, root_entry, self._admin),
                _new_managed_delivery=lambda: SSHCarrier(connection),
                _independent_availability=self.independent_availability,
            )
            self._result = VMCreationResult(self.platform_metadata, self.binding, locator, ssh_trust_published=True)
            self._remaining(deadline)
            return self._result
        finally:
            self._lock.release()

    def _route_transition(self, action: Callable[[Deadline], bool | None], deadline: Deadline) -> bool | None:
        control: BaseException | None = None
        try:
            if self._role_auth is not None:
                self._role_auth.begin(deadline)
            return action(deadline)
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            if self._role_auth is not None:
                try:
                    self._role_auth.close_client()
                except BaseException:
                    if control is not None:
                        raise control from None
                    raise
                finally:
                    self._role_auth.end()

    def _validate_route(self, deadline: Deadline) -> None:
        if self._closed or self.creation is None or self._route is None:
            raise StateError("EC2 creation native route is unavailable")
        locator, instance = self.creation.instance(deadline)
        state = instance.get("State")
        if (
            locator != self._locator
            or instance.get("PublicIpAddress") != self._endpoint
            or type(state) is not dict
            or state.get("Name") != "running"
        ):
            raise StateError("EC2 creation native route association changed")
        self._route_transition(self._route.check, deadline)
        self._remaining(deadline)
        if self._route.cleanup_incomplete or self.creation.cleanup_incomplete:
            raise StateError("EC2 creation native route retains original clients")

    def _check_route(self, deadline: Deadline) -> None:
        self._acquire(deadline)
        try:
            self._validate_route(deadline)
        finally:
            self._lock.release()

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        self._acquire(deadline)
        try:
            if self.creation is None:
                raise StateError("EC2 creation has no observed instance")
            return self.creation.instance(deadline)[0]
        finally:
            self._lock.release()

    def observe_power(self, deadline: Deadline) -> VMStatus:
        self._acquire(deadline)
        try:
            if self.creation is None:
                raise StateError("EC2 creation has no observed instance")
            _, instance = self.creation.instance(deadline)
            state = instance.get("State")
            name = state.get("Name") if type(state) is dict else None
            if type(name) is not str:
                return VMStatus.UNKNOWN
            return {"running": VMStatus.RUNNING, "stopped": VMStatus.STOPPED}.get(name, VMStatus.UNKNOWN)
        finally:
            self._lock.release()

    def complete_bootstrap(self, deadline: Deadline) -> None:
        self._acquire(deadline)
        try:
            if self._closed or self._rollback_requested or self._result is None or self.creation is None:
                raise StateError("EC2 bootstrap resource transfer is unproved")
            self._validate_route(deadline)
            self.creation.reconcile()
            if not self.creation.intent.accepted:
                self.creation.record(replace(self.creation.intent, accepted=True))
            self._remaining(deadline)
        finally:
            self._lock.release()

    def request_rollback(self) -> None:
        with self._lock:
            if self.creation is not None and self.creation.history and self.creation.intent.accepted:
                raise StateError("EC2 bootstrap rollback window has closed")
            self._rollback_requested = True

    def settle(self, deadline: Deadline) -> bool:
        self._acquire(deadline)
        try:
            self._closed = True
            if not self._custody.close(deadline) or not self._readiness.close():
                return False
            if not self._exposure.close(deadline):
                return False
            if self.creation is not None and not self.creation.close():
                return False
            if self._role_auth is not None and self._role_auth.cleanup_incomplete:
                self._role_auth.close_client()
                if self._role_auth.cleanup_incomplete:
                    return False
            if self._route is not None and (
                not self._route.close(deadline) or not self._route_transition(self._route.settle, deadline)
            ):
                return False
            if self._trust is not None and not self._trust.close(deadline):
                return False
            creation = self.creation
            if creation is None:
                return True
            creation.reconcile()
            if self._rollback_requested and creation.history and creation.intent.group_id is not None:
                rollback(creation, deadline)
            self._remaining(deadline)
            return creation.settle(deadline)
        finally:
            self._lock.release()
