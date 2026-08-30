# Instance Model and State: locked

**Lock candidate:** 2026-08-30

This is the final closeout PR for the instance-model child effort. The lock takes effect only when
the PR lands on `main`. Until then, the file records the reviewed implementation state and the two
ready-stage merge gates that remain open: full live VM validation and the parent saga's final
review. A material finding in either gate reopens this candidate rather than being accepted through
the lock.

## What shipped

- PR #632 assessed the persistence estate and settled the smallest typed repository boundary.
- PR #636 added migration 32, the closed `InstanceStateRepository`, desired instance overlays,
  lifecycle-evidence slices, backup projection, and owner-transaction cleanup without a backfill.
- PR #670 added final inline instance specs, paired VM and admin layers, durable and prospective
  live-resource publication, and one collect-then-finalize resource graph.
- PR #686 replaced per-domain merge policy with schema-directed recursive merging for core and
  capability models, including nested replacement, union boundaries, path provenance, and the
  harness-integration version-2 cutover.
- PR #700 added successful-operation hardware-request evidence, passphrase-safe OpenSSH identity
  derivation, SSH write proof, preflight comparison, backup support, and honest unknown or
  unverifiable states.
- PR #703 added resolved template specs, live-instance declaration and lifecycle-evidence
  inspection, additive JSON v1 instance state, and batch doctor diagnostics.
- PR #706 made human live-instance configuration documents safe block YAML while leaving the JSON v1
  machine contract unchanged.

## Verification state

Every implementation PR completed its focused, full repository, isolated-home CLI, private project,
fresh-eyes, and complexity gates at the exact handed-off head. The final merged implementation basis
is `9444c5f47948229f4492ff438a0466fa26c17362` on `main`.

The closeout candidate is not merge-complete until both remaining Phase 6 boxes are supported by
exact-head evidence:

1. Live VM acceptance covers create-time capture, matching preflight, deliberate identity drift, a
   passphrase-protected OpenSSH key, safe cleanup, and independent residue verification.
2. The parent saga's final review campaign accepts the complete child effort and its plan-checkbox
   accounting.

The final closeout update records those reports and the closeout head before merge. The operator
owns the merge; this effort does not merge its own PR.

## Permanent homes

Nothing under this SDD directory is required to implement, extend, or operate the shipped system.
The durable contracts live in:

- `cli/agentworks/db/README.md` for the typed store, payload, extension, absence, transaction, and
  backup contract;
- `cli/agentworks/schema/README.md`, `cli/agentworks/capabilities/README.md`, the
  harness-integration capability README, and ADR 0023 for schema-directed merge behavior and the
  intentionally deferred list-item identity direction;
- ADR 0024 and `docs/guides/resources.md` for database-backed and pending resource publication;
- `cli/command-reference.md` for the exact instance-spec, resolved-spec, describe, doctor, human
  YAML, and JSON v1 surfaces; and
- `cli/README.md`, `docs/guides/idempotency.md`, and `docs/guides/upgrading-to-0.17.md` for operator
  lifecycle, recovery, and upgrade guidance.

No permanent artifact references this SDD path.

## Intentional boundaries

- Existing instances receive no synthesized lifecycle evidence. Their facts remain not recorded
  until a successful supported lifecycle operation establishes the applicable slice. Workspace
  repair is not full convergence and does not create a complete workspace record.
- Instance specs exist only where the corresponding template can be selected or changed. VM reinit,
  workspace repair, session resume, and workspace copy do not accept a spec.
- Passphrase-protected OpenSSH keys remain supported when their envelope exposes the public
  identity. Recognized legacy encrypted formats without that property are unverifiable rather than
  mismatched. SSH-agent identity selection remains deliberately unresolved and is not inferred.
- Hardware request evidence records the configuration associated with successful creation, not
  provider-observed or provider-realized hardware. Detecting provider normalization or inconsistency
  is outside this mechanism.
- Drift is visible but not remediated. Rekey, re-apply, and broader degraded-command policy remain
  separate concerns.
- Append-deduplicated list items remain atomic. ADR 0023 records model-owned item identity as the
  future semantic direction without specifying an API.

-- agw-ns-instance-model (instance-model effort lead)
