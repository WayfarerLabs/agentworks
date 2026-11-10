# Implementation and delivery plan

Owner: the effort lead acting under the operator's single-PR charter. Governing requirements are
[frd.md](frd.md); response artifacts are [hla.md](hla.md),
[runtime-state-lld.md](runtime-state-lld.md), [batch-preparation-lld.md](batch-preparation-lld.md),
[migration-strategy.md](migration-strategy.md), and [prior-art-research.md](prior-art-research.md).

## Delivery vehicle and authorization

All artifacts, implementation, permanent documentation, evidence, and final lock belong to PR #885
on `feat/desired-runtime-state`, based on main. No split PR, stack, early merge, or merge operation
is authorized. Private implementation commits may be partial; each published handoff is complete for
its checkpoint and green. The PR remains draft until the complete implementation handoff.

| Stage                   | Signal                                      | Published fix allowance               | Current record                            |
| ----------------------- | ------------------------------------------- | ------------------------------------- | ----------------------------------------- |
| FRD                     | Draft plus `review-requested`               | Up to two rounds                      | Converged at `c6337609d`, two rounds used |
| Complete design         | Draft plus `review-requested`               | Up to two rounds                      | Not yet handed off, zero rounds used      |
| Complete implementation | Ready                                       | Up to two rounds                      | Not started                               |
| Closeout                | Final lock in the same PR after convergence | No new feature/fix allowance inferred | Not started                               |

The FRD convergence reading is PR comment 6762771633. The final report accepted prior corrections;
its remaining phrase suggestion was independently assessed as optional. The HLA states uniform
activation of all selected running-intent VMs, regardless of children, without amending the FRD.

Before each authorized published fix round, collect the standard minimum one-hour window after the
handoff being answered. Freeze the round's report/thread batch, critically assess every item, remove
`review-requested` or return ready to draft before edits, run required private lanes and checks,
then publish one exact head and scoped summary. Later findings belong to the next batch. Material
scope/requirements changes, unsafe fixes, or an exhausted allowance return to the operator; authored
GitHub feedback does not extend authority. Optional wording does not independently gate.

## Phase 0: requirements and factual survey

- [x] Author and publish the FRD covering all three runnable kinds, non-terminal exit semantics,
      explicit adoption, VM intent replacement, and shared workload secret preparation.
- [x] Complete both authorized FRD feedback rounds with independent private project and complexity
      re-reviews, green CI, and recorded critical readings; establish convergence at `c6337609d`.
- [x] Survey the current VM flag, schema/restore constraints, lifecycle acceptance, activation guard
      ordering, per-session readiness contexts, batch resolution, console pane collection, and
      CLI/completion/JSON seams against main `438c4546c`.

## Phase 1: complete design checkpoint

- [ ] Finish and cross-check every linked artifact, including data/identity constraints, complete
      cutover, concrete preparation/execution seams, failure isolation, preview, compatibility, and
      migration guidance. No unspecified user-facing policy may be deferred to an implementation
      guess.
- [ ] Run an independent project review and complexity pass at one pinned design head in separate
      detached worktrees. Critically resolve findings; add a targeted correctness reading where an
      algorithm or failure example needs it. Keep the lead responsible for architecture and plan.
- [ ] Run documentation lint/format/spelling, typography and link checks, locked-SDD guard, and
      applicable required gates. Reuse unchanged-code evidence honestly; CI must be green at
      handoff.
- [ ] Publish the complete-design checkpoint in PR #885 with a scoped handoff and
      `review-requested`, then monitor and process at most two authorized published fix rounds.
- [ ] Record design convergence and only then begin implementation under the existing charter.

Definition of done: a complete, internally consistent design with no unresolved material finding, an
explicit migration and CLI contract, a green handed-off head, and a recorded convergence basis.

## Phase 2: storage, intent, and VM cutover

- [ ] Add the string-compatible desired-runtime enum, per-kind constraints, nullable legacy workload
      targets, durable VM/console UUIDs, and strict persisted-boundary conversion.
- [ ] Implement atomic migration 39, remove old VM flag storage, preserve all current table facts,
      relationships and constraints, and update schema/foreign-key restore sentinels.
- [ ] Add named expected-UUID database target writers and resource-owned pure intent services.
      Ensure idempotent writes and no runtime/config/secret work in pure target selection.
- [ ] Update VM creation/start/stop and every VM activation/read/display consumer to the new target;
      remove the old flag setter, row field, and decision paths. Retain only historical migration
      references and the derived JSON compatibility projection.
- [ ] Extract runtime-only VM operations usable by reconciliation without changing target. Preserve
      conditional connection repair for provider-running VMs and active holds across guest work.
- [ ] Close the VM auto-start race using the existing family guard, fresh UUID/target checks, and
      explicit held-token reuse through all current guarded agent/workspace/session callers.
- [ ] Verify populated and historical migrations, rollback/retry, UUID stability/replacement,
      database restore, row-backed backup serialization, wrong-kind values, and Windows lock
      behavior.

Definition of done: one VM intent authority with no parallel compatibility policy; migration is
retryable and tested, every affected writer uses the same guard, and standalone VM lifecycle works
with the new model while provider contracts remain unchanged.

## Phase 3: prepared session and console operations

- [ ] Separate session acceptance, read-only eligibility, shared-secret collection, and execution.
      Retain UUID-bound confirmation, per-session scope/transport, command checks, setup readiness,
      continuation, artifact publication, and fingerprinted teardown semantics.
- [ ] Record accepted session intent before execution, including batch running no-ops and blocked
      child starts; keep evidence-only stop/recovery and failed-create cleanup independent of
      intent.
- [ ] Replace the existing per-session full-service batch launch loop with shared preparation and
      one complete eligible workload resolution. Include safe residual starts and ordinary no-ops.
- [ ] Separate console observation/target collection from staging cleanup and realization. Add
      intent/UUID handling, preserve membership independence, and guard definition/member mutations.
- [ ] Retain canonical view identity for healthy and staging-cleanup-only cases; build missing views
      from stored member order, shell panes, and admin-shell configuration with supplied values.
- [ ] Align the existing admin-shell collector and delivery: the baseline collects VM/admin env but
      the builder omits it. Use existing literal env composition/delivery for that shell so
      preparation does not request values it then ignores. Keep the existing env scope and transport
      contract; do not expand into a transport rewrite.
- [ ] Make explicit child stop and reconciliation below an already-off VM avoid pointless VM
      activation. Unknown/broken state keeps existing refusal and explicit-force boundaries.
- [ ] Demonstrate no runtime mutation before shared secret success, no source calls in executors, no
      target changes during internal cleanup, and preservation of healthy runtime identities.

Definition of done: singular and batch lifecycle use the same concrete domain helpers, every
eligible runtime input is prepared before execution, and console panes receive exactly their
existing intended scopes. Refused/no-op paths do not resolve unused workload secrets.

## Phase 4: reconciliation, preview, and operator commands

- [ ] Implement explicit VM/fleet/session/console scopes, strict name-or-all validation, retained
      UUID selection, pending-adoption blockers, and stopped-parent precedence.
- [ ] Implement isolated infrastructure credential preparation with invocation-only success/failure
      caching, shared conditional repair values, and active holds. No new secret backend API or
      cache survives the invocation.
- [ ] Activate all selected running-intent VMs, including both childless VMs and VMs with only
      stopped children, before the workload boundary. Leave idle on-demand VMs off when not needed.
- [ ] Union eligible workload graph/env/pane requirements on the existing Resolver, seed successful
      infrastructure values, fail on any required cached infrastructure error without retrying,
      validate all consumers, and prohibit further source resolution afterward.
- [ ] Execute sessions before console builds and all provider stops after shared secret success;
      isolate independent failures, preserve targets, propagate cancellation, and report partial
      effects.
- [ ] Implement bounded final observations and typed changed/unchanged/failed/blocked results,
      including a nonzero aggregate result when selected targets remain incomplete.
- [ ] Implement dry run with no activation, repair, workload source preview/resolution, staging,
      target write, or execution; explain conditional guest checks when a VM is off.
- [ ] Add thin `set-desired-state` and `reconcile` CLI verbs under all three resource groups,
      existing name completions, static state choices, additive inspection target fields, and the
      derived VM JSON compatibility field. Do not add a separate command-output schema family.

Definition of done: the real CLI can restore a VM scope and fleet, retry one workload, preview
without mutation, and adopt intent without runtime work. Every action is accounted for and uses
existing domain authorities, with no generic runnable lifecycle framework.

## Phase 5: permanent collateral and private quality

- [ ] Promote the surviving contract into `docs/guides/desired-runtime-state.md`, runnable-status
      guidance, command reference, package/root operational guidance as needed, and the
      orchestration README. Explain batch versus per-target compositions and their separate
      readiness contexts.
- [ ] Update retained management teaching and completions with the same commands and safe scope
      guidance. Verify sample config/schema need no new target option; update any outdated
      collateral.
- [ ] Document explicit adoption, accepted intent on failure/cancellation, VM on-demand versus
      running behavior, parent precedence, console independence, preview limits, and batch-secret
      failure changes. Add the implementing commit's breaking-change footer.
- [ ] Run the complete implementation's independent project review, complexity pass, and a
      fresh-context generic correctness/security pass at one pinned head in separate worktrees.
      Scale depth to migration, concurrency, secret delivery, and lifecycle behavior; verify
      material claims.
- [ ] Run required Python lint/format/type/test and typer-isolation gates, file-quality linters,
      locked-SDD and rulesync checks, website Python/Node tests, and deterministic double builds on
      real exit codes. Add meaningful Windows-marked coverage for host-sensitive changes.

Definition of done: no material private finding remains; permanent docs describe shipped behavior
without relying on SDD paths; required gates are green and evidence names the exact tested head.

## Phase 6: live validation and implementation handoff

The ready handoff starts the standard PR-level validation pipeline. Its exact-head reports can
supply the required live evidence below. An owner-run campaign may precede that handoff when its
inventory is available. If live evidence is pending at ready, disclose it, leave those items open,
and require actual evidence or an authorized disposition before claiming convergence or locking.

- [ ] Establish the operator-approved inventory, fixture prefix, platform/workstation lanes, and
      resource budget before live mutation. Use the `agw-test-env` and `integration-testing` skills;
      snapshot operator state first when touched. Missing inventory is not permission to invent one.
- [ ] Invoke a scoped tester in its own pinned worktree with the relevant inventory/naming/budget
      sections and authorization for owned scratch resources. Use this commit's CLI, not a release
      stand-in; confirm the new commands actually exist on every test bed.
- [ ] Drive real VM restart or external shutdown with two shell sessions and a named console,
      restore their intended states, then repeat reconciliation and prove healthy identities remain.
- [ ] Drive pure adoption and dry run; explicit child/console stop; explicit parent stop retaining
      child targets; subsequent VM start/recovery; direct session exit; console staging recovery;
      missing-secret barrier; non-interactive refusal; and reachable unknown/failure cases.
- [ ] Record the full command surface reached, each observed pass/failure, unreachable lanes, and
      both workstation and platform coverage. Unit evidence supplements, not replaces, live
      recovery.
- [ ] Delete owned fixtures and independently verify native, provider, local-file, and tailnet
      cleanup. Preserve operator resources and restore any altered operator state.
- [ ] Publish the complete implementation as ready in this same PR with exact-head evidence, then
      monitor all published review and validation reports. Apply at most the two authorized
      implementation feedback rounds, after collecting the standard window and critically reading
      every finding. Re-run the required private lanes and affected gates before each handoff.
- [ ] Record implementation convergence with all material items resolved and required live evidence
      present. An unavailable live bed must remain an explicit gap until an authorized disposition
      or actual evidence resolves it; it cannot silently become a pass.

Definition of done: complete implementation, truthful exact-head validation, verified cleanup, green
CI, and converged authorized published review. No merge occurs under this plan.

## Phase 7: closeout

- [ ] Audit every requirement and acceptance criterion against code, behavioral tests, permanent
      documentation, and live evidence. Finish outstanding collateral and record actual outcomes.
- [ ] Complete every truthful remaining plan item and create dated `locked.md` in this same PR,
      summarizing the final implementation and actual validation/coverage. Do not lock unfinished
      work.
- [ ] Run closeout project and complexity reviews at a pinned head, required affected checks, and a
      complete handoff for the final green state. Remove checkpoint labels when no checkpoint
      remains.
- [ ] Clean up lead/delegate scratch worktrees, branches, temporary outputs, and owned live residue
      without deleting the reviewable feature branch or unrelated resources. Report PR and final
      state.

## Delegate ownership after design convergence

The lead owns architecture, this plan, shared coordination, CLI/completion integration, permanent
documentation, and final integration. Bounded implementation is delegated in isolated worktrees:

- Data/VM owner: database models/converters/migration/accessors/backup boundaries, VM intent and
  runtime helpers/inspection, and explicit guard threading in the existing guarded VM callers.
- Session owner: session manager launch/stop preparation and execution, batch launch composition,
  session intent and inspection, and their focused tests.
- Console owner: console lifecycle/definition guards, preparation/execution, pane delivery, console
  intent and inspection, and their focused tests.

The data owner delivers the schema/type/accessor foundation first so other workers build against
real interfaces. Subsequent helper commits may integrate while their independent domains proceed.
The lead coordinates shared exports and new reconciliation shims after those owned changes; no
worker edits another owner's files without an explicit handoff. Every writer commits/pushes its own
branch and reports exact heads. Required private review lanes receive their own detached trees.

## Traceability and decisive scenarios

| Requirement | Decisive evidence                                                                              |
| ----------- | ---------------------------------------------------------------------------------------------- |
| R1          | Three-kind target persistence, local inspection, no intent changes on runtime exit             |
| R2          | Accepted failure retention, no-op adoption, pre-accept refusal, blocked-child nonzero          |
| R3          | All explicit scopes, selector refusal, no healthy replacement or hidden initialization         |
| R4          | VM/session/console order, independent membership, parent stop and off-child no-op              |
| R5          | UUID replacement and contention tests, unsafe/unknown refusal, retry idempotence               |
| R6          | Non-mutating preview, bounded final status, per-target results and partial cancellation        |
| R7          | Mixed secret union, failed barrier prevents VM stop, shared repair/cache and zero late prompts |
| R8          | Historical migration/adoption, JSON compatibility, backup/restore and permanent guide          |
| R9          | One PR, actual private/published review record, green gates, live observations and final lock  |

The operator's live inventory and any unresolved validation gap remain explicit work items, not
assumed environment facts. New material design decisions return to the lead; requirements or scope
changes beyond the charter return to the operator.
