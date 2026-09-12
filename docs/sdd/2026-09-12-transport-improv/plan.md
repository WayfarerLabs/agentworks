# Transport Improvements: Design Plan

- Status: Revised draft; current authorization covers design documents and draft PR publication
- Delivery vehicle: Draft PR #795 for design review, labeled `sdd:transport-improv`
- Requirements: [FRD](frd.md)
- Architecture: [HLA](hla.md)
- Proposed interfaces and layout: [Execution contract](execution-contract.md)

This plan records the next design decisions without claiming implementation is approved or done.
Implementation tasks will be expanded after the contracts converge. No lockfile belongs in this
checkpoint.

## Design review

- [ ] Review the required native operations, optional interaction, and recovery environment policy
      with the operator. Done when accepted requirements and unresolved rulings are explicit.
- [ ] Review target ownership and `RunContext` delivery. Done when identity, route, readiness,
      secrets, shell defaults, and lifetime behavior have no conflicting owners.
- [ ] Write execution/context and file/job low-level designs. Done when exact request/result shapes,
      no-staging readiness, bootstrap prerequisites, transfer bounds/path policy, job ownership,
      cancellation, retention, and stale-record handling are specified. Include the existing macOS
      placement-host provisioning/rollback path and its helper prerequisites; it uses the shared job
      mechanism before any guest target exists.
- [ ] Specify interpreter and startup policy. Done when fixed `sh`/`bash`, supported explicit
      interpreters, destination user-shell resolution after elevation, login/interactive startup,
      carrier bootstrap, and readiness preparation have precise behavior and acceptance cases.
- [ ] Establish Proxmox and WSL2 feasibility under a separately authorized live-test charter. Done
      when each design assumption has observed evidence or an explicit operator disposition.
- [ ] Have the SSH developer review the proposed #757 boundary and carrier seam. Done when singular
      ownership of the new SSH package, the `Carrier.execute` request/report and I/O contract,
      trust/configuration preservation, and dependency ordering are agreed. This revises the earlier
      consolidation proposal; the SSH effort must reconcile its own SDD, which this effort does not
      edit.
- [ ] Reconcile PR #789's native recovery path with the common execution service. Done when its
      required behavior has a home without parallel production execution entry points.
- [ ] Expand the migration inventory and implementation plan. Done when each existing adapter,
      context producer/consumer, and helper has a destination and obsolete interfaces have a removal
      point, including handling of surviving detached work.

## Build and cutover preparation

- [ ] Plan the independent new-stack build in destination modules. Done when internal test entry
      points exercise its contracts without changing production context delivery or exposing a
      second public plugin API. This is a design task, not an implementation-completion claim.
- [ ] Finalize package dependencies and the removal inventory. Done when the new SSH and execution
      implementations and tests run with legacy modules unavailable, including indirect imports,
      plugin package initialization and retained utility dependencies. Copied code must satisfy the
      new contract independently; no bridge may call the old stack.
- [ ] Specify SSH state transition and deletion gates. Done when trust/configuration preservation,
      concurrent-writer ownership, rollback evidence, and production operation after physical legacy
      module deletion have testable acceptance criteria. Replacement code reads old data directly.
- [ ] Map FRD R11's future core/plugin workflows to acceptance cases. Done when each facility has a
      concrete scenario regardless of whether an old-stack caller exists.
- [ ] Define cutover gates and rollback/state handling. Done when complete workflow evidence,
      integration with #757, caller shell/identity audits, plugin compatibility, surviving jobs, and
      removal of the old stack are required by one coherent production transition.

## Implementation completion criteria

Future implementation must satisfy FRD R1-R11, common behavioral conformance, and the acceptance
scenarios. Evidence must distinguish optional-feature refusal from operational failure and exercise
required native work without Tailscale or optional features. Live coverage records platform,
supported major, workstation, and gaps.

Promote the accepted, implemented contracts into permanent transport/capability documentation and
CLI/provider guidance with the code changes. Closeout requires complete caller migration, disposal
of temporary compatibility code, evidence-backed validation, and a truthful final plan before
creating `locked.md`.
