# Session Cgroups: Design and Delivery Plan

- Status: FRD/HLA review checkpoint. Implementation is not authorized or planned in detail yet.
- Owner: session-cgroups effort lead; saga coordination remains with the next-steps lead.
- Vehicle: one draft PR combining FRD and HLA with supporting research and migration strategy.
  Labels: `saga:next-steps`, `sdd:session-cgroups`, and `review-requested` at handoff. No merge
  intent.
- Authorization: operator requested these artifacts and up to three published feedback/fix rounds.
  Private pre-handoff review is the normal quality loop, separate from that published round budget.

## Current review

- [ ] Review FRD and HLA together through the independent project and complexity lanes.
- [ ] Validate documentation, check artifact ownership and cross-file consistency, and hand off one
      pushed green head with a scoped PR body and the checkpoint label.
- [ ] Collect and critically assess published feedback under the authorized three-round limit,
      following the standard handoff interval and recording each round in PR comments.
- [ ] Obtain operator disposition of the proposed requirements and open architecture decisions.

This checkpoint does not create a lockfile, amend the saga ledger, or mark future work complete.
Review evidence belongs in the handoff; unchecked items make no claim about implementation progress.

## Proof work before implementation planning

- [ ] Produce a containment LLD and live feasibility evidence covering every reachable execution
      route in HLA's access map, especially sibling tmux, agent SSH authorization, outside same-user
      processes, service managers, schedulers, container APIs, and writable startup state.
- [ ] Demonstrate the proposed existing-UID design on Bookworm and Trixie, including the costs to
      supported harnesses and workflows. Record WSL2 runtime prerequisites separately from Debian
      package versions. Escalate any required compatibility or identity-model change to the
      operator.
- [ ] Select and prove the supervised tmux anchor, pane-exit semantics, startup diagnosis, companion
      shell lifetime, concurrent-fork termination, and empty-group evidence.
- [ ] Produce an identity/lifecycle LLD covering schema ownership, protected registration, process
      identity races, namespace views, Unix socket attribution, secrets, concurrent operations,
      crash recovery, and run invalidation.
- [ ] Settle the legacy transition proof and interruption policy using the migration strategy.

Definition of done for this stage: the FRD's guarantees have a concrete enforceable design,
supported by measured experiments on both Debian baselines; compatibility decisions and remaining
limits have operator disposition; no unexplained outside execution path remains. A failed
feasibility result changes the proposal through review, not the meaning of the guarantee silently.

## Subsequent implementation plan boundary

After design acceptance, expand this plan into bounded implementation steps and definitions of done
covering orchestration, VM/agent initialization, runtime launch/stop/status, identity persistence
and lookup, companion shells, migration, and behavioral tests. Include exact live acceptance
scenarios from the FRD, failed/indeterminate operations, and selected-run isolation. Load the
live-test skills and operator environment inventory before any experiment that changes a VM.

Permanent docs, diagnostics, guide topics, and affected CLI collateral ship alongside the code that
makes them true. Record the adopted systemd lifecycle and containment decision in an ADR, and
promote durable architecture and operating guidance before closeout. A final lockfile is appropriate
only when the effort finishes or is explicitly abandoned; neither a clean draft review nor an
exhausted feedback budget finishes this effort.
