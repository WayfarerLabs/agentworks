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
unowned provider checkpoint, or generalize beyond the Bookworm-to-Trixie transition without new
operator direction.

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

## Phase 1: Core release observation, persistence, and APT values

### Implementation

- [ ] Add the closed Debian release/profile/parser module and the fixed current-release policy,
      keeping the creation constant on the pre-cutover value until Phase 5.
- [ ] Add nullable release and observation-time columns through the ordinary forward migration.
- [ ] Update the exact historical schema inventory used by safe database restore.
- [ ] Extend `VMRow`, converters, repository accessors, and backup serialization.
- [ ] Add the shared live observation service and typed mismatch/unsupported errors.
- [ ] Add release facts to VM list/describe and additive JSON v1 projections after the names-only
      short circuit.
- [ ] Add doctor findings for unknown, unsupported, and recorded/live disagreement. Keep dated
      support-window enforcement dormant until Phase 5 supplies the implementation-release date.
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
- [ ] Prove JSON values are `bookworm`, `trixie`, or null.
- [ ] Cover scalar/map schema, Bookworm/Trixie selection, and vendor-specific ngrok behavior.
- [ ] Prove resolution uses observed VM release rather than current creation release.
- [ ] Prove a missing mapping fails before key/source writes.
- [ ] Cover built-in/plugin parity and generated sample/explain surfaces.
- [ ] Prove through config/schema/CLI searches that there is no public release selector.

### Exit criteria

- [ ] The database faithfully represents unknown, Bookworm, and Trixie observations, and Phase B
      converges either supported observed release without changing which release new VMs create. No
      later phase hides release state in platform metadata or lands against a release-unaware APT
      consumer.

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

## Phase 3: Platform Trixie creation

### Shared contract

- [ ] Add required `debian_release` to `ProvisionRequest` and verified release to `ProvisionResult`.
- [ ] Add the create-time shared release probe helper.
- [ ] Keep core as the only caller selecting the requested release.
- [ ] Require every platform to verify the guest before leaving its backend rollback window.

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
- [ ] Prove a missing mapping fails without fallback.
- [ ] Prove a create-time mismatch triggers backend cleanup and no success record.
- [ ] Prove the manager persists only the returned verified value before Phase B.
- [ ] Prove existing platform metadata and existing VM operations stay compatible.
- [ ] Prove the Proxmox legacy scalar cannot satisfy a Trixie create.
- [ ] Prove no arbitrary provider image field appears in configuration.

### Exit criteria

- [ ] All providers are code-complete for Trixie, while the product cutover remains held until live
      certification in Phase 5.

## Phase 4: Durable `vm upgrade`

### Slice 1: plan and recovery gate

- [ ] Add the thin `vm upgrade NAME [--checkpoint REF]` CLI and VM-name completion.
- [ ] Build the manager boundary that resolves site credentials, canonical/native routes, SSH
      identity, and Tailscale rejoin secret before mutation.
- [ ] Implement live release/database reconciliation and the externally upgraded Trixie adoption
      path.
- [ ] Implement session quiescence and Debian preflight checks from the transition policy.
- [ ] Produce a preliminary simulated removal/source plan without mutation.
- [ ] Create the ordinary VM backup and focused Debian recovery bundle with owner-only local
      storage.
- [ ] Require an external checkpoint reference that identifies an actual recoverable artifact, audit
      it, and obtain explicit operator consent to bring Bookworm current.

- [ ] Focused tests prove every unsafe condition fails before source mutation and neither local
      backup is misrepresented as a bootable checkpoint.

### Slice 2: package state machine

- [ ] Implement the root-owned persistent directory, atomic action journal, private orchestrator
      lock, script, and log. The journal records intent before mutation plus
      `last_completed_action`, active action, attempt identity, and attempt outcome; database/init
      state remains authoritative for product health. Every journal writer takes the same lock, and
      the package service holds it for each remote action.
- [ ] Detect native dpkg/APT locks and automatic update services. Fail closed on another package
      owner; durably record and inhibit known automatic timers. Restore their prior state only after
      a Bookworm-safe abort or verified healthy Trixie completion; retain inhibition on a
      mixed/unhealthy state until forward repair or external restore.
- [ ] Bring Bookworm current under the detached systemd service, rerun every health check, and
      reopen the planning boundary. Recompute the full package/source/removal plan from current
      state, display drift from the preliminary plan, and require a second explicit confirmation
      before changing suites.
- [ ] Classify, archive, and disable third-party sources; generate canonical Trixie deb822 sources,
      then run the minimal and full Trixie package actions.
- [ ] Implement inspection/resume after interruption inside every remote action, SSH loss, and
      package failure. A resume inspects native locks, unit state, active attempt, logs, and
      postcondition checks before deciding whether to continue, retry, or request repair.
- [ ] Preserve sources/logs and fail with forward-repair or external-restore guidance; do not
      restore Bookworm sources onto a mixed package state.

- [ ] Focused tests use a fake transport/systemd boundary and real temporary filesystem state. They
      kill the client inside every action, assert durable intent precedes mutation, exercise plan
      drift and both confirmations, prove one native package owner, distinguish safe timer
      restoration from retained mixed-state inhibition, prove lock exclusion for journal writes and
      reboot dispatch, and resume without replaying a completed action.

### Slice 3: reboot, reconnect, and convergence

- [ ] After the package unit installs Trixie's udev rules, predict interface names and block reboot
      with pinning guidance if connectivity would be unsafe.
- [ ] Take the same journal lock, record intent, dispatch reboot inside its critical section, and
      open a fresh post-reboot operation span.
- [ ] Add strict provider/SSH reconnect rather than warning-and-continue behavior.
- [ ] Use native transport and explicit Tailscale rejoin where current platform capability permits.
- [ ] Record a local `repair-required` outcome with checkpoint/console guidance when no path
      returns; the unreachable guest cannot authoritatively record its own outcome.
- [ ] Persist Trixie immediately after proof, then run health checks and release-aware Phase B.
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

- [ ] Set `CURRENT_DEBIAN_RELEASE` to Trixie and delete the Bookworm platform image selectors.
- [ ] Fill exact compatibility dates from the implementation release and enable the central
      operation-policy gate, including exact-boundary tests.
- [ ] Add the superseding Debian-release ADR and mark ADR 0002 superseded without rewriting history.
- [ ] Update README, CLI reference, platform/resource guides, config samples, schemas, completions,
      release notes, and support/recovery teaching.
- [ ] Search current code/docs for unaccounted release literals, Bookworm-create claims, unbounded
      archive staging in `/tmp`, and VM-backup rollback claims.
- [ ] Create a dated cleanup issue owned by the release maintainer for full-compatibility and 2028
      removal work, and link it from the support-policy code and superseding ADR.
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
- a review proposes arbitrary OS support, a generic release graph, or automatic rollback; or
- live certification exposes a platform incompatibility whose fix materially increases complexity.

Within the standing scope, the effort lead may correct selector values, preflight coverage, state
transitions, tests, docs, and error guidance without reopening the FRD.

## Traceability

| Requirement                   | Primary phases | Proof                                         |
| ----------------------------- | -------------- | --------------------------------------------- |
| R1 closed Debian model        | 1, 3, 5        | schema/CLI absence tests and release type     |
| R2 Trixie-only creation       | 3, 5           | selector contracts plus live create matrix    |
| R3 persisted observation      | 1, 3, 4        | migration/parser/reconciliation/create tests  |
| R4 release maps               | 1, 3, 5        | apt/platform mapping and selector contracts   |
| R5 safe `vm upgrade`          | 4, 5           | preflight, interruption/resume, real upgrades |
| R6 bounded Bookworm support   | 1, 4, 5        | release profiles, diagnostics, dated docs     |
| R7 Trixie operations          | 2, 5           | tmpfs, SSH, PAM, sysctl, NIC, deb822 proofs   |
| R8 operator recovery teaching | 4, 5           | CLI/docs review and failure-stage runbook     |

## Research disposition

The required external research is complete in `prior-art-research.md`. Implementation rechecks live
provider selectors and vendor APT instructions immediately before changing each mapping because
those catalogs are time-sensitive. A changed selector is a bounded implementation update when it
still supplies the same official Trixie artifact and does not change the product boundary.
