"""Scripted EC2 boundary used by the independent creation tests."""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

from botocore.exceptions import ClientError

from agentworks.db import LifecycleObligationState
from agentworks.plugins.aws._creation import KIND

ACCOUNT = "123456789012"
REGION = "us-east-1"
GROUP = "sg-0123456789abcdef0"
INSTANCE = "i-0123456789abcdef0"
VOLUME = "vol-0123456789abcdef0"
NIC = "eni-0123456789abcdef0"
SUBNET = "subnet-0123456789abcdef0"
VPC = "vpc-0123456789abcdef0"
IMAGE = "ami-0123456789abcdef0"
JOIN_KEY = "tskey-secret-never-in-provider-payload"


def receipt(**values):
    return {**values, "ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0, "RequestId": "request-one"}}


def absent(code):
    return ClientError({"Error": {"Code": code, "Message": "scripted absence"}}, "Describe")


class Session:
    def __init__(self, owner):
        self.owner = owner
        self.calls = []
        self.clients = []
        self.closes = []
        self.close_failure = None
        self.fault = None
        self.account = ACCOUNT
        self.state = "running"
        self.termination_states = ["shutting-down", "terminated"]
        self.volume_linger = 0
        self.nic_linger = 0
        self.group_linger = 0
        self.image_floor = 8
        self.volume = VOLUME
        self.interface = NIC
        self.volume_tags = None
        self.deleted = False
        self.nonce = None
        self.rules = []
        self.after = lambda method: None

    def client(self, service, *, region_name, config):
        assert region_name == REGION
        assert config.retries == {"total_max_attempts": 1, "mode": "standard"}
        assert config.connect_timeout > 0 and config.read_timeout == config.connect_timeout
        events = {}
        client = SimpleNamespace(meta=SimpleNamespace(events=SimpleNamespace(register=events.__setitem__)))

        def close():
            self.closes.append(client)
            if self.close_failure is not None:
                raise self.close_failure

        client.close = close
        for method in (
            "get_caller_identity",
            "describe_instance_types",
            "get_parameter",
            "describe_images",
            "describe_subnets",
            "describe_instances",
            "create_security_group",
            "run_instances",
            "describe_security_groups",
            "describe_security_group_rules",
            "authorize_security_group_ingress",
            "revoke_security_group_ingress",
            "describe_volumes",
            "describe_network_interfaces",
            "terminate_instances",
            "delete_security_group",
        ):

            def send(*, _method=method, **kwargs):
                operation = "".join(part.title() for part in _method.split("_"))
                callback = events.get(f"before-send.{service}.{operation}")
                if callback is not None:
                    callback()
                assert self.owner.list_pending_lifecycle_obligations() is not None
                self.calls.append((_method, kwargs))
                result = self.reply(_method, kwargs)
                self.after(_method)
                if self.fault == _method:
                    raise KeyboardInterrupt("scripted lost reply")
                return result

            setattr(client, method, send)
        self.clients.append(client)
        return client

    def instance(self):
        return {
            "InstanceId": INSTANCE,
            "ClientToken": self.nonce,
            "ImageId": IMAGE,
            "VpcId": VPC,
            "SubnetId": SUBNET,
            "PublicIpAddress": "192.0.2.7",
            "State": {"Name": self.state},
            "Tags": self.tags(),
            "SecurityGroups": [{"GroupId": GROUP}],
            "BlockDeviceMappings": [
                {
                    "DeviceName": "/dev/xvda",
                    "Ebs": {
                        "VolumeId": self.volume,
                        "DeleteOnTermination": True,
                    },
                }
            ],
            "NetworkInterfaces": [
                {
                    "NetworkInterfaceId": self.interface,
                    "OwnerId": ACCOUNT,
                    "Attachment": {
                        "AttachmentId": "eni-attach-0123456789abcdef0",
                        "DeviceIndex": 0,
                        "DeleteOnTermination": True,
                    },
                }
            ],
        }

    def tags(self):
        return [{"Key": "agentworks:vm", "Value": "system-vm-one"}, {"Key": "agentworks:creation", "Value": self.nonce}]

    def reply(self, method, args):
        if method == "get_caller_identity":
            return receipt(Account=self.account)
        if method == "describe_instance_types":
            return receipt(
                InstanceTypes=[{"InstanceType": "t4g.small", "ProcessorInfo": {"SupportedArchitectures": ["arm64"]}}]
            )
        if method == "get_parameter":
            return receipt(Parameter={"Value": IMAGE})
        if method == "describe_images":
            return receipt(
                Images=[
                    {
                        "ImageId": IMAGE,
                        "Architecture": "arm64",
                        "RootDeviceType": "ebs",
                        "RootDeviceName": "/dev/xvda",
                        "BlockDeviceMappings": [
                            {
                                "DeviceName": "/dev/xvda",
                                "Ebs": {
                                    "SnapshotId": "snap-0123456789abcdef0",
                                    "VolumeSize": self.image_floor,
                                },
                            }
                        ],
                    }
                ]
            )
        if method == "describe_subnets":
            return receipt(
                Subnets=[{"SubnetId": SUBNET, "VpcId": VPC, "OwnerId": ACCOUNT, "AvailabilityZone": "us-east-1a"}]
            )
        if method == "create_security_group":
            resource = next(
                row for row in self.owner.list_pending_lifecycle_obligations() if row.obligation_kind == KIND
            )
            assert resource.state is LifecycleObligationState.POSSIBLE_EFFECT
            assert json.loads(resource.payload)["group_attempted"]
            self.nonce = args["TagSpecifications"][0]["Tags"][1]["Value"]
            return receipt(GroupId=GROUP)
        if method == "run_instances":
            resource = next(
                row for row in self.owner.list_pending_lifecycle_obligations() if row.obligation_kind == KIND
            )
            assert json.loads(resource.payload)["launch_attempted"]
            assert args["ClientToken"] == self.nonce
            assert args["MinCount"] == args["MaxCount"] == 1
            assert args["NetworkInterfaces"][0]["DeleteOnTermination"] is True
            assert JOIN_KEY.encode() not in gzip.decompress(args["UserData"])
            return receipt(OwnerId=ACCOUNT, Instances=[self.instance()])
        if method == "describe_instances":
            if self.state == "shutting-down" and self.termination_states:
                self.state = self.termination_states.pop(0)
            return receipt(
                Reservations=[] if "Filters" in args else [{"OwnerId": self.account, "Instances": [self.instance()]}]
            )
        if method == "describe_security_groups":
            if self.deleted:
                if self.group_linger:
                    self.group_linger -= 1
                else:
                    raise absent("InvalidGroup.NotFound")
            return receipt(
                SecurityGroups=[
                    {
                        "GroupId": GROUP,
                        "OwnerId": ACCOUNT,
                        "VpcId": VPC,
                        "Tags": self.tags(),
                        "IpPermissions": [],
                    }
                ]
            )
        if method == "describe_security_group_rules":
            return receipt(SecurityGroupRules=self.rules.copy())
        if method == "revoke_security_group_ingress":
            if args.get("DryRun"):
                raise absent("DryRunOperation")
            self.rules = [
                rule for rule in self.rules if rule["SecurityGroupRuleId"] not in args["SecurityGroupRuleIds"]
            ]
            return receipt(Return=True)
        if method == "authorize_security_group_ingress":
            permission = args["IpPermissions"][0]
            ip = permission["IpRanges"][0]
            rule = {
                "SecurityGroupRuleId": "sgr-0123456789abcdef0",
                "GroupId": GROUP,
                "GroupOwnerId": ACCOUNT,
                "IsEgress": False,
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "CidrIpv4": ip["CidrIp"],
                "Description": ip["Description"],
            }
            self.rules.append(rule)
            return receipt(Return=True, SecurityGroupRules=[rule])
        if method == "describe_volumes":
            if self.state == "terminated":
                if self.volume_linger:
                    self.volume_linger -= 1
                else:
                    raise absent("InvalidVolume.NotFound")
            return receipt(
                Volumes=[
                    {
                        "VolumeId": self.volume,
                        "Attachments": [{"InstanceId": INSTANCE, "Device": "/dev/xvda"}],
                        "Tags": self.tags() if self.volume_tags is None else self.volume_tags,
                    }
                ]
            )
        if method == "describe_network_interfaces":
            if self.state == "terminated":
                if self.nic_linger:
                    self.nic_linger -= 1
                else:
                    raise absent("InvalidNetworkInterfaceID.NotFound")
            return receipt(
                NetworkInterfaces=[
                    {
                        "NetworkInterfaceId": self.interface,
                        "OwnerId": ACCOUNT,
                        "VpcId": VPC,
                        "SubnetId": SUBNET,
                        "Attachment": {
                            "InstanceId": INSTANCE,
                            "AttachmentId": "eni-attach-0123456789abcdef0",
                            "DeleteOnTermination": True,
                        },
                    }
                ]
            )
        if method == "terminate_instances":
            resource = next(
                row for row in self.owner.list_pending_lifecycle_obligations() if row.obligation_kind == KIND
            )
            intent = json.loads(resource.payload)
            assert intent["terminate_attempted"] and intent["volumes"] == [VOLUME] and intent["interfaces"] == [NIC]
            self.state = "shutting-down"
            return receipt(TerminatingInstances=[{"InstanceId": INSTANCE}])
        if method == "delete_security_group":
            self.deleted = True
            return receipt(Return=True)
        raise AssertionError(method)
