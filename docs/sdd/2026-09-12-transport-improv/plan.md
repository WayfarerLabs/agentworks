# Transport Improvements: Design Plan

- Status: First draft; current authorization covers drafting and publishing the FRD/HLA checkpoint
- Delivery vehicle: One draft PR for this initial design review, labeled `sdd:transport-improv`
- Requirements: [FRD](frd.md)
- Architecture: [HLA](hla.md)

This plan records the next design decisions without claiming implementation is approved or done.
Implementation tasks will be expanded after the contracts converge. No lockfile belongs in this
checkpoint.

## Design review

- [ ] Review the required native operations, optional interaction, and recovery environment policy
      with the operator. Done when accepted requirements and unresolved rulings are explicit.
- [ ] Review target ownership and `RunContext` delivery. Done when identity, route, readiness,
      secrets, and lifetime behavior have no conflicting owners.
- [ ] Write execution/context and file/job low-level designs. Done when exact request/result shapes,
      no-staging readiness, bootstrap prerequisites, transfer bounds/path policy, job ownership,
      cancellation, retention, and stale-record handling are specified. Include the existing macOS
      placement-host provisioning/rollback path and its helper prerequisites; it uses the shared job
      mechanism before any guest target exists.
- [ ] Establish Proxmox and WSL2 feasibility under a separately authorized live-test charter. Done
      when each design assumption has observed evidence or an explicit operator disposition.
- [ ] Reconcile the reviewed API against ongoing SSH work and PR #789. Done when reusable work,
      semantic conflicts, ownership, and dependency ordering are recorded.
- [ ] Expand the migration inventory and implementation plan. Done when each existing adapter,
      context producer/consumer, and helper has a destination and obsolete interfaces have a removal
      point, including handling of surviving detached work.

## Implementation completion criteria

Future implementation must satisfy FRD R1-R10, common behavioral conformance, and the acceptance
scenarios. Evidence must distinguish optional-feature refusal from operational failure and exercise
required native work without Tailscale or optional features. Live coverage records platform,
supported major, workstation, and gaps. Documentation-only tests must not pin our prose.

Promote the accepted, implemented contracts into permanent transport/capability documentation and
CLI/provider guidance with the code changes. Closeout requires complete caller migration, disposal
of temporary compatibility code, evidence-backed validation, and a truthful final plan before
creating `locked.md`.
