# SSH Phase 2 Implementation Progress

- Updated: 2026-10-10
- PR: [#832](https://github.com/WayfarerLabs/agentworks/pull/832), draft
- Main/contract base: `cea5e8523aac05edfc3a99a940d7cfb4d71fe32f` (#830)
- State: implementation feedback/fix round 3 is open; full terminal and production acceptance remain
  open

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
