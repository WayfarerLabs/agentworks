"""The opt-in ``aws`` vendor bundle and its EC2 contribution.

The bundle publishes the ``aws-ec2`` VM platform and an optional guest-side
``aws-cli`` installer. EC2 lifecycle continues to use boto3, never the guest
CLI. Future AWS implementations retain their own capability contracts and
service-specific names instead of introducing a provider-wide abstraction.

``base`` / ``bootstrap_script`` / ``cloud_init`` / ``ssh_exposure`` stay in the
core ``vm-platform`` capability package: ``base`` is the platform contract every
platform extends, ``bootstrap_script`` / ``cloud_init`` are shared with both the
core VM initializer and the other cloud platforms, and ``ssh_exposure`` is the
operator egress detection both cloud platforms fold into their scoped SSH
allows, so they are core machinery, not aws's to carry.
"""

from __future__ import annotations

from agentworks.plugins.aws.platform import EC2Platform
from agentworks.plugins.base import Plugin

PLUGIN = Plugin(
    name="aws",
    description="Amazon EC2 VM platform and optional AWS CLI",
    capabilities={"vm-platform": (EC2Platform,)},
    manifests="agentworks.plugins.aws",
)
