# Azure Key Vault implementation plan

Delivery vehicle: one Azure feature branch and one PR. The continuing operator goal authorizes PR
delivery and up to three feedback/fix rounds per opened PR. The target is a merge-ready PR; live
validation follows the integration-testing pipeline after the ready handoff.

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

## Continuing PR delivery

The initial local implementation was completed at `915a028b9`. The operator then established the
continuing secret-work goal recorded in the FRD. Its additional delivery and confirmation work
reopens this unmerged SDD; the former local-completion lock is removed. Earlier checked items above
record the work that was actually completed and remain unchanged.

- [x] Refresh `origin/main`, verify merge compatibility, and rebase the unpublished Azure branch.
      The Azure implementation and its tests are byte-identical after the clean rebase.
- [x] Run the additional local PR gates: Typer isolation, 160 Python website tests, 103 Node website
      tests, and deterministic repeated builds at root and project site bases.
- [ ] Publish the ready Azure PR and record its exact head, CI state, and review request.
- [ ] Receive and assess the integration tester's live Azure evidence and disposition.
- [ ] Receive a complete PR-level review separate from the private quality loop.
- [ ] Batch published feedback after the process reporting window and perform at most three
      authorized fix rounds, each with its required checks, reviews, and handoff.
- [ ] Hand off a merge-ready Azure PR with current CI, review disposition, and live evidence.
- [ ] Record operator confirmation that Azure secret support is working.

The overall goal remains active until the operator confirms all secret work is complete and
everything is working. AWS, GCP, and macOS Keychain each ship separately under that goal. The
operator's thread 3 direction routes Azure live testing through the standard ready-PR pipeline.
