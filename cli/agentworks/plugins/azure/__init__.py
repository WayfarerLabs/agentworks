"""The ``azure`` system plugin: the Azure VM platform, the Azure DevOps git
credential provider, and the ``az`` CLI install-command, shipped as one
separable, opt-in plugin (R11, R11.1).

The fullest migration (Phase 11): ONE plugin contributing THREE things across
two capability kinds plus a bundled manifest, the end-to-end validation of the
whole model.

- ``vm-platform``: ``AzureVMPlatform`` (``azure-vm``), seated into
  ``VM_PLATFORM_REGISTRY`` through the ``vm-platform`` adapter (the largest
  impl, formerly ``capabilities/vm_platform/azure_vm.py``).
- ``git-credential-provider``: ``AzDOCredentialProvider`` (``azdo``), seated
  into ``GIT_CREDENTIAL_PROVIDER_REGISTRY`` through the
  ``git-credential-provider`` adapter (formerly
  ``capabilities/git_credential/azdo.py``). ``azdo`` is part of the azure
  plugin, not a standalone plugin (the one-vendor grouping, matching prior
  art).
- the ``az-cli`` ``system-install-command``, a bundled declarable (moved out of
  ``manifests/builtin/install-commands.yaml`` into this package's
  ``manifests/`` subdirectory).

All three rows are present-but-disabled until an operator opts in with
``[plugins] system = ["azure"]``:

- the ``azure-vm`` platform row publishes with a ``system-plugin`` origin; a
  ``vm-site`` on it is not-ready with the "enable plugin `azure`" hint and
  ``resolve_site`` refuses it. The deprecated legacy ``[azure]`` flat-section
  site gets the same hint (a feature: legacy configs are guided, not broken
  with an unknown-name error).
- the ``azdo`` provider row publishes with a ``system-plugin`` origin; a
  ``git-credential`` naming ``provider = "azdo"`` is not-ready via its R14
  propagate hook and refused at use.
- the ``az-cli`` install-command row publishes weak (add-if-absent) while
  disabled, so a vm-template's ``system_install_commands = ["az-cli"]``
  finalizes cleanly (no unknown-name error) and is refused at use by the Phase
  7 recipe gate with the same hint until enabled.

``base`` / ``bootstrap_script`` / ``cloud_init`` stay in the core
``vm-platform`` capability package: ``base`` is the platform contract every
platform extends, and ``bootstrap_script`` / ``cloud_init`` are shared with
both the core VM initializer and the proxmox platform, so they are core
machinery, not azure's to carry. The ``git-credential`` ``base`` (the provider
contract) likewise stays in core.
"""

from __future__ import annotations

from agentworks.plugins.azure.azdo import AzDOCredentialProvider
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.plugins.base import Plugin

PLUGIN = Plugin(
    name="azure",
    description="Azure VM platform, Azure DevOps git credentials, and the az CLI",
    capabilities={
        "vm-platform": (AzureVMPlatform,),
        "git-credential-provider": (AzDOCredentialProvider,),
    },
    # Anchor at this package so its ``manifests/`` subdir (the az-cli
    # install-command) is found by the bundled-manifest loader.
    manifests="agentworks.plugins.azure",
)
