# Azure Key Vault implementation plan

Delivery vehicle: one feature branch and one PR if publication is requested. No push, PR, merge,
cloud creation, or permission change is part of the current instruction.

## Design

- [x] Trace the secret-source framework, version-3 backend contract, 1Password implementation, Azure
      plugin, and existing Azure VM authentication.
- [x] Research Azure SecretClient, credential interaction, URI validation, and provider timeout
      semantics using Microsoft documentation and source.
- [x] Establish a new effort through authenticated operator direction and draft this short SDD.
- [ ] Settle explicit service-principal credential bootstrap with the operator.

## Implementation

- [ ] Add typed source and mapping models, SDK client lifecycle, preview/resolution, and structured
      failure handling in the Azure plugin.
- [ ] Register the capability and add the latest stable Key Vault Secrets SDK dependency.
- [ ] Add behavioral coverage at provider and resolver boundaries, including exact multiline and
      empty values, version selection, missing versus denied access, preview interaction, no-I/O
      registration, cleanup, and sensitive diagnostic containment.
- [ ] Update Azure plugin metadata, secret examples, sample config, and concise online-documentation
      signposts; verify plugin-owned guide discovery if adding a topic.

## Validation and review

- [ ] Run focused tests, Ruff, strict mypy, relevant file linters, and required repository checks.
- [ ] Run independent project review, complexity review, and generic correctness/security review at
      one pinned implementation state; disposition material findings.
- [ ] Identify an authorized live test vault and credentials; exercise the shipped CLI if available,
      otherwise report that coverage gap and request a bounded test fixture.
- [ ] Report the implementation, observed checks, outstanding decisions, and live-test limits.

Done means the new backend is reachable through existing commands, both authentication modes work at
their boundaries, matching docs ship with behavior, and validation evidence is accurate. Lock the
SDD only when the effort is actually complete.
