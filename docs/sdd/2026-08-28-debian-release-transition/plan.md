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

- `frd.md`
- `hla.md`
- `prior-art-research.md`
- `migration-strategy.md`
- `plan.md`

Open a draft pull request labeled `sdd:debian-release-transition` and `review-requested`. The PR is
a review vehicle only and has no merge intent while the design is draft. Resolve bounded review
findings through the authorized review-requested cycle, removing the label while revising and
reapplying it at coherent handoff checkpoints.

Exit criteria:

- FRD requirements and non-goals reflect the operator's Debian-only, Trixie-only-create, persisted
  release, release-map, and `vm upgrade` rulings.
- HLA has no unowned provider artifact lifecycle or implicit recovery promise.
- Prior art uses primary sources and accounts for every current platform.
- Migration strategy gives existing rows, Proxmox config, operator APT resources, Bookworm support,
  and partial upgrade states a deterministic path.
- Independent review finds no unresolved blocker or unjustified complexity.

## Phase 1: Core release observation, persistence, and APT values

### Implementation

1. Add the closed Debian release/profile/parser module and the fixed current-release policy, keeping
   the creation constant on the pre-cutover value until Phase 5.
2. Add nullable release and observation-time columns through the ordinary forward migration.
3. Update the exact historical schema inventory used by safe database restore.
4. Extend `VMRow`, converters, repository accessors, and backup serialization.
5. Add the shared live observation service and typed mismatch/unsupported errors.
6. Add release facts to VM list/describe and additive JSON v1 projections after the names-only short
   circuit.
7. Add doctor findings for unknown, unsupported, and recorded/live disagreement. Keep dated
   support-window enforcement dormant until Phase 5 supplies the implementation-release date.
8. Extend `apt-source` with mutually exclusive scalar `source` and release map `sources` forms.
9. Reject codename-bearing scalars and missing selected mappings before mutation.
10. Convert shipped HashiCorp, tofuutils, and ngrok resources to reviewed maps; keep invariant
    vendor sources scalar.
11. Thread verified release into initializer APT resolution and preserve graph/provenance behavior.
12. Update resource schemas, generated samples, plugin manifests, parity oracles, permanent
    teaching, machine-output contracts, command references, and user manifest errors.

### Focused verification

- migration from every supported historical schema shape;
- no guessed legacy backfill;
- parser matrix for Debian, contradictions, missing fields, and non-Debian guests;
- observation insert/refresh/mismatch semantics;
- unknown rows remain usable for release-insensitive operations;
- list names-only remains probe-free;
- JSON values are `bookworm`, `trixie`, or null;
- scalar/map schema, Bookworm/Trixie selection, and vendor-specific ngrok behavior;
- resolution from observed VM release rather than current creation release;
- missing mapping fails before key/source writes;
- built-in/plugin parity and generated sample/explain surfaces; and
- config/schema/CLI searches prove there is no public release selector.

### Exit criteria

The database can faithfully represent unknown, Bookworm, and Trixie observations, and Phase B can
converge either supported observed release, without changing which release new VMs create. No later
phase needs to hide release state in platform metadata or land against a release-unaware APT
consumer.

## Phase 2: Trixie-safe staging

### Implementation

1. Move VM backup and workspace-copy size-unbounded staging out of `/tmp` into secure disk-backed
   directories, preserving cleanup on success, failure, and interrupt.
2. Inventory remaining active guest `/tmp` uses and record why each is bounded or move it.

### Focused verification

- backup and workspace copy with payload larger than a constrained `/tmp` tmpfs; and
- interrupt cleanup and no world-readable staging.

### Exit criteria

Trixie tmpfs behavior cannot break large Agentworks transfers.

## Phase 3: Platform Trixie creation

### Shared contract

1. Add required `debian_release` to `ProvisionRequest` and verified release to `ProvisionResult`.
2. Add the create-time shared release probe helper.
3. Keep core as the only caller selecting the requested release.
4. Require every platform to verify the guest before leaving its backend rollback window.

After the shared contract is reviewed, provider work splits by ownership on stacked review branches:

| Workstream | Owned implementation                                                                   |
| ---------- | -------------------------------------------------------------------------------------- |
| Lima/WSL   | release-keyed Debian cloud image block; OCI tag/cache/diagnostics; local create tests  |
| AWS/Azure  | SSM release mapping; Azure full image record/disk floor; cloud create tests            |
| GCP        | release and architecture image-family map; GCE create tests                            |
| Proxmox    | release-keyed template config, legacy Bookworm adapter, setup script, guest validation |

Workers do not edit the shared request/result contract after it is handed off. Any required contract
change returns to the effort lead and is reviewed once across all providers. The shared contract and
every provider implementation then enter the merge branch as one green unit; no merge commit leaves
a provider unable to construct or honor the required request.

### Focused verification

- every exposed architecture has an official Trixie selector;
- a missing mapping fails without fallback;
- create-time mismatch triggers backend cleanup and no success record;
- the manager persists only the returned verified value before Phase B;
- existing platform metadata and existing VM operations stay compatible;
- Proxmox legacy scalar cannot satisfy a Trixie create; and
- no arbitrary provider image field appears in configuration.

### Exit criteria

All providers are code-complete for Trixie, while the product cutover remains held until live
certification in Phase 5.

## Phase 4: Durable `vm upgrade`

### Slice 1: plan and recovery gate

1. Add the thin `vm upgrade NAME [--checkpoint REF]` CLI and VM-name completion.
2. Build the manager boundary that resolves site credentials, canonical/native routes, SSH identity,
   and Tailscale rejoin secret before mutation.
3. Implement live release/database reconciliation and the externally upgraded Trixie adoption path.
4. Implement session quiescence and Debian preflight checks from the transition policy.
5. Produce a preliminary simulated removal/source plan without mutation.
6. Create the ordinary VM backup and focused Debian recovery bundle with owner-only local storage.
7. Require an external checkpoint reference that identifies an actual recoverable artifact, audit
   it, and obtain explicit operator consent to bring Bookworm current.

Focused tests prove every unsafe condition fails before source mutation and neither local backup is
misrepresented as a bootable checkpoint.

### Slice 2: package state machine

1. Implement the root-owned persistent directory, atomic action journal, private orchestrator lock,
   script, and log. The journal records intent before mutation plus `last_completed_action`, active
   action, attempt identity, and attempt outcome; database/init state remains authoritative for
   product health. Every journal writer takes the same lock, and the package service holds it for
   each remote action.
2. Detect native dpkg/APT locks and automatic update services. Fail closed on another package owner;
   durably record and inhibit known automatic timers. Restore their prior state only after a
   Bookworm-safe abort or verified healthy Trixie completion; retain inhibition on a mixed/unhealthy
   state until forward repair or external restore.
3. Bring Bookworm current under the detached systemd service, rerun every health check, and reopen
   the planning boundary. Recompute the full package/source/removal plan from current state, display
   drift from the preliminary plan, and require a second explicit confirmation before changing
   suites.
4. Classify, archive, and disable third-party sources; generate canonical Trixie deb822 sources,
   then run the minimal and full Trixie package actions.
5. Implement inspection/resume after interruption inside every remote action, SSH loss, and package
   failure. A resume inspects native locks, unit state, active attempt, logs, and postcondition
   checks before deciding whether to continue, retry, or request repair.
6. Preserve sources/logs and fail with forward-repair or external-restore guidance; do not restore
   Bookworm sources onto a mixed package state.

Focused tests use a fake transport/systemd boundary and real temporary filesystem state. They kill
the client inside every action, assert durable intent precedes mutation, exercise plan drift and
both confirmations, prove one native package owner, distinguish safe timer restoration from retained
mixed-state inhibition, prove lock exclusion for journal writes and reboot dispatch, and resume
without replaying a completed action.

### Slice 3: reboot, reconnect, and convergence

1. After the package unit installs Trixie's udev rules, predict interface names and block reboot
   with pinning guidance if connectivity would be unsafe.
2. Take the same journal lock, record intent, dispatch reboot inside its critical section, and open
   a fresh post-reboot operation span.
3. Add strict provider/SSH reconnect rather than warning-and-continue behavior.
4. Use native transport and explicit Tailscale rejoin where current platform capability permits.
5. Record a local `repair-required` outcome with checkpoint/console guidance when no path returns;
   the unreachable guest cannot authoritatively record its own outcome.
6. Persist Trixie immediately after proof, then run health checks and release-aware Phase B.
7. Use existing init state plus a `repair-required` event for later convergence failures without
   database rollback; do not duplicate Trixie observation or Phase B completion in remote state.
8. Add permanent operator recovery and resume documentation for every stage.

### Exit criteria

The orchestrated command is safe to interrupt inside every remote action, uses one lock for every
journal writer and reboot dispatch, never runs two package managers, restores automatic updates only
from a provably safe package state, never claims automatic rollback, and distinguishes OS transition
success from later initialization health.

## Phase 5: Certification and cutover

### Live matrix

Run the authenticated integration environment for each platform and supported architecture. Record
artifact identity, architecture, test resource names, budgets, checkpoint reference, operation
transcript, and cleanup evidence. Every exposed architecture proves artifact resolution, boot, live
release observation, and delete. Every platform proves at least one full lifecycle and one real
Bookworm-to-Trixie upgrade when the operator-approved recovery prerequisite can be established.
Bookworm inputs come from the last Bookworm-creating Agentworks release or an operator-approved
prebuilt image, with provenance recorded; current code has no hidden Bookworm create selector.

Across the matrix, also exercise:

- Trixie create and live release verification;
- Phase B and a selected release-mapped source where available;
- shell/exec, workspace operation, backup, reboot/reconnect, and delete;
- large transfer with Trixie `/tmp` behavior;
- the shared release-mapped APT and constrained-`/tmp` behavior at least once;
- DSA rejection and supported SSH-key success;
- `~/.pam_environment` independence and continued `/etc/sysctl.d` application;
- network-interface-name and deb822 source assumptions; and
- interruption/resume evidence for the upgrade journal.

A platform failure blocks its Trixie certification. It does not authorize Bookworm fallback, a
custom-image escape hatch, or relaxed release validation. The disposition returns to the operator if
the product cannot ship consistently within the agreed scope.

### Final cutover

1. Set `CURRENT_DEBIAN_RELEASE` to Trixie and delete the Bookworm platform image selectors.
2. Fill exact compatibility dates from the implementation release and enable the central
   operation-policy gate, including exact-boundary tests.
3. Add the superseding Debian-release ADR and mark ADR 0002 superseded without rewriting history.
4. Update README, CLI reference, platform/resource guides, config samples, schemas, completions,
   release notes, and support/recovery teaching.
5. Search current code/docs for unaccounted release literals, Bookworm-create claims, unbounded
   archive staging in `/tmp`, and VM-backup rollback claims.
6. Create a dated cleanup issue owned by the release maintainer for full-compatibility and 2028
   removal work, and link it from the support-policy code and superseding ADR.
7. Run full unit/static/docs/generated gates and the final independent review lanes.

### Exit criteria

Every successful new create is verified Trixie, existing release state remains truthful and
recoverable, the supported upgrade path is documented and live-proven, and all test resources are
removed or explicitly handed back to the operator.

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
