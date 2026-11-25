# Azure secret backend research

## Findings

The existing backend contract already separates source configuration, per-secret mappings,
operation-local clients, preview intent, and actual resolution. The Azure plugin should contribute
one implementation rather than introduce a second resolver or vendor-wide capability superclass. See
`cli/agentworks/capabilities/secret_backend/base.py`,
`cli/agentworks/plugins/onepassword/backend.py:172`, and `cli/agentworks/plugins/azure/__init__.py`.

The official SDK exposes `SecretClient.get_secret(name, version)`; omitting the version gets the
latest version. It requires secret-get permission, so listing the vault is unnecessary. Its
structured exceptions are a better basis for fallback decisions than parsing human CLI messages.
Source:
[SecretClient reference](https://learn.microsoft.com/en-us/python/api/azure-keyvault-secrets/azure.keyvault.secrets.secretclient?view=azure-python).

DefaultAzureCredential includes environment, workload identity, managed identity, local developer
credentials, and broker paths. Disabling interactive-browser authentication alone does not exclude
broker or VS Code credential paths. Preview needs deliberate control over interaction-capable paths.
Source:
[DefaultAzureCredential reference](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential?view=azure-python).

The SDK recommends validating that vault URLs identify real Key Vault endpoints. Keep its challenge
resource verification enabled. Source:
[Microsoft's vault URI guidance](https://devblogs.microsoft.com/azure-sdk/guidance-for-applications-using-the-key-vault-libraries/).

SDK connect/read timeout settings and retry settings describe separate phases. They do not prove one
wall-clock deadline over a credential chain and multiple secret reads. Source:
[Azure Core configuration](https://learn.microsoft.com/en-us/python/api/overview/azure/core-readme?view=azure-python).

## Do not rely on

- Reusing the VM credential helper unchanged: `plugins/azure/auth.py` performs an ARM token probe
  and may initiate browser login, which violates offline construction and zero-impact preview.
- Treating all not-found-looking responses as ordinary missing. Only specific provider evidence of
  absence can justify source fallback.
- Storing a `SecretRef` in a secret-source config. The registration contract forbids it to avoid
  authentication depending on the resolution it must enable.
- Assuming plugins register guide topics. Static first-party package discovery is the supported
  mechanism described in `cli/agentworks/plugins/README.md`.

## Open questions

- Operator disposition of the proposed environment-backed explicit service principal.
- Availability of an authorized live Key Vault fixture and its permitted operations.

The external sources above are primary Microsoft documentation. Local code is authoritative for
Agentworks behavior; external docs explain provider contracts rather than authorizing cloud work.
