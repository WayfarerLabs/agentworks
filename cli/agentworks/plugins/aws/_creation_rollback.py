"""Observe exact EC2 launch attachments before one-shot unaccepted cleanup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentworks.errors import StateError, ValidationError
from agentworks.plugins.aws._creation import identifier
from agentworks.plugins.aws.network import error_code

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.aws._creation import EC2Creation


def _attachments(creation: EC2Creation, instance: dict[str, Any], deadline: Deadline) -> None:
    """Record all exact auto-deleting objects before termination can erase linkage."""
    mappings, interfaces = instance.get("BlockDeviceMappings"), instance.get("NetworkInterfaces")
    if type(mappings) is not list or len(mappings) != 1 or type(interfaces) is not list or len(interfaces) != 1:
        raise StateError("EC2 rollback requires the original root volume and primary interface")
    mapping, interface = mappings[0], interfaces[0]
    if type(mapping) is not dict or type(interface) is not dict:
        raise ValidationError("EC2 rollback attachment observation is invalid")
    ebs, attachment = mapping.get("Ebs"), interface.get("Attachment")
    if (
        type(ebs) is not dict or ebs.get("DeleteOnTermination") is not True
        or type(attachment) is not dict or attachment.get("DeleteOnTermination") is not True
        or attachment.get("DeviceIndex") != 0 or interface.get("OwnerId") != creation.intent.account
    ):
        raise StateError("EC2 rollback attachment deletion is unproved")
    volumes = (identifier(ebs.get("VolumeId"), "vol"),)
    nics = (identifier(interface.get("NetworkInterfaceId"), "eni"),)
    if creation.intent.volumes or creation.intent.interfaces:
        if (creation.intent.volumes, creation.intent.interfaces) != (volumes, nics):
            raise StateError("EC2 rollback attachment identities changed")
    else:
        creation.update(volumes=volumes, interfaces=nics)
    volume_result = creation.call("ec2", "describe_volumes", deadline, VolumeIds=list(volumes))
    observed = volume_result.get("Volumes")
    if type(observed) is not list or len(observed) != 1 or type(observed[0]) is not dict:
        raise StateError("EC2 rollback exact volume association is unproved")
    volume = observed[0]
    if (
        volume.get("VolumeId") != volumes[0] or type(volume.get("Attachments")) is not list
        or len(volume["Attachments"]) != 1 or type(volume["Attachments"][0]) is not dict
        or volume["Attachments"][0].get("InstanceId") != creation.intent.instance_id
        or volume["Attachments"][0].get("Device") != mapping.get("DeviceName")
    ):
        raise StateError("EC2 rollback volume belongs to another attachment")
    nic_result = creation.call("ec2", "describe_network_interfaces", deadline, NetworkInterfaceIds=list(nics))
    observed = nic_result.get("NetworkInterfaces")
    if type(observed) is not list or len(observed) != 1 or type(observed[0]) is not dict:
        raise StateError("EC2 rollback exact interface association is unproved")
    nic = observed[0]
    if (
        nic.get("NetworkInterfaceId") != nics[0] or nic.get("OwnerId") != creation.intent.account
        or nic.get("VpcId") != creation.intent.vpc or nic.get("SubnetId") != creation.intent.subnet
        or type(nic.get("Attachment")) is not dict
        or nic["Attachment"].get("InstanceId") != creation.intent.instance_id
        or nic["Attachment"].get("AttachmentId") != attachment.get("AttachmentId")
        or nic["Attachment"].get("DeleteOnTermination") is not True
    ):
        raise StateError("EC2 rollback interface belongs to another attachment")


def _absent(creation: EC2Creation, method: str, deadline: Deadline, code: str, **kwargs: Any) -> bool:
    try:
        creation.call("ec2", method, deadline, **kwargs)
    except Exception as error:
        # The SDK error may be wrapped, but its exact provider code is retained.
        cause = error.__cause__ if error.__cause__ is not None else error
        if error_code(cause) == code and not creation.cleanup_incomplete:
            creation.remaining(deadline)
            return True
        raise
    return False


def rollback(creation: EC2Creation, deadline: Deadline) -> bool:
    """Observe original outcomes; send each destructive request at most once."""
    creation.reconcile()
    intent = creation.intent
    if intent.accepted:
        raise StateError("EC2 bootstrap rollback window has closed")
    if intent.removed:
        return True
    if intent.group_id is None or (intent.launch_attempted and intent.instance_id is None):
        return False
    if intent.instance_id is not None:
        _, instance = creation.instance(deadline)
        state = instance.get("State")
        if type(state) is not dict:
            raise StateError("EC2 rollback power is unproved")
        if state.get("Name") != "terminated":
            if not intent.terminate_attempted:
                _attachments(creation, instance, deadline)
                creation.group(deadline)
                creation.update(terminate_attempted=True)

                def receipt(result: dict[str, Any]) -> None:
                    changes = result.get("TerminatingInstances")
                    if (
                        type(changes) is not list or len(changes) != 1 or type(changes[0]) is not dict
                        or changes[0].get("InstanceId") != creation.intent.instance_id
                    ):
                        raise ValidationError("EC2 rollback termination receipt is invalid")
                    creation.update(terminate_receipt=result["ResponseMetadata"]["RequestId"])

                creation.call(
                    "ec2", "terminate_instances", deadline, record=receipt, InstanceIds=[intent.instance_id]
                )
            return False
        # Terminal instance state cannot substitute for identities lost before termination.
        if not creation.intent.volumes or not creation.intent.interfaces:
            return False
        for volume in creation.intent.volumes:
            if not _absent(creation, "describe_volumes", deadline, "InvalidVolume.NotFound", VolumeIds=[volume]):
                return False
        for interface in creation.intent.interfaces:
            if not _absent(
                creation, "describe_network_interfaces", deadline, "InvalidNetworkInterfaceID.NotFound",
                NetworkInterfaceIds=[interface],
            ):
                return False
    if not creation.intent.group_delete_attempted:
        creation.group(deadline)
        creation.update(group_delete_attempted=True)

        def deleted(result: dict[str, Any]) -> None:
            creation.update(group_delete_receipt=result["ResponseMetadata"]["RequestId"])

        creation.call("ec2", "delete_security_group", deadline, record=deleted, GroupId=intent.group_id)
    if not _absent(
        creation, "describe_security_groups", deadline, "InvalidGroup.NotFound", GroupIds=[intent.group_id]
    ):
        return False
    creation.update(removed=True)
    return True
