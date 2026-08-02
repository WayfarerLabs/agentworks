"""The ``aws`` system plugin: the EC2 VM platform, shipped as a separable,
opt-in plugin (R11, R11.1).

A capability-only migration in the proxmox mould (no bundled manifests, no
system-install-command): the plugin seats its ``EC2Platform`` into
``VM_PLATFORM_REGISTRY`` through the ``vm-platform`` adapter and publishes a
``vm-platform`` row with a ``system-plugin`` origin. The row is
present-but-disabled until an operator opts in with
``[plugins] system = ["aws"]``; while disabled a ``vm-site`` on the ``ec2``
platform is not-ready with the "enable plugin `aws`" hint and ``resolve_site``
refuses it.

Unlike the ``azure`` plugin there is no bundled ``system-install-command``: the
platform talks to AWS in-process through boto3, so it needs no CLI tool
installed in the fleet OS and therefore no install-command manifest. The
platform is named ``ec2`` (one specific AWS service), following the same
one-service naming rationale ``azure-vm`` uses: other AWS services could
plausibly back platforms of their own someday.

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
    description="Amazon EC2 VM platform",
    capabilities={"vm-platform": (EC2Platform,)},
)
