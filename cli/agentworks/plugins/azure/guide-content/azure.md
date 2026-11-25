---
description: Find Azure capabilities, Key Vault setup, and provider documentation.
---

# Azure

Enable the `azure` system plugin in the operator's configuration to use its VM platform, Key Vault
secret backend, Azure DevOps git credentials, and Azure CLI installation resource. Enabling the
plugin makes its capabilities available; it does not create Azure resources or grant permissions.

Use `agw resource explain vm-platform/azure-vm`,
`agw resource explain secret-backend/azure-keyvault`, and
`agw resource explain git-credential-provider/azdo` for the installed configuration contracts.
`agw resource sample secret-source` provides a starting manifest for a Key Vault source. Sources
select the vault and authentication; each secret maps a source name to its lookup.

For Agentworks VM setup, see
[Using Azure Virtual Machines](https://github.com/WayfarerLabs/agentworks/blob/main/docs/guides/azure.md).
For Key Vault configuration examples, see the
[secret-source documentation](https://github.com/WayfarerLabs/agentworks/blob/main/cli/agentworks/secrets/README.md).
Use Microsoft's online documentation for
[Azure authentication](https://learn.microsoft.com/en-us/azure/developer/python/sdk/authentication/overview),
[Key Vault access](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide), and the
[Key Vault Secrets SDK](https://learn.microsoft.com/en-us/python/api/overview/azure/keyvault-secrets-readme).

`agw secret describe NAME` previews the exact declared secret without returning its value. It may
contact Key Vault without requesting operator action. Use `--allow-interaction` only when the
operator's instruction covers any browser or account approval the preview may require; otherwise
retain the no-impact result. `agw secret verify NAME...` checks that the selected names resolve
without displaying their values. A successful lookup does not authorize displaying, persisting, or
passing those values to another destination.

Ambient certificate and workload-token inputs use checked regular files, including ordinary file
symlinks. Devices, pipes, and direct descriptor aliases such as `/dev/stdin` are refused. Configure
a regular credential file instead of passing terminal or pipe input through a file path.
