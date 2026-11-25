# Azure Key Vault secrets

## Scope

Add Azure Key Vault secret lookup to the existing Azure plugin. The operator selected Azure as the
first provider and requested ambient credentials by default plus explicit service-principal
authentication, following the Azure VM provider's authentication choices. AWS, GCP, and macOS
Keychain remain separate efforts.

## Requirements

- Operators can declare multiple Key Vault secret sources and order them with existing sources.
- Each source selects a vault and an authentication mode. Ambient authentication is the default;
  explicitly selected service principals never fall back to another identity.
- Each mapping selects a secret and optionally a version. Omitted versions follow Azure's latest
  version semantics. Values retain their exact text, including multiline content.
- Existing static inspection, preview, verification, and consuming commands work through the
  existing secret-backend contract. Registration and static inspection do no provider work.
- Preview obeys its operator-impact ceiling and returns no plaintext. Actual lookup remains
  independent of terminal-input availability when authentication needs no terminal input.
- Confirmed absence permits source fallback. Authentication, authorization, malformed values, and
  other provider failures do not masquerade as absence.
- Values and provider-native diagnostics do not escape through logs, exceptions, representations,
  preview results, or cleanup messages. NUL-containing values are rejected.
- The feature is part of the opt-in `azure` plugin. It does not create vaults, write secrets, or
  change permissions.
- Documentation covers Agentworks configuration and links to Microsoft's documentation for Azure
  setup, permissions, and authentication. Avoid duplicating provider manuals.

## Proposed credential bootstrap

The current framework forbids a secret source's configuration from containing an Agentworks
`SecretRef`. Unlike a VM site, a secret source must authenticate before Agentworks can resolve
secrets. The proposed explicit-principal mode takes tenant and client identifiers in source config
and reads its client secret from an explicitly named environment variable, defaulting to
`AZURE_CLIENT_SECRET`. No plaintext credential belongs in a manifest. This proposal is being put to
the operator before implementation of that input path.

## Acceptance

Both authentication modes resolve a declared secret through the real backend boundary. Tests
demonstrate value containment, version selection, source precedence, offline inspection, and plugin
enablement. Live validation requires an authorized test vault; unavailable live coverage is reported
explicitly rather than inferred from mocked tests.
