# Azure Key Vault implementation plan

Delivery vehicle: one feature branch and one PR if publication is requested. No push, PR, merge,
cloud creation, or permission change is part of the current instruction.

## Design

- [x] Trace the secret-source framework, version-3 backend contract, 1Password implementation, Azure
      plugin, and existing Azure VM authentication.
- [x] Research Azure SecretClient, credential interaction, URI validation, and provider timeout
      semantics using Microsoft documentation and source.
- [x] Establish a new effort through authenticated operator direction and draft this short SDD.
- [x] Settle explicit service-principal credential bootstrap with the operator.

## Implementation

- [x] Add typed source and mapping models, SDK client lifecycle, preview/resolution, and structured
      failure handling in the Azure plugin.
- [x] Register the capability and add the latest stable Key Vault Secrets SDK dependency.
- [x] Add behavioral coverage at provider and resolver boundaries, including exact multiline and
      empty values, version selection, missing versus denied access, preview interaction, no-I/O
      registration, cleanup, and sensitive diagnostic containment.
- [x] Update Azure plugin metadata, secret examples, sample config, and concise online-documentation
      signposts; verify plugin-owned guide discovery if adding a topic.

## Validation and review

- [x] Run focused tests, Ruff, strict mypy, relevant file linters, and required repository checks.
- [x] Run independent project review, complexity review, and generic correctness/security review at
      one pinned implementation state; disposition material findings.
- [x] Identify an authorized live test vault and credentials; exercise the shipped CLI if available,
      otherwise report that coverage gap and request a bounded test fixture.
- [x] Report the implementation, observed checks, outstanding decisions, and live-test limits.

Done means the new backend is reachable through existing commands, both authentication modes work at
their boundaries, matching docs ship with behavior, and validation evidence is accurate. Lock the
SDD only when the effort is actually complete.

## Validation and disposition

- Full Linux non-integration suite at `d971b2621`: **15,377 passed, 85 skipped**, using this
  checkout's own `.venv` with Python 3.12.16. The new backend's focused suite passed 163 cases.
- Strict mypy passed for 1,143 source files; Ruff lint and formatting passed. File-quality lint,
  Rulesync drift, and locked-SDD checks passed.
- All three independent review lanes cleared the implementation and subsequent corrections.
  Real-chain regressions cover explicit browser fallback, no-impact desktop/cache exclusions,
  credential selectors, private diagnostics, cleanup, and cancellation.
- The broader suite exposed two test expectations: the schema oracle needed to follow reference
  chains, and the generated field-reference type needed to include structured mappings. Independent
  JSON Schema validation and review confirmed both corrections without changing the product schema.
- The operator selected "No live fixture available" in thread 2, message 2. No Azure service,
  sovereign-cloud endpoint, or native macOS authentication dialog was exercised. No cloud resources
  or permissions were changed.
- Host filesystem tests required normal group semantics outside the sandbox. Unix sockets required
  short session-owned paths under `/tmp/kv-1029-wf-*`; completed fixtures are removed at closeout.
  The checkout-local development environment and its normally installed uv Python runtime remain
  available for further work.
- The optional context-wrapper simplification was declined: the small wrapper follows the existing
  backend pattern. It did not gate delivery. The unchanged closing paragraphs at
  `cli/agentworks/capabilities/secret_backend/README.md:350` still mention contract version 1
  despite its version-3 contract; this is an out-of-scope documentation finding for that document's
  owner.

Delivery remains local on `feat/azure-keyvault-secrets`. No PR, push, release, or merge was
requested.
