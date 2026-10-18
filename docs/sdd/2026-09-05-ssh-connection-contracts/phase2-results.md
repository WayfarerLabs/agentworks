# SSH Phase 2 Implementation Progress

- Updated: 2026-10-18
- PR: [#832](https://github.com/WayfarerLabs/agentworks/pull/832), draft
- Main/contract base: `cea5e8523aac05edfc3a99a940d7cfb4d71fe32f` (#830)
- State: the Windows client-family refusal is implemented; three input-fixture corrections are
  supplied for transport composition. Full terminal and production acceptance remain open

Earlier checkpoint evidence and dependency history live in separate files. The headings below retain
existing plan links; the linked sections preserve the complete original records. Historical round 1
drain conclusions are superseded by the round 2 finite-backlog finding.

## Implemented scope

See the [earlier checkpoint evidence](phase2-initial-results.md#implemented-scope).

## Local evidence at the evaluated code

See the
[earlier checkpoint evidence](phase2-initial-results.md#local-evidence-at-the-evaluated-code).

## Private review disposition

See the [earlier checkpoint evidence](phase2-initial-results.md#private-review-disposition).

## Windows CI correction

See the [earlier checkpoint evidence](phase2-initial-results.md#windows-ci-correction).

## Shared subprocess adoption

See the [earlier checkpoint evidence](phase2-initial-results.md#shared-subprocess-adoption).

## Real SSH file-helper delivery

See the [earlier checkpoint evidence](phase2-initial-results.md#real-ssh-file-helper-delivery).

## Real snapshot helper delivery

See the [earlier checkpoint evidence](phase2-initial-results.md#real-snapshot-helper-delivery).

## Held-owner dependency and runtime admission

See the
[earlier checkpoint evidence](phase2-initial-results.md#held-owner-dependency-and-runtime-admission).

## Forwarding uses the shared held-process owner

See the
[earlier checkpoint evidence](phase2-initial-results.md#forwarding-uses-the-shared-held-process-owner).

## Transport rebase and external managed-process evidence

See the
[earlier checkpoint evidence](phase2-initial-results.md#transport-rebase-and-external-managed-process-evidence).

## Owned upload delivery over SSH

See the [earlier checkpoint evidence](phase2-initial-results.md#owned-upload-delivery-over-ssh).

## Explicit whole-operation resolution

See the
[earlier checkpoint evidence](phase2-initial-results.md#explicit-whole-operation-resolution).

### Windows forwarding fixture correction

See the
[earlier checkpoint evidence](phase2-initial-results.md#windows-forwarding-fixture-correction).

## Joint Windows/WSL2 mechanism proof

See the [earlier checkpoint evidence](phase2-initial-results.md#joint-windowswsl2-mechanism-proof).

## Lifecycle-ledger adoption

See the [earlier checkpoint evidence](phase2-initial-results.md#lifecycle-ledger-adoption).

## Private access and paired-plan dependency update

See the
[earlier checkpoint evidence](phase2-initial-results.md#private-access-and-paired-plan-dependency-update).

## Recovery-dispatch dependency update

See the
[earlier checkpoint evidence](phase2-initial-results.md#recovery-dispatch-dependency-update).

## Durable file-call custody adoption

See the [earlier checkpoint evidence](phase2-initial-results.md#durable-file-call-custody-adoption).

## Managed-process fix integration

See the [earlier checkpoint evidence](phase2-initial-results.md#managed-process-fix-integration).

## Structural preflight integration

See the [earlier checkpoint evidence](phase2-initial-results.md#structural-preflight-integration).

## WSL hold-probe composition checkpoint

See the
[earlier checkpoint evidence](phase2-initial-results.md#wsl-hold-probe-composition-checkpoint).

## Native managed lifecycle on the composed SSH branch

See the
[earlier checkpoint evidence](phase2-initial-results.md#native-managed-lifecycle-on-the-composed-ssh-branch).

## Derived guest boot-fence proof on composed SSH

See the
[earlier checkpoint evidence](phase2-initial-results.md#derived-guest-boot-fence-proof-on-composed-ssh).

## Transport schema correction on the unchanged execution path

See the
[earlier checkpoint evidence](phase2-initial-results.md#transport-schema-correction-on-the-unchanged-execution-path).

## Advanced transport stack refresh

See the [earlier checkpoint evidence](phase2-initial-results.md#advanced-transport-stack-refresh).

## Private local-download stack refresh

See the
[earlier checkpoint evidence](phase2-initial-results.md#private-local-download-stack-refresh).

## Percent-encoded environment regression

See the
[earlier checkpoint evidence](phase2-initial-results.md#percent-encoded-environment-regression).

### Native environment delivery on four clients

See the
[earlier checkpoint evidence](phase2-initial-results.md#native-environment-delivery-on-four-clients).

## Workstation download transport refresh

See the
[earlier checkpoint evidence](phase2-initial-results.md#workstation-download-transport-refresh).

## Windows ancestor-sharing correction

See the
[earlier checkpoint evidence](phase2-initial-results.md#windows-ancestor-sharing-correction).

## Native workstation report and remaining delivery findings

See the
[earlier checkpoint evidence](phase2-initial-results.md#native-workstation-report-and-remaining-delivery-findings).

## Remaining integration and acceptance

See the
[earlier checkpoint evidence](phase2-initial-results.md#remaining-integration-and-acceptance).

## Historical dependency chronology

See the [preserved dependency chronology](phase2-history.md#historical-dependency-chronology).

## Full native feedback and implementation round 1

The [SSH native report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6090196583)
measures public SSH `642dd8af415ae9334a2975a12778f3be04c6ff65`, source tree
`ccc602e16efe11e641c23676dbdb03cd40f4065d`, on transport `dd10ebbeee677570c95f9b0f30688f32f6891aeb`
and main `cea5e852`. The separate
[transport native report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6089972343)
measures that exact shared dependency. Both complete reports and the
[complexity report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6089600797)
were read before round 1 fixes started. The
[critical reading](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6090296937)
records the findings, disagreements and ownership. `review-requested` was removed before fixes; #832
remains draft and the three-round final-product allowance has round 1 in progress.

The SSH raw-byte cells use Debian 12/aarch64, macOS 26.3/arm64 and Windows Server 2022 against a
Debian 13 guest. Installed clients are OpenSSH 9.2p1, 10.2p1, Windows inbox 9.5p2 and Win32 OpenSSH
10.0p2. All four preserve finite binary bytes, separate streams, environment values from #845 and
live duplex bytes. Exit 0/1 is known; exit 255 remains unknown. Expired-before-start requests do not
dispatch, deadline expiry does not claim remote cancellation, pending custody rejects a second
exchange, and forwarding removes its listener after cleanup. Raw SSH 1 MiB output takes 0.85–1.04
seconds. These observations are useful but do not prove the unfinished full surface.

Material SSH findings are Windows implicit cwd executable selection and unusable enrollment after
mandatory custody adoption. Linux integration has 30 passes, nine failures and three skips; the nine
failures all reach missing-custody calls hidden by broad exception wrapping. Mypy repeats three
missing-custody errors. The hosted Windows managed-start failure was not reproduced in 25 serial
SSH-head attempts, 25 transport-head attempts or four parallel Windows selections; no unique cause
is established. The Server 2022 bed differs from hosted Server 2025. Other terminal, cleanup and
descendant-drain failures are separately scoped, not explanations for that failure.

The source fixes now pin one absolute client for discovery and use, allow concurrent shared trust
readers while keeping publication writers exclusive, and implement caller-held forwarding and
enrollment resources around the same native custody. The latter implementation and all combined
reviews/gates are still in progress. Lock publication exclusion remains necessary through directory
flush and failure blocking; an unlocked-reader shortcut was rejected for that durability window. No
legacy stack or replacement native owner is introduced.

Raw SSH status 255 cannot distinguish client refusal from guest exit 255. The report's proposed
host-key diagnostic improvement does not justify parsing stderr into trusted completion evidence;
FRD R4 and mixed-stderr provenance remain unchanged. Strict path admission continues to reject macOS
symlinked `/tmp` and `/var` paths; canonical operator-owned paths need documented guidance.

Transport's measured deadline recovery, WSL independent-job availability and slow helper transfer
are material shared findings. Expired native work can retain an inaccessible VM claim even after
remote exit; an independent WSL job dies after operation-length holding ends; 3 MiB downloads take
85.5–177.6 seconds across WSL2 and QGA. Those corrections, factories and additive RunContext remain
transport-owned. SSH raw-stream throughput does not fix the shared snapshot exchange shape.

Terminal return/cleanup needs retained relay, descriptor and restoration obligations as well as the
native child. Current shared custody retains only `LocalProcessOwner` and assumes pipe borrowers
have stopped before close. The
[coordination concern](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6090393377)
asks transport to evaluate retention of the exact cleanup coordinator under the existing contract.
Public terminal delivery stays disabled until safe bounded composition and native acceptance.

Neither report exercises complete managed-trust/KRL publication, writer coexistence, genuine
creation provenance, full terminal physical input, both usable RunContext paths or all supported
platform R1–R5 workflows. Native Windows client/locking fixes need the next exact combined testing
pin. Tester cleanup deleted its VMs, revoked tokens, closed forwards and stopped beds; residual
operator-owned offline tailnet records do not grant this lane deletion authority. Full merge
acceptance remains open, and this SDD remains unlocked.

## Round 1 private ownership corrections

Code `eb0089267cbcc746d6b72bd93da1f996be66c9d7`, source tree
`2641d209eb1033a2a938afc6b90c9e06a0363f0f`, integrates client selection, trust readers and the first
held-resource implementation. Its complete local Linux Python 3.12 non-integration suite passes
**17,022 tests, 51 skips and 29 warnings**, exit 0 in 345.54 seconds. Full Ruff/format passes 1,368
files and mypy passes 1,331 sources, clearing the three earlier enrollment typing errors. Exact CI
Typer isolation, frozen sync, file quality, locked-SDD, Rulesync and whitespace pass. Website
validation passes 160 Python and 103 Node tests, four builds and both deterministic site-base
comparisons. These are local measurements; native Windows and hosted acceptance remain open.

The three independent private lanes at that pin identify four corrections before handoff:

- Ordinary forwarding thread refusal must not retain an inert native owner forever while waiting for
  a thread that was never admitted. Stop plus the permanent drainer-admission gate proves a late
  inert worker cannot borrow pipes; admitted workers still require completion and joining.
- Enrollment must keep the supplied delivery store fixed. A public reassignment probe otherwise
  closes a fresh store and releases the writer lock while the original native writer is pending.
- A fixture owner must retain forwarding before startup and settle coordinators before raw native
  storage. A pre-yield failure plus incomplete cleanup otherwise permits separate fixture cleanup to
  close pipes underneath a paused borrower.
- A 50 ms setup budget does not establish the pending-native boundary. Expiry must follow proved
  native admission so filesystem scheduling cannot turn the test into a pre-dispatch refusal.

Code `024bd3f82a9d3d0cccf9f29b5d0b99f042e7eead`, source tree
`ed52fe1bea32dcf87a695bcceb8e17e6e0f96773`, corrects all four. Project and complexity lanes each
pass 121 focused tests with two native Windows skips; complexity also records 18 integration tests
excluded. Correctness passes all 507 SSH non-integration tests with six skips. The
replacement-storage and paused-borrower probes now preserve exclusion and native pipes until exact
settlement; owned children are reaped. All three lanes clear the reviewed ownership corrections for
a draft checkpoint, without claiming complete terminal, native or RunContext acceptance. Removing
forwarding's single-user borrow lock and redundant close lock retains the documented serialized
lifecycle and actual borrower barrier.

The complete combined suite at that corrected pin is **not green**: one failure, 17,022 passes, 51
skips and 29 warnings, exit 1 in 273.35 seconds. The sole failure is
`test_terminal_relay_drain.py::test_alternating_sink_stalls_preserve_bounded_inherited_output[True]`,
previously reported as an intermittent fixture failure. Its child raises a `CalledProcessError`, but
the initial trace hides the child assertion and stderr. It is being investigated before a new
handoff; the successful earlier source does not clear it. The corrected source's Ruff/format, mypy,
frozen sync, file quality, locked-SDD and whitespace checks pass. Website inputs and CI workflow are
unchanged from the preceding measured validation.

The optional unused `enrollment_custody` helper has no callers and is removed in `0ad902a79` after
96 affected resource tests and scoped static checks pass. No production API or effect changes in
that removal. The full failed-suite evidence retains its earlier exact source pin.

Both exact owned full-suite fixture directories were independently checked for same-user process
argv, cwd and file-descriptor use after terminal completion, with no permission gaps, before
removal. Public #832 remains draft at `642dd8af` with the checkpoint label removed during this fix
round. No next public iteration or completed-carrier checkbox follows from these private results.

## Round 1 terminal drain correction

Code `4ba974a5e` integrates the SSH-only terminal drain correction from worker commit
`5973e783191bf331f25e46f03fcaa0a3e539b348`. Pending bytes previously paused the post-exit budget
even when the sink accepted each partial write. Continuous inherited output therefore kept
collection alive until the operation deadline. Only an actual sink refusal (`None`) now pauses the
existing 100 ms budget; positive partial progress consumes it. The operation deadline, pending-first
delivery, byte validation and native/terminal cleanup requirements remain unchanged.

The original owned flood probe failed 12 of 32 parallel cases: nine reached the two-second deadline
and three exceeded the existing 1.5-second completion bound. The same wall-clock probe passes all 32
corrected cases in 0.4536–0.4907 seconds. The strengthened existing fixture adds controlled elapsed
time for each accepted 1,024-byte partial write, while keeping two real 160 ms sink stalls and the
original real-time and byte assertions. Old runtime fails all four negative controls with DEADLINE;
corrected runtime passes four serial and 32 parallel cases in 0.3449–0.3941 seconds with 4,096–7,168
bytes per stream and 0.10–0.12 seconds of accepted-delivery time. Fixture failures now expose
measured results and bounded diagnostic tails rather than hiding the child assertion. Worker
validation passes 507 SSH tests with six skips, full typing and style, and file quality.

The unchanged shared process implementation, blob `ca5d285937b4c873292984f24b58b35e93945378`, has
the same pending-byte timer condition. An adapted owned probe uses `EndOfInput`, `SinkOutput`, live
stdio and caller-held `LocalDeliveryCustody`, without a PTY. All 16 controlled-progress cases
exhaust the operation deadline after 1.62–1.66 seconds of accepted-delivery time instead of
returning bounded OUTPUT; actual wall time is 0.344–0.384 seconds. All clients exit 0, native
custody settles and the exact owned descendants are reaped. A preceding wall-clock-only shared probe
passes all 32 cases; the controlled measurement proves budget accounting, not a measured native
platform incident. Shared source is untouched and correction belongs to transport; the
[coordination finding](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6090825072)
records the scoped reproduction. The runnable probe, source pin and results are retained at
`/tmp/agw-shared-drain-clock-bpg22okn` for coordination.

These are private local measurements. Final independent reviews clear candidate
`2cba54735f5d108c18f2eb03d82637d679f5d261`: project passes 154 affected tests, complexity passes 32
selected tests plus negative controls, and correctness passes 42 terminal tests plus finite
partial-delivery probes. Complexity acknowledges an optional redundant final flag reset; it remains
consistent with the existing final stream-state cleanup. Correctness confirms slow successful
partial delivery can exhaust the separate drain budget with truthful OUTPUT/incomplete evidence; the
terminal design now states that consequence. All 25 completed plan blocks remain unchanged, and no
SDD lockfile is introduced. Combined gates for this correction remain pending; native Windows,
hosted managed-start and full terminal/RunContext acceptance remain open. Public #832 is still draft
at `642dd8af` without a checkpoint label during round 1 fixes. This record does not close the round
or complete the carrier.

## Round 1 corrected checkpoint validation

The complete combined Linux Python 3.12 non-integration suite at reviewed candidate
`2cba54735f5d108c18f2eb03d82637d679f5d261` passes **17,023 tests, 51 skips and 29 warnings**, exit 0
in 298.24 seconds. CLI source tree is `e812a756b91205aa5a719943a9b15739027f990c`; subsequent commits
change only documentation. This clears the local inherited-output failure without changing its
bounds. Full Ruff and format pass (1,372 files), mypy passes 1,331 sources, frozen sync checks 70
packages, and Typer isolation passes. File quality passes all 484 Markdown and 457 spelling inputs;
locked-SDD and whitespace checks pass. Rulesync policy inputs are unchanged from the earlier green
check. Website, guide, workflow and dependency inputs are unchanged from `eb0089267`; its measured
160 Python tests, 103 Node tests, four builds and two deterministic comparisons remain the exact
unchanged-input validation basis.

An initial local invocation without the explicit CI marker selection was interrupted and is not
acceptance evidence. The successful complete invocation explicitly selects `not integration` and
uses a fresh private fixture directory with secret variables excluded. Both the interrupted and
completed fixture directories are retained: the independent same-user process scan finds no matching
argv, cwd or descriptor use but has one host inspection permission gap, so it does not justify a
claim of complete independent cleanup. Exact owned probe children and descendants are separately
reaped by their owning fixtures. No operator resources are removed.

Documentation follow-up records the three private reviews and the finite-output consequence; project
review separately clears that documentation delta. All prior completed plan blocks remain unchanged
and the SDD stays unlocked. The new draft checkpoint publishes these corrections plus explicit
tester considerations before its `review-requested` edge, closing implementation round 1 of 3 with
two rounds remaining. No next public fix round starts before full reports from that checkpoint.
Native Windows and hosted managed-start acceptance remain open; the old hosted failure is not
cleared by this local green run. Full terminal custody, genuine creation/trust authority, both
usable RunContext paths and complete supported-platform acceptance still gate merge intent.

## Round 2 collected feedback and dependency adoption

The complete
[round 1 native report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6091160981)
measures SSH `ba18a087802341088ce0daeb114d83b4c9dcd5b6`, tree
`996eb7f6c0700dceb3f42bec75274194e8d675b7`, on unchanged transport `dd10ebbe` and main `cea5e852`.
The complete unchanged
[transport report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6089972343) and
[complexity review](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6091225627)
were also consumed. The
[critical reading](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6091264045)
carries all open items and distinguishes accepted fixes from refuted or optional findings.

The one-hour collection window ended at 2026-10-10 00:17:27 UTC. Round 2 of the authorized three
implementation feedback/fix rounds then started, with `review-requested` removed before edits and
the PR retained as draft. The
[start record](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6091506771) freezes
that batch. Round 2 remains open; no new handoff, merge readiness or full implementation acceptance
is claimed here.

Hosted [run 38003640368](https://github.com/WayfarerLabs/agentworks/actions/runs/38003640368)
finished with five Windows fixture failures, 1,691 passes and 69 skips. The selected file was
correct; standard PATHEXT supplied `.EXE` while assertions required `.exe`. The correction checks
native file identity and absolute selection while retaining exact equality between the probe and all
subsequent launches. The product keeps its native spelling. Separate native measurements confirmed
that a leading empty Windows PATH component selected cwd; the resolver now skips empty Windows
components while preserving explicit `.` and relative entries and POSIX semantics. Windows
acceptance of these corrections remains required.

The focused local selection includes client policy, enrollment, forwarding, shared custody, inline
preparation and file argv fixtures: 366 passed and six skipped under `not integration`. Scoped Ruff
and format pass, and full strict mypy passes 1,331 source/test files. The Windows-only cases and
installed maintenance scenario remain unmeasured; these checks do not close the round or replace the
required private reviews and full gates at its final candidate.

The optional executable argument also allowed argv builders to fall back to the bare command name.
It is now required for strict and enrollment builders. Production callers already supply the
operation's resolved path; passive serialization fixtures explicitly supply their selected test
value. The installed live-duplex spy now observes the resolved path and retains its byte,
child-count, settlement and pipe-close assertions. A new installed-client maintenance case exercises
an empty candidate, strict recovery refusal, independent complete-policy refresh, ordinary strict
use and preservation of the failed candidate. Its injected interruption is a controlled pre-contact
fault, not native timing evidence. Its installed execution remains unmeasured at this record.

The backlog finding invalidates the prior acceptance of finite truncation in
[round 1](#round-1-terminal-drain-correction). A responsive 1 KiB/1 ms sink loses roughly 56-58 KiB
from finite 256 KiB and 1 MiB outputs on Linux and macOS. Truthful `OUTPUT` reporting does not make
that regression acceptable. Independent read-only Linux models preserve pending bytes and a frozen
native pipe prefix after the existing fixed 100 ms collection interval; they also preserve a 256 KiB
enlarged-pipe case that falsifies a fixed one-read tail. Flood, silent inherited writer, alternating
sink stalls, starvation under the original deadline and control propagation were measured in those
owned models. Those experiments inform the selected correction; they do not prove implementation or
native macOS acceptance. Transport owns the corresponding shared-pump correction, with
[the evidence routed to it](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6091264244)
and
[its agreement recorded](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6091306972).

The reviewed transport cleanup seam was integrated without conflicts: original commits `a58517c2`
and `f6ff0511`, plus owner-authored documentation `a8c039cc` and `06d6b342`, produce SSH dependency
base `a6822963e2d6c2fb27c9cd0701753d862f68920d`. Shared custody retains one ordered cleanup
coordinator bound to the exact native owner, requires aggregate settlement before reuse and keeps
cleanup reachable through a lost replacement-owner publication reply. SSH's retained POSIX consumer
implementation is delegated from that exact base. It must preserve the original worker, borrowed
endpoints and restoration obligations through finite caller return or interruption. No new carrier
schema or second owner is introduced.

The consumer is integrated as `0dccd8b96b165ede277e7abb39a35fed6a56108c` from worker
`2dfb6ff1fe496efabe7156ce25f2fddd46b00203`. Shared custody retains the exact native owner, passive
terminal and cleanup coordinator before admission. One original worker owns acquisition, relay,
bounded native cleanup and restoration; finite caller return retains pending resources. Fresh finite
close requests retry cleanup on that worker. A permanent native loss finishes the worker without
restoring the terminal or falsely settling custody. Restoration or descriptor-close uncertainty also
prevents reuse. Fixture cleanup settles this custody before PTY closure, readiness finalization,
mode repair or fallback child rescue.

The integrated drain fixes collection at 100 ms after first observed natural exit and freezes each
unfinished pipe's native unread-byte count once. Pending bytes and fixed quotas drain under the
original operation deadline, followed by at most one one-byte EOF probe. Failed queue observation
preserves pending bytes, forbids new reads on that pipe and leaves the other stream's known prefix
deliverable. Deadline or control can still win. Worker tests preserve finite 64 KiB, 256 KiB and 1
MiB output and an enlarged 256 KiB pipe whose frozen prefix exceeds one 65,536-byte read.

At exact worker `2dfb6ff1`, the non-integration SSH selection passes 524 cases with six skips in
10.34 seconds; full mypy covers 1,332 files, Ruff/format 1,373, canonical file-quality checks and
diff checks all exit zero. Sixteen owned inherited-writer probes at concurrency four have zero
failures: eight silent writers report OUTPUT and eight controlled floods consume the original
deadline. Freeze occurs 104.21-109.76 ms after observed exit; physical elapsed is 353.77-370.49 ms.
Captured counts condition the fixed-quota, byte-conservation, descendant-reaping and terminal-mode
assertions. These are Linux owned-process fixtures, not installed SSH or native macOS acceptance.
Combined-head private reviews and final gates remain required.

Follow-up worker `b48f018b43f96f24304243b3eef683ef295f7c02`, integrated as `0635be840`, makes
`daemon=False` explicit. The private caller boundary also accepts daemon threads; inheriting their
daemon flag would abandon the selected lifetime at normal shutdown. Its new regression holds raw
terminal acquisition past the caller deadline, denies reuse, then restores on the original
non-daemon worker after a fresh finite close. It fails against the preceding runtime and passes the
correction. The complete worker SSH selection then passes 525 cases with six skips; full mypy and
style/file checks exit zero.

Ordinary interpreter shutdown remains a measured limitation. At the same worker commit, pending
construction returns DEADLINE/unsettled in 0.601524 seconds and retryable cleanup in 0.111075
seconds. Both reach normal Python shutdown but remain held by the non-daemon terminal worker after
another 250 ms. Opening the owned rescue boundary permits original-worker restoration, child
reaping, pipe closure and caller PTY closure; exit follows in 43.01 and 40.70 ms. This proves the
tested resources, not settlement of indefinitely unresolved resources. The lead preserves the
non-daemon lifetime rather than adding a global exit policy or abandoning custody. Production
retained-custody and interrupted-exit proof remain gates before terminal activation.

All three independent private reviews at `5d6552dd` agree on a further observation defect: after
cleanup refusal, the shared native owner can settle autonomously on natural exit, but the SSH worker
waits indefinitely on a separate condition and misses that fact. Owned reproductions prove native
cleanup complete while the terminal remains raw and aggregate custody unsettled. Worker
`cb0c314e1a10ad14180a1e4a3cf0cb32e61a6eaa`, integrated as `ae812aa54`, observes pending and
retryable native snapshots at the existing bounded poll interval. It restores after positive natural
settlement without another caller close or signal; new cleanup attempts still require fresh caller
requests. Permanent loss behavior is unchanged. The owned regression fails the preceding runtime,
rescues safely, and passes the correction with one refused signal, natural exit zero,
original-worker restoration and the original DEADLINE result unchanged. Worker SSH tests pass 526
cases with six skips; typing and affected style checks exit zero. The shutdown limitation now
describes genuinely pending native work, not a missed settled observation.

The project review also finds stale mutable Phase 2 plan prose. Its corrected round/dependency
narrative preserves all 25 exact completed checkbox blocks. Two optional complexity simplifications
are taken: remove the uncalled installed-probe child cleanup helper and use one `collection_done`
flag for stopped collection. EOF, pending bytes and the frozen quota remain separate facts. Each
private lane must re-review the final corrected pin before the checkpoint signal.

The full local non-integration suite at source `5d6552dd` passes 17,057 cases with 55 skips in
314.43 seconds. Full Ruff/format, mypy (1,332 files), typer isolation, file quality, rulesync,
locked-SDD checks, 160 Python and 103 Node website tests and both deterministic site-base builds
exit zero. Final correction checks remain required. Hosted
[run 38011039854](https://github.com/WayfarerLabs/agentworks/actions/runs/38011039854) on that
source reports a Linux Python 3.13 finite-output fixture failure after complete bytes and EOF were
already asserted: the fixture requires exactly one frozen observation. A controlled native 4096-byte
pipe reproduces the old fixture's zero-freeze trace after full conservation; this explains a valid
alternate path without claiming the unreported hosted frozen count. Worker
`faeb8d9112d9681bf535199b9387e0ecd0878d47`, integrated as `01b392838`, accepts complete output
before freezing and checks every stream actually frozen. The enlarged-pipe case now holds sink
admission until its actual native snapshot, preserving a deterministic quota greater than 65,536
bytes. Twelve owned probes at concurrency four pass: six small pipes conserve 262,144 bytes with
zero freezes; six enlarged pipes freeze 196,608 bytes and consume three quota reads plus one empty
EOF probe. A defective single-read model still fails the original byte/status/EOF gate and settles
the fixture's owned resources. No runtime deadline, output or cleanup assertion is weakened. Worker
SSH tests pass 527 cases with six skips; final combined private review and hosted CI remain
required.

Hosted [run 38008962459](https://github.com/WayfarerLabs/agentworks/actions/runs/38008962459)
evaluates preceding SSH `8f79863c` on public transport `dd10ebbe`; it does not include this retained
terminal correction. All Linux versions and static/file/website checks pass. Windows Python 3.13.15
on Server 2025 reports 1,714 passes, 70 skips and one shared managed-foreground failure: the checked
zero-status case returns UNKNOWN/DEADLINE under its five-second fixture budget. The five earlier SSH
path assertions no longer fail. The
[dependency report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6091779665)
routes investigation to transport without claiming a diagnosed cause or changing its shared tests.

Transport's subsequent
[reviewed private checkpoint](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6091773515)
is `2a283d3769a75f413a3730822a013c93813dba05`. A separate read-only dependency audit finds its
cleanup seam byte-identical to this adopted seam, with native process, carrier I/O and terminal
handoff interfaces unchanged. Its broader QGA/cloud changes do not require adapting this consumer or
close production enrollment and additive RunContext acceptance. That larger checkpoint remains for
later full standup composition; this correction does not claim it was adopted or tested here.

The
[Windows launch/resize dependency](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6091405773)
remains open with transport. Its
[acknowledgment](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6091564522)
accepts the ownership split but supplies no reviewed Windows launch or resize implementation pin.
Current shared launch uses ordinary pipes and has no Windows pseudoconsole attachment or resize
operation. SSH still owns keyboard reading/encoding and its consumer. Binary preparation and
NUL-bearing readiness through any selected child-terminal channel require joint native proof.
Physical Windows input, full terminal activation, production creation provenance and publication,
writer coexistence/rollback, both usable RunContext paths and remaining R1-R5/platform acceptance
remain unproved. These normal implementation/dependency waits do not mark the goal blocked or
complete this SDD.

All earlier evidence and chronology are preserved in
[earlier implementation checkpoints](phase2-initial-results.md) and
[dependency history](phase2-history.md). Existing section anchors above preserve completed plan
links. Historical claims keep their original scope; this section supplies their current disposition.

## Round 2 handoff and native validation

The [round 2 handoff](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6092179600)
evaluates exact SSH `057621b5db6c49c93240a836e64ef0e7afeea888` on public transport `dd10ebbe` plus
the separately adopted retained-cleanup seam. All three final independent private reviews clear that
correction: project passes 528 cases with ten skips and an independent natural-settlement probe;
complexity and generic correctness each pass 564 cases with ten skips and owned negative controls.
The local complete non-integration suite passes 17,059 cases with 55 skips in 316.49 seconds. Full
Ruff/format, mypy (1,332 sources), file quality, typer isolation and locked-SDD checks pass. Hosted
Linux, static, Rulesync and website checks pass; local website validation has the separately
recorded unchanged-input pin. Neither those reviews nor those gates prove full standup.

Hosted [run 38012115902](https://github.com/WayfarerLabs/agentworks/actions/runs/38012115902) is not
green: Windows reports two failures, 1,713 passes and 70 skips. Both failures are unchanged shared
keeper clock/fence cases, close and binding, with zero scripted carrier calls rather than one. Their
selected one-second budget begins before real registration and arming. No actual Windows stage
causing expiration is diagnosed. The prior foreground failure does not recur. The operator
explicitly requested a checkpoint testing signal despite this shared CI failure; the scoped tester
comment precedes `review-requested` at 2026-10-10 01:28:01 UTC. It is not a green or ready claim.

The complete
[native report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6092448138)
measures that exact head, tree `755f0e70c31deb8df5aeb246917e338ea4726ba8`, using Linux OpenSSH
9.2p1, macOS 10.2p1 and Windows 9.5p2/10.0p2 against a fresh Debian 13 guest. All four preceding SSH
findings are corrected in their measured scope: Windows empty-PATH selection, finite output
preservation, installed-client spy and independent complete-policy maintenance after an empty
candidate. The Linux installed suite passes 40 cases with three skips. Linux/macOS output preserves
64 KiB, 256 KiB and 1 MiB backlogs, both streams and deliberate sink stalls; inherited writers still
yield truthful incomplete output. Native macOS FIONREAD observes the queued prefix correctly.
Original control exceptions, reaping and custody settlement pass the measured interruption cells;
the fast cleanup did not place a second signal inside pending cleanup.

Two new native findings remain material or should-fix. Ignored SIGCHLD, including inherited ignore
across exec, and a foreign reaper make shared wait publish lost status, non-retryable unclean
custody and a permanently raw borrowed terminal in the SSH relay. Transport owns that status/cleanup
correction; SSH owns its restoration consumer. Unknown status must not become fabricated exit zero,
unsafe numeric-PID signaling or unconditional release of uncertain native custody. Separately, macOS
canonical restoration sets PENDIN in lflag even using plain kernel calls. Exact fixture snapshot
assertions produce 39 failures and 15 errors. A scratch masking experiment passes 511 cases with one
remaining isolated-child failure; that last cause is not proved by the experiment. Production
restoration itself is not shown defective by these mode comparisons.

The [complexity report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6092359725)
finds no material issue in its selected delta and performs no tests. Optional guards, chunk traces
and shared custody-fixture suggestions do not establish regressions. Shared pause-aware draining
remains transport-owned; the report's slow-sink/flood hypothesis is unverified. This native report
does not exercise physical Windows input, adopted terminal launch/resize, clean-console operation,
interrupted raw acquisition, delayed construction, permanent live cleanup refusal, ordinary
shutdown, production creation/publication, writer coexistence/rollback, both new RunContext paths or
complete supported-platform R1-R5 acceptance. Its host-context failures and older-pin results are
not acceptance for those gates.

## Round 3 collected feedback and corrections

The full reports, subsequent feature-composition finding and all outstanding items were read before
this batch. The
[critical reading](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6092647076)
records agreement, limits and ownership. The minimum collection interval ended at 2026-10-10
02:28:01 UTC. The
[round start](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6092755208) records
removal of `review-requested` before tracked corrections. Round 3 of the authorized three is open;
no additional public fix round remains after it. The head is private working state, not a new
handoff, and the goal remains active.

Transport's separately reviewed CI commits `6e033123ef39bf9e4f30d41da3bbadb18039e3bd` and
`121f7b7733dfdd5315ba13b17f7ce09e05a3bd88` are adopted as `fcc9e3eca` and `2bb931b7b`. They isolate
only the relevant carrier deadline-clock aliases after fixture setup, preserving real registration,
fencing, deliberate late rejection and global clocks. They are composition fixtures, not wall-clock
timing proof. The root's four-file affected selection passes 151 cases at exact
`2bb931b7bc205b3f3d442274d98f49278f422828`. Hosted Windows validation is still required; serial
passes of the old fixtures do not prove either the failure's cause or these corrections natively.

Worker `4eb59bd286c44802b111320d812073fedb7538bf` is integrated as `5cca7ea7b` and `7b1682c28`. The
carrier now binds its existing immutable feature description once. Positive canonical mode
assertions allow only Darwin's added PENDIN; saved-bit removal and every other mode field still
fail. Embedded probes receive that same assertion function source, including the isolated SIGINT
child. Raw/pending ownership, control characters, speeds and descriptor facts remain checked. Worker
validation passes 540 SSH cases with ten skips, including 12 new comparison controls, plus scoped
style/type and full file quality. Deliberately fresh feature descriptions and broader PENDIN masking
fail their intended controls. These are Linux developer measurements; combined independent reviews,
final gates and native macOS retesting remain required. Shared lost-status cleanup stays open
pending transport's reviewed evidence distinction and exact consumer validation. No global signal
reset or new native owner is introduced.

The fully read Windows mechanism candidate `7c1794602d36dff09e8014b731be23d0f9cbbb4a` is not
adopted. A separate source audit confirms its new ctypes cell has only tests and a local Python
child probe as callers, with current SSH, process, custody and preparation APIs unchanged. It has no
native result or integrated SSH adoption seam. Its fixed nonce and release byte are test machinery;
local acceptance alone does not prove wrapping or installed-client behavior. Native measurement,
integration into the existing shared owner and complete seam reviews must precede SSH adoption.
Printable preparation `4bb25ba78` likewise has not been adopted; its paired host/guest change still
needs installed SSH encoded-canary non-reflection proof.

All final standup and retirement gates retain their scope. This batch must complete its required
private reviews and applicable gates, describe exact tester considerations before the next signal,
and escalate any required contract change or work exceeding the authorized loop. No full-carrier
checkbox, merge intent, SDD lock or platform acceptance follows from these corrections.

## Additional authorized correction round

The three original implementation rounds are spent. At the final third-round checkpoint
`1fcabfe6082ab23b1e04eeac115e842f541eee95`, all ten hosted jobs passed, including Windows (1,715
passed, 70 skipped). The root non-integration suite passed 17,071 cases with 55 skips. The
[complete native report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6093255918)
and [critical reading](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6093386229)
close collection, not full implementation acceptance. Its one-hour window ended at 2026-10-10
03:53:52 UTC. Earlier round-3 text above records its pre-handoff state.

Native macOS SSH tests passed 524 cases with 26 skips, without masks or injected startup code. The
isolated `python -I` relay entered raw mode and restored every saved mode field except the added
PENDIN bit. The previously failing isolated SIGINT case passed. Repeated signals during natural
cleanup also settled custody and restored modes; those probes did not exercise held construction,
live cleanup refusal or interpreter-shutdown holds. Inherited ignored SIGCHLD reproduced lost status
on Linux, not macOS; in-process ignore and foreign reaping affected both. All those native
measurements apply to the third-round pin, not the newly composed retirement correction.

The Windows full selection reported two phase-sensitive SSH assertions alongside its known
sshd-session console limitation. Read-only review reproduced both assertions by delaying successful
trust admission or actual version discovery beyond their 0.1-second operation deadlines. The tests
had not reached the intended forwarding owner or command cleanup-refusal phase. Baseline passed; all
six final-trial cleanup checks passed, exact child pipes closed and children were reaped. This
proves reachable test-phase fragility, not the measured cause of the specific loaded Windows
failures.

The operator authorized one additional round in the authenticated session on 2026-10-17 (local
date), then explicitly directed continuation. The
[round start](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6245164749) records
label removal before changes. Its frozen batch covers those two phase-sensitive fixtures, the
forwarding and terminal lost-status compatibility expectations, associated SSH documentation, and
the focused reviewed shared-retirement dependency. No subsequent public correction round is
authorized. The Python stdin-envelope launcher approach is unchanged.

The six owner-authored commits `1368ada65`, `f28bbf20`, `2f28ed364`, `1f662c9d`, `c1dab268` and
`40748a3c` are integrated as `488229996`, `595517ab6`, `691de395c`, `b25ce011d`, `f33afd71d` and
`bf4cd46e2`. Documentation conflicts preserve only their intended retirement additions and edits;
unrelated private context is excluded. The shared process delta is byte-identical to that focused
owner delta. No shared runtime code is rewritten by SSH. This is not adoption of the entire private
transport branch or native acceptance of its Windows, target, upload, download or cloud candidates.

The [source reading](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6245096592)
agrees that exact POSIX wait loss can retire original local capabilities while preserving
OBSERVATION and unknown status. The SSH relay already restores on positive `cleaned` evidence;
compatibility fixtures must distinguish that case from genuinely uncertain original-pipe close.
Unknown execution never permits fabricated zero status, post-loss numeric-PID operations, remote
completion or clearing dispatch debt. An uncertain original close is never retried against a reused
descriptor and must still prevent terminal restoration and custody reuse.

SSH worker `6b55dcc1d273b684312fcccacacdd8c9594a00d8` is integrated as `b4e8e4458`. Its selected
operation deadlines expire only after the intended native child and pipes exist; delayed trust and
successful version-probe variants retain the command cleanup-refusal and forwarding ownership
assertions. Fresh close uses real cleanup budgets and the same owner. The forwarding loss case
retains OBSERVATION and unknown status while accepting confirmed local cleanup. Disposable terminal
probes distinguish one original-worker restoration from sticky pipe-close uncertainty before effects
and after close/descriptor reuse, with no post-loss PID action or product close retry. Fixture
rescue runs after the product observations. The permanent SSH README now states that distinction.

The developer's final Linux SSH selection passes 546 cases with ten skips; its ten-case correction
selection, scoped Ruff/format, typing and documentation gates pass. Negative controls fail the
intended assertions for premature preflight expiry, suppressed restoration despite confirmed local
retirement, and closed-flag trust after descriptor reuse. Initial fixture instrumentation and type
failures are retained in the developer evidence, not counted as green. Root's first composed shared
selection passes 205 cases with one Windows-only skip; the adopted dependency passes full typing
across 1,336 sources. These are local scoped results, not native validation or final combined gates.

All three independent private lanes reviewed exact `6f6da9597`. Project review found an operative
unchecked plan sentence still describing blanket refusal after lost ownership; `b3ff31301` clarifies
it as unproved local retirement without changing a completed record or requirement. It also found
that the new shared native-child allocation/retry/poll faults were absent from Windows selection.
Transport supplied the owner-authored four-marker commit `cb545f064`, adopted as `90d38180c`.
Exactly seven cases now run under the Windows selection and pass locally; assertions, timers,
platform guards and runtime behavior are unchanged. This is Linux selection proof, not native
Windows evidence. The
[owner response](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6245351907)
retains full native and RunContext limits. The optional duplicate terminal JSON report is retained
as a small structured completion record; its repeated assertions are not independent proof.

At `6f6da9597`, complete local validation passes 17,117 tests with 55 skips and 29 warnings in
339.77 seconds. Full typing covers 1,336 sources; Ruff/format, Typer isolation, locked-SDD,
whitespace, repository file quality and Rulesync pass. Website validation passes 160 Python and 103
Node tests, four builds and both deterministic comparisons. Initial Bun file-quality/Rulesync
attempts failed because its temporary directory was read-only; the same canonical scripts then
passed through their supported npm fallback. Those original failures are retained, not counted as
green. The follow-up at `39b05f80e` only clarifies documentation and Windows markers; all three
private lanes clear that delta. The final checkpoint records subsequent dependency reviews and
checks before its tester-focus comment and signal. The new native combined report must verify
exact-loss retirement/restoration and permanent-close uncertainty while retaining the installed
held/delayed/refused/shutdown coverage obligations. Native Windows
launch/keyboard/resize/clean-console, paired printable preparation and encoded-canary
non-reflection, genuine new creation, settled-writer publication, trust migration/coexistence and
rollback, complete factories, both usable RunContext paths and full R1-R5/workflow acceptance remain
open. No full-carrier checkbox, ready handoff or SDD lock follows from this round.

Hosted [CI run 39095228185](https://github.com/WayfarerLabs/agentworks/actions/runs/39095228185) on
`39b05f80e` passes Linux Python 3.12, 3.13 and 3.14 and all other required jobs, but Windows reports
three failed, 1,722 passed and 71 skipped cases. All three failures occur in inherited transport
managed-operation clock-fault fixtures before their intended carrier injection; no SSH test fails.
The affected test, run and keeper files match public transport `dd10ebbe` exactly. The
[owner diagnostics](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6245543577) and
[controlled reproduction](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6245564208)
preserve the failed result. Baseline passes the three exact cases; delaying real reservation or
admission by 1.1 seconds reproduces all three pre-clock refusals with expired deadlines and zero
carrier calls. Real local drain completes. This demonstrates fixture timing dependence, not the
specific Windows latency source or a new runtime safety defect.

Transport supplied the focused test-only correction `9584f4ae2`, adopted as `9980c80e6` within this
still-private round. The two fault-test functions use a finite 30-second delivery budget; the
parameterized family now also asserts one actual clock-carrier call. Interruption identity, payload
release, reservation and no-start assertions remain, as do real fencing and fresh bounded cleanup.
Production source, deadline policy and global clocks are unchanged. The
[owner handoff](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6245603513) reports
32 module passes and four fault-case passes after each separately delayed real reservation and
admission. Those are owner Linux results, not composed or native Windows clearance. Independent
reviews and affected checks precede the next SSH handoff; the PR remains draft without a new review
edge until a complete green checkpoint. This adoption conveys no clearance for the surrounding
private transport branch or the remaining full-implementation gates.

## Native report and Windows client-family correction

The additional-round checkpoint is exact `1eed1018db7a7cd0c1ba1b648dc4e319418b3611`, tree
`fb27564032f9b4af9a47183bd47aac0340c1861d`. Its
[tester considerations](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6245743833)
precede the review signal at 2026-10-18 04:41:03 UTC. The minimum collection interval ended at
05:41:03 UTC. All ten jobs in
[hosted CI 39097009544](https://github.com/WayfarerLabs/agentworks/actions/runs/39097009544) pass;
Windows reports 1,725 passed, 71 skipped and 25 warnings. The prior failed run remains evidence.

The
[full integration report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6246085333)
uses the same source tree on Linux, macOS and Windows. Its ten corrected fixture cases pass 300 of
300 loaded executions per workstation. The seven owner-marked native retirement cases pass on the
Windows bed. Linux/macOS wait-loss probes retain OBSERVATION and unknown completion, retire local
resources and restore the terminal, without PID operations after the call observing exact loss.
Inherited ignored SIGCHLD again loses status on Linux but not macOS. Those are distinct cells.

The buffered Python helper delivers all 20 environment cases exactly on Linux OpenSSH 9.2p1, macOS
10.2p1, Windows inbox 9.5p2 and native Win32-OpenSSH 10.0p2. Values remain outside SSH argv and
SetEnv. Target exit 255, SIGTERM, SIGKILL and exit zero have distinct helper wait records while the
SSH carrier exits zero; local custody settles. This validates the new stdin path for those clients,
without repairing legacy production callers or adopting the version-specific environment holder
hypothesis. The latter's non-ASCII corruption and MSYS failures are separately recorded on
[#845](https://github.com/WayfarerLabs/agentworks/issues/845#issuecomment-6245832500).

The report is not a complete acceptance pass. Its Linux full suite has four known hidepid-host
failures alongside 17,114 passes and 54 skips. The Windows bed cannot provide an owned console.
Native original-close uncertainty and descriptor reuse were not induced; their coverage remains
injected fixtures. Acquisition/construction and running-interrupt probes settle and restore, but
live cleanup refusal was unreachable because the kernel allowed prompt escalation/reaping. Shutdown
clients were already settled at product return, so those observations do not establish held-resource
shutdown. Final guest/bed teardown confirmation was requested because the report describes cleanup
as in progress. Full terminal, preparation, production creation/publication, trust migration,
factories, both usable RunContext paths and R1-R5 remain open.

The new material finding is MSYS client admission on native Windows. Git's `OpenSSH_10.5p1` passes
the current generic version regex, then mishandles quoted options and truncates the fixed helper
command. Every tested new-stack operation returns UNKNOWN/OBSERVATION. These are native tester
observations; independent source review confirms the acceptance path, not the native measurements. A
post-launch diagnostic cannot safely turn that UNKNOWN into NOT_SENT.

The operator directed the small detect/refuse approach through the authenticated session. The
[critical reading and round start](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6253297714)
records that new direction and removal of the review label before work. The existing version probe
will require `OpenSSH_for_Windows_` on Windows, retain the 8.5 floor and POSIX behavior, and refuse
through existing pre-dispatch outcomes. Command delivery, forwarding and enrollment share that gate.
No quoting workaround, version-specific exclusion, config override, fallback search, shared report
field or legacy patch is selected. The README and operative LLD describe the supported family;
banner recognition does not establish executable authenticity.

Implementation `dc93e821e` is adopted as `2c3c28bd1`. The only production change is the
Windows-family condition inside the existing version-refusal expression. Synthetic successful
clients now identify the supported family on their actual host. Host/banner/floor combinations,
explicit/PATH pinning, non-reflection and pre-dispatch forwarding/enrollment refusal have focused
regressions. The native-only tests retain Windows selection markers.

The developer's full Linux SSH selection passes 562 cases with 12 skips; its focused selection
passes 26 cases with two native-only skips. Scoped lint/format and typing pass. Removing only the
family condition fails the two selected Windows-policy controls while their POSIX counterparts pass.
Initial explicit-path fixture failures and one typing failure are preserved separately from their
corrected passing runs. README spelling initially depends on the root dictionary addition for MSYS;
composition supplies that permanent-doc vocabulary. These are developer Linux results, not native
Windows proof or final composed gates.

This is one focused operator-directed correction and retest after the previous allowance was spent.
Private project, complexity and correctness reviews plus affected local/hosted gates must precede
the next tester-focus comment and checkpoint signal. Transport's later input-completion pin is not
part of this correction or evidence of complete RunContext delivery. No full implementation
checkbox, ready handoff, SDD lock or old-stack deletion follows.

## Input-fixture contribution on the shared completion and writer pins

The operator authorized the two failing SSH fixtures and the additional short-write coverage
correction on 2026-10-18, then directed continued independent SSH work during transport development
and testing. This is one focused contribution, not another blanket public feedback allowance. The
[complete patch handoff](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6255162261)
contains both standalone patch payloads, their original parents, review evidence and native test
focus.

Patch `02e392c53` starts from the frozen shared completion pin
`4dd99cbce2fd4156469405a5da60aee1e5e7cc08`. A consumer that exits 23 while its descendant holds
stdin no longer incurs INPUT solely because offered bytes remain unread. The fixture retains the
two-million-byte offer, exact exit and local status, parent bytes, original return bound and actual
original child, pipe and custody checks before descendant rescue. It additionally requires SENT and
complete output streams. The descendant release/done check remains distinct from containment or
kernel-exit proof.

Patch `a4a7e227b` starts from native-writer pin `ddd17459e64ae64ec92f67014022955ec4caf8ed`. Fault
and short-write injection now target the actual `_process._write_owned_stdin` boundary on Windows
and POSIX. The fault must run exactly once and retain INPUT, unknown dispatch/completion, privacy,
cleanup and one-attempt assertions. Short writes call the original platform writer with at most
seven bytes; multiple positive accepts, exact totals and binary echo prove that the injection ran
without replay. Bypassing `os.write` on Windows can no longer silently remove this test coverage. No
production error normalization, timing relaxation or new runtime path is introduced.

Composition `39b4e3d3552823031e198f17700342f4df423e17`, tree
`241f45e138a9174b649b89b77d926a52f4177102`, applies both patches on the newer writer pin. The full
Linux SSH non-integration selection passes 562 tests with 12 skips. Targeted standalone checks,
Ruff/format, configured typing and whitespace checks pass. All three independent private lanes clear
the exact composition and independently pass the three corrected cases. Hook-bypass controls fail as
intended; a separate injected-pressure check passes with two initial stalls and 450 bytes accepted
through 65 real short writes. These are Linux synthetic-child observations, not native Windows or
installed-client acceptance.

Both complete patches pass an unchanged-worktree applicability check at the later transport head
`a997b5db95126dab58453103e65583c3383cf5b4`. The relevant shared process/custody/carrier and SSH
source are unchanged between those two transport pins. This establishes applicability, not
whole-branch acceptance. The Windows-marked cases still require native execution in the composed
transport validation. The earlier Windows client-family correction separately clears all three
private lanes and the root local gates recorded in its
[local closeout](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6253552962); its
fresh native handoff remains open.

That standalone patch handoff did not adopt shared increments into the older SSH branch or create a
review-requested/ready edge. The later working composition below records adoption separately.
Production terminal and presentation composition, genuine creation/trust publication, both usable
RunContext paths and full R1-R5 acceptance remain open. Completed plan records and the later
retirement sequence are unchanged.

## Current transport composition and independent terminal work

Transport adopted the supplied input fixtures unchanged at
`9011809a70d32535d871b451d7a3e637cc65a107`, using local commits `aeaae39e6` and `9011809a7`. The
complete payload hashes, original authors and session trailers were preserved. These are local
adoption identities, not the original SSH commit objects. The SSH working composition incorporates
that transport head and retains the published SSH artifact head as an ancestor. No public base
change or history rewrite is required.

The initial merge with transport `a3c8e8613` had eight conflicting hunks in five transport-owned
files. Selecting the published owner alternatives reproduced each complete file exactly. A later
file-quality check found a duplicated 55-line retirement section: the two copies were
byte-identical. Removing the second restored the entire transport SDD to its owner-published bytes.
Two independent composition reviews clear the corrected `12630f8be`; they do not review the whole
transport feature. The subsequent `9011809a7` merge is conflict-free. At evaluated joint pin
`ccfbaf6ebdfd32e51c5b56463d5e73a90791f40b`, all production CLI source and the transport SDD match
that owner head exactly.

One additional SSH-owned drain fixture correction distinguishes per-pipe ordering from a global
stream barrier. The original observation counted a valid one-byte stderr EOF probe while stdout
retained pending bytes. The corrected test records the original stdout descriptor, proves a positive
pending prefix and forbids further reads from that descriptor. The other stream may drain. The
query-failure case targets that same descriptor and still requires all 4,096 bytes. Original
deadlines, statuses, control-exception identity, privacy and original process/pipe/PTY cleanup
checks remain. All three independent private lanes clear `ccfbaf6eb`; independent early-read
controls fail all three boundary cases at the revised assertion. No production behavior changed.

The first complete local composition run at `798d14236` (unchanged runtime/tests at `12630f8be`)
reported four failures, 21,844 passes and 78 skips. Two were the SSH drain observations above. One
used a nonstandard virtual-environment location; the normal `cli/.venv` layout passes that case. The
transport metadata fixture assumes a file mode distinct from `0640`; inherited umask `027` already
creates that mode, so its asserted change correctly does not occur. An isolated comparison passes
both setup cases under `022` and preserves the metadata failure under `027`. This is routed to
transport, with no shared fixture or production edit by SSH.

The complete rerun at `ccfbaf6eb`, using normal CLI layout and child umask `022`, reports one
failure, 21,938 passes, 78 skips and 51 fork warnings. All SSH cases pass. The remaining transport
managed-controller fixture fails its peer's initial connection during a 0.15-second admission
window, before sending the wrong handshake or reaching application-entry assertions. That unchanged
node passes alone; no root cause or production correction is established. The
[owner report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6255585869) retains
the exact scope. Full Ruff/format, strict typing across 1,535 sources and canonical file quality
pass. Earlier Rulesync, locked-SDD, Typer isolation and website results remain attributed to their
actual composition pin. Original failed views are retained; the combined head is not green.

Independent SSH terminal work also proceeds under the operator's continuation direction. Private
candidate `e9442d6a8150102656db15a9b410a7645f855231` routes concrete terminal input through the
actual carrier's common admission/version/deadline/custody/report path and existing POSIX relay. Its
three private lanes clear the corrected eight-file unit; the developer and independent correctness
lane each pass 578 SSH cases with 12 skips. Sixteen new cases exercise actual shared terminal
preparation through a synthetic installed client and owned PTYs. Delayed successful preflight and
delayed application-entry controls pass after correcting fixture timing and canonical key
interpretation. The parent observes raw `16 07 CR`, while the restored remote canonical mode
observes `07 LF`. Literal and encoded privacy, exact status/output and retained cleanup remain
checked. These are synthetic Linux observations. The candidate remains separate from public feature
activation. The scoped installed-client observations below do not close macOS/Windows, Windows
owner/keyboard delivery or shared presentation cleanup. Full standup and the later retirement
sequence are unchanged.

## Native Linux terminal component observations

The
[complete private patch handoff](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6255718238)
contains all three mail commits through `e9442d6a8`. Applying the entire sequence onto public
`72a841d830d8b74a99ec27ab64b46b459d25187a` succeeds cleanly and reproduces the native-tested CLI
tree `93efaef5949db5231ca2eae389a9a6fcc9a7fd3d`. Native joint pin
`09dff3511f0f1af39f9499d8c154b7802a854528` includes transport `9011809a7` and the reviewed drain
fixture correction. The patch is component evidence, without public terminal activation or a new
review-requested/ready edge.

Actual preparation and SSHCarrier execution pass on Linux arm64, Python 3.12.13 and installed
OpenSSH client 9.2p1/server 9.2. The lead's positive host case verifies binary source, environment
and argument hashes, 3,740 payload bytes, keyboard input, remote resize observations at 42x113 then
58x144, exact completion/local status zero and literal/encoded privacy. It measures original child
reaping, pipe and owned PTY closure, settled custody and exact borrowed terminal restoration. The
positively identified guest process is observed absent within that same original host execution.

An independent tester's fresh missing-runtime host case passes all 26 measurement gates. It observes
MISSING/prerequisite refusal with zero payload or keyboard release and no application records. The
native selector exits zero: SENT/completion zero/local status zero coexist with Failure.OUTPUT and
incomplete output streams. This records preparation refusal, not application success. Original
process handles are reaped, custody settles, terminal state restores and the exact listener port
accepts an independent bind after cleanup. No application identity or retirement is claimed for this
refusal.

A separate
[native early-input case](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6255774980)
passes all 28 measurement gates at the same joint pin. Fourteen keyboard bytes are positively queued
before the actual client constructor, with runtime UNKNOWN, zero payload and no handoff. The full
queue remains at payload readiness and bootstrap EOF after interactive readiness. The application
then receives those exact bytes, with matching source/environment/argument hashes, complete streams,
zero status, both resizes and measured cleanup. This fixture starts the borrowed terminal raw and
uses `TCSANOW` in the application; the saved remote mode positively excludes canonical
interpretation and relevant input transformations. It does not generalize to canonical processing or
an application's own `TCSAFLUSH`. Keys are sent only once and no production code changes.

All three cases use fresh owned loopback fixtures and isolated owned shell startup, without operator
state, service or account changes. The earlier sandbox PTY ownership failure remains a separate
failed environment observation. These host results do not establish native early-key/fault matrices,
the minimum client version, macOS/Windows, shared presentation cleanup, CLI/RunContext or full R1-R5
acceptance. The complete combined run above still has one transport fixture failure; the owner's
controlled test-only correction is in progress. No implementation checkbox, SDD lock or retirement
step closes on these scoped observations.

## Latest transport increment composition

Joint pin `442c067ccc80409a5b6ed5e372ab4cc3398cc6fb` incorporates published transport
`08b5ce9d1dd0be71bdee7f4df18d9ce4dbd628e5` without conflicts. Production CLI source and the entire
transport SDD exactly match that owner head. Its finite Linux VM DIRECT preparation uses the shared
stage before application dispatch; the existing SSH byte path needs no additional staging protocol.
All three independent integration reviews clear this composition and preserve completed plan
records. Their focused checks include actual passive SSH validation of staged request shapes;
privileged admission in packed fixtures remains injected.

The complete local non-integration selection passes 21,971 tests with 78 skips and 51 fork warnings
in 408.48 seconds. The formerly failing managed-admission case now passes in the full run after the
owner's reviewed peer-publication correction. Ruff, format, strict typing across 1,541 sources,
canonical file quality, Rulesync, locked-SDD and Typer isolation pass. Older failed views remain
preserved. The refreshed private terminal composition `983eb5a38301fdb353be19ad8f642cc0139cebd9`
passes all 578 SSH cases with 12 skips; its SSH production bytes equal reviewed `e9442d6a8`. Native
observations remain attributed to their actual joint pin `09dff3511`.

[Hosted verification](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6255846632)
also clears earlier source `72a841d83`: Linux 3.12/3.13/3.14 each pass 21,707 tests with 308 skips;
Windows 3.13 passes 2,182 with 80 skips. The synthetic Windows client module includes all three
supplied input corrections. Those checks do not prove installed OpenSSH or terminal workflows, and
do not clear fresh hosted verification of this newer transport composition. Full R1-R5 acceptance,
both usable RunContext paths and the existing retirement sequence remain open.
