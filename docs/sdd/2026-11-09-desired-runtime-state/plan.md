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
| Complete design         | Draft plus `review-requested`               | Up to two rounds                      | Converged at `32a176600`; two rounds used |
| Complete implementation | Ready                                       | Up to two rounds                      | Round one validation; one round used      |
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

The operator confirmed on 2026-11-10 that a separate integration tester automatically picks up the
published implementation PR. Its report may take up to four hours. Wait for that handoff's
integration result before starting another implementation iteration, even if other review feedback
has arrived and the minimum one-hour collection window has elapsed. Continue monitoring all lanes;
elapsed time alone is not a test result or permission to proceed.

## Phase 0: requirements and factual survey

- [x] Author and publish the FRD covering all three runnable kinds, non-terminal exit semantics,
      explicit adoption, VM intent replacement, and shared workload secret preparation.
- [x] Complete both authorized FRD feedback rounds with independent private project and complexity
      re-reviews, green CI, and recorded critical readings; establish convergence at `c6337609d`.
- [x] Survey the current VM flag, schema/restore constraints, lifecycle acceptance, activation guard
      ordering, per-session readiness contexts, batch resolution, console pane collection, and
      CLI/completion/JSON seams against main `438c4546c`.

## Phase 1: complete design checkpoint

- [x] Finish and cross-check every linked artifact, including data/identity constraints, complete
      cutover, concrete preparation/execution seams, failure isolation, preview, compatibility, and
      migration guidance. No unspecified user-facing policy may be deferred to an implementation
      guess.
- [x] Run an independent project review and complexity pass at one pinned design head in separate
      detached worktrees. Critically resolve findings; add a targeted correctness reading where an
      algorithm or failure example needs it. Keep the lead responsible for architecture and plan.
- [x] Run documentation lint/format/spelling, typography and link checks, locked-SDD guard, and
      applicable required gates. Reuse unchanged-code evidence honestly; CI must be green at
      handoff.
- [x] Publish the complete-design checkpoint in PR #885 with a scoped handoff and
      `review-requested`, then monitor and process at most two authorized published fix rounds.
- [x] Record design convergence and only then begin implementation under the existing charter.

The complete-design convergence record is PR comment 6766449528 at `32a176600`, after two authorized
fix rounds. Both private lanes and the final published delta reported no material findings, and CI
run 42808435958 was green. Optional wording was assessed without another round.

Definition of done: a complete, internally consistent design with no unresolved material finding, an
explicit migration and CLI contract, a green handed-off head, and a recorded convergence basis.

## Phase 2: storage, intent, and VM cutover

- [x] Add the string-compatible desired-runtime enum, per-kind constraints, nullable legacy workload
      targets, durable VM UUIDs, guarded console selection, and strict persisted-boundary
      conversion.
- [x] Implement atomic migration 39, remove old VM flag storage, preserve all current table facts,
      relationships and constraints, and update schema/foreign-key restore sentinels.
- [x] Add named target writers, expected-UUID checks for VM/session, and resource-owned pure intent
      services; select consoles under the family guard. Ensure idempotent writes and no
      runtime/config/secret work in pure target selection.
- [x] Update VM creation/start/stop and every VM activation/read/display consumer to the new target;
      remove the old flag setter, row field, and decision paths. Retain only historical migration
      references and the derived JSON compatibility projection.
- [x] Extract runtime-only VM operations usable by reconciliation without changing target. Preserve
      conditional connection repair for provider-running VMs and active holds across guest work.
- [x] Close the VM auto-start race using the existing family guard, fresh UUID/target checks, and
      explicit held-token reuse through all current guarded agent/workspace/session callers. Guard
      hold admission too; release admission-only locking before interactive access. Require bounded
      same-keeper guest acknowledgement on WSL entry, with complete failure cleanup.
- [x] Verify populated and historical migrations, rollback/retry, UUID stability/replacement,
      database restore, row-backed backup serialization, wrong-kind values, and Windows lock
      behavior.

Definition of done: one VM intent authority with no parallel compatibility policy; migration is
retryable and tested, every affected writer uses the same guard, and standalone VM lifecycle works
with the new model while provider contracts remain unchanged.

## Phase 3: prepared session and console operations

- [x] Separate session acceptance, read-only eligibility, shared-secret collection, and execution.
      Retain UUID-bound confirmation, per-session scope/transport, command checks, setup readiness,
      continuation, artifact publication, and fingerprinted teardown semantics.
- [x] Record accepted session intent before execution, including batch running no-ops and blocked
      child starts; keep evidence-only stop/recovery and failed-create cleanup independent of
      intent.
- [x] Replace the existing per-session full-service batch launch loop with shared preparation and
      one complete eligible workload resolution. Include safe residual starts and ordinary no-ops.
- [x] Route batch stop through non-activating parent observation and prepared stops, including
      accepted absent no-ops and existing filters/force. Retire all old batch boundary helpers,
      unused best-effort exports, and the sole Resolver late-repair method. Update the obsolete
      activation late-repair docstring and replace tests patching retired helpers with coverage of
      the new composition.
- [x] Separate console observation/target collection from staging cleanup and realization. Add
      intent handling and selection under guards, preserve membership independence, and guard
      definition/member/cascade mutations. Put console delete selection and consent under the same
      guard and reuse held tokens for nested cleanup.
- [x] Retain canonical view identity for healthy and staging-cleanup-only cases; build missing views
      from stored member order, shell panes, and admin-shell configuration with supplied values.
- [x] Align the existing admin-shell collector and delivery: the baseline collects VM/admin env but
      the builder omits it. Use existing literal env composition/delivery for that shell so
      preparation does not request values it then ignores. Keep the existing env scope and transport
      contract; do not expand into a transport rewrite.
- [x] Make explicit child stop and reconciliation below an already-off VM avoid pointless VM
      activation. Unknown/broken state keeps existing refusal and explicit-force boundaries.
- [x] Demonstrate no runtime mutation before shared secret success, no source calls in executors, no
      target changes during internal cleanup, and preservation of healthy runtime identities.

Definition of done: singular and batch lifecycle use the same concrete domain helpers, every
eligible runtime input is prepared before execution, and console panes receive exactly their
existing intended scopes. Refused/no-op paths do not resolve unused workload secrets.

## Phase 4: reconciliation, preview, and operator commands

- [x] Implement explicit VM/fleet/session/console scopes, strict name-or-all validation, retained
      VM/session UUID checks, guarded console selection, adoption blockers, and stopped-parent
      precedence.
- [x] Extend the existing Resolver gate path with invocation-only value-free failure memoization;
      register gate names only after success. Share seeded successes and conditional repair values
      while isolating failed families. Add no parallel cache or secret backend API.
- [x] Activate all selected running-intent VMs, including both childless VMs and VMs with only
      stopped children, before the workload boundary. Leave idle on-demand VMs off when not needed.
- [x] Register eligible workload graph/env/pane requirements on that same Resolver. Check required
      declarations against gate failures inside resolve before any source call, reuse seeded
      successes, and validate every consumer. Failed boundaries terminate and dispose the
      invocation; execution and final observation receive no resolution callback.
- [x] Execute sessions before console builds and all provider stops after shared secret success;
      isolate independent failures, preserve targets, propagate cancellation, and report partial
      effects.
- [x] Implement bounded final observations and typed changed/unchanged/failed/blocked results,
      including a nonzero aggregate result when selected targets remain incomplete.
- [x] Implement dry run with no activation, repair, workload source preview/resolution, staging,
      target write, or execution; explain conditional guest checks when a VM is off.
- [x] Add thin `set-desired-state` and `reconcile` CLI verbs under all three resource groups,
      existing name completions, static state choices, additive inspection target fields, and the
      derived VM JSON compatibility field. For powered-off observations, map stopped intent to
      manual, on-demand to idle, and running to null disposition. Add no command-output schema
      family.

Definition of done: the real CLI can restore a VM scope and fleet, retry one workload, preview
without mutation, and adopt intent without runtime work. Every action is accounted for and uses
existing domain authorities, with no generic runnable lifecycle framework.

## Phase 5: permanent collateral and private quality

- [x] Promote the surviving contract into `docs/guides/desired-runtime-state.md`, runnable-status
      guidance, command reference, package/root operational guidance as needed, and the
      orchestration README. Explain batch versus per-target compositions and their separate
      readiness contexts.
- [x] Update retained management teaching and completions with the same commands and safe scope
      guidance. Verify sample config/schema need no new target option; update any outdated
      collateral.
- [x] Document explicit adoption, accepted intent on failure/cancellation, VM on-demand versus
      running behavior, parent precedence, console independence, preview limits, and batch-secret
      failure changes, per-family preparation isolation, and contention during secret prompts,
      console consent, and hold admission. Add the implementing commit's breaking-change footer.
- [x] Explain that live WSL guests with stopped intent receive no idle protection during access or
      management commands and may stop mid-command. Show explicit on-demand/running selection as
      permitting normal holds and activation, rather than a keepalive-only switch.
- [x] Run the complete implementation's independent project review, complexity pass, and a
      fresh-context generic correctness/security pass at one pinned head in separate worktrees.
      Scale depth to migration, concurrency, secret delivery, and lifecycle behavior; verify
      material claims.
- [x] Run required Python lint/format/type/test and typer-isolation gates, file-quality linters,
      locked-SDD and rulesync checks, website Python/Node tests, and deterministic double builds on
      real exit codes. Add meaningful Windows-marked coverage for host-sensitive changes.

Implementation checkpoint, 2026-11-10: the full feature and permanent guidance are present in this
PR. Independent project, complexity, and generic correctness reviews found and verified corrections
for cancellation, recovery, observation bounds, preview isolation, and repeated preparation. Those
corrections are integrated. A legacy-agent transport report was refuted against the persisted socket
constraint and prompted no change. Evidence-only checkpoint recovery remains behind the shared
secret barrier and never falls through to a launch.

Final verification at `ca3e94194` passed 11,652 tests with 11 skips, plus lint, format, type,
typer-isolation, file-quality, locked-SDD and rulesync checks. Website verification passed 160
Python and 103 Node tests and deterministic double builds at both site bases. All three Linux Python
CI jobs passed. The preview-disappearance regressions use normal public deletion services through a
second database connection and preserve independent results without acquiring a preview guard.

The Windows gate exposed a synthetic producer's text-newline translation. The test-only correction
at `9b1193e00` emits guest LF bytes directly; production validation remains strict. Its keeper suite
passed 19 tests with one Linux skip, and the native Windows CI job passed. Application code,
permanent guidance, and SDD content were unchanged from the complete-feature review head. Full
project, complexity, and generic correctness reviews at `ca3e94194`, followed by all three delta
reviews at `9b1193e00`, were clean. Final handoff CI must be green before ready is applied.

Local CLI process checks used isolated state and exercised all six help paths, pure setters,
repeated setters, invalid child-state choices, and JSON inspection. They did not exercise a
provider. Actual recovery and Windows/WSL live evidence await the separate integration tester.

Definition of done: no material private finding remains; permanent docs describe shipped behavior
without relying on SDD paths; required gates are green and evidence names the exact tested head.

## Phase 6: live validation and implementation handoff

The ready handoff starts the standard PR-level validation pipeline. Its exact-head reports can
supply the required live evidence below. An owner-run campaign may precede that handoff when its
inventory is available. If live evidence is pending at ready, disclose it, leave those items open,
and require actual evidence or an authorized disposition before claiming convergence or locking.

- [x] Obtain the separate integration tester's report identifying its approved inventory, fixture
      prefix, platform/workstation lanes, resource budget, and required state snapshot. The tester
      owns its environment under `agw-test-env` and `integration-testing`; the development session
      does not invent inventory or mutate live resources.
- [x] Publish the ready handoff to trigger the separate tester and verify its report uses this
      commit's CLI in an isolated checkout, rather than a release stand-in. Confirm that the new
      commands exist on every reported test bed.
- [ ] Drive real VM restart or external shutdown with two shell sessions and a named console,
      restore their intended states, then repeat reconciliation and prove healthy identities remain.
- [ ] Drive pure adoption and dry run; explicit child/console stop; explicit parent stop retaining
      child targets; subsequent VM start/recovery; direct session exit; console staging recovery;
      missing-secret barrier; non-interactive refusal; and reachable unknown/failure cases.
- [ ] Exercise delayed WSL keeper attachment versus a concurrent stop on the actual Windows/WSL bed,
      plus hold-entry failure cleanup and diagnostic access without an interactive-body guard.
- [ ] Record the full command surface reached, each observed pass/failure, unreachable lanes, and
      both workstation and platform coverage. Unit evidence supplements, not replaces, live
      recovery.
- [x] Delete owned fixtures and independently verify native, provider, local-file, and tailnet
      cleanup. Preserve operator resources and restore any altered operator state.
- [ ] Publish the complete implementation as ready in this same PR with exact-head evidence, then
      monitor all published review and validation reports. Apply at most the two authorized
      implementation feedback rounds, after collecting the standard window and critically reading
      every finding. Re-run the required private lanes and affected gates before each handoff.
- [ ] Record implementation convergence with all material items resolved and required live evidence
      present. An unavailable live bed must remain an explicit gap until an authorized disposition
      or actual evidence resolves it; it cannot silently become a pass.

The initial ready handoff was `9b864f743` (PR comment 6769651812), with green CI run 42830730004.
The separate tester's completed report, PR comment 6771656760, ran that candidate's CLI against real
remote Lima guests and native Windows/WSL, passed all twelve required gates (11,654 tests passed, 11
skipped), and independently audited fixture cleanup. It established real migration/adoption,
non-mutating preview, secret-barrier failure and recovery, retained intent after session exit and
parent stop, healthy identity preservation, and native WSL keeper/guard behavior. The WSL race
delayed acknowledgement consumption, not guest boot. Provider authentication isolation, model
conversations, all cancellation timings, and a live healthy secret-backed no-op with its input
absent were not established by that campaign; source and unit evidence retain their stated scope.

The report found no new runtime defect in reached scenarios, but main had advanced to `1dd5a06dd`
and introduced three merge conflicts. After the integration result and minimum collection window,
implementation fix round one began in PR comment 6771937129. The feature branch now incorporates
that main commit, preserving selected rekey credentials and forced prompting together with UUID
checks, family guards, cached repair, and Resolver failure memoization. Combined regression tests
cover their composition. This round also removes the retired session stop executor and unused VM
secret bridge, and requires caller-owned console preparation dependencies. The optional VM facade,
rollback/prefix-guard, and WSL wording suggestions were declined on their merits in PR
comment 6770779631. Later feedback belongs to the next batch.

The combined head requires its own green gates, three private review lanes, ready handoff, and
completed integration report. The initial live report does not validate these later changes. The
next campaign must explicitly establish one VM-scope recovery of two desired-running shell sessions
and a named console after a new boot, followed by stable repeated reconciliation; the initial
report's summary did not spell out that exact acceptance scenario. Implementation convergence and
the final lock remain open. Any fixtures created by the next campaign require a fresh cleanup audit.

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

The lead owns architecture, this plan, shared coordination and Resolver changes, CLI/completion
integration, permanent documentation, and final integration. Bounded implementation is delegated in
isolated worktrees:

- Data/VM owner: database models/converters/migration/accessors/backup boundaries, VM intent and
  runtime helpers/inspection, explicit guard threading in existing guarded VM callers, and bounded
  WSL hold acknowledgement.
- Session owner: session manager launch/stop preparation and execution, batch lifecycle composition,
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
