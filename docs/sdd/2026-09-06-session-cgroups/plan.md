# Session Cgroups: Design and Delivery Plan

- Status: FRD/HLA review checkpoint. Implementation is not authorized or planned in detail yet.
- Owner: session-cgroups effort lead; saga coordination remains with the next-steps lead.
- Vehicle: one draft PR combining FRD and HLA with supporting research and migration strategy.
  Labels: `saga:next-steps`, `sdd:session-cgroups`, and `review-requested` at handoff. No merge
  intent.
- Authorization: operator requested these artifacts and up to three published feedback/fix rounds.
  Private pre-handoff review is the normal quality loop, separate from that published round budget.

## Current review

- [x] Initial FRD/HLA checkpoint reviewed through independent project and complexity lanes at
      `6e5903f7`, with no open findings; evidence published in PR #770.
- [x] Initial checkpoint validated and handed off at `6e5903f7`, with full CI green, the scoped PR
      #770 body, and `review-requested`.
- [ ] Collect and critically assess published feedback under the authorized three-round limit,
      following the standard handoff interval and recording each round in PR comments.
- [ ] Obtain operator disposition of the proposed requirements and open architecture decisions.

This checkpoint does not create a lockfile, amend the saga ledger, or mark future work complete.
Review evidence belongs in the handoff; unchecked items make no claim about implementation progress.

## Proof work before implementation planning

- [ ] Price the restricted same-UID and per-run-user candidates side by side before committing
      live-test resources to either; compare the same execution-channel and workflow requirements.
- [ ] Price Bookworm compatibility against requiring an upgrade to Trixie, including R7 lookup,
      kernel/systemd prerequisites, fallback code, maintenance, security, and validation cost.
      Retain Bookworm in the target until that evidence exists; no removal or weakened R7 is
      authorized merely by this review.
- [ ] Produce a containment LLD and live feasibility evidence covering every reachable execution
      route in HLA's access map, especially sibling tmux, agent SSH authorization, outside same-user
      processes, service managers, schedulers, container APIs, and writable startup state.
- [ ] Demonstrate the selected user model on every retained Debian release, including the costs to
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
supported by measured experiments on every retained Debian baseline; compatibility decisions and
remaining limits have operator disposition; no unexplained outside execution path remains. A failed
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
