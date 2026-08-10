# GCP GCE VM platform: implementation plan

## Definition of done

The disabled-by-default `gcp` plugin publishes a contract-v2 `gcp-gce` platform with the reviewed
schema; both auth modes are secret-source conforming; create is complete-or-raise with a
credential-free retained request and one fixed-stdin join; lifecycle, rollback, exposure, docs,
offline gates, and operator-gated live acceptance are complete; the SDD is locked truthfully.

## Phase 0: contract and schema gates

- [ ] Rebase or stack on the review-clean vm-platform v2 complete-or-raise contract from issue #471.
- [x] Review the latest stable `google-cloud-compute` and `google-auth` releases and select only the
      direct dependencies the implementation will import; actual dependency edits remain Phase 1.
- [x] Review and approve `prior-art-research.md` plus `provider-state-machine-lld.md`, which pin the
      official provider semantics and bounded bootstrap/network/cleanup state machine.
- [x] Obtain saga-lead approval for the exact `gcp-gce` config schema in the HLA before code begins.
- [x] Include the narrow shared readiness-command seam in that pre-implementation review because GCP
      startup-script completion differs from Azure/AWS cloud-init readiness.
- [x] Resolve every schema or readiness-seam finding in the FRD/HLA/plan before delegation.

**DoD:** the public manifest shape and only shared seam are approved; implementation has a stable
contract and no schema guesswork.

## Phase 1: unregistered provider foundation

- [ ] Add current stable Google Compute/auth dependencies and regenerate the lock file.
- [ ] Implement the auth union, machine catalog, `GcpGCEConfig`, size selection, and image-family
      mapping with exact schema/default/reference tests, including omitted-versus-null outer auth.
- [ ] Implement secret-free ambient and service-account credential builders. The explicit builder
      consumes one complete JSON secret, never falls back, and does not retain or chain raw parser
      failures.
- [ ] Implement typed Google API/operation mapping plus read-only project, zone, default-network,
      subnet, network-policy enforcement order, live machine shape, `debian-cloud` image, exact
      retained-name collision, priority-zero allow/deny conflict, and external-IP helpers.
- [ ] Implement bounded instance/firewall rollback, including exact-shape reconciliation after
      indeterminate deny/allow inserts, plus first/second-interrupt cleanup helpers with exact
      survivor/manual-removal tests.
- [ ] Generalize `EphemeralTailscaleBootstrap` with the approved non-secret readiness command/label
      while preserving Azure/AWS defaults and all stdin/non-reflection tests.

**DoD:** the new package remains unregistered and therefore unshipped; its reusable provider
foundation is fully typed, secret-free, offline-tested, and the existing platforms are unchanged.

## Phase 2: complete plugin publication

- [ ] Implement `GCEPlatform` contract v2 create/start/stop/delete/status/display behavior and
      register it through a new disabled-by-default `gcp` system plugin.
- [ ] Implement authenticated runup for zone plus configured-subnet or default-network existence,
      plus the inspectable VPC firewall support boundary, with every definitive failure before
      mutation.
- [ ] Implement machine/image/disk selection, metadata SSH identity, credential-free startup script,
      256 KiB encoded size gate, durable run-once marker, auto-deleted boot disk, empty guest
      service-account list, scoped priority-0 TCP/22 allow plus priority-1 all-ingress deny,
      lifetime ephemeral external access, observable secret-free progress, fixed stdin join, and
      optional Tailscale IP result.
- [ ] Implement `post_tailscale_ready`, `secure_failed_vm`, and concurrent-safe `transient_route` so
      provisioning/native scoped allows are bounded while lifetime external access keeps ordinary
      egress available.
- [ ] Add provider-shaped retained-request tests using quoting-hostile Tailscale and service-account
      sentinels; pin one fixed-stdin delivery and secret-free output/log/diagnostic/exception
      graphs.
- [ ] Add create rollback tests for every partial resource set, bootstrap/join failure, first
      interrupt, second interrupt, realized/absent/mismatched firewall-insert timeout, cleanup
      survivor, and exact manual-removal guidance.
- [ ] Add lifecycle/idempotency tests for live-IP reads, start, stop, status, delete, already-gone,
      surviving-VM typed failure, deny retention, exact instance/tag/rule name derivation,
      classic-first policy order, priority-zero allow/deny conflicts, all-ingress deny shape,
      firewall hooks, and concurrent transient routes.
- [ ] Add registration, plugin enablement/provenance, capability conformance, schema/sample, guide
      catalog/rendering/inertness, and completion-adjacent discovery tests.
- [ ] Update the installed-plugin index, permanent plugin/platform/capability docs, command
      reference, resources guide, sample teaching, durable six-surface enumeration, `TopicProse`,
      key-file-to-env-var setup, and recovery guidance in the same publication commit.
- [ ] Confirm shell completion implementation needs no bespoke edit because it remains
      registry-driven; update tests and generated/reference projections that enumerate platforms.
- [ ] Delete the branch-seeded task brief before the PR leaves draft.

**DoD:** enabling plugin `gcp` exposes a fully functional `gcp-gce` platform; disabled behavior is
honest; no incomplete registration or undocumented operator surface exists; all offline focused
suites and strict typing pass.

## Phase 3: integration, review, and live acceptance

- [ ] Update from current main and resolve any overlap with the merged vm-platform contract and
      provider-boundary enumeration.
- [ ] Run Ruff check/format, strict mypy, full parallel non-integration pytest, file lint, Rulesync
      drift, locked-SDD, guide safety, and diff checks.
- [ ] Run the required Agentworks project review and an independent fresh-eyes review; resolve every
      valid finding through the implementation agent and re-review.
- [ ] Load the integration-testing and test-environment skills, inventory the operator-provided GCP
      project/zone/network/auth mode, verify classic-first network-policy order plus the absence of
      conflicting organization/folder terminal rules, set explicit resource/time/cost budgets, and
      obtain operator authorization before any live mutation or credential use.
- [ ] Run one bounded create/init/Tailscale/lifecycle/delete acceptance, then independently query
      the project to prove zero instance, disk, firewall, and address residue.
- [ ] Record exact offline/live/review evidence, add `locked.md`, post the detailed ready-for-review
      disposition, and flip the PR from draft only when every requirement is true.

**DoD:** offline and authorized live evidence prove the shipped behavior and cleanup; reviews and
forge checks are green; permanent docs match code; the merge-ready PR is truthfully locked.

-- agw-ns-gcp-platform (effort lead)
