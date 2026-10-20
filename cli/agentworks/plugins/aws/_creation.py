"""One EC2 creation receipt, its original clients and exact resource disposition."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agentworks.capabilities.vm_platform.base import ProviderLocator, provider_locator_remaining
from agentworks.db import LifecycleObligationState
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import StateError, ValidationError
from agentworks.operations import LifecycleObligation
from agentworks.plugins.aws._activation import _request_id
from agentworks.plugins.aws._native_route import EC2NativeRoute
from agentworks.plugins.aws.network import require_owned_instance, wrap_ec2_error

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationOwner
    from agentworks.plugins.aws._owned_auth import _OwnedRoleSession

KIND = "aws-ec2-created-resource"


def identifier(value: object, prefix: str) -> str:
    """Validate an exact identifier received from the AWS boundary."""
    if type(value) is not str or re.fullmatch(prefix + r"-[0-9a-f]{8}(?:[0-9a-f]{9})?", value) is None:
        raise ValidationError("EC2 creation returned an invalid resource identifier")
    return value


@dataclass(frozen=True, slots=True)
class ResourceIntent:
    account: str
    region: str
    backend: str
    nonce: str
    subnet: str
    vpc: str
    image: str
    root_device: str
    launch_sha256: str
    group_attempted: bool = False
    group_id: str | None = None
    group_receipt: str | None = None
    launch_attempted: bool = False
    instance_id: str | None = None
    launch_receipt: str | None = None
    volumes: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ()
    terminate_attempted: bool = False
    terminate_receipt: str | None = None
    group_delete_attempted: bool = False
    group_delete_receipt: str | None = None
    accepted: bool = False
    removed: bool = False


def encode(intent: ResourceIntent) -> bytes:
    payload = json.dumps(asdict(intent), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(payload) > MAX_LIFECYCLE_PAYLOAD_BYTES:
        raise ValidationError("EC2 creation intent exceeds lifecycle bound")
    return payload


class EC2Creation:
    """Caller-serialized one-shot requests, never mutation replay or tag adoption.

    The resource obligation also owns the rollback plan. Cleanup updates that
    original obligation after ordinary admission closes. Missing create replies
    remain unresolved, even when a name or tag search would find a candidate.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        session: Any,
        region: str,
        *,
        role_auth: _OwnedRoleSession | None = None,
    ) -> None:
        self.owner, self.vm_name, self.session, self.region = owner, vm_name, session, region
        self.role_auth = role_auth
        self.client: Any = None
        self.obligation_id = uuid4().hex
        self.history: list[ResourceIntent] = []
        self._registration_entered = False
        self._group_entered = False

    @property
    def intent(self) -> ResourceIntent:
        return self.history[-1]

    @property
    def cleanup_incomplete(self) -> bool:
        return self.client is not None or (self.role_auth is not None and self.role_auth.cleanup_incomplete)

    def remaining(self, deadline: Deadline) -> float:
        return provider_locator_remaining(deadline, vm_name=self.vm_name)

    def close(self) -> bool:
        """Retire only actual originals; an exceptional public close keeps custody."""
        try:
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    pass
                else:
                    self.client = None
        finally:
            if self.role_auth is not None:
                try:
                    self.role_auth.close_client()
                finally:
                    self.role_auth.end()
        return not self.cleanup_incomplete

    def call(
        self,
        service: str,
        method: str,
        deadline: Deadline,
        *,
        record: Callable[[dict[str, Any]], None] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Retain receipts before late-result checks and original-client closure."""
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError

        self.remaining(deadline)
        self.owner.list_pending_lifecycle_obligations()
        if self.cleanup_incomplete:
            raise StateError("EC2 creation retains an original client")
        control: BaseException | None = None
        try:
            if self.role_auth is not None:
                self.role_auth.begin(deadline)
            remaining = self.remaining(deadline)
            self.client = self.session.client(
                service,
                region_name=self.region,
                config=Config(
                    connect_timeout=remaining,
                    read_timeout=remaining,
                    retries={"total_max_attempts": 1, "mode": "standard"},
                ),
            )
            if self.role_auth is not None:
                operation = "".join(part.title() for part in method.split("_"))
                self.client.meta.events.register(f"before-send.{service}.{operation}", self.role_auth.guard_ec2_send)
            if method in {"create_security_group", "run_instances"}:
                operation = "".join(part.title() for part in method.split("_"))

                def guard_creation_send(**event: Any) -> None:
                    self.remaining(deadline)
                    with self.owner._guard:  # noqa: SLF001
                        self.owner._reconcile_transition_locked()  # noqa: SLF001
                        self.owner._require_dispatch_admission_locked()  # noqa: SLF001

                self.client.meta.events.register(f"before-send.ec2.{operation}", guard_creation_send)
            self.remaining(deadline)
            self.owner.list_pending_lifecycle_obligations()
            raw: object = getattr(self.client, method)(**kwargs)
            if type(raw) is not dict or any(type(key) is not str for key in raw):
                raise ValidationError("EC2 creation returned an invalid response object")
            result: dict[str, Any] = raw
            EC2NativeRoute._receipt(result)
            if not _request_id(result["ResponseMetadata"]["RequestId"]):
                raise ValidationError("EC2 creation returned an invalid request identity")
            if record is not None:
                record(result)
            self.remaining(deadline)
        except (BotoCoreError, ClientError) as error:
            raise wrap_ec2_error(error) from error
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            try:
                self.close()
            except BaseException:
                if control is not None:
                    raise control from None
                raise
        self.remaining(deadline)
        if self.cleanup_incomplete:
            raise StateError("EC2 creation client retirement remains unproved")
        return result

    def record(self, intent: ResourceIntent) -> None:
        self.history.append(intent)
        self.reconcile()

    def reconcile(self) -> LifecycleObligation | None:
        row = self.owner.inspect_lifecycle_obligation(self.obligation_id)
        if row is None:
            if self._group_entered:
                raise StateError("EC2 creation resource registration is unavailable")
            return None
        revision = row.payload_revision
        if (
            row.ownership != self.owner.ownership
            or row.obligation_kind != KIND
            or row.payload_version != 1
            or not 0 <= revision < len(self.history)
            or row.payload != encode(self.history[revision])
        ):
            raise StateError("EC2 creation resource custody conflicts")
        can_resolve = not self.cleanup_incomplete and (
            not self._group_entered or self.intent.accepted or self.intent.removed
        )
        if row.state is LifecycleObligationState.RESOLVED:
            if not can_resolve or revision != len(self.history) - 1:
                raise StateError("EC2 creation resource was prematurely resolved")
            return LifecycleObligation(self.owner, row)
        obligation = LifecycleObligation(self.owner, row)
        if self._group_entered and row.state is not LifecycleObligationState.POSSIBLE_EFFECT:
            raise StateError("EC2 creation has no durable effect admission")
        for next_revision in range(revision + 1, len(self.history)):
            obligation.publish_payload(
                expected_revision=next_revision - 1, payload_version=1, payload=encode(self.history[next_revision])
            )
        return obligation

    def launch(
        self,
        deadline: Deadline,
        *,
        account: str,
        backend: str,
        nonce: str,
        subnet: str,
        vpc: str,
        image: str,
        root_device: str,
        arguments: dict[str, Any],
    ) -> None:
        if self._registration_entered:
            raise StateError("EC2 creation was already attempted")
        public_arguments = {**arguments, "UserData": hashlib.sha256(arguments["UserData"]).hexdigest()}
        digest = hashlib.sha256(json.dumps(public_arguments, sort_keys=True).encode("ascii")).hexdigest()
        self.history.append(
            ResourceIntent(account, self.region, backend, nonce, subnet, vpc, image, root_device, digest)
        )
        self._registration_entered = True
        obligation = self.owner.register_lifecycle_obligation(
            KIND, payload_version=1, payload=encode(self.intent), obligation_id=self.obligation_id
        )
        self.remaining(deadline)
        obligation.mark_possible_effect()
        self.record(replace(self.intent, group_attempted=True))
        self.remaining(deadline)
        self._group_entered = True

        def group_receipt(result: dict[str, Any]) -> None:
            self.record(
                replace(
                    self.intent,
                    group_id=identifier(result.get("GroupId"), "sg"),
                    group_receipt=result["ResponseMetadata"]["RequestId"],
                )
            )

        self.call(
            "ec2",
            "create_security_group",
            deadline,
            record=group_receipt,
            GroupName=f"agentworks-{nonce}",
            GroupDescription=f"Agentworks VM {backend}",
            VpcId=vpc,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": self.tags()}],
        )
        if self.group(deadline).get("IpPermissions") != []:
            raise StateError("EC2 creation group does not have an empty ingress baseline")
        self.record(replace(self.intent, launch_attempted=True))
        self.remaining(deadline)

        def launch_receipt(result: dict[str, Any]) -> None:
            instances = result.get("Instances")
            if result.get("OwnerId") != account or type(instances) is not list or len(instances) != 1:
                raise ValidationError("EC2 launch receipt has an invalid reservation")
            instance = instances[0]
            if type(instance) is not dict or instance.get("ClientToken") != nonce:
                raise ValidationError("EC2 launch receipt has an invalid request association")
            # Keep the complete receipt in memory before any publication can fail.
            # Attachments may not yet be populated in an asynchronous launch reply.
            mappings, interfaces = instance.get("BlockDeviceMappings"), instance.get("NetworkInterfaces")
            volumes: tuple[str, ...] = ()
            nics: tuple[str, ...] = ()
            if type(mappings) is list and len(mappings) == 1 and type(mappings[0]) is dict:
                ebs = mappings[0].get("Ebs")
                if type(ebs) is dict and ebs.get("VolumeId") is not None:
                    volumes = (identifier(ebs["VolumeId"], "vol"),)
            if (
                type(interfaces) is list
                and len(interfaces) == 1
                and type(interfaces[0]) is dict
                and interfaces[0].get("NetworkInterfaceId") is not None
            ):
                nics = (identifier(interfaces[0]["NetworkInterfaceId"], "eni"),)
            self.record(
                replace(
                    self.intent,
                    instance_id=identifier(instance.get("InstanceId"), "i"),
                    launch_receipt=result["ResponseMetadata"]["RequestId"],
                    volumes=volumes,
                    interfaces=nics,
                )
            )

        arguments = {
            **arguments,
            "NetworkInterfaces": [
                {
                    "DeviceIndex": 0,
                    "AssociatePublicIpAddress": True,
                    "DeleteOnTermination": True,
                    "Groups": [self.intent.group_id],
                    "SubnetId": subnet,
                }
            ],
        }
        self.call("ec2", "run_instances", deadline, record=launch_receipt, **arguments)

    def tags(self) -> list[dict[str, str]]:
        return [
            {"Key": "agentworks:vm", "Value": self.intent.backend},
            {"Key": "agentworks:creation", "Value": self.intent.nonce},
        ]

    def group(self, deadline: Deadline) -> dict[str, Any]:
        result = self.call("ec2", "describe_security_groups", deadline, GroupIds=[self.intent.group_id])
        groups = result.get("SecurityGroups")
        if type(groups) is not list or len(groups) != 1 or type(groups[0]) is not dict:
            raise ValidationError("EC2 creation group observation is incomplete")
        group = groups[0]
        if (
            group.get("GroupId") != self.intent.group_id
            or group.get("OwnerId") != self.intent.account
            or group.get("VpcId") != self.intent.vpc
            or type(group.get("Tags")) is not list
            or any(group["Tags"].count(tag) != 1 for tag in self.tags())
        ):
            raise StateError("EC2 creation group association changed")
        return group

    def instance(self, deadline: Deadline) -> tuple[ProviderLocator, dict[str, Any]]:
        intent = self.intent
        if intent.instance_id is None or intent.launch_receipt is None:
            raise StateError("EC2 creation has no acknowledged instance")
        result = self.call("ec2", "describe_instances", deadline, InstanceIds=[intent.instance_id])
        reservations = result.get("Reservations")
        if (
            type(reservations) is not list
            or len(reservations) != 1
            or type(reservations[0]) is not dict
            or reservations[0].get("OwnerId") != intent.account
        ):
            raise StateError("EC2 creation instance account is unproved")
        instances = reservations[0].get("Instances")
        if type(instances) is not list or len(instances) != 1 or type(instances[0]) is not dict:
            raise StateError("EC2 creation instance observation is incomplete")
        instance = instances[0]
        if (
            instance.get("InstanceId") != intent.instance_id
            or instance.get("ClientToken") != intent.nonce
            or instance.get("ImageId") != intent.image
            or instance.get("SubnetId") != intent.subnet
            or instance.get("VpcId") != intent.vpc
            or type(instance.get("Tags")) is not list
            or any(instance["Tags"].count(tag) != 1 for tag in self.tags())
        ):
            raise StateError("EC2 creation exact instance association changed")
        state = instance.get("State")
        if type(state) is not dict or state.get("Name") != "terminated":
            require_owned_instance(instance, intent.backend, str(intent.group_id))
        return ProviderLocator(f"aws-ec2:{intent.account}:{intent.region}:{intent.instance_id}"), instance

    def settle(self, deadline: Deadline) -> bool:
        self.remaining(deadline)
        if not self.close():
            return False
        self.remaining(deadline)
        obligation = self.reconcile()
        if obligation is None:
            return not self._group_entered
        if not self._group_entered or self.intent.accepted or self.intent.removed:
            if obligation.state is not LifecycleObligationState.RESOLVED:
                obligation.resolve()
            self.remaining(deadline)
            return True
        return False
