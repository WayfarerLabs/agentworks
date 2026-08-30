# Instance Model and State: Implementation Plan

<!-- cspell:ignore sdds -->

- Status: R1-R4 and merge strategy merged; R5 implemented, final verification pending
- Date: 2026-08-23
- Last revised: 2026-08-30
- Requirements: [frd.md](./frd.md)
- R1 assessment: [database-assessment.md](./database-assessment.md)
- R2 contract: [store-contract.md](./store-contract.md)
- Instance-spec CLI: [instance-spec-cli.md](./instance-spec-cli.md)
- Merge strategy: [merge-strategy-lld.md](./merge-strategy-lld.md)
- R5 design: [resolved-drift-surfaces-lld.md](./resolved-drift-surfaces-lld.md)
- Code basis: `79e555a6` on `main`
- Delivery vehicle: merged R1 artifact PR #632, merged R2 store and R4 design PR #636, merged R4
  implementation PR #670; merged merge-strategy SDD checkpoint and implementation PR #686; merged R3
  SDD checkpoint and implementation PR #700; R5 SDD checkpoint and implementation on one draft PR
  from `main`

## Delivery posture

R1 is an independently reviewed coordination artifact merged through PR #632. R2 is an independently
valuable, always-green persistence increment merged through PR #636: it establishes the store
contract needed by this effort and wave 4 without exposing the later SSH, merge, CLI, and diagnostic
risk in the same review. Its accepted R4 design artifacts remain response material for the later
implementation.

PR #670 merged the complete R4 overlay and live-publication work. The merge-strategy correction uses
one PR in two explicit stages: an SDD-only draft checkpoint under `review-requested`, followed after
checkpoint convergence by implementation on the same draft PR. The PR becomes ready only at the
complete implementation handoff. Each later delivery starts from `main` and is independently
complete, reviewed, and green. Stack only actual dependencies and merge a stack bottom-up.

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
- [x] Hand the exact green R2 head to the saga lead.

Checkpoint evidence before handoff: the project reviewer and fresh-eyes reviewer approved the
corrected tree with no remaining findings. The reproducible repository, backup, and migration
selection passed 83 tests: `tests/db/test_instance_state.py`, `tests/test_database_backup.py`, and
`tests/test_db_migration_harness_integration_state.py`. The current full Python gate passed Ruff,
formatting, strict mypy, Typer isolation, and 7,280 tests with one skip. Repository file lint,
locked-SDD, Rulesync, website tests, and deterministic website builds passed. Disposable-home
acceptance proved fresh v32 creation, repository round trips and owner cleanup, safe v31-to-v32 CLI
migration with a pre-migration backup, and no synthesized records. R2 has no live-provider boundary.

Definition of done: the shipped repository contract is sufficient for R3 and for wave 4 to add a new
named typed consumer without changing table, connection, transaction, or absence semantics. R3
implementation does not begin until the saga lead accepts this checkpoint. Accepted at `cd533a1b`.

## Phase 3: general layer stack and desired instance overlays

- [x] Finalize the R4 design from the four existing per-kind folds. Route a split for authenticated
      operator direction if one shared layer-stack mechanism cannot replace the four loops without a
      fifth instance-only merge.
- [x] Introduce one general ordered layer fold while retaining each domain's field merge semantics,
      defaulting rules, validation, and provenance.
- [x] Preserve current template clearing semantics: empty additive lists and maps do not invent a
      removal tombstone, and an overlay cannot declare `name`, `inherits`, metadata, or framework
      provenance.
- [x] Define typed per-kind overlay payloads and codecs over the shared store, with one final
      overlay layer after the template chain and no required ceremony when it is absent.
- [x] Reject JSON `null` at every depth and report each material layer decision as set, retained,
      replaced, cleared, or explicitly absent using a value-safe field-name summary.
- [x] Add inline JSON `--spec JSON` exactly where an instance template can be selected or changed:
      the four direct creation commands and `agent reinit`. Add `--workspace-spec JSON` and
      `--agent-spec JSON` to compound session creation for newly created child owners. Omit
      standalone spec mutation verbs and do not add the option to VM reinit, workspace repair,
      session resume, or workspace copy. Treat `{}` or the exact empty CLI value on `agent reinit`
      as clearing the prior layer, whitespace-only input as invalid, and omission as retaining it.
      Apply declaration-time effective-instance reference and capability validation matching
      template error quality without publishing a fake template or creating an instance manifest.
- [x] Prove scalar override, list/map merge behavior, defaults, provenance, invalid overlays, absent
      overlays, persistence, deletion, and parity across VM, workspace, agent, and session kinds.
- [x] Extend VM backup's exact snapshot and archive projection for desired overlays across the VM
      owner tree, with explicit archive versioning and safe handling for plaintext environment
      values.
- [x] Update command reference, completions, sample configuration or manifest teaching, and guide
      collateral in the same phase that makes each claim true.

Definition of done: every instance kind resolves template declarations plus an optional final
instance layer through one shared stack mechanism, and no fifth per-kind merger exists.

Completed on 2026-08-25. Two independent review passes reported no findings after the feedback/fix
loop. The final non-integration suite passed with 7,438 tests and 1 skip; Ruff, Mypy, file lint,
Rulesync, locked-SDD, website, deterministic-build, and Typer-isolation gates passed. A bounded
isolated-home shipped-CLI pass confirmed the option surface and strict validation boundaries without
contacting a provider; no authorized live-provider inventory was present.

### R4 correction: paired VM and admin final layers

- [x] Add `vm create --admin-spec JSON` beside `--admin-template`, retain unprefixed `--template`
      and `--spec` for the primary VM declaration, and add no `--vm-*` aliases or VM-reinit spec
      inputs.
- [x] Fold and validate the VM and admin layers independently, then persist them atomically as one
      typed VM desired payload that reinit and runtime access paths consume together.
- [x] Prove strict input, field merge behavior, effective references, lifecycle ordering, rollback
      and retention, backup projection, reinit reuse, value-safe reporting, and no schema migration.
- [x] Update the SDD contracts and permanent CLI/store collateral, run the full R4 quality gates,
      and repeat the private review and complexity passes for the corrected implementation.

Completed on 2026-08-26. The correction keeps `--template` and `--spec` as the VM pair, adds
`--admin-spec` beside `--admin-template`, and stores both final layers as one payload-version-2 VM
declaration while retaining flat payload-version-1 reads without a database migration. The private
project, fresh-eyes, and complexity loops finished clean after three feedback/fix rounds. Full
Python, repository, and website gates passed. A 20-invocation isolated-home shipped-CLI pass proved
the paired option surface, strict/value-safe input refusal, legacy reads, composite version-skew
classification, admin-only consumption, and unchanged stored rows using a passphrase-protected
scratch key. No authorized live-provider inventory was available, so provisioning, remote reinit,
and SSH transport remain explicit live-test gaps.

### R4 correction: live and pending resource publication

- [x] Add database-backed VMs, workspaces, agents, sessions, and consoles as typed live-resource
      publishers in the ordinary Registry collection phase before its single finalization pass; keep
      the Registry publisher-agnostic and retain one read snapshot for the publication.
- [x] Publish each live resource's effective desired references through the existing per-domain
      resolution and extraction contracts, including paired VM/admin state, without raw JSON
      scanning, provider observation, or plaintext value exposure.
- [x] Replace operation-local missing-target acceptance with prospective pending-resource
      publication for direct and compound creation, so normal finalization validates and
      auto-declares the candidate graph before mutation and a failed command leaves no publication.
- [x] Route secret list, describe, verify, doctor, and graph/used-by projections through the one
      finalized graph, removing transient fallbacks or post-finalize DB projections where the
      unified graph now owns the answer.
- [x] Prove explicit-over-auto precedence, multiple live owners, last-owner garbage collection,
      deletion and overlay clearing, missing or unreadable desired state, paired VM atomicity,
      snapshot consistency, failed-create non-publication, and the agent-env secret regression.
- [x] Update permanent architecture and operator collateral, run the full code gates, repeat the
      project/fresh-eyes/complexity review loop, and perform isolated shipped-CLI validation of
      persisted and prospective publications.

Definition of done: database-backed and pending resources enter through the same publish/collect
boundary as every other resource, one finalization pass owns reference resolution and
auto-declaration, and every inspection surface observes the same resulting graph.

Completed on 2026-08-27. The database and pending candidates are peer Registry publishers before one
finalization pass; durable absence recovery omits unavailable edges without inventing defaults, and
JSON v1 `used_by` remains compatible while ordinary graph queries expose direct live relationships.
A post-handoff correction keeps doctor non-migrating while preserving its declared-resource checks
when database publication is unavailable; the report explicitly marks the missing live-resource
coverage instead of treating the partial view as complete. Feedback round one then preserved empty
selectors as unresolved across durable, pending, runtime, and rendering paths; restored
declared-only completion fallback for typed live-publication failures; replaced authored-prose
assertions with structural checks; and pinned runtime-closure secret usage plus the database
publisher's live-kind vocabulary. Correction code head `6ad9bf2e` passed 7,813 non-integration
Python tests with one skip, and the full local suite passed 7,816 tests with one skip; Ruff,
formatting, Mypy, file lint, Rulesync, locked-SDD, website, and deterministic-build gates also
passed. The greenfield project re-review found no remaining code or test issue, the final fresh-eyes
review was clean, and Muntz reported `SHIP`. Final feedback-round code head `f1e36ed4` then made the
four create-command validation registries declaration-only while retaining their later authoritative
pending-plus-durable builds, and corrected the completion helper guidance. Its four precedence
regressions and affected suites passed; the exact CI selection passed 7,815 tests with one skip,
alongside clean static, file, Rulesync, locked-SDD, website, and deterministic-build gates. The
greenfield reviewer and Muntz reported `SHIP`, and fresh-eyes review was clean. An isolated
shipped-CLI pass proved durable overlay-only secret discovery, direct graph topology,
session-oriented usage, clearing, last-owner removal, absent and unreadable completion fallback, and
a pending disabled-resource refusal with no durable publication or provider work. No authorized
provider inventory was available, so successful provider-backed creation remains covered by unit and
review evidence rather than a live backend run.

### R4 correction: schema-directed merge strategies

- [x] Amend the R4 requirements and architecture so core model definitions and capability model
      definitions that participate in layered merging, rather than five domain policy reducers or
      capability callbacks, own merge behavior at arbitrary nesting depth.
- [x] Define and registration-validate one closed strategy vocabulary with object merge,
      append-deduplicated list, and scalar replacement defaults plus field-level and model-level
      overrides, mapping-value annotations, and rejection of duplicate or shape-incompatible
      placements.
- [x] Implement recursive object merge, whole-node replacement, stable list append-deduplication,
      type-sensitive structural JSON equality, union-arm replacement, later raw replacement for
      unknown conflicts, cycle-safe malformed-input totality, and value-safe nested provenance in
      one schema walker used by template inheritance and final instance layers.
- [x] Migrate `ProvenancePath` end to end across `LayeredResolution`, every `LayerContribution`
      constructor, `run_layer_fold` defaults and accumulation, all five `effective_references`
      consumers, declared-template finalize, session pending, live, and lifecycle validation, and
      the capability error bridge. Project selector and config paths into one capability-local map,
      preserve optional source-location framing, use replacement for a new list index and
      contribution for an equal existing index, and use one longest-prefix lookup throughout. Retain
      no value- or `repr`-based path and no second session ownership channel.
- [x] Reduce VM, admin, workspace, agent, and session reducers to authored-field and seed adapters;
      replace same-integration capability callbacks with registered config-model policy while
      retaining complete config reset when the integration selector changes. Increment the
      harness-integration capability contract from version 1 to version 2 and refuse old plugins
      clearly rather than bridging two merge authorities.
- [x] Annotate every existing non-default field so shipped core and capability behavior remains
      compatible, including whole-entry environment replacement and replacement argument lists.
- [x] Prove nested core and capability behavior, explicit empty replacement, containing replacement
      before arm selection, same-arm field-over-model merge, same-arm model replacement,
      different-arm or selection-failure reset despite containing merge, registration refusal,
      invalid-input preservation, typed equality, normalized validation locations, result-index list
      references, inherited contributors at newly materialized paths, uniform validation-alias
      refusal through nested mapping values and replacement boundaries, unsafe append-dedupe-schema
      refusal, non-string merge-key refusal with replacement escape, validator-admitted carrier and
      non-carrier values, an unsupported Python object whose equality raises, non-finite floats,
      cyclic items, and the absence of persistence or CLI schema changes. Mutation-test exactly
      object replacement, list replacement, union reset, and longest-prefix attribution.
- [x] Update permanent schema, capability, harness-integration, ADR, and active upgrade collateral
      in the same implementation range that removes the imperative hook and increments its contract;
      document shipped atomic list-item behavior in the schema README and preserve model-declared
      list-item identity as unsupported future direction in ADR 0023.
- [x] Complete the SDD-only checkpoint review, then the implementation private review, fresh-eyes,
      complexity, test-quality, full-gate, and isolated shipped-CLI passes before merge intent.

Completed on 2026-08-28. One iterative schema walker now owns nested field behavior and value-path
provenance across the five core layer adapters and opted-in capability config. Harness integration
contract version 2 removes its executable merge callback; existing argument-vector and environment
replacement behavior is model-declared, and no database, desired-payload, or CLI contract changed.
The private project, fresh-eyes, and complexity loops converged with no material finding. All four
required safety mutations were killed by focused tests: forcing model replacement to merge failed
`test_model_replacement_discards_the_complete_previous_subtree`; forcing marked list replacement to
append failed `test_objects_recurse_lists_dedupe_and_marked_lists_replace`; removing union reset
failed `test_different_or_unselectable_union_arms_replace_whole_values`; and disabling prefix
seeding failed `test_a_new_contribution_seeds_sources_from_its_longest_prefix`. The exact
non-integration suite passed 7,948 tests with one skip, alongside Ruff, formatting, Mypy, Typer
isolation, file lint, Rulesync, locked-SDD, website, and deterministic-build gates. An isolated-home
shipped-CLI pass proved nested core list/map merging, complete environment-entry replacement,
same-integration harness finalization, merged-list error indexing, bounded malformed-input recovery,
empty live tables, and complete scratch cleanup. No provider inventory was authorized, so provider,
VM, SSH, and session-launch behavior was deliberately not exercised; this correction does not change
those surfaces.

Definition of done: every template and instance layer resolves through one schema-directed field
policy at arbitrary depth; object replacement is an honest subtree boundary; capability authors use
their config models rather than executable merge hooks; current shipped fields retain their
behavior; and no database migration, payload-version change, or new operator syntax is introduced.

## Phase 4: R3 lifecycle evidence and SSH proving slice

- [x] Complete the authoritative OpenSSH research and low-level design in `prior-art-research.md`
      and `applied-state-ssh-lld.md`, including the password-protected-key, transaction,
      comparison-boundary, backup, and ssh-agent non-goal decisions.
- [x] Add domain-owned versioned codecs for the row-backed successful hardware-request snapshot and
      provisioned SSH identity slices, without storing secrets, private key bytes, passphrases, or
      duplicate CPU, memory, disk, and swap values.
- [x] Parse the authoritative public blob directly from `openssh-key-v1` private files and derive
      the OpenSSH SHA-256 fingerprint without consulting an adjacent public key or spawning a
      passphrase prompt.
- [x] Compare the configured public and private identities before remote application when both are
      verifiable, so a successful public-key write cannot create a false private-identity record.
- [x] Represent encrypted formats that cannot expose an identity non-interactively as unverifiable,
      not mismatch or unsupported; leave ssh-agent selection unresolved.
- [x] Make authorized-key reconciliation return a typed applied/unproven outcome instead of
      inferring proof from lifecycle success or unrelated warnings.
- [x] Validate the configured public/private pair on reinit after cheap declaration checks and
      before activation, secret resolution, or transport work, while retaining the fresh validation
      at the final remote write.
- [x] Capture only successful configuration-snapshot slices whose required evidence is established
      by VM create and reinit operations, remove stale SSH proof after an unproven remote write or
      unstable final identity, and write lifecycle-row plus applied-state changes in one honest
      transaction where they compose.
- [x] Document the one-time reinit required for historic VMs and emit cautious recovery guidance
      immediately when the final admin key write leaves SSH evidence unproven.
- [x] Add preflight comparison before SSH transport and structural diagnostic facts for not
      recorded, unverifiable, match, and drift without remediation.
- [x] Extend the VM backup projection with the non-secret R3 applied records and prove the archive
      does not silently omit the hardware or SSH provenance it claims to preserve.
- [x] Prove matching, replaced private key at the same path, stale or mismatched adjacent public
      key, encrypted OpenSSH key, encrypted legacy PEM, missing/unreadable key, absent historic
      state, successful capture, failed-operation non-capture, atomic lifecycle behavior, and
      diagnostic non-disclosure without prose-policing assertions.
- [x] Publish the payload and comparison contract in the permanent store documentation.

Implementation evidence: the complete non-integration suite passed 8,045 tests with one skip. Ruff,
formatting, Mypy, Typer isolation, file lint, Rulesync, locked-SDD, website, and deterministic build
gates also passed. The first independent implementation review found no production blocker but
identified stale checkpoint-construction surface, missing high-risk lifecycle and backup cases, and
an incomplete permanent contract. The implementation now derives persistence from the exact
post-write proof, removes unused API and comparison surface, and directly covers the identified
password-protected, instability, atomicity, deletion, malformed-backup, and diagnostic
non-disclosure cases. A subsequent review identified missing early reinit validation and transition
guidance. Reinit now refuses an invalid configured identity before activation or transport, and the
release guide plus permanent CLI documentation explain the one successful reinit historic VMs need
and the recovery choices when their installed key no longer works.

Definition of done: the identity established by the successful authorized-key write is recorded and
compared with the identity the current transport will present, the successful VM provisioning
request is recorded without claiming provider-observed hardware, and no password-protected-key path
regresses.

## Phase 5: R5 resolved-spec and drift surfaces

- [x] Extend the focused `resource show` service with fully resolved template values and path, map
      key, or list-item provenance sufficient to distinguish declared, inherited, defaulted, and
      overlaid contributors truthfully.
- [x] Extend existing VM, workspace, agent, and session `describe` with current declared resolution,
      lifecycle evidence, and explicit not recorded, match, drift, or unverifiable comparison state
      in one structural read snapshot.
- [x] Add doctor batch reads, owner-existence validation for orphaned records, visibility for
      unconsumed newer-release records, and structural SSH drift checks without opening a sidecar or
      repeating one query per instance.
- [x] Preserve JSON v1 fields, add tagged `instance_state` to every current live-description
      producer under additive compatibility, and keep human and JSON facts reconciled without
      prose-policing tests.
- [x] Prove effective CPU, memory, disk, and swap requests are available before `vm create`;
      template and instance provenance; recorded/current comparison; batch query behavior; and no
      overlay ceremony in the simple case.
- [x] Update permanent resource, machine-output, doctor, command-reference, and guide collateral.

Definition of done: an operator or agent can inspect effective pre-mutation specs and honest drift
between current declarations and recorded lifecycle evidence, including visible ignorance, from the
supported CLI surfaces. Hardware evidence describes the successful provisioning request, not
provider-realized hardware.

## Phase 6: complete verification and closeout

- [x] Run focused tests after every phase and the full gate on the complete exact head.
- [ ] Run equal-capability project review, independent fresh-eyes review, and the saga review
      campaign scaled to the final blast radius; resolve every material finding.
- [x] Run isolated-home CLI acceptance for overlay declaration, resolved template and instance show,
      doctor comparison output, malformed state, and JSON v1 compatibility.
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
