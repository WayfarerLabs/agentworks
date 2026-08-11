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

- [x] Amend the shared secret-value contract so the env-var source preserves terminal CR/LF and
      resolution preserves all CR/LF as opaque string content while continuing to reject NUL, with
      value-free outcomes and exception graphs.
- [x] Move line-safety enforcement to the environment composition/reveal, Git credential, Proxmox
      HTTP-header, and Tailscale stdin consumers whose syntax requires a single logical line; prove
      each rejection happens through pure consumer-owned validation immediately after delivery and
      preserves that path's existing resolve-to-mutation ordering, as well as happening before
      transport, client/header construction, authenticated probing, or rendering, and cannot retain
      the value in its exception graph. Pin zero-mutation VM-create and Tailscale-rekey paths; prove
      conditional repair stays lazy and validates immediately after late delivery but before any
      rejoin-specific mutation; retain final-sink checks as defense in depth.
- [x] Prove the GCP explicit-auth path accepts the exact pretty-printed LF and CRLF JSON downloaded
      from Google, including its terminal line ending, through the real env-var secret source and
      operation resolver, without compaction, base64 encoding, fallback, persistence, or value
      reflection.
- [x] Update the permanent secret contract, SSH environment ADR, GCP guide, samples or command
      references if affected, colocated Secrets guide contribution, VM-platform author contract, and
      GCP auth remediation so every operator surface teaches direct whole-document storage and
      sink-local restrictions accurately; pin guide rendering and safety.
- [x] Run focused secret/env/Git/Tailscale/GCP tests, Ruff, strict mypy, file lint, locked-SDD, and
      full non-integration gates; obtain both required code reviews and resolve every valid finding.

**DoD:** structured text secrets remain unchanged from source to capable consumer; GCP accepts the
downloaded JSON directly; line-oriented consumers still fail closed without exposing values; no
existing secret name, backend, configuration, CLI, sample, or completion contract changes.

## Phase 3: integration, review, and live acceptance

**Operator ruling, 2026-08-10:** for this effort, the PR draft/ready state is also the live-test
dispatch signal. Draft means the exact head is not ready to test; ready is a merge-intent handoff
that asks the tester to run that head. Any requested changes return the PR to draft before
correction. The final evidence record and SDD lock still follow the passing live disposition below.

- [x] Update from current main and resolve any overlap with the merged vm-platform contract and
      provider-boundary enumeration.
- [x] Run Ruff check/format, strict mypy, full parallel non-integration pytest, file lint, Rulesync
      drift, locked-SDD, guide safety, and diff checks.
- [x] Run the required Agentworks project review and an independent fresh-eyes review; resolve every
      valid finding through the implementation agent and re-review.
- [ ] Load the integration-testing and test-environment skills, inventory the operator-provided GCP
      project/zone/network/auth mode, verify classic-first network-policy order plus the absence of
      conflicting organization/folder terminal rules, set explicit resource/time/cost budgets, and
      obtain operator authorization before any live mutation or credential use.
- [ ] Run one bounded create/init/Tailscale/lifecycle acceptance using an explicit `machine_types`
      override selecting `e2-small` so the opt-in shared-core path is witnessed. While the VM still
      exists, independently query the realized instance and exact `machineTypes.get` CPU, memory,
      architecture, shared-CPU, disk-capacity presence, and accelerator fields, and prove the
      instance has no guest service account or OAuth scopes. Then delete it and query the project to
      prove zero instance, disk, firewall, and address residue.
- [ ] In that bounded acceptance, select both `gcloud-cli` and `aws-cli`; verify both executables
      are available in the guest, require `aws --version` to begin with `aws-cli/2.`, and verify no
      authenticated Google account, AWS credential file, or AWS profile; rerun initialization to
      prove both installers are idempotent; prove they created no guest authentication state; and
      prove the operator's pre-existing host credential baseline is unchanged. Watch the existing
      120-second install-command timeout explicitly during the `gcloud-cli` live pass.
- [x] Once the offline gates, code reviews, and operator prerequisites are green, post the exact
      head and flip the PR from draft to ready as the explicit request for this bounded live
      acceptance.
- [ ] After the live pass, record exact offline/live/review evidence, add `locked.md`, and post the
      detailed merge-ready disposition only when every requirement is true.

### Phase 3a: second live rejection correction

- [x] Route a first `KeyboardInterrupt` raised during ordinary-failure rollback into one more
      idempotent bounded interrupt rollback attempt after at least one owned artifact was removed;
      preserve that interrupt object's identity, prove convergence from partial cleanup, and prove
      only a second interrupt abandons with exact provider-ID survivor guidance.
- [x] Split extended-operation failures into definitive `GCEOperationError`, typed
      `GCECapacityError`, and indeterminate `GCEIndeterminateOperationError`. Classify the exact
      allowlisted `ZONE_RESOURCE_POOL_EXHAUSTED` code only from a DONE operation's
      `operation.error.errors[*].code`, using the cached `operation.status` rather than another
      provider-refreshing predicate, with no provider text or object retention. Have instance and
      firewall inserts reconcile only the indeterminate type, power operations propagate every
      failure, and delete/rollback continue to use verified final state as their postcondition.
- [x] Add provider-shaped DONE/HTTP-503 known-capacity, DONE unknown/malformed, and non-DONE timeout
      regressions; prove classification makes no post-wait provider refresh, definitive insert
      failures cannot become success, partial-cleanup interrupt convergence, second-interrupt
      abandonment, secret-free detached exception graphs, and exact retained coordinates. Update the
      `GCEPlatform` guide contribution and permanent GCP guide with selected-zone capacity recovery,
      and cover rendered wording and safety.
- [x] Run focused GCP rollback/operation/platform tests, Ruff, strict mypy, file lint, locked-SDD,
      Rulesync, full non-integration tests, and both required code reviews; resolve every valid
      finding.

The exact-head disposition and ready handoff originally planned here moved to Phase 3b because the
catalog/support-boundary correction must precede the next live acceptance.

**DoD:** offline and authorized live evidence prove the shipped behavior and cleanup; reviews and
forge checks are green; permanent docs match code; the merge-ready PR is truthfully locked.

### Phase 3b: catalog and machine-compatibility correction

- [x] Add `e2-small` and `e2-medium` ahead of the standard E2 built-in ladder, with deterministic
      size-selection tests and permanent teaching that they expose two guest vCPUs but sustain an
      aggregate 0.5 and 1 vCPU respectively with automatic bursting.
- [x] Reject known live incompatibilities with the CPU-only, Balanced Persistent Disk contract
      before mutation using provider fields rather than a machine-name allowlist. Reject populated
      zero Persistent Disk capacity or required guest accelerators with a typed, actionable,
      secret-free configuration error that names the selected machine type and supported boundary;
      accept omitted output-only capacity as unknown. Because GCE has no read-only complete
      machine/disk-pair validator, add fixed prerequisite plus machine/`pd-balanced` guidance to
      residual definitive instance-insert failures and retain bounded rollback.
- [x] Add a DONE structured-operation regression proving a longer string containing
      `ZONE_RESOURCE_POOL_EXHAUSTED` remains a generic definitive `GCEOperationError`, then run the
      focused and repository gates plus both required code reviews and resolve every valid finding.
- [x] Post the signed exact-head handoff and flip the PR from draft to ready for the next
      operator-gated live acceptance.

**DoD:** small templates have honest low-cost burstable defaults; known-incompatible custom machine
types fail before mutation, while residual pair incompatibilities fail definitively with useful
guidance and bounded rollback; capacity classification stays exact; the next live test starts from a
reviewed, green handoff.

The built-in shared-core default and command-only AWS completion predicate completed above were
superseded before merge by Phase 3c after live/review evidence exposed their semantic limits.

### Phase 3c: sustained-default and installer-idempotency correction

- [x] Restore the standard E2 built-in ladder so existing two-vCPU templates retain two sustained
      vCPUs. Keep `e2-small` and `e2-medium` available through the existing site `machine_types`
      override, and teach the exact override shape, deterministic selection rule, guest-visible vCPU
      count, and sustained/burst capacity without a future product promise.
- [ ] Extend the shared install-command contract so a slash-containing `test_exec` uses `test -x` in
      VM and agent runners while a bare name retains PATH lookup. Give AWS CLI a public-launcher
      executable predicate plus an Agentworks-owned completion marker written only after verified
      install success; require the marker and both public/internal executables in the command's
      managed fast path. Before managed repair, invalidate any prior marker; after installer
      success, require both executable checks before recreating it. Retain the version probe
      for valid external v2 and the update path for v1, missing markers, broken launchers, or
      partial layouts. Add real Bash/Zsh/Dash predicate and initializer-runner regressions proving
      completed managed reinit performs no installer transport, every incomplete state repairs,
      failed or malformed repair leaves no marker, unavailable local shells skip, and controlled
      Linux CI exercises all three shells.
- [x] Distinguish omitted required live CPU or memory fields, present non-positive values, and
      present positive declaration mismatches with typed, actionable pre-mutation errors. Add
      separate zero-mutation regressions for CPU and memory across those branches; document that
      omitted optional disk capacity proceeds to insert; align the capacity error description and
      SDK-shape fallback comment.
- [ ] Run focused and full gates plus both required code reviews, resolve every valid finding, and
      refresh the PR's exact-head/current-main/evidence record.
- [ ] Post the signed exact-head handoff and flip the PR from draft to ready for the single bounded
      Phase 3 live charter above, including proof that AWS CLI reinit skips the completed managed
      install.

**DoD:** standard defaults preserve sustained capacity, shared-core remains an explicit and live
witnessed opt-in, managed AWS CLI reinit converges without reinstall, permanent teaching matches the
shipped behavior, and the next ready transition identifies one reviewed exact head.

-- agw-ns-gcp-platform (effort lead)
