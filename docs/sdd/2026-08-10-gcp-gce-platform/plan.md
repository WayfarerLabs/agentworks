# GCP GCE VM platform: implementation plan

## Definition of done

The disabled-by-default `gcp` vendor plugin publishes a contract-v2 `gcp-gce` platform and optional
guest-side `gcloud-cli` install command, while the existing `aws` vendor plugin publishes an
optional guest-side `aws-cli` install command; both auth modes are secret-source conforming; create
is complete-or-raise with a credential-free retained request and one fixed-stdin join; neither CLI
is a provisioning or authentication dependency; lifecycle, rollback, exposure, docs, offline gates,
and operator-gated live acceptance are complete; the SDD is locked truthfully.

## Phase 0: contract and schema gates

- [x] Rebase or stack on the review-clean vm-platform v2 complete-or-raise contract from issue #471.
- [x] Review the latest stable `google-cloud-compute`, `google-auth`, and `google-api-core` releases
      and select only the direct dependencies the implementation will import; actual dependency
      edits remain Phase 1.
- [x] Review and approve `prior-art-research.md` plus `provider-state-machine-lld.md`, which pin the
      official provider semantics and bounded bootstrap/network/cleanup state machine.
- [x] Obtain saga-lead approval for the exact `gcp-gce` config schema in the HLA before code begins.
- [x] Include the narrow shared readiness-command seam in that pre-implementation review because GCP
      startup-script completion differs from Azure/AWS cloud-init readiness.
- [x] Resolve every schema or readiness-seam finding in the FRD/HLA/plan before delegation.

**DoD:** the public manifest shape and only shared seam are approved; implementation has a stable
contract and no schema guesswork.

## Phase 1: unregistered provider foundation

- [x] Add current stable Google Compute/auth dependencies and regenerate the lock file.
- [x] Implement the auth union, machine catalog, `GcpGCEConfig`, size selection, and image-family
      mapping with exact schema/default/reference tests, including omitted-versus-null outer auth.
- [x] Implement secret-free ambient and service-account credential builders. The explicit builder
      consumes one complete JSON secret, never falls back, and does not retain or chain raw parser
      failures.
- [x] Implement typed Google API/operation mapping plus read-only project, zone, default-network,
      subnet, network-policy enforcement order, live machine shape, `debian-cloud` image, exact
      retained-name collision, priority-zero allow/deny conflict, and external-IP helpers.
- [x] Implement bounded instance/firewall rollback, including exact-shape reconciliation after
      indeterminate deny/allow inserts only after request/operation/provider-ID ownership proof,
      plus first/second-interrupt cleanup helpers with exact survivor/manual-removal tests.
- [x] Generalize `EphemeralTailscaleBootstrap` with the approved non-secret readiness command/label
      while preserving Azure/AWS defaults and all stdin/non-reflection tests.

**DoD:** the new package remains unregistered and therefore unshipped; its reusable provider
foundation is fully typed, secret-free, offline-tested, and the existing platforms are unchanged.

## Phase 2: complete plugin publication

- [x] Implement `GCEPlatform` contract v2 create/start/stop/delete/status/display behavior and
      register it through a new disabled-by-default `gcp` system plugin.
- [x] Implement authenticated runup for zone plus configured-subnet or default-network existence,
      plus the inspectable VPC firewall support boundary, with every definitive failure before
      mutation.
- [x] Implement machine/image/disk selection, metadata SSH identity, credential-free startup script,
      256 KiB encoded size gate, durable run-once marker, auto-deleted boot disk, empty guest
      service-account list, scoped priority-0 TCP/22 allow plus priority-1 all-ingress deny,
      lifetime ephemeral external access, observable secret-free progress, fixed stdin join, and
      optional Tailscale IP result.
- [x] Implement `post_tailscale_ready`, `secure_failed_vm`, and concurrent-safe `transient_route` so
      provisioning/native scoped allows are bounded while lifetime external access keeps ordinary
      egress available.
- [x] Add provider-shaped retained-request tests using quoting-hostile Tailscale and service-account
      sentinels; pin one fixed-stdin delivery and secret-free output/log/diagnostic/exception
      graphs.
- [x] Add create rollback tests for every partial resource set, bootstrap/join failure, first
      interrupt, second interrupt, realized/absent/mismatched firewall-insert timeout, cleanup
      survivor, and exact manual-removal guidance.
- [x] Add lifecycle/idempotency tests for live-IP reads, start, stop, status, delete, already-gone,
      surviving-VM typed failure, deny retention, exact instance/tag/rule name derivation,
      classic-first policy order, priority-zero allow/deny conflicts, all-ingress deny shape,
      firewall hooks, and concurrent transient routes.
- [x] Add registration, plugin enablement/provenance, capability conformance, schema/sample, guide
      catalog/rendering/inertness, and completion-adjacent discovery tests.
- [x] Update the installed-plugin index, permanent plugin/platform/capability docs, command
      reference, resources guide, sample teaching, durable six-surface enumeration, `TopicProse`,
      key-file-to-env-var setup, and recovery guidance in the same publication commit.
- [x] Confirm shell completion implementation needs no bespoke edit because it remains
      registry-driven; update tests and generated/reference projections that enumerate platforms.
- [x] Delete the branch-seeded task brief before the PR leaves draft.

**DoD:** enabling plugin `gcp` exposes a fully functional `gcp-gce` platform; disabled behavior is
honest; no incomplete registration or undocumented operator surface exists; all offline focused
suites and strict typing pass.

## Phase 2a: provider-bundle growth correction

- [x] Reframe `gcp` as an extensible vendor bundle whose service-specific capability names and
      models remain independent; document that future implementations, including secret backends,
      use existing capability contracts rather than a speculative provider abstraction.
- [x] Bundle one `gcloud-cli` `system-install-command` using Google's signed Debian/Ubuntu apt
      repository, current `google-cloud-cli` package, completed-install `gcloud` probe, retry-safe
      key/source reconciliation, and no host or guest authentication side effect.
- [x] Bundle one `aws-cli` `system-install-command` using AWS's current official CLI v2 archive,
      architecture selection, pinned signing-key fingerprint, mandatory detached-signature
      verification, command-owned v2-aware completed-install probe, private temporary extraction,
      retry-safe explicit update/install directories, and no host or guest authentication side
      effect; omit generic `test_exec` because it cannot distinguish CLI v1 from v2.
- [x] Add disabled/enabled provenance, recipe-gate, operator-override, manifest-payload, discovery,
      multi-contribution plugin, architecture, v1-present/v2-present, signing-key/signature
      rejection, and partial-install retry tests; confirm provider lifecycle remains independent of
      both CLIs.
- [x] Update permanent plugin-author, resource, sample, GCP, and AWS operator teaching to
      distinguish optional guest tooling from ambient host ADC/AWS credential sources and optional
      host recovery tooling, and leave completion code registry-driven.
- [x] Run focused/full offline gates and both required reviews for the amended publication before
      recording this corrective phase complete.

**DoD:** vendor plugins can grow through the existing capability/manifest composition boundary;
enabling `gcp` publishes both current GCP contributions, enabling `aws` publishes its existing EC2
platform and optional CLI, and neither guest CLI becomes a provisioning or authentication
dependency.

## Phase 2b: opaque multiline secret correction

- [ ] Amend the shared secret-value contract so the env-var source preserves terminal CR/LF and
      resolution preserves all CR/LF as opaque string content while continuing to reject NUL, with
      value-free outcomes and exception graphs.
- [ ] Move line-safety enforcement to the environment composition/reveal, Git credential, Proxmox
      HTTP-header, and Tailscale stdin consumers whose syntax requires a single logical line; prove
      each rejection happens through pure consumer-owned validation immediately after delivery and
      preserves that path's existing resolve-to-mutation ordering, as well as happening before
      transport, client/header construction, authenticated probing, or rendering, and cannot retain
      the value in its exception graph. Pin zero-mutation VM-create and Tailscale-rekey paths; prove
      conditional repair stays lazy and validates immediately after late delivery but before any
      rejoin-specific mutation; retain final-sink checks as defense in depth.
- [ ] Prove the GCP explicit-auth path accepts the exact pretty-printed LF and CRLF JSON downloaded
      from Google, including its terminal line ending, through the real env-var secret source and
      operation resolver, without compaction, base64 encoding, fallback, persistence, or value
      reflection.
- [ ] Update the permanent secret contract, SSH environment ADR, GCP guide, samples or command
      references if affected, colocated Secrets guide contribution, VM-platform author contract, and
      GCP auth remediation so every operator surface teaches direct whole-document storage and
      sink-local restrictions accurately; pin guide rendering and safety.
- [ ] Run focused secret/env/Git/Tailscale/GCP tests, Ruff, strict mypy, file lint, locked-SDD, and
      full non-integration gates; obtain both required code reviews and resolve every valid finding.

**DoD:** structured text secrets remain unchanged from source to capable consumer; GCP accepts the
downloaded JSON directly; line-oriented consumers still fail closed without exposing values; no
existing secret name, backend, configuration, CLI, sample, or completion contract changes.

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
      the realized instance to prove it has no guest service account or OAuth scopes, and query the
      project after delete to prove zero instance, disk, firewall, and address residue.
- [ ] In that bounded acceptance, select both `gcloud-cli` and `aws-cli`; verify both executables
      are available in the guest, require `aws --version` to begin with `aws-cli/2.`, and verify no
      authenticated Google account, AWS credential file, or AWS profile; rerun initialization to
      prove both installers are idempotent; prove they created no guest authentication state; and
      prove the operator's pre-existing host credential baseline is unchanged.
- [ ] Record exact offline/live/review evidence, add `locked.md`, post the detailed ready-for-review
      disposition, and flip the PR from draft only when every requirement is true.

**DoD:** offline and authorized live evidence prove the shipped behavior and cleanup; reviews and
forge checks are green; permanent docs match code; the merge-ready PR is truthfully locked.

-- agw-ns-gcp-platform (effort lead)
