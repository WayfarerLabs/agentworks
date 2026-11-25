# Azure Key Vault secrets: completed locally

Date: 2026-11-24

The Azure plugin now contributes the version-3 `azure-keyvault` secret backend. Sources bind one
vault and choose ambient authentication or an explicit environment-backed service principal.
Mappings select the latest secret version by name or pin a name/version pair. Existing secret
commands use the backend without a resolver or CLI contract change.

No-impact preview retains unattended credential paths and defers desktop/cache credentials. Allowed
preview and resolution can use an explicit ambient browser fallback; selected service principals
never fall back. Structured failures preserve source precedence, and provider values and diagnostics
remain contained. Permanent configuration examples and provider-documentation links live with the
secrets package; the Azure plugin owns a static `concept-azure` guide shell.

The implementation passed independent project, complexity, and correctness/security reviews. The
full Linux non-integration run at `d971b2621` passed 15,377 tests with 85 skips. Strict typing,
lint, formatting, and repository consistency checks passed. The plan records the bounded test
corrections and review dispositions.

The operator reported that no live test fixture was available. Azure service behavior and native
macOS authentication remain untested; there was no cloud mutation. This is a local implementation on
`feat/azure-keyvault-secrets`, with no publication or merge performed. AWS, GCP, and macOS Keychain
backend implementations remain separate efforts.
