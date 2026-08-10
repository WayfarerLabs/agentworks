"""AWS EC2 site config, instance catalog, and cloud-init projection."""

from __future__ import annotations

from typing import Annotated, Literal, NamedTuple

import yaml
from pydantic import Field

from agentworks.errors import ConfigError
from agentworks.schema import AgwModel, NonEmptyStr, PositiveInt, SecretRef


class AwsAmbientAuth(AgwModel):
    """Authenticate with boto3's ambient default credential chain."""

    mode: Literal["ambient"]


class AwsAccessKeyAuth(AgwModel):
    """Authenticate with an explicit IAM access key."""

    mode: Literal["access-key"]
    access_key_id: NonEmptyStr
    access_key_secret: Annotated[
        NonEmptyStr,
        SecretRef(usage="the AWS secret access key", default_template="aws-secret-access-key"),
    ]
    assume_role_arn: NonEmptyStr | None = None


AwsAuth = Annotated[AwsAmbientAuth | AwsAccessKeyAuth, Field(discriminator="mode")]


class AwsInstanceType(AgwModel):
    """One entry in an EC2 instance-type selection catalog."""

    cpus: PositiveInt
    memory: PositiveInt
    type: NonEmptyStr
    arch: Literal["x86_64", "arm64"]


class AwsEC2Config(AgwModel):
    """Where an aws-ec2 site creates instances, and as whom."""

    name: Literal["aws-ec2"]
    region: NonEmptyStr = Field(examples=["us-east-1"])
    subnet_id: NonEmptyStr | None = Field(default=None, examples=["subnet-00000000000000000"])
    instance_types: Annotated[list[AwsInstanceType], Field(min_length=1)] | None = None
    auth: AwsAuth = AwsAmbientAuth(mode="ambient")


class _InstanceType(NamedTuple):
    """One selectable EC2 instance type and its capacity."""

    cpus: int
    memory_gib: int
    type: str
    arch: str


_DEFAULT_INSTANCE_TYPES: tuple[_InstanceType, ...] = (
    _InstanceType(2, 2, "t4g.small", "arm64"),
    _InstanceType(2, 4, "t4g.medium", "arm64"),
    _InstanceType(2, 8, "t4g.large", "arm64"),
    _InstanceType(4, 16, "t4g.xlarge", "arm64"),
    _InstanceType(8, 32, "t4g.2xlarge", "arm64"),
    _InstanceType(12, 48, "m7g.3xlarge", "arm64"),
    _InstanceType(16, 64, "m7g.4xlarge", "arm64"),
)


def _instance_catalog(config: AwsEC2Config) -> tuple[_InstanceType, ...]:
    """Return the site's declared catalog or the built-in Graviton ladder."""
    if config.instance_types is None:
        return _DEFAULT_INSTANCE_TYPES
    return tuple(_InstanceType(e.cpus, e.memory, e.type, e.arch) for e in config.instance_types)


def _select_instance_type(catalog: tuple[_InstanceType, ...], *, cpus: int, memory_gib: int) -> _InstanceType:
    """Return the smallest catalog entry satisfying both requested axes."""
    fits = [entry for entry in catalog if entry.cpus >= cpus and entry.memory_gib >= memory_gib]
    if not fits:
        largest = max(catalog, key=lambda entry: (entry.cpus, entry.memory_gib))
        raise ConfigError(
            f"no EC2 instance type satisfies the requested {cpus} vCPU / "
            f"{memory_gib} GiB (largest available is {largest.type}: "
            f"{largest.cpus} vCPU / {largest.memory_gib} GiB)",
            hint="shrink the vm-template's cpus/memory, or add a larger entry to the site's instance_types catalog",
        )
    return min(fits, key=lambda entry: (entry.cpus, entry.memory_gib))


def _generate_ec2_user_data(
    *,
    admin_username: str,
    ssh_public_key: str,
    hostname: str,
    bootstrap_script: str | None,
) -> str:
    """Build the exact cloud-init UserData submitted to RunInstances.

    EC2 has no out-of-band admin-key channel, so the user and SSH key live in
    UserData. The optional shared bootstrap script is always credential-free;
    its Tailscale join happens later through the provisioning transport.
    """
    config: dict[str, object] = {
        "hostname": hostname,
        "preserve_hostname": False,
        "users": [
            {
                "name": admin_username,
                "shell": "/bin/bash",
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "groups": ["sudo"],
                "ssh_authorized_keys": [ssh_public_key],
            }
        ],
    }
    if bootstrap_script is not None:
        config["write_files"] = [
            {"path": "/tmp/agentworks-bootstrap.sh", "permissions": "0755", "content": bootstrap_script}
        ]
        config["runcmd"] = [["/bin/bash", "/tmp/agentworks-bootstrap.sh"]]
    return "#cloud-config\n" + yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
