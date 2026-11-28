# Azure Key Vault secrets

<!-- cspell:ignore Agention -->

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

## Credential bootstrap

The current framework forbids a secret source's configuration from containing an Agentworks
`SecretRef`. Unlike a VM site, a secret source must authenticate before Agentworks can resolve
secrets. The explicit-principal mode takes tenant and client identifiers in source config and reads
its client secret from an explicitly named environment variable, defaulting to
`AZURE_CLIENT_SECRET`. No plaintext credential belongs in a manifest.

### Operator ruling, 2026-11-24

In Agention thread 1, message 5, the operator selected "Use environment (recommended)" in response
to "Use an environment-backed client secret for explicit Key Vault service-principal
authentication?"

## Acceptance

Both authentication modes resolve a declared secret through the real backend boundary. Tests
demonstrate value containment, version selection, source precedence, offline inspection, and plugin
enablement. Live validation requires an authorized test vault; unavailable live coverage is reported
explicitly rather than inferred from mocked tests.

## Operator direction: PR delivery

The operator established this continuing goal through the authenticated session:

> work with the operator here and in Agention to define the work to be done and then get any PRs
> merge-ready via our standard agentic-dev-process, with up to 3 feedback/fix rounds per PR that you
> open. This goal can't complete until all secret work is confirmed done by the operator and
> everything is confirmed working.

This resumes Azure delivery beyond the completed local implementation. The implementation's prior
validation remains evidence; PR checks, live confirmation, feedback disposition, and operator
confirmation remain open work under the continuing goal.

In Agention thread 3, message 2, the operator added:

> Include it but each of these should be shipped individually

This includes macOS Keychain in the overall secret effort while preserving separate delivery for
each backend. For Azure live validation, the operator directed:

> The integration tester will be able to test this. Just mark it ready when you are ready and await
> the results per our agentic-dev-process
