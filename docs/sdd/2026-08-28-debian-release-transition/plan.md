# Plan: Debian Release Transition

- Status: Draft
- Date: 2026-08-28
- Governing artifacts: `frd.md`, `hla.md`, `prior-art-research.md`, and `migration-strategy.md`

## Delivery posture

This is a standalone significant effort. The SDD lands for review before implementation begins.
Implementation uses a sequence of independently green merge units because the database migration,
package catalog, six-provider release contract, remote state machine, and live certification should
not arrive as one review surface. Stacked provider branches can make review parallel, but they are
review inputs rather than independently mergeable changes when the shared contract would leave any
provider broken.

The effort lead owns the FRD/HLA rulings, shared release and persistence contracts, integration of
the upgrade state machine, and final closeout. Platform work can proceed in parallel only after the
common request/result and selector-map contracts are reviewed. Each worker owns disjoint provider
files and tests and is told that concurrent changes belong to other agents.

No implementation PR may introduce an operator OS/release/image selector, automatically create an
unowned provider checkpoint, or grow the ordered adjacent-release mechanism into an arbitrary
release graph or multi-hop upgrader.

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

### Implementation

- [ ] Add the typed ordered Debian release/profile/parser module and relative support classifier.
      Derive current only from the final active profile; keep Bookworm as that final profile until
      Phase 5 and use explicit candidate-profile fixtures for Trixie work.
- [ ] Add nullable release and observation-time columns through the ordinary forward migration,
      enforcing pair-null state without enumerating codenames in SQL.
- [ ] Update the exact historical schema inventory used by safe database restore.
- [ ] Extend `VMRow`, converters, repository accessors, and backup serialization.
- [ ] Add the shared live observation service and typed mismatch/unsupported errors.
- [ ] Add release facts to VM list/describe and additive JSON v1 projections after the names-only
      short circuit.
- [ ] Add doctor findings for unknown, recorded/live disagreement, previous-release upgrade
      availability, and legacy best-effort status.
- [ ] Emit one warning from the named-VM operation boundary before accessing a recognized legacy
      release without blocking an ordinary operation merely because of release position.
- [ ] Extend `apt-source` with mutually exclusive scalar `source` and release map `sources` forms.
- [ ] Reject codename-bearing scalars and missing selected mappings before mutation.
- [ ] Convert shipped HashiCorp, tofuutils, and ngrok resources to reviewed maps; keep invariant
      vendor sources scalar.
- [ ] Thread verified release into initializer APT resolution and preserve graph/provenance
      behavior.
- [ ] Update resource schemas, generated samples, plugin manifests, parity oracles, permanent
      teaching, machine-output contracts, command references, and user manifest errors.

### Focused verification

- [ ] Cover migration from every supported historical schema shape.
- [ ] Prove there is no guessed legacy backfill.
- [ ] Cover the parser matrix for Debian, contradictions, missing fields, and non-Debian guests.
- [ ] Cover observation insert/refresh/mismatch semantics.
- [ ] Prove unknown rows remain usable for release-insensitive operations.
- [ ] Prove list names-only remains probe-free.
- [ ] Prove JSON values are recognized codenames or null and a future registry value needs no SQL
      shape change.
- [ ] Cover scalar/map schema, Bookworm/Trixie selection, and vendor-specific ngrok behavior.
- [ ] Prove resolution uses observed VM release rather than current creation release.
- [ ] Prove a missing mapping fails before key/source writes.
- [ ] Prove current, previous, and legacy classifications derive from positions in the one profile
      registry; appending a profile changes tiers without persisted rewrites or calendar input.
- [ ] Prove a release absent from the active registry refuses release-sensitive mutation with a
      supporting-build diagnostic rather than creating an ahead-of-current tier.
- [ ] Prove legacy ordinary operations warn and continue to their concrete checks, while
      `vm upgrade` refuses without attempting a multi-hop path.
- [ ] Cover built-in/plugin parity and generated sample/explain surfaces.
- [ ] Prove through config/schema/CLI searches that there is no public release selector.

### Exit criteria

- [ ] The database faithfully represents unknown and every registry-recognized observation, and
      Phase B selects the observed release independent of current creation policy. No later phase
      hides release state in platform metadata, persists support position, or lands against a
      release-unaware APT consumer.

## Phase 2: Trixie-safe staging

### Implementation

- [ ] Move VM backup and workspace-copy size-unbounded staging out of `/tmp` into secure disk-backed
      directories, preserving cleanup on success, failure, and interrupt.
- [ ] Inventory remaining active guest `/tmp` uses and record why each is bounded or move it.

### Focused verification

- [ ] Exercise backup and workspace copy with payload larger than a constrained `/tmp` tmpfs.
- [ ] Prove interrupt cleanup and no world-readable staging.

### Exit criteria

- [ ] Trixie tmpfs behavior cannot break large Agentworks transfers.

## Phase 3: Platform current-release creation

### Shared contract

- [ ] Bump the vm-platform kind and every implementation from contract version 2 to 3; add required
      `debian_release` to `ProvisionRequest` and verified release to `ProvisionResult`.
- [ ] Add the create-time shared release probe helper.
- [ ] Keep core as the only owner of current and pass its concrete value in every manager-built
      request; platforms have no local current default.
- [ ] Add the shared release-map resolver and focused errors: code-owned misses name the outdated
      platform/plugin, while operator-owned misses name the exact vm-site release key. Resolve
      before backend mutation and never fall back.
- [ ] Preserve both typed release-map failures and their remediation hints through the manager's
      provisioning wrapper after provisional-row unwind.
- [ ] Require every platform to verify the guest before leaving its backend rollback window.
- [ ] Update `cli/agentworks/capabilities/README.md`,
      `cli/agentworks/capabilities/vm_platform/README.md`, `cli/agentworks/plugins/README.md`, the
      kind's topic prose, and the request, result, and `create` docstrings in the same merge unit as
      contract version 3.

After the shared contract is reviewed, provider work splits by ownership on stacked review branches:

- [ ] **Lima/WSL:** implement the release-keyed Debian cloud image block, derived OCI
      tag/cache/diagnostics, and local create tests.
- [ ] **AWS/Azure:** implement the SSM release mapping, Azure full image record/disk floor, and
      cloud create tests.
- [ ] **GCP:** implement the release and architecture image-family map and GCE create tests.
- [ ] **Proxmox:** implement release-keyed template config, the legacy Bookworm adapter, setup
      script, and guest validation.

Workers do not edit the shared request/result contract after it is handed off. Any required contract
change returns to the effort lead and is reviewed once across all providers. The shared contract and
every provider implementation then enter the merge branch as one green unit; no merge commit leaves
a provider unable to construct or honor the required request.

### Focused verification

- [ ] Prove every exposed architecture has an official Trixie selector.
- [ ] Prove contract version 2 platforms fail registration with an incompatibility error.
- [ ] Prove a contract-current missing code-owned mapping fails before backend mutation with the
      platform-update hint and no fallback.
- [ ] Prove a mismatch raised by the shared in-window verifier triggers backend cleanup and no
      success row.
- [ ] Prove a nonconforming returned mismatch persists backend identifiers, retains one failed and
      uninitialized row that `vm delete` can address, and returns the typed delete/retry diagnostic.
- [ ] Prove the manager supplies current and persists only the matching returned verified value
      before Phase B.
- [ ] Prove existing platform metadata and existing VM operations stay compatible.
- [ ] Prove the Proxmox legacy scalar cannot satisfy a current-release create and a missing current
      template names `template_vmids.<release>`.
- [ ] Prove no arbitrary provider image field appears in configuration.
- [ ] Review both capability READMEs and the plugin-author README for the
      request/map/failure/verification/plugin contract without adding tests that pin authored
      wording.

### Exit criteria

- [ ] All providers honor one version 3 current-release contract and are code-complete for Trixie,
      while the product cutover remains held until live certification in Phase 5.

## Phase 4: Durable `vm upgrade`

### Slice 1: plan and recovery gate

- [ ] Add the thin `vm upgrade NAME [--checkpoint REF]` CLI and VM-name completion.
- [ ] Build the manager boundary that resolves site credentials, canonical/native routes, SSH
      identity, and Tailscale rejoin secret before mutation.
- [ ] After activation, scan the fixed journal root before new-upgrade eligibility. Resume or
      diagnose exactly one incomplete validated adjacent pair through its retained target policy,
      even after a later profile append; refuse multiple journals and never start a second beside
      one.
- [ ] With no incomplete journal, derive source and target from the final two registry profiles.
      Return already-current for current, select only previous-to-current, and refuse legacy with
      fresh-VM/data-copy guidance and no multi-hop attempt.
- [ ] Implement live release/database reconciliation and the externally completed adjacent adoption
      path, with Bookworm-to-Trixie as the first instance.
- [ ] Implement session quiescence and Debian preflight checks from the transition policy.
- [ ] Produce a preliminary simulated removal/source plan without mutation.
- [ ] Create the ordinary VM backup and focused Debian recovery bundle with owner-only local
      storage.
- [ ] Require an external checkpoint reference that identifies an actual recoverable artifact, audit
      it, and obtain explicit operator consent to bring the source release current within its suite.

- [ ] Focused tests prove every unsafe condition fails before source mutation and neither local
      backup is misrepresented as a bootable checkpoint.

### Slice 2: package state machine

- [ ] Implement the root-owned `{source}-to-{target}` persistent directory, atomic action journal,
      private orchestrator lock, script, and log. The validated directory alone owns the
      source/target pair. `plan.json` stores the computed plan and `state.json` records intent
      before mutation plus `last_completed_action`, active action, attempt identity, and attempt
      outcome, with no duplicate pair fields. Database/init state remains authoritative for product
      health. Every journal writer takes the same lock, and the package service holds it for each
      remote action.
- [ ] Detect native dpkg/APT locks and automatic update services. Fail closed on another package
      owner; durably record and inhibit known automatic timers. Restore their prior state only after
      a source-safe abort or verified healthy target completion; retain inhibition on a
      mixed/unhealthy state until forward repair or external restore.
- [ ] Bring the source release current under the detached systemd service, rerun every health check,
      and reopen the planning boundary. Recompute the full package/source/removal plan from current
      state, display drift from the preliminary plan, and require a second explicit confirmation
      before changing suites.
- [ ] Classify, archive, and disable third-party sources; generate canonical target deb822 sources
      from the selected adjacent policy, then run its minimal and full package actions.
- [ ] Implement inspection/resume after interruption inside every remote action, SSH loss, and
      package failure. A resume inspects native locks, unit state, active attempt, logs, and
      postcondition checks before deciding whether to continue, retry, or request repair.
- [ ] Preserve sources/logs and fail with forward-repair or external-restore guidance; do not
      restore source-release entries onto a mixed package state.

- [ ] Focused tests use a fake transport/systemd boundary and real temporary filesystem state. They
      kill the client inside every action, assert durable intent precedes mutation, exercise plan
      drift and both confirmations, prove one native package owner, distinguish safe timer
      restoration from retained mixed-state inhibition, prove lock exclusion for journal writes and
      reboot dispatch, and resume without replaying a completed action.

### Slice 3: reboot, reconnect, and convergence

- [ ] After the package unit installs the target release's udev rules, predict interface names and
      block reboot with pinning guidance if connectivity would be unsafe.
- [ ] Take the same journal lock, record intent, dispatch reboot inside its critical section, and
      open a fresh post-reboot operation span.
- [ ] Add strict provider/SSH reconnect rather than warning-and-continue behavior.
- [ ] Use native transport and explicit Tailscale rejoin where current platform capability permits.
- [ ] Record a local `repair-required` outcome with checkpoint/console guidance when no path
      returns; the unreachable guest cannot authoritatively record its own outcome.
- [ ] Persist the target immediately after proof, then run health checks and release-aware Phase B.
- [ ] Use existing init state plus a `repair-required` event for later convergence failures without
      database rollback; do not duplicate Trixie observation or Phase B completion in remote state.
- [ ] Add permanent operator recovery and resume documentation for every stage.

### Exit criteria

- [ ] The orchestrated command is safe to interrupt inside every remote action, uses one lock for
      every journal writer and reboot dispatch, never runs two package managers, restores automatic
      updates only from a provably safe package state, never claims automatic rollback, and
      distinguishes OS transition success from later initialization health.

## Phase 5: Certification and cutover

### Live matrix

- [ ] Run the authenticated integration environment for each platform and supported architecture,
      recording artifact identity, architecture, test resource names, budgets, checkpoint reference,
      operation transcript, and cleanup evidence.
- [ ] Prove artifact resolution, boot, live release observation, and delete for every exposed
      architecture.
- [ ] Prove at least one full lifecycle and one real Bookworm-to-Trixie upgrade on every platform
      where the operator-approved recovery prerequisite can be established.
- [ ] Seed Bookworm inputs from the last Bookworm-creating Agentworks release or an
      operator-approved prebuilt image, record provenance, and prove current code has no hidden
      Bookworm create selector.

Across the matrix, also exercise:

- [ ] Exercise Trixie create and live release verification.
- [ ] Exercise Phase B and a selected release-mapped source where available.
- [ ] Exercise shell/exec, workspace operation, backup, reboot/reconnect, and delete.
- [ ] Exercise a large transfer with Trixie `/tmp` behavior.
- [ ] Exercise the shared release-mapped APT and constrained-`/tmp` behavior at least once.
- [ ] Prove DSA rejection and supported SSH-key success.
- [ ] Prove `~/.pam_environment` independence and continued `/etc/sysctl.d` application.
- [ ] Prove network-interface-name and deb822 source assumptions.
- [ ] Capture interruption/resume evidence for the upgrade journal.

A platform failure blocks its Trixie certification. It does not authorize Bookworm fallback, a
custom-image escape hatch, or relaxed release validation. The disposition returns to the operator if
the product cannot ship consistently within the agreed scope.

### Final cutover

- [ ] Atomically append the certified Trixie profile, making it current by registry position, and
      delete the Bookworm platform image selectors.
- [ ] Enable the relative support classifier and named-VM legacy warning; prove Bookworm is previous
      at this cutover and that no wall-clock value changes its tier.
- [ ] Add the superseding Debian-release ADR and mark ADR 0002 superseded without rewriting history.
- [ ] Update README, CLI reference, platform/resource guides, config samples, schemas, completions,
      release notes, and release-specific support/recovery teaching. Contract-author teaching
      remains owned by the Phase 3 version-cutover merge unit.
- [ ] Search current code/docs for unaccounted release literals, platform-local current defaults,
      Bookworm-create claims, unbounded archive staging in `/tmp`, and VM-backup rollback claims.
- [ ] Prove a synthetic release-promotion fixture can add mapping values and append one successor
      profile with its upgrade-from-previous policy, changing current and adjacent policy selection
      without a second current authority, codename-specific database migration, new create field,
      CLI target selector, or arbitrary upgrade graph. Treat this as a structural no-hardcoding
      proof, not a compatibility claim or a promise of unchanged workflow internals.
- [ ] Run full unit/static/docs/generated gates and the final independent review lanes.

### Exit criteria

- [ ] Every successful new create is verified Trixie, existing release state remains truthful and
      recoverable, the supported upgrade path is documented and live-proven, and all test resources
      are removed or explicitly handed back to the operator.

## Coordination and escalation

Escalate to the authenticated operator instead of expanding scope when:

- an official Trixie artifact is unavailable on a supported provider/architecture;
- a platform cannot establish the required recovery prerequisite in the provided environment;
- a provider needs a new image override, snapshot ownership model, or materially broader permissions
  to pass;
- Debian release-note blockers require application-specific migration behavior beyond fail-closed
  preflight;
- a review proposes arbitrary OS support, an arbitrary or multi-hop release graph, or automatic
  rollback; or
- live certification exposes a platform incompatibility whose fix materially increases complexity.

Within the standing scope, the effort lead may correct selector values, preflight coverage, state
transitions, tests, docs, and error guidance without reopening the FRD.

## Traceability

| Requirement                   | Primary phases | Proof                                         |
| ----------------------------- | -------------- | --------------------------------------------- |
| R1 ordered Debian model       | 1, 3, 5        | registry-tail and schema/CLI absence tests    |
| R2 current-release creation   | 3, 5           | v3 request/selector plus live create matrix   |
| R3 persisted observation      | 1, 3, 4        | migration/parser/reconciliation/create tests  |
| R4 release maps               | 1, 3, 5        | apt/platform mapping and selector contracts   |
| R5 safe `vm upgrade`          | 4, 5           | preflight, interruption/resume, real upgrades |
| R6 relative release support   | 1, 4, 5        | classifier, warnings, adjacent-only upgrade   |
| R7 Trixie operations          | 2, 5           | tmpfs, SSH, PAM, sysctl, NIC, deb822 proofs   |
| R8 operator recovery teaching | 4, 5           | CLI/docs review and failure-stage runbook     |

## Research disposition

The required external research is complete in `prior-art-research.md`. Implementation rechecks live
provider selectors and vendor APT instructions immediately before changing each mapping because
those catalogs are time-sensitive. A changed selector is a bounded implementation update when it
still supplies the same official Trixie artifact and does not change the product boundary.
