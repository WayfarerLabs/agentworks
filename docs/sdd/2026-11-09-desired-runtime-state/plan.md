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

| Stage                   | Signal                                      | Published fix allowance                                  | Current record                            |
| ----------------------- | ------------------------------------------- | -------------------------------------------------------- | ----------------------------------------- |
| FRD                     | Draft plus `review-requested`               | Up to two rounds                                         | Converged at `c6337609d`, two rounds used |
| Complete design         | Draft plus `review-requested`               | Up to two rounds                                         | Converged at `32a176600`; two rounds used |
| Complete implementation | Ready                                       | Two original rounds plus one explicit supplemental round | Supplemental round active                 |
| Closeout                | Final lock in the same PR after convergence | No new feature/fix allowance inferred                    | Lock and closeout checks completed        |

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
provider. Live validation was pending at the initial handoff; the reports below supply its outcomes.

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
- [x] Drive real VM restart or external shutdown with two shell sessions and a named console,
      restore their intended states, then repeat reconciliation and prove healthy identities remain.
- [x] Drive reachable live lifecycle, adoption, dry-run, missing-secret and refusal cases.
      Supplement unreached console staging, unknown-runtime and cancellation faults with behavioral
      tests, and identify those limits explicitly rather than claiming a live pass.
- [x] Exercise delayed WSL keeper acknowledgement versus a concurrent stop on the actual Windows/WSL
      bed, qualifying acknowledgement-consumption delay separately from guest boot delay. Verify
      hold-entry failure cleanup through host-sensitive tests, and diagnostic access without an
      interactive-body guard through the live campaign.
- [x] Record the full command surface reached, each observed pass/failure, unreachable lanes, and
      both workstation and platform coverage. Unit evidence supplements, not replaces, live
      recovery.
- [x] Delete owned fixtures and independently verify native, provider, local-file, and tailnet
      cleanup. Preserve operator resources and restore any altered operator state.
- [x] Publish the complete implementation as ready in this same PR with exact-head evidence, then
      monitor all published review and validation reports. Apply at most the two authorized
      implementation feedback rounds, after collecting the standard window and critically reading
      every finding. Re-run the required private lanes and affected gates before each handoff.
- [x] Record implementation convergence with all material items resolved and required live evidence
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

The combined head required its own green gates, three private review lanes, ready handoff, and
completed integration report. The initial live report did not validate those later changes or spell
out the exact two-session/named-console recovery scenario. The second report below closes that gap
and independently audits its new fixtures.

Round-one private verification at `55d14026e` passed 11,678 tests with 11 skips, all Python static
checks, typer isolation, file-quality, rulesync and locked-SDD checks, the isolated local CLI
checks, 160 Python and 103 Node website tests, and deterministic double builds at both site bases.
Project, complexity, and generic correctness/security reviews were clean at that same head. The
reviewers passed 213, 231, and 146 focused tests respectively; the correctness lane additionally
verified 21 isolated checks, including cleanup after rekey failures and both cancellation signals.
No live operation was performed by those private lanes. CI run 42851513484 passed all ten jobs at
the round-one handoff `452eb0622`, including Windows with 369 passes and 16 skips.

The ready transition at 16:15:38 UTC was followed by clean published delta review 6772829295 and
completed integration report 6774690468 at 17:43:15 UTC. Both assessed the actual combined head. The
report passed all twelve gates with 11,678 tests and 11 skips. Linux and Windows Server 2025
workstations each restored two desired-running sessions and a named console with one VM reconcile
after a real provider stop/new boot, then preserved identities on a healthy repeat. Linux exercised
real PTY forced prompting, selected-key racing repair with one resolution, a two-family workload
secret barrier, and a healthy secret-backed no-op without its unused input. Windows drove twenty
PowerShell 5.1/7.6.9 cases against remote Lima, including selected-key byte-stdin delivery, pure
setters and non-TTY refusal. Both campaigns independently audited cleanup.

Implementation convergence is recorded in PR comment 6774724254. One of the two authorized
implementation fix rounds was used. The final report and minimum collection window both arrived
before closeout began. The optional delta observations were assessed in comment 6772847664; no
material published or private item remains unresolved. [validation.md](validation.md) maps all
requirements and acceptance criteria to evidence, retaining exact heads and coverage limits. The
live checklist wording above distinguishes actual native observations from supplemental fault tests;
it does not assert live unknown-runtime corruption, staging faults, or every cancellation timing. No
requirement was waived. No PR merge is authorized.

Definition of done: complete implementation, truthful exact-head validation, verified cleanup, green
CI, and converged authorized published review. No merge occurs under this plan.

## Phase 7: closeout

- [x] Audit every requirement and acceptance criterion against code, behavioral tests, permanent
      documentation, and live evidence. Finish outstanding collateral and record actual outcomes.
- [x] Create dated `locked.md` in this same PR after implementation convergence, summarizing the
      final implementation and actual validation/coverage.
- [x] Run closeout project and complexity reviews at a pinned head and required affected local
      checks. Remove checkpoint labels when no checkpoint remains.
- [x] Clean up lead/delegate scratch worktrees, branches, temporary outputs, and owned live residue
      without deleting the reviewable feature branch or unrelated resources.

Publish the final closeout only after exact-head CI is green, with a scoped evidence comment and the
ready transition. The PR records that publication; completed checkboxes record work already done.
Closeout changes only SDD records, preserving the tested application and permanent guidance.

Project and complexity closeout reviews were clean at `ef2127941`; affected local checks passed. All
nine delegated worktrees, eight subordinate local/remote branches, generated website outputs, and
task-owned lead scratch were retired. The feature branch and PR remain available for review.

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

The tester owns its live inventory and cleanup evidence. Unreached platform or fault cells remain
qualified in validation rather than assumed passes. New material design decisions return to the
lead; requirements or scope changes beyond the charter return to the operator.

## 2026-11-11: operator-directed current-main integration

After the completed closeout at `ad4600df8`, the operator asked this session to address PR #885's
new conflicts. Main advanced to `834eb715666d2d8539874a042484bf9da60fdb00`, adding SSH trust and
execution changes, native artifact delivery, session liveness handling, and temporary Shell
placeholder compatibility. Ten content conflicts were reproduced. This follow-up preserves both sets
of behavior under the existing requirements; it authorizes no new feature or PR merge. The earlier
completed checkboxes remain historical records. Earlier live reports retain their tested-head
attribution and do not validate the new integration.

- [x] Integrate current main, resolve the ten conflicts, and verify automatically merged lifecycle,
      trust, artifact, compatibility, CLI and documentation seams against both contracts.
- [x] Exercise combined behavior with focused tests and required gates. Obtain independent project,
      complexity and generic correctness reviews at one pinned candidate head.
- [x] Publish the complete green candidate to the same PR with a scoped ready handoff; wait for its
      associated separate integration report before any further iteration. Record actual results and
      coverage limits without converting previous reports into fresh evidence.
- [ ] Retire this follow-up's owned worktrees, subordinate branches, caches and temporary outputs;
      preserve the feature branch, PR and unrelated resources.

Merge `5e9b8e90f` combines the prior PR head and the stated main commit. It preserves early SSH
trust refusal before explicit VM start acceptance and secret interaction, adds the same fence to
known reconciliation activation and its consequential runtime helper, and leaves provider-only
observation/stop independent of SSH. Console provider preflight remains local; guest-required
consumers enforce the SSH boundary, including both live-parent stop observations. Prepared session
execution passes the complete artifact application so stable native entries survive restart. The
existing public restart tests cover file and ownership retention; no duplicate assertion-only test
was added. Focused composition/regression verification passed 174 cases, including all remaining
failures from the preceding impacted run. Root full-suite and independent review results are still
required before the ready handoff.

All three private lanes were clean at `67b7b0530`: project review passed 245 focused tests,
complexity review passed 151, and generic correctness/security review passed 251 plus four isolated
checks. The root full run then found six missing-trust fixtures among 12,359 collected cases; 12,319
passed and 34 skipped. Test-only correction `03939d126` seeds actual isolated host-key records in
three retained fixture files, preserving every behavioral assertion. Their 30 targeted tests passed.
The full rerun and exact-head CI remain required. The retained VM guide now names the guest
SSH/activation prerequisite precisely, preserving provider-only and pure-intent exceptions.

The corrected full rerun passed 12,325 tests with 34 skips in 281.21 seconds. Ruff checked and
formatted 1,033 files; strict mypy passed 998 files. File-quality checks passed 504 Markdown and 477
spelling inputs. Typer isolation, rulesync, locked-SDD checks, 160 Python and 103 Node website
tests, and deterministic double builds at both site bases returned zero. All three private delta
reviews were clean at `101071778`, each verifying the 30 affected tests. Production code is
unchanged from the earlier clean review head; the corrections retain actual trust enforcement and
every original fixture assertion. Fresh exact-head CI must still pass before publication, and the
new live report remains pending.

That candidate was published at `dbbbd31cc` in PR comment 6785541656 at 02:51:06 UTC, with all ten
CI jobs green in run 42947972175. The completed separate report, comment 6786419851 at 03:47:03 UTC,
passed all twelve gates with 12,325 tests and 34 skips. Linux and Windows Server 2025 workstations
recovered desired sessions and ordered named consoles on remote Lima after a new boot, preserved
identities on repetition, and verified trust refusal/import and native artifact retention. The
report also independently reproduced the preview trust blocker described below. The model health
call failed for insufficient API credit; no successful model conversation is established. Cleanup
was independently audited, with no remote Windows private-key scan claimed and possible offline
tailnet metadata retained. Earlier migration, WSL and cloud results retain their original heads.

## 2026-11-11: final implementation feedback round and GCP resume

The operator's GCP follow-up authorizes correction of the existing platform activation path in this
same PR. The remaining original implementation feedback allowance covers round two of two. Its
frozen batch is review 6785968262, integration report 6786419851 and the non-finding Copilot quota
notice; no inline thread was open. Critical readings are comments 6786145253 and 6786538784. The
actual integration report arrived before the collection window ended at 03:51:06 UTC. The PR was
returned to draft before source changes. No further original fix allowance or merge is inferred.

The accepted R6 correction makes preview check the same local host-key prerequisite as apply before
provider credentials or observation when the selected intent requires a running VM. A shared
predicate keeps that decision aligned while preserving provider-only stop, off-parent child stop and
idle on-demand exceptions. A real-boundary Console regression protects refusal before intent
acceptance; no production Console rewrite is needed. The optional shared predicate and short
defensive-boundary rationale are included. Other suggested boundary coverage remains optional.

GCP activation resumes a native suspended instance, starts a terminated instance and preserves the
running no-op. Known stopping/suspending states settle through the existing bounded, numeric-ID
checking poller before dispatch. Resume uses the existing request UUID, operation target/type
verification, operation wait, running-state poll and cancellation semantics. The IAM guide includes
the resume permission. This changes no desired-state vocabulary, global status taxonomy, schema or
CLI. No live GCP pass is claimed by the source implementation or its behavioral tests.

- [x] Integrate the disjoint GCP adapter and reconciliation/test corrections from their owning
      workers, with permanent GCP guidance and meaningful ownership, timeout, cancellation,
      read-only preview and preacceptance regressions.
- [x] Complete the combined candidate's required gates and independent project, complexity and
      generic correctness/security reviews at one pinned head; resolve material private findings.
- [x] Publish a complete green round-two handoff to PR #885, naming the spent allowance and GCP
      direction, then wait for its associated separate integration report before any further
      iteration. Record every result and remaining limit before claiming follow-up convergence.

The lead retains architecture, SDD ownership, integration and handoff. The two isolated owners
changed only their assigned GCP or reconciliation/test files. New material feedback after this round
requires an authenticated disposition when the existing authority does not cover it; it cannot
become optional merely because the original two-round allowance is spent. The preceding follow-up
cleanup item also covers these workers, review trees and temporary outputs.

Private project, complexity and generic correctness/security reviews were clean at `98b957b40`.
Their independent focused runs passed 184, 121 and 121 tests respectively. Complexity verification
also compared 216 decision combinations and confirmed expected failures after removing preview
trust, resume selection, transition settling, operation identity and Console preacceptance checks.
The Console removal experiment protects the early refusal boundary; its incomplete provider
configuration prevented reaching acceptance, so it is not evidence of an actual intent write. All
experiments were confined to separate trees and restored. No private lane used live GCP.

The non-overlapping root confirmation returned zero with 12,366 tests passed and 34 skipped in
308.04 seconds. An earlier overlapping run returned one after a handover-cleanup timeout in the
execution conformance code, which is byte-identical to main. That exact case then passed in
isolation before the complete confirmation passed. The original cause remains unproven; no source
change or waiver was made for it. Separate log and exit-code records preserve the confirmation. Ruff
lint and format checks passed for 1,035 files and strict mypy passed 1,000. File quality passed 504
Markdown and 477 spelling inputs after correcting the new SDD spelling. Typer isolation, rulesync,
locked-SDD checks, 160 Python and 103 Node website tests, and deterministic double builds at both
site bases all returned zero. Exact final-head CI, ready publication and the associated separate
integration report remain required.

The round-two handoff was published at `863c499aa` in comment 6787227335 with all ten CI jobs green
in run 42960165124. All three final SDD delta reviews were clean. Completed separate preflight
6787498628 found main had advanced to `07feb4a45` and stopped on four conflicts before gates, wheel
build, delegated review or native tests. This is not fresh GCP validation. Clean delta review
6787472615 confirmed both prior material corrections and proposed an optional deterministic test
clock. Critical reading 6787501750 assessed that and inherited stop-transition behavior.

## 2026-11-11: authorized supplemental main integration

The operator explicitly authorized one more feedback round and required the goal to remain active
through monitoring and disposition. Comment 6787360629 records that authenticated direction. The
original two implementation rounds remain spent; this is one additional round, not a standing
allowance. No PR merge is authorized.

Supplemental round one of one starts in comment 6788141928 after the actual associated report and
more than one hour after ready. The frozen batch is review 6787472615, preflight 6787498628 and the
non-finding Copilot quota notice; no inline threads were open. The PR was verified draft at
`863c499aa`. Fresh main is `07feb4a4538ed26b7e82918d120bebac33438769`.

Main adds #776's parallel batch stop, conditional runtime completion and PID-repair fencing. The
database conflict is imports only; no new schema migration is needed. Factual scouts and private
project/complexity design scrutiny identified automatic-merge seams beyond the four markers. The
lead owns integration decisions in the [architecture](hla.md) and
[batch design](batch-preparation-lld.md); one isolated owner implements code, tests and matching
permanent collateral. The incoming locked SDD remains untouched.

- [x] Integrate current main and all four conflicts, preserving intent/secret boundaries and main's
      native teardown, conditional writes and public bulk concurrency.
- [x] Correct the GCP transition test's clock without changing production activation; retain the
      deliberate inherited stop-transition policy.
- [ ] Verify named versus filtered bulk stop, admin routing, strict bulk unknown refusal, original
      snapshot fencing, off-parent behavior, secret barriers and truthful interrupted completion.
      Obtain all three private lanes and required gates at one pinned candidate.
- [ ] Publish the complete green supplemental handoff, wait for its actual associated integration
      report and critically dispose of feedback before claiming convergence or finishing.

This uses the one supplemental allowance. Later feedback belongs to a later batch and creates no new
round. Keep the goal active through monitoring and disposition. The open follow-up cleanup item
covers all remaining and new task resources. Four finished worker/scout trees and three subordinate
branches were retired after round two; review trees and two new factual scout trees remain.

Merge `d7260da42` integrates the pinned main and supplemental implementation. The owner passed 1,337
impacted tests, full Ruff lint/format across 1,043 files, strict mypy across 1,008 files, file
quality across 511 Markdown and 484 spelling inputs, the locked-SDD guard and diff checks. Removing
an accepted GCP settled state produced the expected two failures and four passes in 0.54 seconds;
the test clock no longer waits for the real timeout. GCP production code, schema, public grammar,
policy and incoming locked artifacts are unchanged by this supplemental work.

Explicit named stop also defers safe admin repair until after shared secret success and before
status, preserving main's writer-failure refusal while remaining synchronous. Ten cases with an
enclosing read transaction in the twenty-case PID-repair matrix preaccept intent before that
transaction and narrowly replace the setter with a read-only seam. They isolate post-accept repair
and retain original conflict and stale-snapshot assertions. Separate public-controller tests
exercise real named/bulk acceptance and repair-writer refusal before status or teardown. This
qualification is not a waiver or a production change made for the old harness.

Root combined gates, three independent source reviews, exact-head CI and the new ready handoff and
associated integration report remain required. The owner's focused and static results are not live
validation, and the goal remains active.

### Private adversarial corrections

All ten CI jobs passed at `9c38dff93`, but private source review found material gaps before handoff.
Project review passed 338 focused tests. Complexity review passed 431 and demonstrated five public
controller/SQLite failures where parent-off fallback erased worker, stopped-write, observer or
stale-runtime completion errors. The cold correctness lane passed 314 focused tests and reproduced
lost reconciliation after a real caller SIGINT while retrieving a successful future. That engine gap
is inherited from main, relevant to the modified completion contract, and not an observer-only
regression. These findings remain inside the same private round.

- [x] Preserve authoritative execution/completion errors as failed outcomes even after parent
      shutdown, without losing sibling successes or weakening pre-dispatch repair refusal.
- [x] Distinguish stored worker failure from caller interruption during completed-future retrieval;
      retain successful futures for stopped CAS/observer reconciliation and drain before rethrow.
- [x] Retain last-known session fingerprints on provider-OFF observation, preserving original
      snapshot checks and guest-free inactive reporting, then verify safe same-memory resume.

The owner produced `cce3268a6` for the first two corrections with 1,357 impacted tests and full
static checks passing; it is included in the later combined correction below. A separate scout then
confirmed the suspension concern through source and four local model probes: named, bulk and generic
OFF stop cleared a retained fingerprint. Same-memory resume then became unknown with no existing
automatic recovery despite retained run/socket tags. The Console control retained ownership and
reconciled safely. Provider/native fingerprint edges were simulated; this is not live GCP evidence.

The lead ruled that OFF should preserve last-known native evidence until actual guest absence or
teardown is verified. Project contract scrutiny confirmed compatibility with R4, R5 and R6 and no
operator requirement amendment. The correction uses existing fields/states, not a new provider or
power policy. Permanent teaching and the owned response artifacts must distinguish inactivity from
proof of process death. Native GCP and the final combined candidate still require fresh validation.

The combined corrections are integrated at `afe92857f`. The owner passed 1,365 impacted tests, Ruff
lint/format for 1,046 files, strict mypy for 1,011 files, file quality and locked-SDD checks. The
preserved-memory model drives all three actual session stop compositions with real status and native
teardown/CAS authorities behind modeled transport/fingerprint edges. OFF preserves the row except
accepted target/timestamp; same-boot resume safely stops without force. Changed ownership refuses,
while new-boot recovery, peer preservation and healthy repeats pass. Console, provider power APIs,
global statuses and schema are unchanged. New independent delta reviews, root gates, exact-head CI
and the subsequent actual integration report remain required; the goal stays active.
