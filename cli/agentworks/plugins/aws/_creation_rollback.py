"""Observe exact EC2 launch attachments before one-shot unaccepted cleanup."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from agentworks.errors import StateError, ValidationError
from agentworks.plugins.aws._creation import identifier
from agentworks.plugins.aws.network import error_code

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.aws._creation import EC2Creation


def observe_attachments(
    creation: EC2Creation,
    instance: dict[str, Any],
    deadline: Deadline,
    *,
    record_initial: bool = False,
) -> None:
    """Prove original launch objects before guest work, then refuse replacements."""
    mappings, interfaces = instance.get("BlockDeviceMappings"), instance.get("NetworkInterfaces")
    if type(mappings) is not list or len(mappings) != 1 or type(interfaces) is not list or len(interfaces) != 1:
        raise StateError("EC2 rollback requires the original root volume and primary interface")
    mapping, interface = mappings[0], interfaces[0]
    if type(mapping) is not dict or type(interface) is not dict:
        raise ValidationError("EC2 rollback attachment observation is invalid")
    ebs, attachment = mapping.get("Ebs"), interface.get("Attachment")
    if (
        type(ebs) is not dict
        or ebs.get("DeleteOnTermination") is not True
        or type(attachment) is not dict
        or attachment.get("DeleteOnTermination") is not True
        or type(attachment.get("DeviceIndex")) is not int
        or attachment["DeviceIndex"] != 0
        or interface.get("OwnerId") != creation.intent.account
        or mapping.get("DeviceName") != creation.intent.root_device
    ):
        raise StateError("EC2 rollback attachment deletion is unproved")
    volumes = (identifier(ebs.get("VolumeId"), "vol"),)
    nics = (identifier(interface.get("NetworkInterfaceId"), "eni"),)
    attachment_id = identifier(attachment.get("AttachmentId"), "eni-attach")
    intent = creation.intent
    if (intent.volumes and intent.volumes != volumes) or (intent.interfaces and intent.interfaces != nics):
        raise StateError("EC2 original launch attachment identities changed")
    if not record_initial and (not intent.volumes or not intent.interfaces):
        raise StateError("EC2 rollback has no recorded original launch attachments")
    volume_result = creation.call("ec2", "describe_volumes", deadline, VolumeIds=list(volumes))
    observed = volume_result.get("Volumes")
    if type(observed) is not list or len(observed) != 1 or type(observed[0]) is not dict:
        raise StateError("EC2 rollback exact volume association is unproved")
    volume = observed[0]
    if (
        volume.get("VolumeId") != volumes[0]
        or type(volume.get("Attachments")) is not list
        or len(volume["Attachments"]) != 1
        or type(volume["Attachments"][0]) is not dict
        or volume["Attachments"][0].get("InstanceId") != creation.intent.instance_id
        or volume["Attachments"][0].get("Device") != mapping.get("DeviceName")
        or type(volume.get("Tags")) is not list
        or any(volume["Tags"].count(tag) != 1 for tag in creation.tags())
    ):
        raise StateError("EC2 rollback volume belongs to another attachment")
    nic_result = creation.call("ec2", "describe_network_interfaces", deadline, NetworkInterfaceIds=list(nics))
    observed = nic_result.get("NetworkInterfaces")
    if type(observed) is not list or len(observed) != 1 or type(observed[0]) is not dict:
        raise StateError("EC2 rollback exact interface association is unproved")
    nic = observed[0]
    if (
        nic.get("NetworkInterfaceId") != nics[0]
        or nic.get("OwnerId") != creation.intent.account
        or nic.get("VpcId") != creation.intent.vpc
        or nic.get("SubnetId") != creation.intent.subnet
        or type(nic.get("Attachment")) is not dict
        or nic["Attachment"].get("InstanceId") != creation.intent.instance_id
        or nic["Attachment"].get("AttachmentId") != attachment_id
        or nic["Attachment"].get("DeleteOnTermination") is not True
    ):
        raise StateError("EC2 rollback interface belongs to another attachment")
    if (intent.volumes, intent.interfaces) != (volumes, nics):
        creation.record(replace(intent, volumes=volumes, interfaces=nics))


def _absent(creation: EC2Creation, method: str, deadline: Deadline, code: str, **kwargs: Any) -> bool:
    try:
        creation.call("ec2", method, deadline, **kwargs)
    except Exception as error:
        # The SDK error may be wrapped, but its exact provider code is retained.
        cause = error.__cause__ if isinstance(error.__cause__, Exception) else error
        if error_code(cause) == code and not creation.cleanup_incomplete:
            creation.remaining(deadline)
            return True
        raise
    return False


def _wait_for_group(creation: EC2Creation, deadline: Deadline) -> None:
    """Observe the acknowledged group's identity before admitting its deletion.

    A newly created group can be temporarily invisible. NotFound before our
    delete cannot prove that the acknowledged create has been removed.
    """
    while True:
        try:
            creation.group(deadline)
            return
        except Exception as error:
            cause = error.__cause__ if isinstance(error.__cause__, Exception) else error
            if error_code(cause) != "InvalidGroup.NotFound" or creation.cleanup_incomplete:
                raise
        time.sleep(min(0.1, creation.remaining(deadline)))


def rollback(creation: EC2Creation, deadline: Deadline) -> bool:
    """Poll original outcomes within this budget; submit each mutation only once."""
    creation.reconcile()
    intent = creation.intent
    if intent.accepted:
        raise StateError("EC2 bootstrap rollback window has closed")
    if intent.removed:
        return True
    if intent.group_id is None or (intent.launch_attempted and intent.instance_id is None):
        return False
    if intent.instance_id is not None:
        while True:
            _, instance = creation.instance(deadline)
            state = instance.get("State")
            if type(state) is not dict or state.get("Name") not in {
                "pending",
                "running",
                "stopping",
                "stopped",
                "shutting-down",
                "terminated",
            }:
                raise StateError("EC2 rollback power is unproved")
            if state["Name"] != "terminated":
                if not creation.intent.terminate_attempted and state["Name"] != "shutting-down":
                    observe_attachments(creation, instance, deadline, record_initial=True)
                    _wait_for_group(creation, deadline)
                    creation.record(replace(creation.intent, terminate_attempted=True))

                    def receipt(result: dict[str, Any]) -> None:
                        changes = result.get("TerminatingInstances")
                        if (
                            type(changes) is not list
                            or len(changes) != 1
                            or type(changes[0]) is not dict
                            or changes[0].get("InstanceId") != creation.intent.instance_id
                        ):
                            raise ValidationError("EC2 rollback termination receipt is invalid")
                        creation.record(
                            replace(creation.intent, terminate_receipt=result["ResponseMetadata"]["RequestId"])
                        )

                    creation.call(
                        "ec2", "terminate_instances", deadline, record=receipt, InstanceIds=[intent.instance_id]
                    )
            else:
                if not creation.intent.volumes or not creation.intent.interfaces:
                    return False
                volumes_absent = all(
                    _absent(
                        creation,
                        "describe_volumes",
                        deadline,
                        "InvalidVolume.NotFound",
                        VolumeIds=[volume],
                    )
                    for volume in creation.intent.volumes
                )
                nics_absent = all(
                    _absent(
                        creation,
                        "describe_network_interfaces",
                        deadline,
                        "InvalidNetworkInterfaceID.NotFound",
                        NetworkInterfaceIds=[interface],
                    )
                    for interface in creation.intent.interfaces
                )
                if volumes_absent and nics_absent:
                    break
            time.sleep(min(0.1, creation.remaining(deadline)))
    if not creation.intent.group_delete_attempted:
        _wait_for_group(creation, deadline)
        creation.record(replace(creation.intent, group_delete_attempted=True))

        def deleted(result: dict[str, Any]) -> None:
            creation.record(replace(creation.intent, group_delete_receipt=result["ResponseMetadata"]["RequestId"]))

        creation.call("ec2", "delete_security_group", deadline, record=deleted, GroupId=intent.group_id)
    while not _absent(
        creation, "describe_security_groups", deadline, "InvalidGroup.NotFound", GroupIds=[intent.group_id]
    ):
        time.sleep(min(0.1, creation.remaining(deadline)))
    creation.record(replace(creation.intent, removed=True))
    return True
