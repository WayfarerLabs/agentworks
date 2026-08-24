# Instance Model and State: Implementation Plan

<!-- cspell:ignore sdds -->

- Status: R2 checkpoint ready for saga review
- Date: 2026-08-23
- Requirements: [frd.md](./frd.md)
- R1 assessment: [database-assessment.md](./database-assessment.md)
- R2 contract: [store-contract.md](./store-contract.md)
- Instance-spec CLI: [instance-spec-cli.md](./instance-spec-cli.md)
- Code basis: `d1c5fbc7`, stacked on accepted R1 PR #632
- Delivery vehicle: accepted R1 artifact PR #632, then one stacked implementation branch and draft
  PR for R2 through R5

## Delivery posture

R1 is an independently reviewed coordination artifact and is ready to merge. R2 through R5 remain
one feature because their value is the complete declaration, applied-state, and resolved-spec arc.
The implementation PR is a shallow stack on R1 until #632 merges, then rebases onto `main`.

The implementation PR uses two checkpoints while it remains draft:

1. R2 store contract and implementation, reviewed by the saga lead before R3 lifecycle code lands.
2. The complete R2 through R5 feature, reviewed and validated before merge intent.

Completed checkboxes are immutable. The effort lead updates them only after the named behavior,
tests, permanent collateral, and independent review are complete.

## Full gate

From `cli/`:

```console
uv run ruff check agentworks/ tests/
uv run ruff format --check agentworks/ tests/
uv run mypy agentworks/ tests/
uv run pytest tests/ -m 'not integration'
```

From the repository root:

```console
./scripts/lint-files.sh
./scripts/check-locked-sdds.sh
./scripts/rulesync-upgen.sh --check
python3 -m unittest discover -s website/tests -p 'test_*.py'
node --test website/tests/*.test.mjs
```

The complete code PR also receives real isolated-home CLI validation and live-backend validation for
the SSH identity drift crux under the integration-testing and test-environment protocols.

## Phase 1: persistence assessment

- [x] Inventory the complete current persistence estate, query shapes, migrations, concurrency, pain
      points, and R3 through R5 storage needs with HEAD evidence.
- [x] Settle the minimum one-table, closed-method repository shape and the no-backfill rule with the
      saga lead.
- [x] Record safe password-protected OpenSSH identity derivation, legacy encrypted-format unknowns,
      the adjacent-public-file hazard, and the deliberately unresolved ssh-agent question.
- [x] File declined persistence defects with root cause and call sites in issues 505, 633, 634,
      and 635.

Definition of done: the saga lead accepts R1 and unblocks storage implementation. Completed at
`d1c5fbc7`.

## Phase 2: R2 store contract and implementation

- [x] Finalize `store-contract.md` and the matching architecture section with a closed, enumerated
      consumer API and no generic record, query, filter, or blob surface.
- [x] Add the additive `instance_records` migration, exact schema sentinel, natural key, batch-read
      index, and no backfill.
- [x] Add frozen typed carriers and an `InstanceStateRepository` sharing the `Database` connection,
      read snapshot, migration gate, and transaction.
- [x] Implement desired-overlay get, put, clear, and list; applied-slice get, atomic partial
      replace, and batch list; and typed instance-record deletion.
- [x] Make VM, workspace, agent, and session deletion remove their owned records atomically,
      including children removed by aggregate delete paths.
- [x] Add the permanent store contract beside `agentworks.db`, including the extension rule later
      integration and artifact consumers must follow.
- [x] Prove fresh and v31 migration, no backfill, schema/index constraints, canonical round trips,
      absent and malformed reads, operation/timestamp provenance, deterministic ordering, atomic
      rollback, enclosing transaction composition, read snapshots, and owner cleanup.
- [x] Review the touched database tests for duplicate or prose-policing coverage; add neither and
      preserve migration, backup, restore, lock, and snapshot behavior coverage.
- [x] Obtain equal-capability project review and an independent fresh-eyes pass, resolve every
      material finding, and run focused and full gates.
- [ ] Hand the exact green R2 head to the saga lead.

Checkpoint evidence before handoff: the project reviewer and fresh-eyes reviewer approved the
corrected tree with no remaining findings. The focused suite passed 88 tests. The full Python gate
passed Ruff, formatting, strict mypy, Typer isolation, and 7,310 tests with one skip. Repository
file lint, locked-SDD, Rulesync, website tests, and deterministic website builds passed.
Disposable-home acceptance proved fresh v32 creation, repository round trips and owner cleanup, safe
v31-to-v32 CLI migration with a pre-migration backup, and no synthesized records. R2 has no
live-provider boundary.

Definition of done: the shipped repository contract is sufficient for R3 and for wave 4 to add a new
named typed consumer without changing table, connection, transaction, or absence semantics. R3
implementation does not begin until the saga lead accepts this checkpoint.

## Phase 3: general layer stack and desired instance overlays

- [ ] Finalize the R4 design from the four existing per-kind folds. Route a split for authenticated
      operator direction if one shared layer-stack mechanism cannot replace the four loops without a
      fifth instance-only merge.
- [ ] Introduce one general ordered layer fold while retaining each domain's field merge semantics,
      defaulting rules, validation, and provenance.
- [ ] Preserve current template clearing semantics: empty additive lists and maps do not invent a
      removal tombstone, and an overlay cannot declare `name`, `inherits`, metadata, or framework
      provenance.
- [ ] Define typed per-kind overlay payloads and codecs over the shared store, with one final
      overlay layer after the template chain and no required ceremony when it is absent.
- [ ] Add inline JSON `--spec JSON` to the four direct instance creation commands,
      `--workspace-spec JSON` and `--agent-spec JSON` to compound session creation, and matching
      `set-spec NAME SPEC` and `clear-spec NAME` verbs to all four instance groups, with
      declaration-time effective-instance reference and capability validation matching template
      error quality without publishing a fake template or creating instance manifests. Reject
      copied-workspace specs while their synthetic `copied` template has no resolvable base.
- [ ] Prove scalar override, list/map merge behavior, defaults, provenance, invalid overlays, absent
      overlays, persistence, deletion, and parity across VM, workspace, agent, and session kinds.
- [ ] Update command reference, completions, sample configuration or manifest teaching, and guide
      collateral in the same phase that makes each claim true.

Definition of done: every instance kind resolves template declarations plus an optional final
instance layer through one shared stack mechanism, and no fifth per-kind merger exists.

## Phase 4: R3 applied instance state and SSH proving slice

- [ ] Add domain-owned versioned codecs for the resolved applied specification and provisioned SSH
      identity slices, without storing secrets, private key bytes, or passphrases.
- [ ] Parse the authoritative public blob directly from `openssh-key-v1` private files and derive
      the OpenSSH SHA-256 fingerprint without consulting an adjacent public key or spawning a
      passphrase prompt.
- [ ] Compare the configured public and private identities before remote application when both are
      verifiable, so a successful public-key write cannot create a false private-identity record.
- [ ] Represent encrypted formats that cannot expose an identity non-interactively as unverifiable,
      not mismatch or unsupported; leave ssh-agent selection unresolved.
- [ ] Make authorized-key reconciliation return a typed applied/not-applied outcome instead of
      inferring proof from lifecycle success or unrelated warnings.
- [ ] Capture only slices proven by successful VM create and reinit operations, with one operation
      and timestamp, and write lifecycle-row plus applied-state changes in one honest transaction
      where they compose.
- [ ] Add preflight comparison before SSH transport and structural diagnostic facts for not
      recorded, unverifiable, match, and drift without remediation.
- [ ] Prove matching, replaced private key at the same path, stale or mismatched adjacent public
      key, encrypted OpenSSH key, encrypted legacy PEM, missing/unreadable key, absent historic
      state, successful capture, failed-operation non-capture, and atomic lifecycle behavior.
- [ ] Publish the payload and comparison contract in the permanent store documentation.

Definition of done: the identity used at apply time is recorded and compared with the identity the
current transport will present, and no password-protected-key path regresses.

## Phase 5: R5 resolved-spec and drift surfaces

- [ ] Extend the focused `resource show` service with fully resolved template values and path, map
      key, or list-item provenance sufficient to distinguish declared, inherited, defaulted, and
      overlaid contributors truthfully.
- [ ] Extend live-instance show with current declared resolution, applied slices, and explicit not
      recorded, match, drift, or unverifiable comparison state in the existing read snapshot.
- [ ] Add doctor batch reads and structural SSH drift checks without opening a sidecar or repeating
      one query per instance.
- [ ] Preserve JSON v1 fields and add only optional tagged data; keep human and JSON facts
      reconciled without prose-policing tests.
- [ ] Prove effective CPU, memory, disk, and swap are available before `vm create`; template and
      instance provenance; applied/current comparison; batch query behavior; and no overlay ceremony
      in the simple case.
- [ ] Update permanent resource, machine-output, doctor, command-reference, and guide collateral.

Definition of done: an operator or agent can inspect effective pre-mutation specs and honest
post-apply drift, including visible ignorance, from the supported CLI surfaces.

## Phase 6: complete verification and closeout

- [ ] Run focused tests after every phase and the full gate on the complete exact head.
- [ ] Run equal-capability project review, independent fresh-eyes review, and the saga review
      campaign scaled to the final blast radius; resolve every material finding.
- [ ] Run isolated-home CLI acceptance for overlay declaration, resolved template and instance show,
      doctor tri-state output, malformed state, and JSON v1 compatibility.
- [ ] Run live VM validation for create-time capture, matching preflight, deliberate identity drift,
      password-protected OpenSSH keys, safe cleanup, and independent residue verification.
- [ ] Promote all load-bearing contract and operator teaching into permanent docs, confirm no
      permanent artifact depends on this SDD path, and add `locked.md` only in the final PR.
- [ ] Record exact evidence, commit with the required session trailer, push, hand off the green
      head, and set ready only when the operator supplies merge intent.

Definition of done: all FRD acceptance criteria are true in the shipped CLI, the complete exact head
is reviewed and green, live evidence covers the SSH correctness crux, collateral is current, and the
SDD is ready to lock with the final implementation.

## Coordination and escalation

- The parent saga owns its target-state artifacts. This effort carries `saga:next-steps` on every PR
  and does not edit saga-owned files.
- Wave 4 may consume only the accepted permanent store contract. It adds its own domain payload and
  closed methods rather than a generic record API.
- Wave 6 has stated no artifact-ownership requirements. This effort preserves an extension path and
  invents no artifact schema or methods.
- Stop for authenticated operator direction if R4 needs a separate effort, if a lifecycle operation
  cannot state which slices it proves, if correct SSH comparison requires choosing agent-held
  identities, or if compatibility requires a public generic record escape hatch.

## Research disposition

External prior-art research is unnecessary for R2. The governing facts are the repository's SQLite
migration, backup, transaction, and typed-facade mechanics. SSH private-key envelope parsing will be
checked against the OpenSSH format specification during R3 design rather than inferred from local
examples alone.
