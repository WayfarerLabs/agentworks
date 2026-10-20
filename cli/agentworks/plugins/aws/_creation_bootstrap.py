"""Public early cloud-init prerequisites for independently created EC2 guests."""

from __future__ import annotations

import gzip

import yaml

from agentworks.capabilities.vm_platform.bootstrap_script import generate_instance_marker_installer
from agentworks.errors import ValidationError
from agentworks.plugins.aws.config import _generate_ec2_user_data


def early_user_data(*, admin: str, public_key: str, hostname: str, marker: str) -> bytes:
    """Install only public prerequisites before core's guest configuration."""
    config = yaml.safe_load(
        _generate_ec2_user_data(
            admin_username=admin, ssh_public_key=public_key, hostname=hostname, bootstrap_script=None
        )
    )
    config["disable_root"] = admin != "root"
    config["ssh_pwauth"] = False
    config["packages"] = ["python3", "sudo", "util-linux"]
    config["runcmd"] = [
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            "set -euo pipefail\n" + generate_instance_marker_installer(instance_marker=marker),
        ]
    ]
    payload = gzip.compress(("#cloud-config\n" + yaml.safe_dump(config, sort_keys=False)).encode("utf-8"), mtime=0)
    if len(payload) > 16384:
        raise ValidationError("EC2 early cloud-init exceeds the raw user-data limit")
    return payload
