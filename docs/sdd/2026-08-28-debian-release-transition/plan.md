# Plan: Debian Release Transition

- Status: Operator-directed simplification in progress
- Date: 2026-08-28
- Governing artifacts: `frd.md`, `hla.md`, `prior-art-research.md`, and `migration-strategy.md`

The assisted-upgrade and managed-checkpoint direction was superseded on 2026-09-01. Completed
checkboxes below remain unchanged as the truthful record of work performed; the target design no
longer retains those production surfaces. The checked platform-local attestation item is likewise
superseded; the surviving design attests once in core.

## Delivery posture

This is a standalone significant effort. The SDD lands for review before implementation begins.
Implementation uses independently green merge units for the database migration, package catalog,
six-provider release contract, and live certification. Stacked provider branches can make review
parallel, but they are review inputs rather than independently mergeable changes when the shared
contract would leave any provider broken.

The effort lead owns the FRD/HLA rulings, shared release and persistence contracts, explicit release
adoption, and final closeout. Platform work can proceed in parallel only after the common
request/result and selector-map contracts are reviewed. Each worker owns disjoint provider files and
tests and is told that concurrent changes belong to other agents.

No implementation PR may introduce an operator OS/release/image selector, Agentworks-managed
provider checkpoint, distribution upgrade executor, arbitrary release graph, or multi-hop upgrader.

## Full gate

Each implementation PR runs its focused tests and repository lint. Before a PR is ready to merge,
run from `cli/`:

```console
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy agentworks
```

Run the repository's required lint/generation gate from the root, including docs, generated schemas,
completion metadata, and manifest parity. The exact command follows the current contribution guide
at implementation time.

The final cutover additionally runs the live integration matrix under the `integration-testing` and
`agw-test-env` protocols. Cloud or Proxmox resources use the operator-provided environment
inventory, names, cost budgets, recovery prerequisites, and cleanup rules. A platform does not
become certified from mocks alone.

The exact green merge-intent handoff triggers the external integration-test and PR review lanes. Any
resulting correction returns the PR to draft before the head changes, then publishes a new green
ready handoff. Live evidence and its authenticated disposition remain merge gates even though the
ready transition starts those lanes.

Every PR gets an independent `agentworks-reviewer` lane and a `muntz` complexity pass before the
first operator handoff. Capability-spanning implementation PRs also get a fresh-eyes correctness
review. Review corrections stay within the relevant phase and do not add another configuration
authority, upgrade graph, or recovery lifecycle.

## Phase 0: SDD review

Deliver the draft artifact set:

- [x] `frd.md`
- [x] `hla.md`
- [x] `prior-art-research.md`
- [x] `migration-strategy.md`
- [x] `plan.md`

- [x] Open a draft pull request labeled `sdd:debian-release-transition` and use `review-requested`
      for the checkpoint cycle. Keep the PR as a no-merge-intent review vehicle, remove the
      checkpoint label while revising, reapply it only for coherent handoffs, and remove it when the
      bounded cycle closes.

Exit criteria:

- [x] FRD requirements and non-goals reflect the operator's Debian-only, Trixie-only-create,
      persisted release, release-map, and `vm upgrade` rulings.
- [x] HLA has no unowned provider artifact lifecycle or implicit recovery promise.
- [x] Prior art uses primary sources and accounts for every current platform.
- [x] Migration strategy gives existing rows, Proxmox config, operator APT resources, Bookworm
      support, and partial upgrade states a deterministic path.
- [x] Independent review finds no unresolved blocker or unjustified complexity.

The 2026-08-29 operator amendment supersedes the first draft's calendar-bounded Bookworm policy and
pair-specific platform wording. The completed boxes above remain the truthful record of that first
draft and review.

- [x] Revise the FRD, HLA, prior-art disposition, migration strategy, and implementation tracker for
      relative current/current-1/current-2 support and the reusable adjacent-release mechanism.
- [x] Specify the explicit core-to-platform create value, vm-platform contract version cutover,
      missing-release failures, and base plus vm-platform capability README updates.
- [x] Run fresh independent project and complexity reviews on the material amendment and resolve
      every material finding.

After these amendment checks pass, the exact pushed head re-enters the remaining authorized draft
`review-requested` round. The PR label and signed checkpoint comments record that published cycle
without requiring a post-review documentation-only bookkeeping commit.

## Phase 1: Core release observation, persistence, and APT values

Implementation note (2026-08-30): the implementation branch is the atomic cutover candidate, so
Trixie is active in its registry. It is not ready to ship until the Phase 5 live matrix and final
reviews pass; the unchecked certification items below are deliberate.

### Implementation

- [x] Add the typed ordered Debian release/profile/parser module and relative support classifier.
      Derive current only from the final active profile and use explicit candidate-profile fixtures
      to prove future promotion without introducing another current authority.
- [x] Add nullable release and observation-time columns through the ordinary forward migration,
      enforcing pair-null state without enumerating codenames in SQL.
- [x] Update the exact historical schema inventory used by safe database restore.
- [x] Extend `VMRow`, converters, repository accessors, and backup serialization.
- [x] Add the shared live observation service and typed mismatch/unsupported errors.
- [x] Add release facts to VM list/describe and additive JSON v1 projections after the names-only
      short circuit.
- [x] Add doctor findings for unknown, recorded/live disagreement, previous-release upgrade
      availability, and legacy best-effort status.
- [x] Emit one warning from the named-VM operation boundary before accessing a recognized legacy
      release without blocking an ordinary operation merely because of release position.
- [x] Extend `apt-source` with mutually exclusive scalar `source` and release map `sources` forms.
- [x] Reject codename-bearing scalars and missing selected mappings before mutation.
- [x] Convert shipped HashiCorp, tofuutils, and ngrok resources to reviewed maps; keep invariant
      vendor sources scalar.
- [x] Thread verified release into initializer APT resolution and preserve graph/provenance
      behavior.
- [x] Update resource schemas, generated samples, plugin manifests, parity oracles, permanent
      teaching, machine-output contracts, command references, and user manifest errors.

### Focused verification

- [x] Cover migration from every supported historical schema shape.
- [x] Prove there is no guessed legacy backfill.
- [x] Cover the parser matrix for Debian, contradictions, missing fields, and non-Debian guests.
- [x] Cover observation insert/refresh/mismatch semantics.
- [x] Prove unknown rows remain usable for release-insensitive operations.
- [x] Prove list names-only remains probe-free.
- [x] Prove JSON values are recognized codenames or null and a future registry value needs no SQL
      shape change.
- [x] Cover scalar/map schema, Bookworm/Trixie selection, and vendor-specific ngrok behavior.
- [x] Prove resolution uses observed VM release rather than current creation release.
- [x] Prove a missing mapping fails before key/source writes.
- [x] Prove current, previous, and legacy classifications derive from positions in the one profile
      registry; appending a profile changes tiers without persisted rewrites or calendar input.
- [x] Prove a release absent from the active registry refuses release-sensitive mutation with a
      supporting-build diagnostic rather than creating an ahead-of-current tier.
- [x] Prove legacy ordinary operations warn and continue to their concrete checks, while
      `vm upgrade` refuses without attempting a multi-hop path.
- [x] Cover built-in/plugin parity and generated sample/explain surfaces.
- [x] Prove through config/schema/CLI searches that there is no public release selector.

### Exit criteria

- [x] The database faithfully represents unknown and every registry-recognized observation, and
      Phase B selects the observed release independent of current creation policy. No later phase
      hides release state in platform metadata, persists support position, or lands against a
      release-unaware APT consumer.

## Phase 2: Trixie-safe staging

### Implementation

- [x] Move VM backup and workspace-copy size-unbounded staging out of `/tmp` into secure disk-backed
      directories, preserving cleanup on success, failure, and interrupt.
- [x] Inventory remaining active guest `/tmp` uses and record why each is bounded or move it.

### Focused verification

- [x] Exercise backup and workspace copy with payload larger than a constrained `/tmp` tmpfs.
- [x] Prove interrupt cleanup and no world-readable staging.

### Exit criteria

- [x] Trixie tmpfs behavior cannot break large Agentworks transfers.

## Phase 3: Platform current-release creation

> **Supersession (2026-08-31):** The completed version 2 to 3 and later version 4 boxes below record
> truthful intermediate work, but their compatibility-cutover design is superseded. Vm-platform is
> internal, so the shipping descriptor and all six bundled implementations retain one complete
> contract at version 1 and mutate atomically. Completed references to external or third-party
> vm-platform implementations are superseded by that same internal-only boundary.

### Shared contract

- [x] Bump the vm-platform kind and every implementation from contract version 2 to 3; add required
      `debian_release` to `ProvisionRequest` and verified release to `ProvisionResult`.
- [x] Add the create-time shared release probe helper.
- [x] Keep core as the only owner of current and pass its concrete value in every manager-built
      request; platforms have no local current default.
- [x] Add the shared release-map resolver and focused errors: code-owned misses name the outdated
      platform/plugin, while operator-owned misses name the exact vm-site release key. Resolve
      before backend mutation and never fall back.
- [x] Preserve both typed release-map failures and their remediation hints through the manager's
      provisioning wrapper after provisional-row unwind.
- [x] Require every platform to verify the guest before leaving its backend rollback window.
- [x] Update `cli/agentworks/capabilities/README.md`,
      `cli/agentworks/capabilities/vm_platform/README.md`, `cli/agentworks/plugins/README.md`, the
      kind's topic prose, and the request, result, and `create` docstrings in the same merge unit as
      contract version 3.

After the shared contract is reviewed, provider work splits by ownership on stacked review branches:

- [x] **Lima/WSL:** implement the release-keyed Debian cloud image block, derived OCI
      tag/cache/diagnostics, and local create tests.
- [x] **AWS/Azure:** implement the SSM release mapping, Azure full image record/disk floor, and
      cloud create tests.
- [x] **GCP:** implement the release and architecture image-family map and GCE create tests.
- [x] **Proxmox:** implement release-keyed template config, the legacy Bookworm adapter, setup
      script, and guest validation.

Workers do not edit the shared request/result contract after it is handed off. Any required contract
change returns to the effort lead and is reviewed once across all providers. The shared contract and
every provider implementation then enter the merge branch as one green unit; no merge commit leaves
a provider unable to construct or honor the required request.

### Focused verification

- [x] Prove every exposed architecture has an official Trixie selector.
- [x] Prove contract version 2 platforms fail registration with an incompatibility error.
- [x] Prove a contract-current missing code-owned mapping fails before backend mutation with the
      platform-update hint and no fallback.
- [x] Prove a mismatch raised by the shared in-window verifier triggers backend cleanup and no
      success row.
- [x] Prove a nonconforming returned mismatch persists backend identifiers, retains one failed and
      uninitialized row that `vm delete` can address, and returns the typed delete/retry diagnostic.
- [x] Prove the manager supplies current and persists only the matching returned verified value
      before Phase B.
- [x] Prove existing platform metadata and existing VM operations stay compatible.
- [x] Prove the Proxmox legacy scalar cannot satisfy a current-release create and a missing current
      template names `template_vmids.<release>`.
- [x] Prove no arbitrary provider image field appears in configuration.
- [x] Review both capability READMEs and the plugin-author README for the
      request/map/failure/verification/plugin contract without adding tests that pin authored
      wording.

### Exit criteria

- [x] All providers honor one version 3 current-release contract and are code-complete for Trixie,
      while the product cutover remains held until live certification in Phase 5.

## Phase 4: Durable `vm upgrade`

### Slice 1: plan and recovery gate

- [x] Add the thin `vm upgrade NAME [--checkpoint REF]` CLI and VM-name completion.
- [x] Build the manager boundary that resolves site credentials, canonical/native routes, SSH
      identity, and Tailscale rejoin secret before mutation.
- [x] After activation, scan the fixed journal root before new-upgrade eligibility. Resume or
      diagnose exactly one incomplete validated adjacent pair through its retained target policy,
      even after a later profile append; refuse multiple journals and never start a second beside
      one.
- [x] With no incomplete journal, derive source and target from the final two registry profiles.
      Return already-current for current, select only previous-to-current, and refuse legacy with
      fresh-VM/data-copy guidance and no multi-hop attempt.
- [x] Implement live release/database reconciliation and the externally completed adjacent adoption
      path, with Bookworm-to-Trixie as the first instance.
- [x] Implement session quiescence and Debian preflight checks from the transition policy.
- [x] Produce a preliminary simulated removal/source plan without mutation.
- [x] Create the ordinary VM backup and focused Debian recovery bundle with owner-only local
      storage.
- [x] Require an external checkpoint reference that identifies an actual recoverable artifact, audit
      it, obtain explicit checkpoint/console attestation, and separately obtain operator consent to
      bring the source release current within its suite.

- [x] Focused tests prove every unsafe condition fails before source mutation and neither local
      backup is misrepresented as a bootable checkpoint.

### Slice 2: package state machine

- [x] Implement the root-owned `{source}-to-{target}` persistent directory, atomic action journal,
      private orchestrator lock, script, and log. The validated directory alone owns the
      source/target pair. `plan.json` stores the computed plan and `state.json` records intent
      before mutation plus `last_completed_action`, active action, attempt identity, and attempt
      outcome, with no duplicate pair fields. Database/init state remains authoritative for product
      health. Every journal writer takes the same lock, and the package service holds it for each
      remote action.
- [x] Detect native dpkg/APT locks and automatic update services. Fail closed on another package
      owner; durably record and inhibit known automatic timers. Restore their prior state only after
      a source-safe abort or verified healthy target completion; retain inhibition on a
      mixed/unhealthy state until forward repair or external restore.
- [x] Bring the source release current under the detached systemd service, rerun every health check,
      and reopen the planning boundary. Recompute the full package/source/removal plan from current
      state, display drift from the preliminary plan, and require a second explicit confirmation
      before changing suites.
- [x] Classify, archive, and disable third-party sources; generate canonical target deb822 sources
      from the selected adjacent policy, then run its minimal and full package actions.
- [x] Implement inspection/resume after interruption inside every remote action, SSH loss, and
      package failure. A resume inspects native locks, unit state, active attempt, logs, and
      postcondition checks before deciding whether to continue, retry, or request repair.
- [x] Preserve sources/logs and fail with forward-repair or external-restore guidance; do not
      restore source-release entries onto a mixed package state.

- [x] Focused tests use a fake transport/systemd boundary and real temporary filesystem state. They
      kill the client inside every action, assert durable intent precedes mutation, exercise plan
      drift, checkpoint attestation, and both mutation confirmations, prove one native package
      owner, distinguish safe timer restoration from retained mixed-state inhibition, prove lock
      exclusion for journal writes and reboot dispatch, and resume without replaying a completed
      action.

### Private-review corrections

- [x] Reject unsafe links, ownership, or modes at the fixed journal root, pair directory, lock,
      plan, and state boundaries; fence completion, failure, retry, and repeated reboot-dispatch
      writes with the active attempt identity.
- [x] Keep every automatic APT timer stopped until the complete recorded configuration is restored,
      validate the exact owned timer inventory before root mutation, and persist a repair-required
      event whenever restoration cannot be proved, including operator cancellation.
- [x] Isolate target simulation from guest APT configuration and hooks through a primary scratch APT
      config, and aggregate conservative requirements for shared or separate `/`, `/var`, cache, and
      `/boot` filesystems.
- [x] Parse enabled binary and source-only `.list`/deb822 stanzas, require exact official Debian URI
      hosts, and require every policy target suite at convergence.

### Slice 3: reboot, reconnect, and convergence

- [x] After the package unit installs the target release's udev rules, predict interface names and
      block reboot with pinning guidance if connectivity would be unsafe.
- [x] Take the same journal lock, record intent, dispatch reboot inside its critical section, and
      open a fresh post-reboot operation span.
- [x] Add strict provider/SSH reconnect rather than warning-and-continue behavior.
- [x] Use native transport and explicit Tailscale rejoin where current platform capability permits.
- [x] Record a local `repair-required` outcome with checkpoint/console guidance when no path
      returns; the unreachable guest cannot authoritatively record its own outcome.
- [x] Persist the target immediately after proof, then run health checks and release-aware Phase B.
- [x] Use existing init state plus a `repair-required` event for later convergence failures without
      database rollback; do not duplicate Trixie observation or Phase B completion in remote state.
- [x] Add permanent operator recovery and resume documentation for every stage.

### Exit criteria

- [x] The orchestrated command is safe to interrupt inside every remote action, uses one lock for
      every journal writer and reboot dispatch, never runs two package managers, restores automatic
      updates only from a provably safe package state, never claims automatic rollback, and
      distinguishes OS transition success from later initialization health.

## Phase 5: Certification and cutover

### Live matrix

- [ ] Run the authenticated integration environment for each available platform and supported
      architecture, recording artifact identity, architecture, test resource names, budgets,
      operation transcript, and cleanup evidence.
- [ ] Prove artifact resolution, boot, live release observation, and delete for every exposed
      architecture.
- [ ] Prove an ordinary Trixie lifecycle on every available platform and explicitly exclude Debian
      package upgrades from product certification.
- [ ] Exercise matching `vm confirm-release` observation and separate `vm reinit` on a disposable
      representative VM without changing its distribution.

Across the matrix, also exercise:

- [ ] Exercise Trixie create and live release verification.
- [ ] Exercise Phase B and a selected release-mapped source where available.
- [ ] Exercise shell/exec, workspace operation, backup, reboot/reconnect, and delete.
- [ ] Exercise a large transfer with Trixie `/tmp` behavior.
- [ ] Exercise the shared release-mapped APT and constrained-`/tmp` behavior at least once.
- [ ] Prove DSA rejection and supported SSH-key success.
- [ ] Prove `~/.pam_environment` independence and continued `/etc/sysctl.d` application.
- [ ] Prove network-interface-name and deb822 source assumptions.

A platform failure blocks its Trixie certification. It does not authorize Bookworm fallback, a
custom-image escape hatch, or relaxed release validation. The disposition returns to the operator if
the product cannot ship consistently within the agreed scope.

### Final cutover

- [ ] Certify the already-appended Trixie profile as current by registry position and prove no
      public or platform-local path can create Bookworm.
- [x] Enable the relative support classifier and named-VM legacy warning; prove Bookworm is previous
      at this cutover and that no wall-clock value changes its tier.
- [x] Add the superseding Debian-release ADR and mark ADR 0002 superseded without rewriting history.
- [x] Update README, CLI reference, platform/resource guides, config samples, schemas, completions,
      release notes, and release-specific support/recovery teaching. Contract-author teaching
      remains owned by the Phase 3 version-cutover merge unit.
- [x] Search current code/docs for unaccounted release literals, platform-local current defaults,
      Bookworm-create claims, unbounded archive staging in `/tmp`, and VM-backup rollback claims.
- [x] Prove a synthetic release-promotion fixture can add mapping values and append one successor
      profile with its upgrade-from-previous policy, changing current and adjacent policy selection
      without a second current authority, codename-specific database migration, new create field,
      CLI target selector, or arbitrary upgrade graph. Treat this as a structural no-hardcoding
      proof, not a compatibility claim or a promise of unchanged workflow internals.
- [x] Run full unit/static/docs/generated gates and the final independent review lanes.
- [x] Record authenticated operator direction that an exact green ready handoff triggers external
      integration testing and complete PR-level review, with up to four feedback/fix rounds using
      the draft/ready handoff protocol.

### Ready-triggered feedback round 1

- [x] Resolve the full integration report's source-permission and false action-failure blockers,
      batch its accepted resume, repair, reboot, secret-resolution, and collateral findings, and
      retain the approved interactive and legacy-site boundaries.
- [x] Replace the duplicate remote journal scanner with one temporarily staged execution of the
      canonical guest journal implementation.
- [x] Reconcile current `main`, run the complete gate and independent review lanes on the corrected
      exact head, then publish the signed round 1 of 4 ready handoff for external revalidation.

### Ready-triggered feedback round 2

- [x] Critically accept operation-specific early Proxmox mapping validation while preserving config
      loading and best-effort existing-VM operations for legacy sites.
- [x] Pass the concrete core-selected release through pending-create preflight, resolve the Proxmox
      template through one shared lookup, and fail before secret resolution or authenticated `runup`
      when the mapping is missing.
- [x] Correct the governing HLA to record the operation-specific preflight seam and the Proxmox
      setup script's no-selector current-release behavior.
- [x] Reconcile current `main`, run the complete focused and repository gates, and obtain exact-head
      project, correctness, and complexity reviews before the signed round 2 of 4 ready handoff.

### Operator-directed feedback round 3

- [x] Record that core requests only its single current release for creation, while older entries in
      an operator-owned catalog do not create a release-selection surface.
- [x] Attest a cloned Proxmox guest through QEMU guest-agent `/etc/os-release` before Agentworks
      bootstrap, retain the final post-bootstrap probe, and expose no verification bypass.
- [x] Prove wrong, unreadable, and mismatched templates roll back before bootstrap while successful
      creation executes both attestation boundaries.

The operator's core-boundary correction supersedes the platform-authored final observation above;
the completed boxes remain the truthful record of the first round-three implementation.

- [x] Remove the platform-authored release field from `ProvisionResult`; core independently probes
      the returned transport against its pre-dispatch current release and persists only that live
      observation, even if a plugin mutates its request object.
- [x] Retain failed, addressable VM state when core attestation fails after platform success, before
      Phase A or release persistence.
- [x] Keep Proxmox's early QEMU-agent guard but remove its redundant post-bootstrap platform probe;
      core owns the final attestation for built-in and third-party platforms alike.
- [x] Run the complete focused and repository gates, then obtain exact-head project, correctness,
      and complexity reviews before the signed round 3 of 4 ready handoff.

### Operator-directed live-upgrade correction

The historical completed Slice 1 label remains unchanged under the plan's immutability rule. The
current design, implementation, tests, and operator guidance use the plain requirement that all
sessions must be stopped.

- [x] Fail before expensive package preflight when any VM session is not stopped, name the blocking
      sessions in operator language, and provide the exact VM-filtered stop and verification
      commands.
- [x] Warn about named-console live-state loss while preserving console definitions, and make every
      preflight group, durable package action, reconnect, and post-reboot verification visibly
      progress.
- [x] Update the governing FRD/HLA and permanent upgrade guide, prove the behavior without testing
      authored prose, run the proportionate gates and independent review lanes, and push the signed
      checkpoint while keeping PR #702 in draft.

### Operator-directed managed checkpoint expansion

The operator replaced the manual external-artifact attestation with an Agentworks-owned checkpoint
lifecycle after live Lima testing showed that the old prompt could not provide a consistent or
restorable safety boundary. This is an explicit scope amendment to the governing draft, not
incidental implementation work.

- [x] Amend the FRD, HLA, migration strategy, and provider research for one managed checkpoint per
      VM, flat second-object CLI commands, automatic upgrade acquisition, explicit restore/delete,
      and the vm-platform version 4 hard cutover. Complete a draft `review-requested` design cycle
      before implementation.
- [x] Add forward migration 34 and its exact schema sentinel without modifying already-applied
      migration 33. Persist the one-slot checkpoint lifecycle, provider descriptor, adjacent upgrade
      pair, immutable recognized Debian capture release, operation identity, and canonical effective
      desired-state fingerprint; derive purpose from the adjacent pair, include it in database
      backup projection, and prove version-33 to version-34 plus fresh-ladder migration.
- [x] Add mandatory vm-platform create/list/restore/delete operations and implement replay-safe,
      identity-preserving checkpoints for Lima, WSL2, AWS EC2, Azure VM, GCP GCE, and Proxmox,
      including provider ownership checks, destructive-restore intermediates, permission teaching,
      and exact contract/conformance coverage.
- [x] Implement `vm create-checkpoint`, `vm list-checkpoints`, `vm restore-checkpoint`, and
      `vm delete-checkpoint` with the flat second-object convention, one-slot enforcement,
      stopped-VM and stopped-session gates, confirmations, names-only/JSON output, describe
      projection, completion, and VM-delete cleanup.
- [x] Remove `vm upgrade --checkpoint` and manual recovery attestation. Create the managed offline
      checkpoint after both local backups and before the first package mutation; block unrelated
      checkpoints, require database/journal/provider agreement on resume, retain the checkpoint
      after success, and keep restore explicitly operator-directed.
- [x] After restore, independently attest and persist the live Debian release, mark initialization
      for reconciliation without rolling back desired declarations, return the VM to stopped state,
      and preserve the checkpoint until explicit deletion. Refuse before provider mutation when the
      current effective declarations, including inherited templates, transitive release-resolved
      resource specs, and the complete authorized-key and non-secret site identity, differ from the
      capture fingerprint evaluated against the checkpoint's immutable capture release.
- [x] Add narrow shared/exclusive per-VM operation exclusion around checkpoint lifecycle, the
      complete upgrade, and Agentworks VM/session entry points that could race an offline provider
      operation. Preserve concurrency among ordinary shared holders and keep general ordered
      cross-resource locking as separate follow-up work.
- [x] Update capability, vm-platform, plugin-author, platform setup/permission, CLI reference,
      upgrade guide, schema/sample, completion, topic, and release-note collateral in the same merge
      unit. Do not add a checkpoint resource kind, operator name/selector, arbitrary retention
      engine, clone API, automatic rollback, or general cross-resource locking framework.

### Operator-directed integration feedback round 1

- [x] Renumber the internal vm-platform descriptor, all six bundled implementations, fixtures,
      focused tests, topic prose, permanent capability docs, and current SDD to one complete version
      1 contract. Preserve the completed intermediate boxes above as history and add no external
      compatibility adapter.
- [x] Use typed Azure SDK request models for snapshot, replacement-disk, VM-update, and disk-tag
      operations; prove their ARM serialization shape with provider tests.
- [x] Inventory provider checkpoints before claiming a fresh create row. Keep ordinary checkpoint
      and VM deletion reconciliation-first and blocking; add explicit forced disowning only after
      cleanup failure, with confirmation, residue/billing warnings, compare-and-delete, and an
      atomically recorded distinct audit event.
- [x] Separate provider lifecycle state from derived restore eligibility in list, describe, and JSON
      output, including live provider inventory proof. Keep desired-state drift a hard restore
      refusal with no force bypass and preserve the names-only fast path.
- [x] Run a read-only upgrade-checkpoint viability pass before creating backup artifacts, disclose a
      reused checkpoint's creation time, and name the retained billed checkpoint plus deletion
      command after successful upgrade.
- [x] Reject partial WSL exports before destructive restore, announce AWS's required temporary start
      while preserving a primary restore error across stop cleanup, and use a 3600-second Proxmox
      checkpoint task timeout.

### Operator-directed Lima VZ checkpoint correction

Live operator testing established that the Lima VMs requiring upgrade use Lima 2.2.0's default VZ
driver, whose native snapshot methods are unimplemented. The operator authorized up to three
additional feedback/fix rounds and provided direct `msm4` access to the integration tester.

- [x] Amend the FRD, HLA, prior-art research, capability contract teaching, and permanent upgrade
      guide for driver-aware Lima checkpoints. Keep Lima's host-appropriate default and add no
      driver setting or unsafe checkpoint bypass.
- [x] Preserve native QEMU snapshots. Implement a stopped, protected VZ recovery clone with
      deterministic owned names, additional-disk refusal, a retained pre-first-restore emergency
      instance, operation-marked replay-safe atomic sibling-directory swaps, complete cleanup, and
      visible progress. Do not use Lima 2.2's file-by-file public rename implementation. Permit a
      persisted interrupted restore to reconcile through its temporary missing-name window without
      permitting restore of a running VM.
- [x] Harden the VZ correction after exact-head review: use an exact-path exclusive rename primitive
      on the Lima host, refuse SSH-placed VZ creation and restore before mutation, fail closed on
      hidden host entries and unreadable partial clones, validate a completed replay before cleanup,
      permit deletion of an owned unprotected interrupted clone, and exercise the real forward path
      at both atomic interruption boundaries.

### Operator-directed assisted-upgrade removal

The operator accepted the exact-head complexity finding on 2026-09-01. Assisted upgrades and managed
checkpoints are superseded before merge. The retained release model, Trixie create path, and
release-aware initialization now support an operator-led transition without owning Debian or
provider recovery.

- [x] Remove `vm upgrade`, its remote journal and package state machine, all checkpoint core and
      provider surfaces, operation guards introduced only for them, and experimental upgrade gates.
- [x] Keep migration 34 immutable and add migration 35 that drops only an empty checkpoint table,
      refusing without data loss when ownership rows remain.
- [x] Add `vm confirm-release NAME [-y|--yes]`; atomically adopt a recognized changed observation
      and set existing initialization status pending, but leave `vm reinit` separate.
- [x] Remove every Debian release and upgrade addition from doctor so doctor performs no live VM
      probe, activation, or network wait.
- [x] Rewrite the governing SDD, ADR, operator guide, capability docs, command reference, samples,
      permissions, completions, and release collateral around the operator-led boundary.
- [ ] Run focused and full gates, exact-head project/correctness/complexity review, hosted CI, and
      the signed ready handoff for the corrected product surface.

- [ ] After live certification or an authenticated disposition, create `locked.md`, record the final
      evidence, and leave the exact green reviewed head ready to merge.

### Exit criteria

- [ ] Every successful new create is verified Trixie, existing release state remains truthful,
      operator-led adoption and reinitialization are documented, and all test resources are removed
      or explicitly handed back to the operator.

## Coordination and escalation

Escalate to the authenticated operator instead of expanding scope when:

- an official Trixie artifact is unavailable on a supported provider/architecture;
- a provider needs a new image override or materially broader creation permissions to pass;
- a review proposes arbitrary OS support, an arbitrary or multi-hop release graph, or automatic
  rollback; or
- live certification exposes a platform incompatibility whose fix materially increases complexity.

Within the standing scope, the effort lead may correct selector values, create-time verification,
release observation, tests, docs, and error guidance without reopening the FRD.

## Traceability

| Requirement                  | Primary phases | Proof                                          |
| ---------------------------- | -------------- | ---------------------------------------------- |
| R1 ordered Debian model      | 1, 3, 5        | registry-tail and schema/CLI absence tests     |
| R2 current-release creation  | 3, 5           | v1 request/selector plus live create matrix    |
| R3 persisted observation     | 1, 3, 5        | migration/parser/reconciliation/create tests   |
| R4 explicit release adoption | 5              | confirm/consent/transaction/reinit split tests |
| R5 release maps              | 1, 3, 5        | apt/platform mapping and selector contracts    |
| R6 relative release support  | 1, 5           | classifier and warning behavior                |
| R7 Trixie operations         | 2, 5           | tmpfs, SSH, PAM, sysctl, NIC, deb822 proofs    |
| R8 bounded doctor            | 5              | schema and no-live-probe tests                 |
| R9 schema retirement         | 5              | migration 34-to-35 empty/nonempty tests        |

## Research disposition

The required external research is complete in `prior-art-research.md`. Implementation rechecks live
provider selectors and vendor APT instructions immediately before changing each mapping because
those catalogs are time-sensitive. A changed selector is a bounded implementation update when it
still supplies the same official Trixie artifact and does not change the product boundary.
