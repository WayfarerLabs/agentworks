# Independent SSH Carrier: Staged Delivery Plan

- Updated: 2026-10-06
- Requirements: [frd.md](frd.md)
- Architecture: [hla.md](hla.md)
- Shared contract:
  [transport-owned definition](../2026-09-12-transport-improv/execution-contract.md)
- Shared PoC:
  [transport plan](../2026-09-12-transport-improv/plan.md#2-prove-the-shared-boundary-before-broad-implementation)
- Coordination baseline: SSH PoC #796 merged; transport design #830 merged as `cea5e852`

## Delivery and ownership

The SSH PoC in #796 is merged. PR #832 on `feat/ssh-full-implementation` delivers the full
independent carrier and connection/trust migration machinery for the new execution surface.
Transport exposes that surface alongside the unchanged old SSH/transport path in RunContext, with
both available and usable. It then leads consumer migration, removes legacy RunContext access, and
deletes old transport. SSH waits during those stages except for issues needing its attention. A
later operator request starts a separate PR to delete old SSH and close this SDD.

The [2026-09-19 ruling](frd.md#operator-ruling-2026-09-19) supersedes the earlier two-PR retirement
sequence. Implementation delivery may finish before migration and deletion; it does not finish the
SDD. These artifacts ride the SSH work, with no separate design-only PR. Use coordinated PRs from
main where independent, and stack only actual dependencies. The integration tester can combine
pinned SSH/transport branches for evidence before they land.

Post integration-testing considerations before a checkpoint's `review-requested` signal; complete
private reviews first. Remove the label before fixes and wait for full integration reports before
another iteration. Use ready when the complete green handoff has merge intent, with no checkpoint
label. The four-round PoC allowance below is historical and exhausted, not a new implementation
feedback budget. The operator authorized one artifact feedback/fix round on #832, followed by
implementation on the same PR and up to three additional public feedback/fix rounds after its ready
handoff. The artifact checkpoint at `66298c59` closed without a fix round: full tester and
complexity reports passed the documents; the title finding assumed a design-only merge and was
refuted by this single-PR scope. Its
[disposition](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-5744746307) records
the reports and removal of `review-requested` before implementation. Record each later handoff and
budget disposition on #832. Contract changes or unexpected scope return to the operator.

Transport solely owns the carrier contract, common types, acceptance criteria, shared preparation,
public outcomes and proof harness. SSH owns its independent carrier, connection/trust migration,
fixtures and implementation evidence. SSH raises feasibility concerns to transport rather than
maintaining another contract or acceptance matrix. Requirement changes return to the operator.
Transport also owns non-SSH adapters, all consumer migration, legacy RunContext removal and old
transport deletion. SSH owns final old SSH deletion under the later operator request.

Use the transport artifacts in the same checkout as the live design reference. Record the exact
transport contract and implementation commits with each proof or integration result; a provenance
pin is not a competing definition. When common implementation code is not yet on main, make its
transport PR an explicit dependency instead of adding SSH-local substitute types or a second
harness. Stack only actual dependencies and merge them in order. This does not authorize edits to
transport-owned artifacts. Its removal inventory remains the shared dependency boundary; the latest
operator ruling assigns the final old SSH deletion here after old transport is gone.

The earlier legacy-consolidation work through `2f11662d` is superseded and retained as source
material only; it is not independent-carrier implementation or proof evidence. Local proof fixtures
now exercise the new buffered carrier. Transport's
[acceptance disposition](../2026-09-12-transport-improv/proof-lld.md#joint-buffered-proof-acceptance-2026-09-17)
accepts the joint buffered PoC. Full implementation remains Phase 2; final SSH retirement and this
SDD's lockfile belong to Phase 3, after the intervening transport stages.

Current dependency provenance lives in [poc-results.md](poc-results.md#revisions-and-delivery).
Completed checkboxes below record the revisions used when their work happened; they are historical
records, not floating dependency declarations. The FRD preserves earlier requirements and records
the new operator ruling verbatim; this plan carries the current delivery sequence. R1-R5 acceptance
obligations remain in force through final retirement.

## External feedback rounds

- [x] Receive the full first live report for SSH `1c32e415`, its later combined-tree report on
      transport, and the published complexity review. Remove `review-requested` and open round 1
      under the operator's four-round allowance after the review window. Record findings and
      dispositions on #796 before changing the implementation.
- [x] Complete round 1 policy and process regressions, investigate the real Windows timeout, update
      measured evidence and limitations, and re-run private reviews and affected gates. Post exact
      tester instructions before raising `review-requested` again. Windows completion and joint
      acceptance remain open until measured; a synthetic or parser-only pass cannot close them.
- [x] Complete round 2 after the full reports and collection window: record the authenticated
      Windows resolution, align SSH wording and its harness field use with transport's reviewed
      completion evidence correction, and retain affected macOS coverage as open until measured. Run
      the private review lanes and applicable gates, then post the tester handoff before raising the
      label. This round does not weaken public outcomes or claim completed joint acceptance.
- [x] Complete round 3 after the full reports and collection window: correct the macOS agent-socket
      fixture's path length without changing carrier behavior, record the six current live cells and
      their qualifications, and run private reviews and applicable gates. Post the exact macOS
      fixture retest considerations before raising the label. Joint acceptance remains with
      transport; one authorized fix round remains after this handoff.
- [x] Complete round 4 after the full reports and collection window: consume the reviewed transport
      acceptance record, record the successful macOS fixture retest and unchanged-tree evidence,
      reconcile the SSH artifacts and rebase onto the final transport head. Complete independent
      closeout reviews and gates, and prepare testing considerations for the final handoff. This
      exhausts the operator's four-round allowance; further material fixes need direction.

## Baseline work

- [x] Compare main `e440a28c` with the original `7c744828` baseline and transport proposal
      `6809827f`; record source anchors and revise SSH-owned design/migration obligations. This
      completes the source comparison only, not contract agreement or runtime proof.
- [x] Rebase #796 onto the merged #795 baseline `857110df`, reconcile published ownership and
      mirrored-inventory feedback, and define the two implementation phases under this SDD. The
      [main comparison](main-comparison.md) retains the earlier source snapshot and records this
      reconciliation; neither checked item claims PoC completion.

## Phase 1: Complete SSH PoC in PR #796

### Prepare the proof

- [x] Consume transport's concrete candidate types and acceptance criteria. Record the exact
      contract and code revisions in the proof record; send unresolved SSH feasibility questions to
      transport before accepting the boundary. Transport owns any common-contract correction. The
      [proof record](poc-results.md#revisions-and-delivery) records the consumed transport
      revisions, boundary and combined acceptance.
- [x] Write [ssh-lld.md](ssh-lld.md) with the SSH decisions needed for the PoC: explicit connection
      and trust inputs, executable selection/version refusal, supported account-shell bootstrap,
      subprocess I/O and cleanup. Separate observed decisions from open hypotheses and from Phase 2
      details. Inventory workstation, platform-host and provider-inner client locations under the
      HLA's OpenSSH 8.5 boundary, recording actual versions and separate server compatibility for
      the exercised cases. Record unexercised locations as gaps, not inherited compatibility.
- [x] Resolve proof prerequisites with transport, including initial SSH delivery before Python
      installation and no-staging readiness. A proposed helper must not require the package that the
      first invocation needs to install. Platform-host userspace is a separate inventory.
- [x] Use the operator's integration tester and existing authorized inventory/budgets for the
      bounded live-proof charter. The [proof record](poc-results.md#proof-charter-and-prerequisites)
      links the shared and SSH handoffs, explicit fixture identity/trust, measured versions, bounded
      workloads, carrier resource limits and independent cleanup reports. Transport owns QGA access
      and the joint harness. Private inventory and spending ceilings remain with the tester; no new
      resource authority or implicit operator trust-store access is conveyed by this SDD.

### Build and demonstrate the complete SSH contribution

- [x] Implement the independent buffered SSH binding in `execution/carriers/ssh/` and its tests in
      `tests/execution/carriers/ssh/`, using transport's leaf contract and shared preparation. The
      code used by the proof is the destination implementation, not a wrapper around legacy SSH or a
      separate public prototype API. Production factories and RunContext remain on the old stack
      during this phase.
- [x] Supply explicit isolated connection policy for the proof, including identity/agent selection,
      strict fixture trust, version refusal and ambient-config isolation. Build enough real policy
      to substantiate those guarantees; do not disable verification to make the fixture pass.
      Production config conversion and the complete migration matrix belong to Phase 2.
- [x] Exercise every SSH-applicable case of the transport-owned PoC matrix and detailed carrier
      contract through the joint harness, including its focused I/O ownership/failure tests. Link
      results to the authoritative cases rather than copying their inventory here. No failed or
      missing SSH proof case can be deferred to Phase 2 and called a complete SSH PoC.
- [x] Run the proof's new-stack imports and tests with the transport-defined retirement modules
      unavailable. Audit dependency closure, including normal package initialization and fixtures;
      copied tests must not call legacy code to calculate expected results.
- [x] Record reproducible commands, pinned code/contract revisions, executable/server inventory,
      observed evidence, safe failure reports and independently verified cleanup in
      `poc-results.md`. Keep credentials and payload-bearing diagnostics out of retained evidence.
      Separate local fixtures from live observations and list all unsupported or untested cases.

### Accept the PoC and reconcile

- [x] Have transport evaluate the combined proof, including its real non-SSH case, against its
      acceptance criteria. Record the disposition and links to its evidence without claiming SSH
      performed or owns that work. Unresolved feasibility blocks acceptance; a changed mechanism
      returns for the appropriate contract or operator decision and affected cases run again.
- [x] Update this SDD and `ssh-lld.md` with observed SSH decisions and the transport-owned proven
      revision. Transport updates its own artifacts. Carry the reconciled SSH artifacts in #796 with
      the PoC, not in an intervening design-only PR.
- [x] Complete independent project, complexity and correctness reviews, applicable local/hosted
      gates and authorized live-proof evidence at the evaluated closeout head. Record its pinned
      results and the unchanged-code basis for final readiness bookkeeping in `poc-results.md`. Keep
      this SDD unlocked.

Final delivery on #796 publishes the testing considerations before the ready signal, with the exact
published head's CI green and `review-requested` removed when no checkpoint remains. Transport #826
lands first. This PR's ready state, head and checks record that handoff; these completed work items
do not authorize a merge or replace its final checks.

**Phase 1 definition of done:** #796 contains all SSH PoC code, independent fixtures, observed SSH
evidence, cleanup results and reconciled artifacts. Its transport implementation dependencies are
available in the landing order, and transport has accepted the combined proof against its one
contract. Missing required evidence needs explicit operator disposition; an artifact review, green
unit suite or merged transport design cannot stand in for that disposition. Broader file workflows,
full terminal/forwarding support and additive production delivery are Phase 2 work, except for
anything the transport PoC itself requires. Later migration and retirement follow the current staged
sequence. Unsupported optional behavior refuses before dispatch.

## Phase 2: Full SSH implementation in the second PR

Start from the accepted PoC and merged #830 design. Continue this SDD and extend the proven carrier.
Coordinate integration and landing order with transport's complete additive RunContext delivery;
consumer migration and retirement do not gate this implementation PR. SSH does not take ownership of
transport's files, shared supervision or consumer migration.

The [implementation progress record](phase2-results.md) pins the completed independent code and
local validation separately from the remaining shared integration and platform acceptance. The
[trust workflow record](trust-workflow-results.md) distinguishes newly composed installed-client
proof fixtures from native execution and genuine production publication.

The
[local persisted-configuration record](trust-workflow-results.md#local-persisted-configuration-restoration)
proves that restoring old TOML does not discard current managed policy, revocations, blocking or
partial-update custody. Production writer coexistence and rollback through both usable RunContext
paths still require transport composition and native acceptance.

Shared subprocess adoption introduces an implementation dependency on transport #833. Initial draft
integration uses `12dcb01b8f76575a55e515940022f0a14a74e34f`, which adds mandatory caller-held
`LocalDeliveryCustody`. The preceding published SSH head `dda32ee69` remains based on
`e1d4c9b1189edbc247210c6c900975c6290ac531`; its hosted evidence does not validate this adoption.
Private execution binds the selected owner scope and bootstrap boot; the WSL2 native factory
requires both prepared identity plans and shares one elevated-plan/full-guest bootstrap between file
and execution state. Numeric file workflows retain that context and each record's actual envelope
version. New private recovery/lifecycle candidates and fixed body-free Proxmox generation
observation advance transport's composition, with source and faked-provider evidence recorded in its
plan. Its latest private already-running Proxmox composition retains one VM owner and numeric
bootstrap, while account and guest preparation preserve observed facts and original interruption
when borrow release fails. Its private stopped-Proxmox composition now retains the selected VM owner
and root-QGA route before one durably armed start request. Only a fully matching successful ordinary
task settles that request; unknown acknowledgments, errors and HA handoffs retain custody without
replay. Fresh running-power and passive guest-info observations precede exact guest/account
preparation, and normal teardown leaves the VM running. Plugin registration now imports without the
retirement roots; old operation imports remain local to their legacy callers. These source and
scripted-provider results do not prove native startup, atomic stale-request prevention or
queued-request drain. Native recovery, provider freshness at dispatch, fencing/drain, complete
platform availability, Windows terminal delivery and additive RunContext remain open. These private
increments supply no new SSH terminal or production acceptance. Earlier rebased code pin
`e38f5fcff35bd85d099129fdbe5dd8480017c007` passes 15,768 non-integration tests with 51 skips and 27
warnings, full Ruff/format, strict mypy (1,277 sources), exact CI typer isolation, file quality,
locked-SDD, Rulesync and whitespace checks. Website validation passes 160 Python and 103 Node tests,
four builds and both deterministic comparisons. The first build target was refused inside the
checkout; corrected external owned output roots pass. No SSH code changes or native acceptance
follow from that dependency refresh.

The latest dependency implements the private guest operation lease and its controller consumer.
Expiry remains bound to one launch/boot and an earlier guest-clock sample plus 60 seconds. Fresh
clock checks after renewal reads and immediately before workload release prevent delayed I/O from
reviving expired work or admitting an expired initial lease. Packed Python 3.11/3.12 real-child
fixtures observe body exclusion under a synthetic process-group boundary; they supply no native
cgroup, SSH or QGA proof. Host OPERATION admission still refuses. The keeper, recovery and aggregate
lifecycle remain open, including measured exchange budgets and publisher drain. Service admission
uses existing trusted-source docstring compaction without changing other helper callers' default.
The exact 10,000-byte workload fixture fits Proxmox's unchanged 65,536-byte HTTP cap with only 581
bytes of independent-request margin and 217 bytes of private-operation margin; this is no general
workload-size or native envelope proof. The Windows migration fixture now uses bounded coordination
and failure-path child cleanup without changing product deadlines or schema assertions. Transport
records all three private lanes clear at `a2d27446c`; its final head changes only evidence from that
source pin. No shared SSH interface changed.

Transport's carrier contract requires caller-held `LocalDeliveryCustody` at actual delivery. It
retains the existing inert native process owner before admission, so bounded local cleanup may
return with exact ownership still held by the enclosing operation, keeper or provider workflow.
Pending/lost local ownership prevents another exchange; remote effects remain separate. The SSH LLD
records native descriptor/terminal retention and aggregate drain without reopening dispatch. Private
buffered/live and forwarding-discovery adoption at `28679a6f4` passes 558 focused non-integration
tests with five skips and focused strict mypy. Full mypy still reports three missing enrollment
custody arguments in two files; merely threading those arguments would release the candidate writer
lock before native settlement. Enrollment resource retention, bounded terminal integration and
native/production acceptance remain open. The earlier design-only dependency and SSH response
preserved executable/configuration trees from published `2ef07cd6e`; earlier code-gate evidence
below retains its measured pin. The preceding transport source's hosted CI `37513250709` and CodeQL
`37513246961` pass, including the corrected Windows fixture. Its original failure's cause remains
unknown. No native gate follows from these hosted results.

Private corrections at `7f02ad436` retain actual forwarding fixture resources through early test
failure, correct permanent enrollment availability and remove a redundant discovery check. All three
private lanes clear those corrections and the scoped buffered/live adoption, while retaining the
open production forwarding, enrollment and terminal obligations. The affected suite passes 207 tests
with three skips. Full strict mypy checks 1,299 sources and reports exactly the three held
enrollment errors, exit 1; file quality and locked-SDD checks exit 0. No public handoff or native
acceptance follows, and all 25 completed records remain unchanged.

The [enrollment resource proposal](enrollment-lld.md#proposed-maintenance-resource-interface) and
[forwarding resource proposal](forwarding-lld.md#proposed-caller-held-forwarding-interface) at
`aa48bfa9f` pass private project and complexity review. They specify explicit bounded cleanup,
retained ownership and preservation of original control exceptions. Both operator ownership/API
decisions remain pending; these documentation proposals authorize no implementation. File quality,
locked-SDD and whitespace checks pass, with no executable changes or new completed plan records.

Draft `8afaaa71b` publishes that adoption on transport `12dcb01b`. Hosted CI
[`37538665063`](https://github.com/WayfarerLabs/agentworks/actions/runs/37538665063) completes with
failure. Its merge `2e11f40131549ce1a6a0e1d7b008190be90e13df` has those transport and SSH parents
and tree `68fd409f89a0072b8f0171b6beebf8463cb7b4d3`, exactly the published SSH tree. Linux Python
3.12-3.14 each report 6 failed, 16,065 passed, 201 skipped and 27 warnings; Windows Server
2025/Python 3.13 reports 2 failed, 1,226 passed, 63 skipped and 25 warnings. Every pytest failure
occurs in transport's unchanged `test_buffered_delivery_custody.py` SSH factory, which supplies a
bare trust path instead of the current explicit trust type. These failures precede custody
observations. That file also fabricates successful version discovery with unsettled storage; the
real pump reports observation failure for pending cleanup. Shared fixture correction belongs to
transport. Full mypy repeats the three held enrollment errors across 1,299 sources; Ruff/format,
Rulesync, file quality, locked-SDD and website jobs pass. Typer isolation is skipped after mypy
fails. No complete green or native handoff follows.

Transport's
[consumer compatibility reading](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6026464411)
accepts both proposed resource shapes within its existing caller-retained custody model. SSH owns
the candidate lock and forwarding worker/native-owner lifetimes; transport will retain the enclosing
resources through uncertain outcomes and cleanup retries. Its actual consumers and native acceptance
remain open. This compatibility feedback does not dispose either pending operator decision or
authorize implementation.

Transport's reviewed working publication `fc2e1992d35c31d9147f9bb77084681aa7727828` adds private
managed lease, keeper and exact-run recovery composition. SSH rebases without conflicts at
`26dce2dd86f6c9b116452838b4e3a0ee7cea1d64`; range-diff verifies all 167 SSH commit patches unchanged
from `ce1fb59a9`. The shared process owner, delivery custody, subprocess pump and shared buffered
custody fixture still match transport exactly. Ruff and formatting pass across 1,349 files. Full
strict mypy checks 1,312 sources and repeats only the three held enrollment errors, exit 1. The full
Linux Python 3.12 non-integration suite reports 6 failed, 16,489 passed, 49 skipped and 27 warnings,
exit 1 in 197.51 seconds. Every failure remains in the unchanged shared SSH custody fixture
described above. The initial run also fails nine Unix-socket fixtures because the lead's scratch
path exceeds native socket-path limits; the full rerun with a short private root passes all nine
without product changes. Locked-SDD and Rulesync checks pass. No complete green or native acceptance
follows. Transport's new publication leaves the carrier contract unchanged and retains open
production RunContext, aggregate cleanup and native evidence. Neither pending SSH resource decision
is disposed.

Transport's subsequent
[fixture coordination](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6027469928)
corrects the earlier ownership assumption: its independently green PoC constructor still takes a
bare path, so the typed-trust fixture adaptation must accompany the SSH API change on this branch.
Source comparison confirms that distinction. The three-line correction at `9ff370da8` uses
`SSHTrustFiles` and gives pending-cleanup version evidence `Failure.OBSERVATION`, matching the
existing native core. It preserves all custody assertions, WSL cases and production files. Private
project and complexity reviews clear that exact unit. The complexity lane independently passes all
10 fixture cases and observes the expected failure after removing version-failure propagation. The
lead's adjacent selection passes 201 tests with three skips; the full Linux Python 3.12 suite passes
16,495 tests with 49 skips and 27 warnings, exit 0 in 217.02 seconds. Full mypy repeats the three
held enrollment errors across 1,312 sources. Scoped typing, Ruff and formatting pass. This resolves
the six shared fixture failures; it neither implements the retained resources nor closes native,
terminal, production RunContext or whole-PR acceptance. No public final-product fix round is
consumed.

Hosted CI for draft `cb071a8b163130c23a88a831acf308ee3a151fd8`, run
[`37548572709`](https://github.com/WayfarerLabs/agentworks/actions/runs/37548572709), completes with
failure. Checkout merge `e09c04e569b44cf150edea6e1e0788f4b1bf8a1b` has transport `fc2e1992d` and
that SSH head as parents; tree `702fd25f18919aa24c8eb3f471e08fd019369696` exactly matches the SSH
head. Linux Python 3.12-3.14 each pass 16,339 tests with 203 skips and 27 warnings. Windows Server
2025/Python 3.13 reports 1 failed, 1,403 passed, 64 skipped and 25 warnings. The sole pytest failure
is `test_non_main_originating_caller_owns_database_and_actual_start`: its two-second thread join
expires while the caller remains alive. That transport-owned test matches the base exactly and also
fails in predecessor run `37547767878`; its cause remains unproved. All SSH custody fixtures now
pass. Full mypy repeats the three held enrollment errors across 1,312 sources. Ruff/format,
Rulesync, file quality, locked-SDD and website jobs pass; Typer isolation is skipped after mypy
fails. This is hosted gate evidence, not native acceptance or a complete handoff. Both operator
resource decisions, implementation and the recorded terminal/RunContext/native gates remain open.

The following documentation tip `50bdd045e` has the same CLI tree. Its hosted run
[`37549693530`](https://github.com/WayfarerLabs/agentworks/actions/runs/37549693530) repeats the
same Linux counts, single Windows thread-join failure and three enrollment typing errors. Merge
`8d5ed22c6b668c33dd6e8b053c12293f34839783` has transport `fc2e1992d` and that SSH tip as parents;
tree `82e39974fcb0415425f3dbcff667cf1ea5f21e2f` exactly matches the source tip. Other repository and
website jobs pass; hosted Typer isolation is skipped after mypy fails.

Transport's
[reviewed working increment](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6027924954)
`fa730f1fcc5d6e5a1e6340fd9aa3e83b4ba743a8` adds private bound MANAGED OPERATION start and aggregate
normal close, plus synchronized bounded phases in the failing Windows fixture. Its three private
lanes clear code `9c9d93cfe`; native Windows confirmation and public RunContext remain open. SSH
rebases without conflicts at `0d5d42bee0dab2e09e16fd448fcb68ab6f01fd22`. Range-diff retains all 171
SSH commits: 170 patches match exactly, and the fixture patch drops only the observation correction
now supplied by transport. The SSH implementation directory is byte-identical to the previous tip;
shared process/custody/pump files match the new base exactly. The full Linux Python 3.12
non-integration suite passes 16,529 tests with 49 skips and 27 warnings, exit 0 in 211.03 seconds.
No live process has an argv path under its exact private test root. Full strict mypy checks 1,313
sources and repeats only the three held enrollment errors, exit 1. Ruff/format (1,350 files), exact
Typer isolation, file quality, locked-SDD, Rulesync and whitespace checks pass. Local website and
native validation were not rerun for this draft rebase; new hosted results need their own full
report. Both resource decisions and all recorded terminal, trust, RunContext and native acceptance
gates remain open. No final-product feedback/fix round is consumed.

Published SSH `933eaac48c92b42dc1f82a0caef4326dd7f24613` has a complete
[hosted report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6028131142) on
transport `fa730f1fc`. Run `37551266260` passes every pytest job: Linux Python 3.12-3.14 each report
16,373 passed, 203 skipped and 27 warnings; Windows Server 2025/Python 3.13 reports 1,431 passed, 64
skipped and 25 warnings. Merge `99ba6924e0b8817c924497b6c3c73a205abb212d` has those exact
transport/SSH parents, with tree `6cb4410fe79421e6acade33009a9c723edf7d9ea` equal to the published
SSH tree. The overall result remains red on the three held enrollment typing errors. Other
repository and website jobs pass; hosted Typer isolation is skipped after mypy fails. This is
combined hosted fixture evidence, not native workflow or physical-key proof.

Transport's next
[reviewed private increment](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6028912666)
`ebc8eedd43921fff57d6a72d4e11fe16430aec59` adds managed observe, selected-stream output, bounded
wait, explicit stop and terminal disposal. It separates positive resource/controller closure from
application-exit precision and preserves exact settled disposal retry identity. Three private lanes
clear the whole unit at `ed3603c23`; public RunContext and native acceptance remain open. SSH
rebases without conflicts at `1266b3ae82977328c001ae7fc3b2382532a224a3`. All 172 SSH patches match
exactly across the rebase; the SSH implementation directory is unchanged and shared native
process/custody/pump files match transport exactly. The full combined Linux Python 3.12
non-integration suite passes 16,583 tests with 49 skips and 29 warnings, exit 0 in 209.85 seconds.
Full strict mypy checks 1,315 sources and repeats only the three held enrollment errors, exit 1.
Ruff/format (1,352 files), local Typer isolation, locked-SDD, Rulesync and whitespace checks pass.
The exact private pytest root has no same-user argv/cwd/fd references and is removed; logs remain.
New hosted results need their own complete report. Local website/native validation was not rerun for
this draft dependency refresh. Both operator resource decisions and the recorded terminal, trust,
writer coexistence, enclosing-consumer, RunContext and native acceptance gates remain open. All 25
completed plan blocks remain unchanged. No final-product feedback/fix round is consumed.

Published SSH `04a460a0566f3cdc2eccabf0177ca4c6295aa332` has a complete
[hosted report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6029147754) on
transport `ebc8eedd4`. Run `37557868516` passes every pytest job: Linux Python 3.12-3.14 each report
16,427 passed, 203 skipped and 29 warnings; Windows Server 2025/Python 3.13 reports 1,482 passed, 67
skipped and 25 warnings. Checkout merge `8f64366685dd85549e3a7f95f2c79ae027548fef` has those exact
parents and tree `ee721515a8993e0befd4f608069a5471ec743b1a`, equal to the published SSH tree. The
overall run remains red on the three held enrollment errors. Other repository/site jobs pass; hosted
Typer isolation is skipped after mypy fails. Transport's base run `37557251890` is independently
verified green in every job. Neither run establishes complete native or RunContext workflows.

Transport's
[reviewed foreground increment](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6029389391)
`62164b4a80299161808b147976c27d8ce8027fec` composes private MANAGED/OPERATION run from the existing
launch/wait machinery and one finite deadline. Budget expiry retains an acknowledged safe job
reference without stopping, draining, disposing or releasing its owner. Three private lanes clear
the corrected unit at `0a8ceaf6c`; the carrier contract is unchanged. SSH rebases without conflicts
at `0b72d013d1c6c0e61d4d5d5011628bd5b997fdbb`, retaining all 173 commit patches exactly. SSH
implementation files are unchanged, and shared process/custody/pump files match transport. The full
combined Linux Python 3.12 non-integration suite passes 16,628 tests with 49 skips and 29 warnings,
exit 0 in 213.86 seconds. Full mypy checks 1,316 sources and repeats only the three held enrollment
errors. Ruff/format (1,353 files), local Typer isolation, locked-SDD, Rulesync and whitespace checks
pass. New hosted results require their own complete report; local website/native checks were not
rerun for this draft refresh. Both operator resource decisions and the existing
terminal/trust/writer/enclosing-consumer/RunContext/native gates remain open.

Transport also reports a controlled SQLite/framed-carrier capacity failure after 42 completed
MANAGED cycles. Both lifecycle admission queries still count resolved history against 128 rows
(`cli/agentworks/db/operations.py`); a preceding file call can shift the refusal into keeper
registration and strand aggregate close. Transport owns the required capacity correction, with exact
retry receipts and bounded recovery still required. This defect remains open in this base;
foreground composition and green fixtures do not dispose it. No public fix round is consumed, no
completed checkbox changes, and this SDD remains unlocked.

Published SSH `126a10c84fd2002fc4f62b069058c42264f5c670` has a complete
[hosted report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6029621599) on
transport `62164b4a8`. Run `37561145205`, attempt 1, passes all four pytest jobs: Linux Python
3.12-3.14 each report 16,472 passed, 203 skipped and 29 warnings; Windows Python 3.13 reports 1,527
passed, 67 skipped and 25 warnings. Checkout merge `9681f3ef18dcbb988ace3eefedfd91deb9261452` has
those exact parents and tree `8a93c09c1bfb78e067f3b142c4fc007fc53fd153`, equal to the published SSH
tree. The run fails on the three held enrollment typing errors, a website Chromium startup failure
and their aggregate gate. Website reports one failure among 160 Python tests; Node tests and
deterministic builds are skipped. The exact failing browser test passes locally, but the hosted
startup cause remains unproved. GitHub refuses the single website-only diagnostic rerun; no retry
starts. Other repository gates pass, with hosted Typer isolation skipped after mypy failure.
Transport's exact-base run `37560542843` passes every job. These results do not establish complete
native or RunContext acceptance.

Transport's
[reviewed capacity increment](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6030161108)
`94054f4ce19550a20997a6a1ed2d0166c63507b2` corrects the historical-capacity defect described above.
Both admission queries now bound 128 unfinished obligations. Immutable resolved receipts remain
until owner release, with fenced exact-ID inspection and bounded pending enumeration sharing the
ownership read snapshot. Migration 42 adds the partial index without changing migrations 39-41 or
existing data. Transport records three private lanes clear and actual SQLite/framed-carrier proof of
300 completed managed cycles and 300 clean file admissions. This is local core evidence; native
mixed-workload and full-RunContext acceptance remain open.

SSH rebases without conflicts at `b0a0662d7c9593fda5fd32cd3c84c44423872a93`, retaining all 174 SSH
patches exactly. The SSH implementation directory is unchanged, and shared process/custody/pump
files match transport. The SSH-owned upload fixture at `813615877e579ef69e96a5e9106db86c3799a02b`
captures actual admission IDs before dispatch and inspects each completed receipt individually,
preserving token, state, target, metadata, suppression and cleanup assertions. It also requires zero
pending obligations. Private project and complexity reviews clear that fixture unit; each review is
narrower than final whole-PR acceptance. The complexity lane independently passes the real loopback
fixture and finds only optional removal of sorting, retained for reproducible inspection order. Lead
real Linux SSH upload validation passes one integration case in 5.81 seconds. The full combined
Linux Python 3.12 non-integration suite passes 16,666 tests with 49 skips and 29 warnings, exit 0 in
419.73 seconds. Full mypy checks 1,318 sources and repeats only the three held enrollment errors.
Ruff/format (1,355 files) and local Typer isolation pass. Website passes 160 Python and 103 Node
tests, four builds and both deterministic comparisons. Rulesync first fails in the selected Bun
runner's incompatible YAML dependency; the same pinned version passes through Node. Exact owned
SSH-fixture, pytest and build roots have no same-user argv/cwd/fd references or permission gaps and
are removed; logs remain. Fresh combined hosted results require their own complete report.

Both operator resource decisions remain pending. Retained enrollment/forwarding consumers, terminal
state and physical Windows-key evidence, trust/creation authority, writer coexistence and rollback,
complete additive RunContext and supported-workstation R1-R5 acceptance remain open. This draft
dependency refresh consumes no public final-product fix round and changes none of the 25 completed
plan blocks. The SDD remains unlocked.

Published SSH `35585959fbf108fcb84ffcd0698d2f52483aa01f` has a complete
[hosted report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6030473477) on
transport `94054f4ce`. Run `37566943581` passes all four pytest jobs: Linux Python 3.12-3.14 each
report 16,510 passed, 203 skipped and 29 warnings; Windows Server 2025/Python 3.13 reports 1,564
passed, 67 skipped and 25 warnings in 800.31 seconds. Checkout merge
`d58f602a7db64601866dd8d6ecbcb430af6d1e4d` has those exact parents and tree
`5fab66e9d11025b62d79d48839662f6e7651e0cf`, equal to the published SSH tree. The run fails only on
the three held enrollment typing errors across 1,318 sources and their aggregate gate. Other
repository gates pass; hosted Typer isolation is skipped after mypy failure. Website passes 160
Python and 103 Node tests, four builds and both deterministic comparisons. The preceding Chromium
startup cause remains unproved. Transport's exact-base run `37565868558` passes all ten jobs: Linux
Python 3.12-3.14 each report 16,088 passed, 203 skipped and 29 warnings; Windows Python 3.13 reports
1,349 passed, 57 skipped and 25 warnings. Its checkout tree equals the transport source. These
hosted fixtures establish neither full native nor production RunContext acceptance.

Transport's
[reviewed default-shell increment](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6030455006)
`8ac6e625bbf0cc5e077cd6cae348914f38bd2394` freezes finite caller input before observing the selected
numeric workload account's shell. Resolution precedes reservation, its interpreter is persisted with
the run, and lookup and launch share the original deadline. Unsupported or unsettled lookup creates
no run. Shared helper setup preserves original control identity and exact custody across allocation
failures for inline, lookup, observation and stop calls. Three transport private lanes clear code
`332a7d501`; independent controls, complete additive RunContext and native acceptance remain open.
No shared carrier/custody wire changes.

SSH rebases without conflicts at `c036e50e49bd54c898933542e36e41b9bfcac27e`, preserving all 176
patches exactly. SSH production and fixture files are unchanged, and shared process/custody/pump
files match transport. The full combined Linux Python 3.12 non-integration suite passes 16,716 tests
with 49 skips and 29 warnings, exit 0 in 344.69 seconds. Full strict mypy checks 1,320 sources and
repeats only the three held enrollment errors. Ruff/format (1,357 files), local Typer isolation and
Rulesync through Node pass. The exact private pytest root has no same-user argv/cwd/fd references or
permission gaps and is removed; logs remain. Website files are unchanged; local website and native
checks were not rerun for this draft rebase. Fresh combined hosted evidence requires its own
complete report. Both operator resource decisions and the recorded terminal, trust/creation, writer
coexistence/rollback, enclosing-consumer, RunContext and supported-workstation R1-R5 acceptance
gates remain open. All 25 completed plan blocks are unchanged, no public final-product fix round is
consumed, and the SDD remains unlocked.

Published SSH `e58c47d73ab7bd474d4da0232d3d32ca9fa2a9f6` has a complete
[hosted report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6030840279) on
transport `8ac6e625b`. Run `37569107178` passes all four pytest jobs: Linux Python 3.12-3.14 each
report 16,560 passed, 203 skipped and 29 warnings; Windows Server 2025/Python 3.13 reports 1,613
passed, 68 skipped and 25 warnings in 1,041.39 seconds. Checkout merge
`fa6d7b89c3de05d09ebe5272da47e455f5e514e2` has those exact parents and tree
`15f67918894afe10abf5aa626821661846517c09`, equal to the published SSH source. The run fails only on
the three held enrollment typing errors across 1,320 sources and their aggregate gate. Other
repository gates pass; hosted Typer isolation is skipped after mypy failure. Website passes 160
Python and 103 Node tests, four builds and both deterministic comparisons. Transport's exact-base
run `37568074729` passes all ten jobs: Linux Python 3.12-3.14 each report 16,138 passed, 203 skipped
and 29 warnings; Windows Python 3.13 reports 1,398 passed, 58 skipped and 25 warnings. Its checkout
merge `40c668235cac4ee96d15b4cfc4e06a0e34fc2e87` has main `cea5e8523` and transport `8ac6e625b` as
parents and exactly the transport source tree. These hosted fixtures do not establish complete
native or production RunContext acceptance.

Transport's
[reviewed independent-control increment](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6030804264)
`b54461580c2369700ee95eb442f54d36ab196dd3` requires the exact core-supplied RESOURCE owner before
private observation, output, wait, stop, disposal or reconciliation. Same-VM foreign resource and
prior-operation references refuse before borrowing or dispatch; JobRef is not authority. Unused
helper setup closes acquired but never-used borrows after constructor failure, preserves original
control identity and distinguishes interrupted relinquishment from a lost reply after successful
close. All three transport private lanes clear both units. Carrier/custody wire and SSH production
are unchanged. This establishes namespace correctness and setup custody, not permission activation,
independent launch/availability or the complete additive RunContext. Transport's demonstrated second
environment traversal during independent USER_DEFAULT lookup remains a separate private correction.

SSH rebases without conflicts at `f353c9128f176c1c7ec0abd7415a6fa244c7fb05`, preserving all 177
patches exactly. SSH production and fixture files are unchanged, and shared process/custody/pump
files match transport. The full combined Linux Python 3.12 non-integration suite passes 16,818 tests
with 49 skips and 29 warnings, exit 0 in 345.62 seconds. Full strict mypy checks 1,322 sources and
repeats only the three held enrollment errors. Ruff/format (1,359 files), local Typer isolation and
Rulesync through Node pass. The exact private pytest root has no same-user argv/cwd/fd references or
permission gaps and is removed; logs remain. Website files are unchanged; local website and native
checks were not rerun for this draft rebase. Fresh combined hosted evidence requires its own report.

Transport's exact-base hosted run `37570594566` is complete with failure. Linux Python 3.12-3.14
each report 16,240 passed, 203 skipped and 29 warnings. Windows Server 2025/Python 3.13 reports one
failure, 1,397 passed, 58 skipped and 25 warnings in 921.28 seconds. The 300-run lifecycle-capacity
test raises "Operation keeper renewal is closed or expired" during initial sampling; the trace does
not distinguish a closed keeper from an expired deadline, and its cause is unproved. The test,
fixture and keeper code match transport exactly. Checkout merge
`3dcd2ae367b76a4b0b8387bdc9602442d730f995` has main `cea5e8523` and transport `b54461580` as parents
and tree `6a01436d789a77eb1d4a319a297ed082cf7e0a03`, equal to the transport source. Ruff,
formatting, full mypy (1,278 sources), Typer isolation and other repository gates pass. Website
passes 160 Python and 103 Node tests, four builds and both deterministic comparisons. Only Windows
pytest and the aggregate gate fail. No transport-owned correction or rerun is made here.

Both operator resource decisions and the recorded terminal, trust/creation, writer
coexistence/rollback, enclosing-consumer, RunContext and supported-workstation R1-R5 acceptance
gates remain open. All 25 completed plan blocks are unchanged, no public final-product fix round is
consumed, and the SDD remains unlocked.

Published SSH `c46c9cd8714a425a455bea5a7e8a75e9f6a05261` has a complete
[hosted report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6031204274) on
transport `b54461580`. Run `37572192872` passes all four pytest jobs: Linux Python 3.12-3.14 each
report 16,662 passed, 203 skipped and 29 warnings; Windows Server 2025/Python 3.13 reports 1,613
passed, 68 skipped and 25 warnings in 927.62 seconds. All six test/static/website logs identify
checkout merge `27c94d2d5e1afc400db2a819ce5ce325501ac7e2`, whose exact transport/SSH parents and
tree `995b9538996702c7e75feb99a32f4fe6f334f14e` match the published source. The run fails only on
the three held enrollment typing errors across 1,322 sources and their aggregate gate. Other
repository gates pass; hosted Typer isolation is skipped after mypy failure. Website passes 160
Python and 103 Node tests, four builds and both deterministic comparisons. The combined Windows pass
does not establish the preceding transport capacity failure's cause or validate its later private
correction. These fixtures do not establish full native or production RunContext acceptance.

Transport's
[reviewed RESOURCE-read increment](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6031314958)
`d8b4dfcee2edf340e7efff6676989e1060c22734` composes private observe, read_output and wait through
ExecutionAccess under a fresh core-bound operation. The native VM factory supplies its persisted
resource namespace; foreign resources and stale contexts refuse before effects. Reads retain the
observer's helper custody without adopting the independent run, its keeper or its terminal cache;
observer cleanup leaves the run alone. Missing WAIT can leave application status unknown with
positive resource-closure facts, never unknown controller or helper custody. Later uncertain output
helpers prevent cleanup confirmation even after a terminal observation. Ordinary observation does
not reconcile uncertain launch; output collection requires a confirmed receipt.

The same increment retains the complete independent USER_DEFAULT body through selected-account shell
lookup, avoiding a second traversal of caller environment. Its reviewed test-only capacity
correction controls monotonic time while preserving renewal threads and all 300 SQLite/framed
custody cycles; unexpected fifth observations fail promptly instead of polling a frozen clock.
Production deadline enforcement is unchanged. All three transport private lanes clear the complete
units. Carrier/custody wire, schema and SSH implementation are unchanged. Hosted Windows acceptance,
unified independent launch/mutations, job-length availability, full additive RunContext and native
acceptance remain open. The
[coordination response](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6031322885)
adds no carrier requirement or operator resource-ownership ruling.

SSH rebases without conflicts at `e07cd70b671644e88acb676e3c4d328d77a6cf80`, preserving all 178
patches exactly. SSH production and fixtures are unchanged, and shared process/custody/pump files
match transport. Full combined Linux Python 3.12 non-integration validation passes 16,874 tests, 49
skips and 29 warnings, exit 0 in 232.05 seconds. Full strict mypy checks 1,324 sources and repeats
only the three held enrollment errors. Ruff/format (1,361 files), exact local CI Typer isolation,
frozen dependency sync and Rulesync through Node pass. The exact private pytest root has no
same-user argv/cwd/fd references or permission gaps and is removed; logs remain. Website files are
unchanged; local website and native tests were not rerun for this draft rebase. Transport's
exact-head hosted static and website jobs pass on source-equivalent checkout
`d5c7ddd295508e7994b5d805132c685d733aa802`; its pytest and aggregate results remain pending. Fresh
combined hosted evidence requires its own full report.

Both operator resource decisions and the recorded terminal, trust/creation, writer
coexistence/rollback, enclosing-consumer, complete RunContext and supported-workstation R1-R5
acceptance gates remain open. All 25 completed plan blocks are unchanged, no public final-product
fix round is consumed, and the SDD remains unlocked.

Published SSH `205ab558580beff584bed23872753ea2f75ecb27` has a complete
[hosted report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6031682801) on
transport `d8b4dfcee`. Run `37575293299` passes all four pytest jobs: Linux Python 3.12-3.14 each
report 16,718 passed, 203 skipped and 29 warnings; Windows Server 2025/Python 3.13 reports 1,613
passed, 68 skipped and 25 warnings in 1,044.20 seconds. All six test/static/website logs identify
checkout `9c2aafeff7949c8392dfbc48f7f347b738afcad2`; its exact transport/SSH parents and tree
`37bd0222a12d7603214a76590ce7e25583c9a36f` match published source. Only the three enrollment custody
typing errors and aggregate gate fail. Other repository/site jobs pass, including website 160 Python
and 103 Node tests, four builds and both deterministic comparisons. Hosted Typer isolation skips
after mypy failure; the exact local check passes. Transport's exact base also has complete green
hosted evidence from run `37574481185`, including Windows 1,398/58/25 and the reviewed capacity
correction. Neither report establishes full native workflow acceptance.

Transport's
[reviewed RESOURCE stop increment](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6031855882)
`2add24df7a79f904a67a7516c2398a1fdbe2ac1e` uses the existing ExecutionAccess and tracked ordinary
helper lifetime under a fresh core-bound operation. Stale, foreign and unconfirmed bindings refuse
before effects; planned OPERATION IDs never fall back. It does not adopt the independent job, drain
its keeper or cache OPERATION terminal proof. Termination requires fresh positive closure beyond
accepted intent; uncertain helper custody remains held. The corrected late pre-observation expiry
reports DEADLINE without another dispatch or renewed budget. The existing OPERATION analogue remains
recorded with transport for subsequent shared-control work, not claimed fixed here. All three
private code lanes and both plan-closeout lanes clear. Carrier/custody wire, schema and SSH
implementation are unchanged. The
[coordination notice](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6031856124)
adds no carrier requirement or ruling on the operator-held resource APIs.

SSH rebases without conflicts at `bbf8bcda3db70faa358b2dd3467b5a84d8b45904`, preserving all 179
prior patches exactly. SSH production and fixtures are unchanged, and shared process/custody/pump
files match transport. Full combined Linux Python 3.12 non-integration validation passes 16,910
tests, 49 skips and 29 warnings, exit 0 in 230.43 seconds. Full strict mypy checks 1,326 sources and
repeats only the three held enrollment errors, exit 1. Ruff/format (1,363 files), exact local CI
Typer isolation, frozen dependency sync and Rulesync through Node pass. The exact owned pytest root
has no same-user argv/cwd/fd references or permission gaps and is removed after terminal completion.
Website files are unchanged; local website and native tests were not rerun for this draft rebase.
Transport's new hosted static and website jobs pass on checkout
`a1a752db4193f28eb2cda5e7291ab840c3495f73`; its exact main/transport parents and tree
`abb85a9bc5aab1ca2ca61b1aa2f3e3461c1c2431` match source. That is partial evidence only: its full CI
and fresh combined hosted results still require their complete reports.

Both operator resource decisions, retained consumers, terminal and physical Windows input, genuine
creation/trust authority, writer coexistence/rollback, both usable RunContext paths and complete
R1-R5/native acceptance remain open. Transport's disposal and production composition continue
separately. All 25 completed plan blocks are unchanged, no public final-product fix round is
consumed, and this SDD remains unlocked.

Combined code pin `38934134e3b8d12af814f9371b62075daa49db3e` preserves all 64 Python paths in the
SSH contribution, including deletions, from published `186458307b`. Its full suite passes 16,125
non-integration tests with 51 skips and 27 warnings, exit 0 in 267.18 seconds. Full Ruff/format
(1,329 files), strict mypy (1,292 sources), exact CI typer isolation, file quality, locked-SDD,
Rulesync and whitespace checks pass. Website validation passes 160 Python and 103 Node tests, four
builds and both deterministic comparisons. Exact settled test/build roots have no same-user live
cwd/fd references and are removed; logs are retained. All 25 completed plan records remain
unchanged, and the SDD remains unlocked. This refresh closes no native SSH, terminal, trust or
production RunContext acceptance gate.

Hosted CI `37514481493` passes every required job for published SSH `2ef07cd6e`. Its actual merge
`7441230222828189a56ee8d5392d46d47831a7d3` has transport `16a6cecba` and SSH `2ef07cd6e` as parents
and exactly the published SSH tree. Linux Python 3.12-3.14 each pass 15,972 tests with 202 skips and
27 warnings; Windows Server 2025/Python 3.13 passes 1,142 tests with 59 skips and 25 warnings. This
verifies that earlier code pin, not native terminal, trust or production RunContext acceptance, and
does not attribute its hosted run to the later custody design response.

The preceding combined code pin `a38e5814fd180fe65899001d5a74426c4587dcf0` preserves all 64 Python
files in the SSH contribution, including retained deletions, from published `47013bcf2`. Its full
suite passes 16,046 non-integration tests with 51 skips and 27 warnings, exit 0 in 251.43 seconds.
Full Ruff/format (1,320 files), strict mypy (1,283 sources), exact CI typer isolation, file quality,
locked-SDD, Rulesync and whitespace checks pass. Fresh website checks pass 160 Python and 103 Node
tests, four builds and both deterministic comparisons. Exact settled test/build roots have no
same-user live cwd/fd references and are removed; logs remain retained. All 25 completed records
remain unchanged and this SDD remains unlocked. This dependency refresh adds no native SSH terminal,
authentication, trust or production RunContext acceptance.

The preceding code pin `7e88676faaf5566f9f7b75a394e6e94ac4c6ab9d` preserves every SSH source and
test byte from published `f4dccab54`. Its complete suite passes 15,881 non-integration tests with 51
skips and 27 warnings, exit 0 in 257.82 seconds. Full Ruff/format (1,316 files), strict mypy (1,279
sources), exact CI typer isolation, file quality, locked-SDD, Rulesync and whitespace checks pass.
Website validation passes 160 Python and 103 Node tests, four builds and both deterministic
comparisons. This proves the local combined dependency refresh, not native activation or new SSH
terminal/production acceptance. The
[Windows ancestor-sharing correction](phase2-results.md#windows-ancestor-sharing-correction) records
that rebase and its remaining limits. The earlier
[local-download stack refresh](phase2-results.md#private-local-download-stack-refresh) and
[advanced-stack refresh](phase2-results.md#advanced-transport-stack-refresh) retain the pins they
validated. The
[structural preflight integration](phase2-results.md#structural-preflight-integration) records the
combined SSH result. The
[private-access dependency update](phase2-results.md#private-access-and-paired-plan-dependency-update)
records an earlier rebased SSH head and its scope. The preceding
[recovery-dispatch dependency update](phase2-results.md#recovery-dispatch-dependency-update) retains
its measured composition. The
[file-custody adoption](phase2-results.md#durable-file-call-custody-adoption) records the
managed-target binding and durable obligations in the SSH upload proof. The earlier
[joint Windows/WSL2 report](phase2-results.md#joint-windowswsl2-mechanism-proof) retains its
measured composition and mechanism scope. #832 stacks on transport's implementation branch;
transport lands first. Buffered adapter validation does not close the shared launch-interruption
gate, live I/O native acceptance, terminal preparation or additive RunContext delivery. Terminal/PTY
work is proceeding in parallel in the transport lane, as confirmed by the operator.

Transport's
[launch-boundary response](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6009186902)
confirms that it owns the borrowed native-stdin addition to the existing held process owner. Its
[published dependency checkpoint](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6010031679)
now supplies the privately reviewed shared types and launch option. The
[terminal implementation record](terminal-results.md) separates shared adoption, the privately
reviewed POSIX relay and Windows caller resource from native acceptance. The public terminal feature
remains disabled. SSH retains its PTY slave until launcher/client settlement and keeps the native
resource worker through interrupted waits and relay cleanup. An
[owner-mediated resize boundary](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6010061812)
is now
[accepted by transport](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6010143834),
with its
[reviewed pin now published](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6010798164).
The private SSH relay uses that actual finite-budget notification API; an accepted local signal does
not prove remote geometry propagation. SSH does not access a private client PID or add a second
process owner. Transport's
[Windows response](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6010798696)
accepts native launch/preparation ownership while leaving selection and native proof open. The
[SSH reading](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6009208568) records
the current resource increment and remaining proof separately from that planned shared seam.

### Complete the carrier and migration

The [POSIX resource implementation](terminal-lld.md#posix-resource-implementation) and privately
reviewed relay advance acquisition, cleanup evidence and retained restoration. Resize composition,
shared presentation cleanup and complete native terminal delivery remain open; the completed-carrier
and supported-workstation gates below are unchanged.

- [x] Adopt transport #833's shared finite-input subprocess pump at `e85e9f5c` while preserving SSH
      environment policy and report provenance. Integration code `1b2c6832` passes the combined
      execution and full local suites, recorded in
      [phase2-results.md](phase2-results.md#shared-subprocess-adoption). This records buffered
      adoption only; launch-interruption ownership, live/terminal delivery and production
      composition remain open.
- [x] Supply the standalone buffered-mode refusal in `678e487d` so transport can widen shared I/O
      before SSH adopts the new modes. Validate its separate cherry-pick onto `a885ef5a`, existing
      buffered behavior and refusal before admission/process work. The
      [integration record](phase2-results.md#remaining-integration-and-acceptance) distinguishes
      simulated future types from pending real-extension proof.
- [x] Adopt transport `bb7ebb58` runtime selection and stdin helper framing in the SSH file
      fixtures. Require positive runtime admission before helper observations, retain explicit SSH
      trust in command-size fixtures, and repeat read, stage and snapshot delivery through installed
      Linux OpenSSH. All three cases pass at `e852aef2`; production file and native acceptance
      remain open.
- [x] Adopt the transport-owned `LocalProcessOwner` for forwarding, retaining ownership before
      dispatch and stopping pipe borrowers before release. The operator authorized the scoped SSH
      contribution on 2026-09-21; transport already published the extraction, so reuse it and keep
      any integration-required shared correction separable. Terminal and RunContext remain with
      transport. Implementation `8c3c4e5b` passes the focused and full local suites, installed Linux
      forwarding cases and all three private reviews; the
      [evidence record](phase2-results.md#forwarding-uses-the-shared-held-process-owner) retains the
      remaining shared interruption and native acceptance limits.
- [x] Prove the private owned upload/publication composition through real Linux SSH at `eea4ba2ba`:
      create, revision-matched replacement, duplicate-create and stale-revision refusal, typed
      reduction, binary integrity, sensitive delivery and exact custody/scratch cleanup. The
      [file-workflow record](phase2-results.md#owned-upload-delivery-over-ssh) scopes this to the
      private composition; production FileAccess, recovery faults and native platform acceptance
      remain open.
- [x] Refuse input unsupported by SSH's byte adapter before trust admission, installed-client
      probing or dispatch, including an already expired operation. This prepares for transport's
      terminal input extension without mapping it to pipe EOF. The synthetic extension regression
      proves passive refusal; native terminal implementation and proof remain open.
- [ ] Finish the SSH LLD for R1-R5: complete connection validation, installed-client/path policy,
      config schema and conversion, trust preservation/enrollment/refusal, process/terminal and
      forwarding lifetimes, diagnostics and acceptance fixtures. Name the authority and refresh
      procedure for copied CA/revocation policy, failed updates and rollback.
- [ ] Complete the carrier's supported delivery modes and explicitly owned forwarding, retaining the
      proven buffered implementation and the transport-owned execution semantics. Optional SCP
      acceleration must use the same connection policy and obey shared file semantics. Adopt the
      concrete shared caller-held delivery custody at actual SSH delivery, with no temporary
      adapter-local storage. Prove bounded pending construction/cleanup, serialized settlement,
      refusal while ownership is pending/lost, native descriptor/terminal retention and aggregate
      drain after borrow handoff. Retain immutable evidence and original interruption; local cleanup
      establishes no remote cancellation or dispatch-debt resolution. Enrollment must retain its
      same acquired candidate writer lock alongside native custody until settlement; its enclosing
      creation/maintenance resource consumer remains unresolved as recorded in the
      [enrollment LLD](enrollment-lld.md#caller-held-delivery-custody-adoption). Forwarding must
      retain its native lifetime before admission, including failed startup, and expose bounded
      cleanup retries through that same retained lifetime; the
      [forwarding gap](forwarding-lld.md#retained-cleanup-adoption-gap) records the synthetic proof.
- [ ] Implement and validate connection/trust migration with isolated copies: supported operator
      policy, authentication offers, strict verification, genuine creation provenance, concurrent
      writer ownership and rollback evidence. Follow the
      [migration strategy](migration-strategy.md).
- [ ] Verify R1-R5 across supported Linux/macOS/Windows workstations and target/provider boundaries.
      Exercise terminal restoration, forwarding/listener failure and cleanup, plus the complete
      isolation and trust acceptance requirements. Record missing live coverage for operator
      disposition; inherited PoC evidence covers only its observed cases. Include
      [issue #845](https://github.com/WayfarerLabs/agentworks/issues/845)'s percent-encoded JSON
      environment shape: verify exact guest bytes for percent tokens, quotes, backslashes, Unicode
      and multiline values through the production new path, with values absent from SSH argv. Cover
      the reported Windows OpenSSH 9.5 client and an expansion-enabled 10.x client, alongside
      supported Linux/macOS workstations. Private Linux candidate proof does not close this
      production/native gate or fix callers still using the legacy path.

The [native environment report](phase2-results.md#native-environment-delivery-on-four-clients) now
proves all 14 synthetic cases through private preparation and SSH carrier composition at `4e32a9f9`,
on Linux 9.2p1, Windows 9.5p2/10.0p2 and macOS 10.2p1 clients. Production RunContext delivery
remains open. Its unchanged production-path comparison reproduces expansion failures on 10.x and
exact delivery on 9.x; the original Windows 11 executable remains unconfirmed. No legacy fix or
completion checkbox follows from that evidence.

The
[native managed-lifecycle report](phase2-results.md#native-managed-lifecycle-on-the-composed-ssh-branch)
adds test-scoped Linux/GCE evidence on the #833 + #832 composition. The
[follow-up native boot-fence report](phase2-results.md#derived-guest-boot-fence-proof-on-composed-ssh)
verifies stable and changed guest-boot identity across real Linux SSH reboots and WSL2 distribution
restarts. Neither supplies the production composer, public RunContext path, lost-hold recovery,
terminal delivery or supported-workstation acceptance. The boot-fence recovery gate remains with
transport. The later
[transport schema-correction report](phase2-results.md#transport-schema-correction-on-the-unchanged-execution-path)
resolves the database-open collision and records green hosted CI without adding a new SSH runtime
composition or closing those Phase 2 gates.

The
[native workstation report](phase2-results.md#native-workstation-report-and-remaining-delivery-findings)
adds private Linux/macOS SSH publication and Windows WSL2 observations. It also finds macOS home ACL
refusal, a hardened-admin boot-probe failure and slow repeated-chunk SSH downloads. Transport owns
their corrections; ordinary admin composition, usable home destinations and practical file delivery
remain unproved. The earlier root-only fence proof does not close the admin-path gate. These
findings remain within the open workflow acceptance below, not new completion claims.

### Integrate and deliver the usable new path

- [ ] Integrate with transport's platform-host/provisioning consumers and review provider-inner
      isolation evidence. Exercise reusable host composition separately from Lima management.
- [ ] Supply SSH delivery/failure evidence for transport's later file-only slice and other required
      workflows, using its current acceptance definitions. Transport owns the file API, policy and
      harness; this SDD does not mirror the operation list. The later slice gates broader file
      migration, not the Phase 1 carrier proof.
- [ ] Reconcile current consumer risks with transport's migration inventory, including bootstrap
      tools, sensitive discovery, ownership/cleanup checkpoints and activation behavior. Transport
      owns those caller changes. Use the
      [current risk disposition](migration-strategy.md#current-migration-risk-disposition),
      retaining the historical comparison pointer and truthful baseline checkboxes. Source
      reconciliation does not complete the required workflow evidence.
- [ ] Deliver connection/trust migration and rollback evidence before the first new production use.
      Resolve old/new writer ownership and config compatibility while both stacks remain usable; no
      duplicate mutation dispatch or permanent bridge. Shared job/plugin migration stays with
      transport.
- [ ] Verify the complete SSH-backed workflows required for additive delivery with transport's new
      surface. Prove new targets usable through `admin_execution_target()` and
      `agent_execution_target()` alongside unchanged `admin_target()` and `agent_target()` behavior.
      Run new-stack tests with retirement modules unavailable in isolation; this does not delete the
      old production path. Remove or promote SSH-owned proof-only scaffolding while retaining useful
      regressions.
- [ ] Pass independent project, complexity and correctness reviews, applicable local/hosted gates
      and authorized live validation at the integrated implementation head. Promote implemented SSH
      contracts, configuration and recovery guidance into permanent documentation with the code.
- [ ] Record R1-R5 implementation acceptance, exact integration evidence and remaining retirement
      obligations. Hand off the implementation PR without `locked.md` or a claim of full SDD
      closure.

**Phase 2 definition of done:** the full carrier and connection/trust migration machinery satisfy
R1-R5 for additive delivery, required integrated workflows pass, both paths are usable through
RunContext with old behavior preserved, and permanent collateral reflects what ships. The SSH PR may
land in the agreed dependency order; transport's consumer migration and physical deletion are later
work, not prerequisites for implementation delivery or claims of completion here.

## Intervening transport stages: SSH waits and responds to issues

Transport leads these ordered stages, with SSH assistance only when an issue requires it:

1. Migrate all production/plugin consumers, including direct calls, from old to new.
2. Remove legacy access from RunContext after callers have migrated.
3. Delete old transport and validate the resulting production workflows, leaving old SSH in place.

These are external dependencies, not SSH implementation tasks or completed claims. Record their
actual merged revisions and acceptance evidence when the operator requests final SSH retirement.
Keep this SDD open and unlocked while waiting. Do not begin old SSH deletion automatically when
transport finishes. No new recipient permission isolation is claimed during coexistence; transport
owns activation against its complete removal gate. SSH trust and operational safety apply from the
first new-stack use.

## Phase 3: Remove old SSH on the operator's later request

This final SSH PR begins only after consumer migration, legacy RunContext removal, old transport
deletion and explicit operator direction. It completes this SDD rather than creating a new effort.

- [ ] Receive the retirement request and verify the preceding stages against their merged revisions
      and full reports. Refresh the legacy SSH inventory, including direct/lazy imports, provider
      paths, plugin entry points, fixtures and retained utility dependencies. Resolve remaining
      consumers before deleting their implementation.
- [ ] Physically delete the old SSH stack and obsolete SSH-only scaffolding/tests. Retain useful
      replacement regressions and deliberately retained utilities with an audited dependency
      closure. Preserve operator configuration, credentials, complete trust/revocation records and
      rollback/cleanup evidence; deleting implementation does not authorize deleting that state.
- [ ] Verify installed-package startup and complete SSH-backed production workflows after physical
      deletion, including supported workstation/platform coverage. Refresh independence against
      transport's authoritative retirement set and record any missing evidence for operator
      disposition rather than claiming a pass.
- [ ] Complete private reviews, applicable local/hosted gates and full integration reports at the
      retirement head. Update permanent guidance for the behavior now shipped, and record R1-R5
      final acceptance and dependency dispositions.
- [ ] Add `locked.md` only with this final retirement and evidence-backed closeout.

**Phase 3 definition of done:** old SSH is physically absent after the earlier transport stages,
complete installed workflows pass without either legacy stack, retained state is preserved, and the
final acceptance record and permanent collateral are current. Only then is this SDD complete.
