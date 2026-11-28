"""The opt-in Azure plugin.

Contributes the Azure VM platform, Key Vault secret backend, Azure DevOps
git credential provider, and bundled Azure CLI install command.
"""

from __future__ import annotations

from agentworks.plugins.azure.azdo import AzDOCredentialProvider
from agentworks.plugins.azure.keyvault import AzureKeyVaultBackend
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.plugins.base import Plugin

PLUGIN = Plugin(
    name="azure",
    description="Azure VM platform, Key Vault secrets, Azure DevOps git credentials, and the az CLI",
    capabilities={
        "vm-platform": (AzureVMPlatform,),
        "git-credential-provider": (AzDOCredentialProvider,),
        "secret-backend": (AzureKeyVaultBackend,),
    },
    # Anchor at this package so its ``manifests/`` subdir (the az-cli
    # install-command) is found by the bundled-manifest loader.
    manifests="agentworks.plugins.azure",
)
