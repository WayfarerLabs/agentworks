# SSH Phase 2 Implementation Progress

- Updated: 2026-10-09
- PR: [#832](https://github.com/WayfarerLabs/agentworks/pull/832), draft
- Initial evaluated code: `e63a1ec4a8e9d99e89d09d6b162e908d3a4b96d6`
- Main/contract base: `cea5e8523aac05edfc3a99a940d7cfb4d71fe32f` (#830)
- State: SSH carrier and private file-custody composition validated locally; terminal and production
  integration remain open

## Implemented scope

The new SSH adapter now has additive passive operator settings, shared isolated client policy,
explicit raw/managed trust, fresh per-operation admission, identity companion validation and owned
local forwarding. Four `config` commands expose complete-policy import, inspection, blocking and
refresh without loading operator configuration or the database. Existing callers remain unchanged.

Enrollment retains one candidate per stable resource creation identity. It verifies a first-contact
acknowledgment with a separate strict connection, supports only strict recovery against the same
active policy generation and leaves publication explicit. The receipt is evidence, not a new
ordinary connection. Production creation provenance and complete-policy publication binding remain
transport's responsibility.

Permanent module/operator guidance, the sample settings, completion mappings and command references
accompany the implementation. No legacy execution module is deleted, no consumer is migrated and no
RunContext accessor is changed by this increment.

## Local evidence at the evaluated code

| Check                                                   | Observed result           |
| ------------------------------------------------------- | ------------------------- |
| `uv run pytest tests/ -m 'not integration'` from `cli/` | 10,417 passed, 12 skipped |
| Ruff check and format                                   | Exit 0                    |
| Full mypy over `agentworks/` and `tests/`               | Exit 0, 894 source files  |
| Typer isolation                                         | Exit 0                    |
| File formatting, Markdown and spelling                  | Exit 0                    |
| Locked-SDD and rulesync checks                          | Exit 0                    |
| Website Python and Node tests                           | Exit 0                    |
| Deterministic double builds at `/` and `/agentworks/`   | Both diffs empty          |
| Previously completed plan records                       | All 19 unchanged          |

The SSH fixtures include synthetic process faults and actual installed OpenSSH against owned
loopback sshd. Forwarding covers binary traffic, requested-listener failures, address families,
trust/authentication/command refusal and cleanup. Enrollment covers actual first-contact writes, CA
and revocation policy, mismatch refusal, retained keys after failed authentication and strict
recovery. These are local Linux fixtures, not the supported workstation/provider matrix.

Fresh-process checks keep legacy execution unavailable during new-stack imports and config loading.
No operator credentials, trust stores, VMs or cloud resources were used in these local checks.
Fixture servers, agents, processes, listeners and temporary policy files are owned and cleaned by
the tests. Native workstation and provider cleanup need their own live observations.

## Private review disposition

Independent project, complexity and generic correctness reviews evaluated the implemented scope.
Their correction reviews are clean at `e63a1ec4`. Material findings corrected before publication of
this record included:

- Ambiguous malformed public identity/sibling fallback could select an unrelated agent key. Direct
  public identities now require no sibling; private identities verify their companion fingerprint.
- Deeply nested persisted JSON now produces typed trust refusal before dispatch.
- Forwarding acknowledgment acceptance no longer depends on pipe read boundaries.
- Ownership is retained before forwarding worker startup; interrupted startup joins the worker or
  reports bounded cleanup uncertainty while preserving the interruption.
- Enrollment flushing preserves control-flow interruption, and permanent command/settings indexes
  describe maintenance refusal and recovery accurately.

The correctness lane reproduced the identity issue with fixture-only sshd/agent credentials. The
forwarding correction was independently reproduced with a normally scheduled worker and a worker
held beyond the cleanup allowance. Mutation tests confirmed the strict enrollment follow-up,
managed-file integrity and forwarding interruption regressions detect removed safeguards.

## Windows CI correction

Hosted Windows CI at `369a1859` exposed false stable-file snapshot refusals and an enrollment test
that expected native path spelling in OpenSSH options. Code
`ee6301679a5cb61309937b6114a12785d1823d0b` corrects both. CPython 3.13.15's
[pathname query](https://github.com/python/cpython/blob/v3.13.15/Modules/posixmodule.c#L2073) and
[descriptor query](https://github.com/python/cpython/blob/v3.13.15/Python/fileutils.c#L1035) give
`ctime` different meanings on Windows. The correction keeps complete descriptor before/after
metadata checks, path device/inode/size/mtime checks and POSIX path change-time equality.

All three independent correction reviews are clean. Mutation experiments show the regressions catch
the original mismatch and removal of descriptor or pathname protection. The corrected full local
suite passed **10,433 tests with 12 skips**; full mypy and Ruff passed. All
[hosted checks](https://github.com/WayfarerLabs/agentworks/actions/runs/35468781056) passed,
including Python 3.12/3.13/3.14 and Windows Python 3.13 (**461 passed, 25 skipped**). This
establishes the correction on Windows CI; it does not supply terminal, provider or
production-composition evidence.

## Shared subprocess adoption

Integration code `1b2c683293d5cddd8390b59146fd7fe3d03bf808` rebases the SSH work onto transport
`e85e9f5c4752ae926315fa7c0e69b871de42e4fc`. SSH now uses the shared finite-input pump and cleanup,
retaining its Windows environment filter, carrier stdout and mixed stderr provenance. The
independence fixture imports `Command` from the canonical invocation models module.

The combined execution suite passed **613 tests with 5 skips**, and the full local non-integration
suite passed **10,581 tests with 12 skips**. Ruff and mypy passed, covering 939 Python files and 902
typed source files respectively. File lint, locked-SDD, rulesync and typer-isolation checks passed;
website checks passed 160 Python and 103 Node tests, and both site-base double builds were
identical. These local results do not establish native Windows/macOS or full production integration
acceptance.

## Real SSH file-helper delivery

Test revision `9375234d`, integrated with transport `806741ca`, adds two real OpenSSH cases using
transport's current fixed helper bundles and result decoding. Both passed in the implementation
worktree and an independent lead repeat. The lead repeat used the local host because its sandboxed
fixture could not start sshd and skipped both cases; that skipped run supplies no acceptance
evidence. The passing bed was Linux 6.1.0-52-arm64 with OpenSSH 9.2p1 Debian-2+deb12u10, project
Python 3.12.13 and guest-helper `/usr/bin/python3` 3.11.2. Every case used fresh loopback keys/trust
and the fixture account's direct identity, without operator configuration, remote infrastructure or
elevation.

The read case proves exact binary data and SHA-256 plus distinct typed absence and size-limit
refusal. The transfer case proves stage creation, two exact-offset/digest chunks, reading through
the helper, receipt reconciliation and typed cleanup of the exact scratch artifact. Each of the nine
operations uses one carrier call. Raw carrier reports declare delivered retention and contain no
output bytes; payload canaries are absent from invocation and result representations. The stage root
is empty following cleanup. The lead independently found no process referring to the owned fixture
directory, then removed and verified absence of that directory, including credentials and read
fixtures.

Project, complexity and correctness reviews are clean at `6cc50745`; the latter two also reran both
cases successfully. Full Ruff/format and mypy (1,005 source files), file lint, locked-SDD and
whitespace checks pass for this test/documentation increment. Earlier full-suite and website results
remain pinned to their integrated revisions; hosted CI records the new published head separately.

These cases supply SSH boundary evidence for the private read/staging helpers. They do not close
production FileAccess, malformed-response and interruption coverage, publication, database
coordination, elevation, native workstation/provider or full file-only workflow acceptance.

## Real snapshot helper delivery

SSH integration `c14edf65` adds the actual fixed snapshot helper over installed OpenSSH on transport
`5b570442`. The host fixture passes all three read, stage and snapshot cases. The snapshot case
downloads binary data in two chunks, including a nonzero offset, verifies per-chunk and complete
digests, recovers cleanup-only ownership and removes the exact random-token scratch object. All five
snapshot operations use one carrier call each, sensitive finite input and delivered outputs with no
retained bytes. Source contents, identity, mode, size and modification/change times remain
unchanged.

The fixture independently requires a root-owned mode-1777 `/tmp` before dispatch; an owner-remapped
sandbox skips this happy-path test rather than overriding helper policy or treating a helper refusal
as a skip. Failure cleanup requires observed successful remote completion of every preceding helper
attempt. Unknown completion retains and reports the exact owned path; reconciliation alone does not
prove remote quiescence. The lead's host repetition found no owned fixture processes or new snapshot
paths afterward and removed the temporary fixture directory and credentials.

Project, complexity and correctness reviews are clean at `32eefc50`. Review removed redundant chunk
bookkeeping and added a successful-completion check after fallback reconciliation as well as before
it. A focused in-memory fault experiment returned typed recovered ownership with unknown raw SSH
completion: the test reported the retained exact path and made zero cleanup calls. This fault
experiment is control-flow evidence, not a live SSH interruption result. The corrected happy path
passed on the host; final Ruff/format, full mypy and whitespace checks pass.

This is Linux loopback proof for private snapshot exchanges. Public download composition,
publication, interruption and malformed-response acceptance, production FileAccess, and supported
workstation/provider workflows remain open.

## Held-owner dependency and runtime admission

SSH integration `e852aef2` incorporates transport `bb7ebb58588b3c2a6ea8eb09d0e297e06d8a3ddd`, the
reviewed milestone with successful hosted Linux and Windows validation in
[run 35617821328](https://github.com/WayfarerLabs/agentworks/actions/runs/35617821328). This brings
the private `LocalProcessOwner`, runtime selection and fixed stdin helper framing into the SSH
combination. The newer transport draft remains separately under validation; its current Windows
collection correction does not change the owner being adopted.

The SSH file fixtures now select Linux `/usr/bin/python3` explicitly and require positive runtime
admission before accessing a helper observation. Two transport command-size fixtures retain their
current bootstrap/prefix shape with SSH's explicit trust selection and native absolute paths. The
size/protocol selection passes 83 cases, and the lead's host repetition passes all three installed
OpenSSH read, stage and snapshot cases. Fixture processes are absent afterward; the exact temporary
directory and credentials are removed. These results cover private helper delivery and preserve the
existing unknown-completion cleanup restriction, not production FileAccess or native-platform
acceptance.

The combined baseline at `e852aef2` passes **12,605 non-integration tests with 14 skips**. Full
Ruff/format and mypy (1,059 source files), rulesync and typer-isolation checks pass. Website checks
pass 160 Python and 103 Node tests, with identical double builds at both site bases. Forwarding
adoption has separate implementation and validation below; these baseline counts do not claim to
cover it.

The operator authorized a scoped SSH contribution to the shared held-process extraction on
2026-09-21. Transport had already published that extraction, so forwarding adopts its existing
owner. Any necessary shared correction stays separable; terminal and RunContext work stay with
transport. No new public carrier contract or second process owner is introduced.

## Forwarding uses the shared held-process owner

SSH runtime `140f6758` adopts transport's existing `LocalProcessOwner`; no shared process code
changes are required. The owner is retained before startup, and the pipe drain worker remains inert
until shared startup returns successfully. Closing fences every pipe operation, serializes owner
settlement and observes worker termination. A delayed worker can make termination uncertain, but
cannot skip shared cleanup or resume pipe use. Natural client exit remains distinct from a status
produced by local cleanup.

At `140f6758`, the initial forwarding selection passes 48 non-integration cases, the broader SSH
selection passes 265 with five skips, and all 20 shared-owner tests pass. Private project review
then caught an unsupported concurrent wait/close test claim; `6c53edcb` adds the actual two-thread
regression. Complexity review removes duplicate terminal/worker state, redundant owner-fact
assertions and unused stdin configuration in `8c3c4e5b`.

The corrected forwarding selection passes **49 cases** at `8c3c4e5b`, plus all 20 shared-owner
cases. Coverage includes worker-start interruption before and after native startup, interruption
after process admission with no pipe reads, interruption before pipe publication, repeated close
interruptions preserving the first control exception, concurrent wait/close, natural exit with held
stdin, safe startup failure reporting and delayed worker termination. Full Ruff/format and mypy
(1,059 source files) pass.

The lead repeated all eight installed-OpenSSH forwarding cases at both `140f6758` and `8c3c4e5b`;
all passed. They exercise real binary forwarding, first/later listener refusal, IPv4/IPv6 partial
setup, authentication/trust/command refusal and listener release. The fixture verifies rebinding;
the lead independently found no owned fixture SSH processes and removed each exact directory and its
credentials.

All three private review lanes are clean at `8c3c4e5b`. The full combined non-integration suite
passes **12,613 tests with 14 skips**. Full Ruff/format, mypy (1,059 source files), file lint,
locked-SDD and whitespace checks pass. The earlier baseline records the unchanged website, rulesync
and typer-isolation gate results. Hosted CI records the published branch separately and can also
include later transport base commits.

These are focused Linux loopback results, not complete asynchronous interruption or native-platform
acceptance. The shared cleanup-entry gap and unbounded process-construction time retain their
existing qualifications. Terminal and RunContext integration remain with transport.

## Transport rebase and external managed-process evidence

SSH `ce0d8ca0` rebases cleanly onto transport `126c12a4`. The SSH runtime and test files are
unchanged from published SSH `f6af80cc`. Transport supplies the Windows collection correction and a
private managed-process candidate; shared terminal endpoints and the additive RunContext accessors
are still absent. The combined non-integration suite passes **12,700 tests with 14 skips** on the
lead's Linux workstation.

The integration tester's
[complete native report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5767397749)
covers a separately composed tree `7101ecfc`, using transport `126c12a4` and SSH `f6af80cc`. It
reports identity, descendant cleanup, binary delivery, exit propagation, boundary integrity and
honest interrupted observation through SSH on Debian 12 and 13, plus QGA on Debian 12. These are
Linux-workstation observations for the private managed-process candidate, not complete SSH standup
acceptance. Windows/macOS workstations, terminal mode and native asymmetric stream failure are not
covered; Proxmox 9 was unavailable due to capacity.

Two material findings remain transport-owned: the candidate's completion predicate depends on an
orthogonal input failure, and failed transient units are retained after startup failure. Transport's
[round disposition](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5768591053)
accepts both, confines its proposed fixes to the managed-process candidate and leaves shared process
and SSH behavior unchanged. Neither finding authorizes an SSH contract change or closes an
acceptance item here. The subsequent fixed head needs its own complete test report.

Hosted [run 35665206011](https://github.com/WayfarerLabs/agentworks/actions/runs/35665206011) passes
every check for published rebase `191031231`, including Linux Python 3.12/3.13/3.14, Windows Python
3.13 and the aggregate gate. Full local Ruff/format, mypy (1,066 sources), file lint and locked-SDD
checks also pass. This resolves the earlier inherited Windows collection failure; it does not supply
native Windows SSH evidence.

## Owned upload delivery over SSH

Test `eea4ba2ba` composes the actual private `FileOperation` with a temporary database owner,
installed OpenSSH, fixture-owned keys/server and production fixed helper bundles. It creates binary
content, replaces it using the returned revision, then refuses duplicate creation and a stale
revision through typed conflict errors. Content, digest and stat checks prove successful content
delivery and destination preservation on refusal. Sensitive finite input, empty retained carrier
output and canary-free representations are checked at the actual carrier boundary.

Each completed call clears active/unfinished upload custody and its exact scratch object. The
workflow finishes with only the destination in its directory and no database ownership record. The
fixture retains unresolved custody and reports the exact scratch path if settlement fails; this
happy-path/refusal proof does not inject unknown remote completion or certify recovery.

The implementation run passes in 4.66 seconds and the lead's independent repeat in 4.84 seconds.
Targeted Ruff/format and strict mypy pass. The lead independently verifies no owned SSH processes or
scratch objects remain, then removes the exact fixture directory and credentials. Complexity review
also observes a passing unmodified run and proves that corrupted finite input fails the digest
assertion. Its optional redundant final hash assertion is removed after an independently passing
deletion experiment.

Project and complexity reviews are clean at `eea4ba2ba`; project review independently repeats the
live workflow in 5.96 seconds. All recorded fixture directories and generated credentials are
removed, with no owned SSH processes or scratch remaining. The test-only increment changes no
runtime behavior; earlier runtime correctness reviews retain their original pins.

This is Linux loopback evidence for private upload/publication composition. It neither exposes
production FileAccess nor closes terminal, RunContext, creation binding, full file-workflow or
native Windows/macOS acceptance.

## Explicit whole-operation resolution

Transport checkpoint `c4d9e54d667a0c9bdb0915f21a828e02e8eec77e` separates child-attempt settlement
from whole-operation resolution. SSH rebases cleanly onto it at `58292ba3`. The installed-OpenSSH
upload proof reproduces refusal at `owner.close()` after all four uploads finish: child settlement
alone no longer releases the durable claim.

The proof now records whole-operation effects resolved only after verifying all four outcomes, empty
active/unfinished custody, no cleanup debt, no pending remote effects or coordination uncertainty,
and absence of every exact scratch object. These uploads are the entire operation; there is no
activation, route or power hold to settle. A failing assertion closes the fixture database without
asserting resolution or automatically releasing the claim. SSH runtime code is unchanged, and
production orchestration remains transport-owned.

The adapted proof passes over installed Linux OpenSSH in 4.32 seconds. Independent cleanup
inspection finds no matching fixture process and only the intended destination in each upload
directory; both before/after fixture directories and credentials are removed. This is local
private-composition evidence, not production lifecycle or native Windows/macOS acceptance.

Project, complexity and independent correctness/security reviews are clean at `c55c1aa16`. The
correctness reviewer independently repeats the installed-OpenSSH proof in 4.95 seconds and verifies
its fixture process, scratch and credential cleanup. The combined non-integration suite passes
**12,945 tests with 14 skips**. Full Ruff/format, mypy (1,093 sources), file lint, locked-SDD,
Rulesync and typer-isolation checks pass. Website checks pass 160 Python and 103 Node tests, with
identical deterministic double builds for both site bases. Subsequent documentation formatting and
this validation record do not change the reviewed CLI tree.

The earlier managed-process native report below retains its measured tree and scope; it does not
certify the newer platform identity, creation marker, FileAccess or ownership composition. Terminal,
additive RunContext/platform orchestration, production creation/publication binding and
supported-platform acceptance remain open. This dependency adaptation consumes no public feedback
round and leaves the PR draft without a checkpoint or ready signal.

### Windows forwarding fixture correction

Hosted [run 35696932549](https://github.com/WayfarerLabs/agentworks/actions/runs/35696932549) passes
Linux Python 3.12/3.13/3.14 and the static/repository/website gates, but Windows Python 3.13 fails
the synthetic partial-listener test at its final rebind with WinError 10048. Exact child-exit and
pipe-closure assertions already passed. The log does not distinguish another port owner from
socket-closing latency, both permitted by Microsoft's
[Windows socket error definition](https://learn.microsoft.com/en-us/windows/win32/winsock/windows-sockets-error-codes-2).
It does not establish a production process leak.

The fixture previously released a parent-selected ephemeral port before the child bound it and
accepted any forwarding error, so it did not prove the partial listener ever existed. The child now
binds port zero, listens, and writes its selected port to an owned sidecar before emitting the
invalid readiness marker. The test requires that receipt, the precise invalid-response failure and
exact child/pipe cleanup. Default socket rebinding retries only address-in-use for at most one
second; other errors fail immediately, and a persistent holder still fails. No address-reuse option
or production cleanup relaxation is introduced. Native Windows acceptance still requires its own
evidence; the corrected hosted test must pass before this increment is considered green.

Project, complexity and correctness/security reviews are clean at `c1b7ebaa1`. The 49 focused
forwarding tests pass, and the independent correctness reviewer repeats the corrected case in 0.79
seconds with its scratch directory removed. The combined non-integration suite passes **12,945 tests
with 14 skips** in 185.60 seconds. Ruff/format, mypy (1,093 sources), file lint and locked-SDD
checks pass. This test-only follow-up leaves runtime code and the previously validated Rulesync,
typer and website surfaces unchanged; hosted validation records the new head separately.

Hosted [run 35698219630](https://github.com/WayfarerLabs/agentworks/actions/runs/35698219630)
subsequently passes every required check at `3cf322f93`, including the corrected Windows fixture.
This closes that hosted-test gate without identifying the earlier address-in-use cause or proving
native installed-OpenSSH acceptance.

## Joint Windows/WSL2 mechanism proof

The
[complete live report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5774494551)
combines transport `f13f48e55c632d33c405de9210ff658ddc627612` with SSH
`3cf322f93a35f1352fdc6abcb9a7bfc3a9a8e9a7` at composed head
`3dde458aaf0715feea05ab3b01a191601eac2674`. SSH rebases cleanly onto that transport checkpoint at
`8a9ad5f7142b697c566acd55648d466742da5392`: all 68 carried commits are patch-equivalent and the CLI
tree exactly matches the tester's `11c7413e2f58c77f253f9e0fc86a20cb0a9ade72`. The subsequent SSH
evidence and dependency-pin updates change only these SDD records.

The workstation is Windows Server 2022 build `10.0.20348.5622`, with Python 3.13.15. The platform is
WSL2 2.7.14.0 with kernel 6.18.33.2-2, Ubuntu 24.04, systemd 255 and guest `/usr/bin/python3`, bound
to distribution `Ubuntu` and user `root`. Real buffered WSL delivery preserves all 13 literal argv
vectors, binary streams, separate sensitive finite stdin and EOF, bounded retained output and
pre-dispatch refusal. Measured exit/signal collisions confirm the conservative completion policy:
only zero produces typed completion; nonzero local status remains observation evidence.

The tester composes the native Windows client owner and guest-anchor owner against real `wsl.exe`.
An independent observer confirms the nonce-bound exact guest PID/start-time identity before and
after ordinary release. Abruptly killing only the controller, without a process-tree kill or
settlement, also removes its WSL client and the exact guest anchor. Unrelated guest and Windows work
survives. Repeated startup is refused and repeated release is idempotent. Independent process, guest
and distribution observations find no anchor residue; the tester removes its artifacts and
deallocates its bed. Distribution lifetime is recorded separately from anchor ownership.

The composed gates pass **12,979 non-integration tests with 21 skips**, Ruff/format, mypy (1,098
sources), file lint, locked-SDD and Rulesync checks, website Python and Node tests, and
deterministic double builds for both site bases. These are the tester's results on the identical CLI
tree; the rebase itself is a source-equivalence check. Earlier SSH private reviews retain their
recorded pins, and transport reports clean project, complexity and correctness/security reviews at
its checkpoint. No SSH runtime or fixture correction is required by this report.

This establishes the WSL mechanism on the named Windows workstation and guest. SSH and QGA live
cells were not repeated, macOS remains uncovered, and the report does not prove Windows OpenSSH,
terminal restoration, production factories, RunContext accessors, platform holds, permissions or
exact recovery wiring. The full Phase 2 acceptance gates remain open. This dependency adoption
consumes no SSH public feedback/fix round and leaves #832 draft without a checkpoint or ready
signal.

## Lifecycle-ledger adoption

Transport checkpoint `a51a28ff157796827aa3d862cf41039d7440275f` adds durable lifecycle obligations
and requires their ledger to be sealed before whole-operation resolution. SSH rebases cleanly at
`a57db43df97a533be80a65dca2dbc97689ec171e`; all 69 carried commits remain patch-equivalent.
Transport records clean project, complexity and correctness reviews of its private ledger at
`3f06c91c`. Its production orchestration and recovery takeover remain separate work.

The unchanged real-SSH upload proof reproduces the new sealing refusal after all four upload cases
and their cleanup assertions pass. It now calls `seal_lifecycle_obligations()` immediately before
`record_effects_resolved()`, after confirming the complete operation has no pending transfer,
scratch object, cleanup debt or uncertain custody. No activation, route or platform hold belongs to
this fixture. The adapted proof passes over installed Linux OpenSSH in 4.28 seconds. This changes
only the SSH test's final ownership sequence; the shared ledger and all runtime code remain
transport-owned.

The combined non-integration suite passes: **13,037 passed, 22 skipped** in 195.07 seconds. Ruff
checks and formatting pass, as does mypy across 1,101 sources. File lint, typer isolation,
locked-SDD and Rulesync checks pass. Website validation passes all 160 Python and 103 Node tests,
with identical double builds at both site bases. Independent fixture inspection finds no owned SSH
daemon or transfer scratch residue: the refused baseline retains one claim and four obligations,
while the adapted proof leaves no owner, claim or obligation rows. Both exact fixture directories
are removed.

Independent project, complexity and correctness reviews are clean at
`2b23952be7310e864702bb231b70a8f9c4215ab1`. The correctness lane reruns the real SSH proof: **1
passed in 4.26 seconds**, with host-visible daemon absence, zero owner/claim/obligation rows, only
the intended destination and no scratch residue. Its exact fixture and generated credentials are
removed. These are scoped adaptation reviews, not full-PR approval.

Transport's [hosted CI run](https://github.com/WayfarerLabs/agentworks/actions/runs/35725994550)
fails an existing subprocess stream-order assertion on Python 3.14 and an authenticated Proxmox
request test on Windows. The
[SSH coordination report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5776442982)
routes both observed failures to transport without assuming their cause. Local success does not
resolve those failures; hosted validation of the updated SSH head remains pending.

The prior Windows/WSL2 report applies to its recorded CLI tree, not this newer composition. Terminal
delivery, production RunContext/platform composition, creation/publication binding, recovery and the
remaining native acceptance gates stay open. This dependency adaptation consumes no public
feedback/fix round and keeps #832 draft without a checkpoint or ready signal.

## Private access and paired-plan dependency update

Transport's
[DIRECT access checkpoint](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5810118686)
at `b1ca9c8123d1991a9cbf64790f2edb072516e3cd` adds private, owned foreground access. Its
[paired-plan checkpoint](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5810504915)
at `bdaffc1388fff85ac05683f83648125faf2d7aea` prepares ordinary and optional elevated identity plans
under one owner and deadline. Neither checkpoint adds a public target, SSH carrier ABI, production
factory, terminal endpoint or RunContext binding; there is no SSH adapter change to make yet.
MANAGED jobs, permission enforcement and live destination elevation remain open in transport.

All 75 SSH commits replay cleanly onto both checkpoints. The stable aggregate SSH patch ID is
unchanged at `5dbf5a2ad18ff1cd39a88a44e9e77222dbcb3671` from the previously reviewed
`c92336e9`-based head through the new `bdaffc13`-based head `12fe3cab8`. The combined tree on
`b1ca9c81` passes **13,168 non-integration tests with 22 skips**. An earlier attempt lost its report
to shared-host disk exhaustion; another had one unrelated two-second Git-credential reconciliation
timeout, which passed immediately in isolation. The complete isolated rerun is green. Ruff, format,
mypy across 1,108 sources, file lint, locked-SDD and Rulesync checks pass there.

On `bdaffc13`, the changed target-plan and SSH selection passes **343 tests with 5 skips**; 23
integration-marked cases are deselected. Ruff and format pass across 1,145 files, and mypy passes
across 1,108 sources. The owned Linux loopback upload proof passes in 4.29 seconds, with no fixture
sshd remaining; the exact temporary credentials and test directories were removed. This preserves
the private file-delivery evidence, not a claim that the new production target or recovery route
exists. Hosted SSH validation is recorded separately from these local results. #832 remains draft
without a checkpoint or ready signal, and no public feedback/fix round is consumed.

## Recovery-dispatch dependency update

Transport's
[recovery checkpoint](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5809553811)
publishes `c92336e96e6a682888dc7b9cb7e1802ab65135d0`. It adds restricted recovery dispatch and local
DOWNLOAD snapshot reconciliation behind obligation-wide drain evidence. It does not supply an SSH
recovery adapter, production operation root, target composition or RunContext binding. The SSH
upload proof still acquires ordinary ownership through `OperationOwner.acquire()` and does not use
the new recovery path.

SSH rebases cleanly from transport `e27a466f38d9a257a6c20f4ced4308163851657a` to this checkpoint at
`9b6a25fc1a74ad77a70ab6ce36aeb9d78596501d`. All 73 carried commits replay without conflict, and the
stable aggregate patch ID is unchanged at `867f18e9c54303a2b38818f8af1b2ad2e54f7f8a`. The combined
SSH and recovery selection passes **332 tests with 5 skips**. Transport's hosted checks pass at this
checkpoint in
[CI run 35968419961](https://github.com/WayfarerLabs/agentworks/actions/runs/35968419961), including
Windows, website and the aggregate gate.

The rebased SSH tree passes **13,136 non-integration tests with 22 skips** in 225.35 seconds, Ruff
and formatting across 1,143 files, mypy across 1,106 sources, file lint, locked-SDD and Rulesync
checks. Its owned Linux loopback upload proof passes in 4.45 seconds; the fixture database has zero
owner, claim and obligation rows after release, and host-visible inspection finds no sshd tied to
the fixture. The exact generated test directories and credentials were removed after verification.
Website validation also passes 160 Python and 103 Node tests, with identical double builds at `/`
and `/agentworks/`. Independent project, complexity and correctness reviews are clean at
`d6a998ca5a3f0305f3f97f3e1ae8d9727d6622d0`; they found no integration regression or new SSH
abstraction. Hosted SSH checks and full native integration are still pending. This dependency update
adds no SSH recovery or native-platform acceptance claim; #832 remains draft.

## Durable file-call custody adoption

Transport's
[file-custody checkpoint](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5777873573)
publishes implementation `693e9e48cbe5edc4032e4e5fbcaaf619d40efae4` and evidence head
`e27a466f38d9a257a6c20f4ced4308163851657a` after clean private project, complexity and correctness
reviews. SSH rebases cleanly at `16bfa95c8884add45994926524bbd8c4c84558cc`; all 71 carried commits
remain patch-equivalent.

The existing real upload proof now supplies the shared test helper's managed-target identity to
`FileOperation`. Its resource kind and name match the operation scope; its incarnation and boot
values are synthetic fixture data. This exercises private composition, not production creation
provenance or a real VM incarnation fence. The foreground SSH server remains an owned Linux loopback
fixture.

After create, matched replacement, duplicate-create refusal and stale-revision refusal, the proof
inspects the durable ledger before sealing and release. It requires exactly four resolved
`file-call` obligations with the expected target, confined path and upload tokens, and checks that
the file-content canaries are absent from their persisted payloads. Existing binary integrity,
metadata, typed conflict, sensitive delivery and exact scratch/custody assertions remain in place.
The adapted proof passes over installed OpenSSH: **1 passed in 4.37 seconds**.

Independent project, complexity and correctness reviews are clean at
`4f747f1fef98bbad5b0b3fb4075f8868df36ddc0`. The correctness lane independently repeats the real
proof: **1 passed in 4.33 seconds**. Both runs leave no host-visible fixture daemon, no scratch and
zero operation owner, claim or obligation rows; only the intended destination remains before exact
fixture teardown removes the generated keys and files. These reviews cover this adaptation only.

Combined validation passes **13,093 non-integration tests with 22 skips** in 183.18 seconds. Ruff
checks and formatting across 1,141 files, mypy across 1,104 sources, typer isolation, file lint,
locked-SDD and Rulesync checks all pass. Website validation passes 160 Python and 103 Node tests,
with identical double builds at both site bases. Hosted validation of the updated SSH head remains
pending.

This clean-transfer proof does not establish process-loss recovery, retained-debt handoff under
faults, production recovery takeover, activation/publication binding or native platform acceptance.
Those gates remain open alongside terminal and production RunContext composition. Earlier native
reports retain their measured compositions. This dependency adaptation consumes no public
feedback/fix round and keeps #832 draft without a checkpoint or ready signal.

## Managed-process fix integration

Transport's
[round-1 handoff](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5768952091)
publishes `ae1ce293` after clean private reviews and hosted validation. It removes the candidate's
post-wait helper-failure veto from terminal/lifecycle completion and adds fixed transient-unit
collection. It changes neither the shared process pump nor SSH interfaces. The requested second
native round retests both prior material findings, including systemd 252 and the previously
unavailable Proxmox 9 cell; green local checks alone do not close them.

Local SSH rebase `3c7ede4d64c4034d87dca88aa1e42bbc00b55284` has no conflicts or SSH-source edits.
Its full tree `5857ccbc0f66e8591b56cba8bf78282275757ced` exactly equals the requested composition of
transport `ae1ce293` and published SSH `b57b45df1`; the CLI tree is
`77a52a47fb94ae74eb7106f41b156eed8b2e7bea`. This proves source equivalence, not native acceptance.
The published SSH pin stayed unchanged until the complete native report arrived.

The combined non-integration suite passes **12,707 tests with 14 skips**. Full Ruff/format, mypy
(1,067 sources), file lint, locked-SDD, rulesync and typer-isolation checks pass. Website Python and
Node suites and deterministic double builds for both site bases pass. Earlier SSH runtime reviews
and live evidence retain their recorded pins; this dependency rebase introduces no SSH behavior.

The
[complete second native report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5769116518)
uses composed head `73cdc437013c503bb9b893c6447896c895b06a4b` with the identical CLI tree above. It
verifies both fixes through SSH on Debian 12/systemd 252 and Debian 13/systemd 257, plus QGA on
Proxmox 8.4.21 and 9.2.11 with the corresponding guests. Immediate-exit, delayed-exit and partial
reader cases retain stable lifecycle completion and honest input-failure facts. Four repeated
invalid-identity starts per cell leave no failed unit or cgroup, while successful and nonzero exits
retain wait, stream and boundary evidence.

The tester also repeats identity/group selection, child-cgroup placement, detached-descendant
cleanup, binary stream fidelity, forgery resistance, honest observation interruption without replay,
and absence of staging. The report verifies teardown at provider, unit, cgroup, process and scratch
layers. Its composed gates pass, including **12,708 non-integration tests with 13 skips** on the
tester's workstation. These measured results resolve the two reported candidate defects and the
previously missing Proxmox 9 cell; they do not turn lifecycle completion into overall success.

The workstation axis is still Linux-only. Native asymmetric stream failure and `waitpid` fault
injection remain unmeasured. Terminal delivery, RunContext composition, production creation binding
and full supported-platform SSH acceptance remain open. Publishing this dependency update does not
consume an SSH public feedback/fix round or authorize a ready signal.

## Structural preflight integration

Transport #833 at `f937cac098c43e81e9bea2e5daa6f9520ae4ca96` adds the shared pure `Carrier.validate`
protocol and calls it during private managed-start preparation before durable `possible-dispatch`.
It includes the SSH-owner-authored buffered validator as cherry-pick `10fb0f0fa`; that version
refuses live input and sink output because its carrier cannot execute them. SSH #832 rebases onto
this head at code revision `98fd5f349b917a28d95253849ecfdd66aee636ee`. The final SSH validator
accepts all current shared I/O shapes without effects, and `execute` calls it before trust
admission, client probing or process work. Runtime source/sink faults still report `Failure.INPUT`
and `Failure.OUTPUT`.

On this combined tree, the SSH and managed-start selections pass **323 tests with six skips**. Full
Ruff check/format covers 1,171 files, mypy passes 1,130 sources, and file lint and locked-SDD checks
pass. These local tests include structural refusal sequencing and shared managed-start unit
coverage; they do not execute a managed service through an installed SSH client. #833's exact-head
hosted matrix is still running at this checkpoint. Terminal delivery, production RunContext and
native binding, genuine creation/publication integration, shared cleanup interruption and full
supported-platform acceptance remain open. This dependency integration is not an SSH public feedback
round or a ready signal.

## WSL hold-probe composition checkpoint

The
[complete Tier 2 report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5822948740)
locally composes transport `333b17bc84f03588aebdf9bb0cb8201de80462c8` with SSH
`922893b4d492c622ff1cfcaa22f656c0708e4d2a` at merge head `29f3979e` and CLI tree `aa5d3de1`. The
combined non-integration suite passes **13,746 tests with 24 skips**; Ruff check/format and mypy
across 1,149 sources pass. The transport-only tree also passes its full local pipeline. This is a
tester-built composition, not a rebase or native SSH acceptance at a new #832 head; the WSL probe is
byte-identical in both trees.

The official Windows/WSL2 probe result is `UNKNOWN`: its cold-open deadline expires before the
measured refusal, and its selected Ubuntu/Debian path is a symlink that the UNC share cannot open. A
separately labeled, one-line path correction let the probe's existing retention cases run and showed
a single handle retaining an already-running distro for 180 seconds, with release after close or
holder death and no unrelated-distro impact. Transport owns those probe corrections and the
still-unproved strict dispatch-drain gate. The report reran no native SSH or Proxmox QGA cells; it
does not close SSH terminal, platform binding, RunContext or supported-workstation acceptance.

## Native managed lifecycle on the composed SSH branch

The
[full exact-head transport report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5825224430)
composes transport `7d0746f5a3f0b12eeabbcadfdc094d97028f4d5a` with SSH
`5f4693aa71c6e6e987c1f36fe3115c980b46e425` at tester merge head `a9c141d7`. On a Linux workstation,
its private managed-lifecycle driver ran over the composed SSH carrier against disposable GCE Debian
Trixie/systemd 257 and Bookworm/systemd 252 guests. The transport branch's standalone buffered SSH
carrier rejects the required `SinkOutput`; the #832 carrier accepts the shared sink mode. The tester
drove the private execution side-car directly from Python, not through `agw vm create` or a
production RunContext composer.

Both SSH guests returned receipt-confirmed starts with exact workload identity, environment, stdin
and cgroup placement. An exit-7 payload produced WAIT, both stream ends and an empty descendant
boundary with byte-matched output. Stop and disposal, including a second idempotent disposal and
refusal to observe afterward, behaved as designed; same-record replay was refused. A TERM-trapping
payload reported its exit and output after stop. Independent residue checks found inactive units, no
failed units or remaining cgroups/helpers, and only disposal receipts. The tester removed both GCE
beds and their firewall rules. This is new native Linux SSH evidence for the private managed
mechanism, not proof of the absent production composer, creation/provisioning binding or complete
SSH-backed workflow.

The report's exact #833 head passes its full non-integration suite (**13,504 passed, 23 skipped**),
static/docs/website gates and 14 hosted checks. Those gates are for transport's exact head; the
report does not claim a full combined-suite run at `a9c141d7`. Its WSL2 cells use #833 without SSH
code. At that head they find that WSL's kernel `boot_id` can survive distribution power-off and
restart, leaving a transport-owned managed-run fence unresolved. The Linux SSH cells do not
establish native macOS/Windows SSH acceptance, public RunContext use, terminal delivery or recovery
across that WSL power boundary. Those Phase 2 gates remain open.

## Derived guest boot-fence proof on composed SSH

The
[complete round-7 report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5825746788)
tests transport `e7568a22c6a558f0b6def316c1ed4edf91bac1ba` with SSH
`e02704cfe28dea0d24fb088f12053d93642f4911` in a clean local composition. On a Linux workstation, the
private guest-identity probe ran over real SSH against disposable GCE Debian Trixie/systemd 257 and
Bookworm/systemd 252 guests. Independent reads matched the probe's instance marker, kernel boot UUID
and PID 1 start ticks. The derived managed boot UUID remained stable across repeat probes and PID 1
re-exec, then changed after a real reboot on both guests. The GCE fixtures accepted host keys on
first contact because the images did not publish them out of band; this does not prove the SSH trust
migration or strict pre-enrolled host-key path.

The WSL2 cells used transport's exact head without #832 code. The derived UUID stayed stable within
one running distribution and changed after `wsl --terminate`, natural idle stop and restart, and
utility-VM shutdown; a concurrent clone sharing the instance marker had a distinct fence. Managed
complete and TERM-trapping stop regressions still passed. Lost-hold recovery could not run because
that path does not exist at this head, and no production target composer or public RunContext path
was exercised. Native macOS/Windows SSH, terminal delivery and complete production workflows also
remain unproved.

Transport's exact-head local gates pass **13,507 non-integration tests with 23 skips**, but hosted
Website and aggregate `ci-success` failed when Chromium did not publish its DevTools endpoint; this
is not a green transport CI result. The report also finds that databases built by earlier unreleased
branch migrations 39 to 41 can open under the consolidated schema without the expected tables.
Transport owns that disposition. Neither finding is an SSH carrier defect, and the native boot-fence
result alone does not close Phase 2 acceptance.

## Transport schema correction on the unchanged execution path

The
[complete round-8 report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5826356000)
tests transport `77063be11399f973195a7e614d0517885f0c5c30`. Its four-commit delta from the round-7
head changes database open validation, its focused tests and the transport plan; it changes no
carrier, guest or execution runtime. On the tester's Linux workstation, writable, read-only and CLI
open paths refused real branch-built databases stamped 39 to 41 without changing their main files or
WAL and without creating an invalid backup. A copy of the released v38 operator database upgraded to
the canonical schema and opened successfully. The report is clean and carries forward the round-7
SSH and WSL2 observations without claiming a new guest or workstation run.

Transport's exact-head non-integration suite passed **13,515 tests with 23 skips**. Its
[hosted CI](https://github.com/WayfarerLabs/agentworks/actions/runs/36091351948) is green, including
Linux Python 3.12–3.14, Windows Python 3.13, Website and aggregate `ci-success`; CodeQL also passed.
This resolves the reported schema collision and the prior hosted Website failure at this transport
head. A separate
[saga review](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5826298893) asks the
operator to choose whether the guard's branch-specific scope should remain; that transport-owned
decision may change its later head. SSH #832 has not rebased onto this database-only delta, and the
earlier composed SSH proof retains its explicit pins. Public RunContext composition, trust
migration, terminal delivery, lost-hold recovery and full SSH workflow acceptance remain open.

## Advanced transport stack refresh

SSH #832 rebases its 84 SSH commits from transport `f937cac0` onto transport
`ad960430a558882022df217e23d48aa4012c9754`. Transport's intervening private work changes file
operations, VM availability, lifecycle ownership and local download preparation. It also adds
`SinkOutput.required_complete_stdout_bytes`: a finite-buffering carrier must refuse an unsupported
complete-success stdout requirement before dispatch. SSH's existing `SinkOutput` path streams raw
output through the shared subprocess pump, so it needs no finite ceiling or SSH runtime change.

The rebase resolved one overlapping test import by retaining both the transport file-bundle fixture
and SSH's explicit trust policy. A new transport helper-sizing test used the retired bare-path SSH
constructor and omitted the required admitted `trust` argument. It now constructs `SSHTrustFiles`
and passes that same policy to the pure argv builder. The isolated combined checkout first passed
**419 focused tests with 6 skips** at transport runtime head `b65444d2`; after this rebase and test
adaptation, the full CLI suite passed **14,298 tests with 27 skips**. Full Ruff and format checks
passed; mypy reported no issues in **1,199 source files**. These are local composed-tree results,
not hosted CI or native platform acceptance for the new SSH head.

That rebased implementation head, `d350516c3a9d6638d75fbf124f267bd762c4630e`, then passed
[hosted CI run 37397919601](https://github.com/WayfarerLabs/agentworks/actions/runs/37397919601):
Linux Python 3.12/3.13/3.14, Windows Python 3.13, static checks, website, file lint, locked-SDD,
Rulesync drift and the aggregate gate. This verifies the pinned implementation revision in CI; it
does not supply the pending native SSH workflows or production composition.

Transport's new draft head removes its branch-specific migration 39–41 guard without deleting the
historical development databases. Those databases are not migration acceptance evidence. Production
RunContext, ExecutionAccess, FileAccess and JobAccess composition, terminal delivery, recoverable
job-length availability where needed, genuine creation/publication binding, full Linux/macOS/Windows
SSH workflows, and exact-head native integration still remain open. The earlier native results
retain their pinned revisions and do not validate this new head.

## Private local-download stack refresh

SSH #832 rebases its 86 SSH commits from transport `ad960430a558882022df217e23d48aa4012c9754` onto
`1dbb6245d96cf7dc76299c57088602b52d2cf60d`. Transport's new private Linux download composition
connects verified remote transfer to local create or explicit-replace publication under one
deadline, and tightens pathname-ancestry custody. It also records the requirement for a concrete
activation producer and selected availability hold. This transport increment changes no shared SSH
carrier contract, terminal endpoint or public RunContext surface. The rebase needed no SSH source or
test adaptation.

The combined CLI suite passed **14,301 tests with 26 skips**. Full Ruff and format checks passed on
1,238 files, mypy passed 1,201 source files, and repository file lint passed. These are local
combined-tree results. The preceding hosted CI and native reports retain their exact historical
heads; neither validates this new SSH head. Public FileAccess and RunContext composition, terminal
delivery, genuine binding and supported-platform SSH workflows remain open.

## Percent-encoded environment regression

[Issue #845](https://github.com/WayfarerLabs/agentworks/issues/845) reports an opaque Windows
OpenSSH failure when the legacy `SetEnv` path receives percent-encoded JSON with embedded quotes.
The new SSH carrier does not build `SetEnv` options: transport's private inline candidate carries
the workload environment as stdin data. The legacy production commands remain separate during the
agreed parallel migration, so candidate evidence does not close that live issue.

The SSH conformance regression now exercises that inline candidate through both the synthetic
POSIX-shell client and the installed loopback SSH client/server. It verifies exact guest bytes for
synthetic JSON containing `%40`, quotes and backslashes, plus literal `%h`, `%%`, `${HOME}`,
Unicode, multiline and empty values. It also verifies the values stay out of raw SSH argv and the
percent-encoded JSON marker stays out of Windows command-line serialization. All **4 conformance
tests passed**, including the installed-peer case, on Linux with OpenSSH client **9.2p1** and server
**9.2** (Debian `2+deb12u10`). The owned fixture creates temporary keys and one server and cleans
them up; it uses no operator secrets or VM state.

This is exact-byte Linux candidate proof, not native Windows or production caller acceptance. Repeat
the vector through the additive RunContext path on the reported Windows 9.5 client and an
expansion-enabled 10.x client, plus supported Linux/macOS workstations, before claiming new-path
delivery complete. Any urgent compatibility correction to the legacy path requires its own native
reproduction and disposition; no legacy runtime correction is made in this increment.

### Native environment delivery on four clients

The
[integration tester's report](https://github.com/WayfarerLabs/agentworks/issues/845#issuecomment-6009410929)
exercises exact SSH `4e32a9f9eece7ce3abb87a63f28f196547c7b947`, containing transport
`08bf36fd6ce270862ceceb3450e6a282039faaa2`. Its private preparation, carrier and output-decoder
composition delivers all **14 cases byte-exact on all four clients**, with complete framing and exit
code zero. The workstation/client pairs are Debian 12 with OpenSSH 9.2p1, Windows Server 2022 with
OpenSSH-for-Windows 9.5p2 and 10.0p2, and macOS 26.3 with OpenSSH 10.2p1. All reach Debian 13 guests
over SSH. This adds native Windows and macOS evidence for the stdin-envelope environment boundary;
it does not exercise the production RunContext surface or terminal delivery.

The same report drives the unchanged production `agw agent exec` path on main `cea5e852` using
synthetic data. Both 9.x clients deliver every case exactly, including Windows 9.5p2. Both 10.x
clients reject invalid percent tokens and silently expand valid percent tokens and `${HOME}`. The
captured Windows argument boundaries and client configuration parse preserve embedded quotes. This
reproduction therefore attributes its failures to client option expansion, without reproducing the
original reporter's claimed 9.5p2 failure. The reporter's Windows 11 environment and exact resolved
executable remain untested.

The isolated percent-doubling experiment corrupts percent-bearing values on both 9.x clients while
correcting those values on 10.x; it leaves workstation dollar-variable expansion intact. A synthetic
workstation-only variable also reaches the guest through the old macOS 10.2 path. These observations
support retaining literal environment data outside SSH options. They neither authorize a legacy
compatibility patch nor close the issue while production callers still use that path.

Proxy configurations, other Windows SSH distributions and larger values were not exercised. The
tester reports no repository changes or pushes. Production new-path environment acceptance remains
part of the open workflow gate, with this exact-pin native vector evidence available for comparison.

## Workstation download transport refresh

The later [shared terminal dependency adoption](terminal-results.md) records transport `d9315847`
and SSH's explicit stdin adaptation. Its shared terminal types do not enable SSH terminal delivery;
actual relay, resize, native ownership and production workflow acceptance remain open.

SSH code head `1de533873f13f6cef363ea252b05c6e7dd724f65` rebases all 88 SSH commits onto transport
`a274a6264cd6b2ea3ced4864b52a8fcdad96bf80`. The transport increment selects private Linux, macOS and
Windows local download publishers. It retains caller-private Create and implements the approved
macOS/Windows Replace against a held existing file, with unsupported metadata refused before
mutation and later local effects reported separately from remote transfer and cleanup. These are
transport-owned candidate mechanisms, not public FileAccess or native acceptance.

The rebase needed no SSH source, fixture or contract adaptation. The combined non-integration suite
passed **14,333 tests with 50 skips** in 216.40 seconds. Full Ruff and format checks passed on 1,243
files, and mypy passed 1,206 source files. All four SSH conformance cases passed again, including
exact environment delivery through the owned Linux OpenSSH peer. Installed-peer file read and stage
round-trip cases also passed. The snapshot case skipped because this environment's `/tmp` has mode
1777 but owner UID 65534, whereas the helper requires root ownership. This result does not repeat
the earlier snapshot acceptance.

Earlier hosted and native evidence retains its recorded heads. Terminal delivery, additive
RunContext/SSHSettings composition, supported-workstation file publication and complete SSH workflow
acceptance remain open.

## Windows ancestor-sharing correction

[Hosted Windows CI](https://github.com/WayfarerLabs/agentworks/actions/runs/37404101867) at SSH
`e2ae6168c27846aa7a3e1a6d4dc0eaa3239f97bb` failed the inherited transport ancestor-hold regression:
requesting DELETE access did not raise the expected sharing violation. The Windows selection
recorded **1 failed, 803 passed and 48 skipped**. This is a native publisher failure, not SSH
delivery evidence; the affected source and test are unchanged from transport's base.

Transport correction `08bf36fd6ce270862ceceb3450e6a282039faaa2` requests ordinary directory-list
access on held ancestors without delete sharing. It retains the DELETE-open regression, adds actual
rename refusal and verifies rename after release. Unsupported ancestor access refuses before
staging; no privilege is enabled. SSH code head `c4f9718185f5823eaea4feac0c6e9a5f370b87fc` rebases
all 89 SSH commits onto that correction without SSH source or fixture adaptation.

Focused local publication/coordinator tests passed **131 cases with 25 native-platform skips**. Full
Ruff, format and mypy checks passed. The prior full Linux suite and owned SSH peer results retain
their earlier pins; this Windows-only correction does not repeat them. Native CI for the corrected
combined head is required before treating the sharing failure as resolved. Production and complete
native workflow acceptance remain open.

## Native workstation report and remaining delivery findings

The
[complete round-9 report](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6008723722)
tests transport `08bf36fd6ce270862ceceb3450e6a282039faaa2`. Its Linux and macOS SSH cells use a
local composition with older SSH `508192acdc451c5934e598a6002725dcd76b60c4`; Windows Server 2022
uses WSL2Carrier. The report measures private publisher and coordinator behavior, not production
RunContext or native Windows SSH. Its test-file merge conflict does not require another SSH rebase:
published SSH `dfe02e81d8434a50b4f83b4ce87120c8c1c8339c` already contains the exact transport head.
The only SSH runtime difference from the tested pin is its unsupported-input guard.

Native Create/Replace cells establish exact content, truncation, metadata preservation or refusal,
and local staging cleanup on Linux ext4, macOS APFS outside the home directory and Windows NTFS.
Windows independently blocks ancestor rename while held and permits it after release. Failed or
uncertain remote transfers are not published; retained guest scratch and unresolved operation
ownership remain explicit. These observations do not establish recovery of that retained debt.

Three material transport-owned findings remain dependencies of usable SSH-backed workflows:

- The macOS publisher rejects extended ACLs on ancestors, including the measured stock home
  directory policy. Successful publication outside home does not establish ordinary home paths.
- The guest boot-fence probe reads `/proc/1/stat`, which shipped `hidepid=1` hardening denies to the
  VM admin. The earlier round-7 proof ran as root and does not establish ordinary admin composition.
  The hardened test host also reports four related local-suite failures.
- A 3 MiB SSH download takes 152.9 seconds and exceeds a 120-second deadline with retained debt. The
  current transport protocol requests 12 KiB chunks through repeated carrier executions. SSH
  supplies live sink delivery; useful transfer performance is still unproved at the shared file
  boundary. No connection-sharing or alternate file protocol is introduced by this evidence record.

The report also observes that an administrative Windows SSH session can inherit already enabled
backup/restore privileges, weakening the stated ancestor-access refusal. The code enables no
privilege; transport owns the caller-authority disposition. Native terminal, public composition,
full SSH workstation acceptance and reviewed corrections with fresh live evidence remain open.

The
[input-admission checkpoint](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6008564312)
at `dfe02e81d` passed all hosted checks, including **806 Windows tests with 48 skips**, and both
private review lanes. It refuses unsupported input before admission or process activity while
terminal capability remains disabled. This does not resolve the native delivery findings or close
Phase 2. Recording the report consumes no SSH public feedback/fix round.

## Remaining integration and acceptance

Earlier integration `fefc2b9e` uses transport `f3339f3d3cccace129be58711dc7eeb30ec66dc2`, which adds
private publication-stage ownership recovery. The rebase is clean and changes no SSH runtime, SSH
fixtures, carrier interface or shared process core. Publication delivery is not yet implemented by
that milestone. Transport records complete Windows command sizing as a gate for its upcoming fixed
helper; the current snapshot helper's separate sizing and live evidence remain scoped to their
recorded revisions. At that revision the
[forwarding ownership proposal](forwarding-lld.md#launch-ownership-integration) awaited transport
extraction; the dependency above now supplies it. Terminal and RunContext composition remain open.

The combined suite at `fefc2b9e` passes **12,112 non-integration tests with 14 skips**. Full
Ruff/format, mypy (1,017 source files), file lint, locked-SDD, rulesync and typer-isolation checks
pass. Website tests pass 160 Python and 103 Node cases, and both deterministic build comparisons are
identical. This is a clean dependency rebase with evidence bookkeeping only; earlier SSH private
review dispositions and live results retain their pins. Transport reports three clean private lanes
for its publication recovery runtime at `86189dc2`. These checks do not establish remote publication
or native workflow acceptance.

Earlier snapshot integration uses transport `5b57044260405089e972cd371462cefe325f906c`, which adds
private snapshot download exchanges and preserves verified cleanup debt on final deadline expiry.
The rebase applied cleanly without SSH runtime or shared-interface changes. Earlier evidence below
retains its measured integration pins. The combined suite at `8eff3c23` passes **12,077
non-integration tests with 14 skips**. Full Ruff/format, mypy (1,013 source files), file lint,
locked-SDD, rulesync and typer-isolation gates pass. Website tests pass 160 Python and 103 Node
cases, and both deterministic double-build comparisons are identical.

Transport [#833](https://github.com/WayfarerLabs/agentworks/pull/833), observed at
`84ac8cafee8c6ac97587bcc98e8785b9be62de8a`, supplies the shared process core and concrete
`LiveInput`/`SinkOutput` byte endpoints. SSH integration `8467d4e0` adopts those types and uses the
shared status owner for forwarding cleanup. Focused SSH tests passed 269 cases with five skips. The
combined non-integration suite passed 12,002 tests with 14 skips after updating a file-helper sizing
fixture's SSH call site to explicit trust. Full Ruff and mypy passed, with 1,005 typed source files.
Test-only follow-up `5986290d` adds an installed Linux OpenSSH live-byte case with fresh loopback
credentials: sensitive binary input, partial/stalled sinks, stream provenance, empty retained report
data and truthful exit 23 all passed. Final runtime revision `c97fc8c0` removes duplicate SSH mode
validation and redundant tests after shared byte-mode adoption. The final combined non-integration
suite passed **11,991 tests with 14 skips**; both SSH duplex tests passed, including the installed
OpenSSH case. Full Ruff/format, mypy (1,005 source files), file lint, locked-SDD, rulesync and
typer-isolation checks passed. Website checks passed 160 Python and 103 Node tests, and both
site-base double builds were identical. Independent project, complexity and correctness reviews are
clean at `0e499da6`. These are local results; hosted CI records the published head separately.
Transport's private two-gate terminal preparation is also implemented, but a shared terminal
endpoint type is not yet enabled or frozen. The operator confirmed #833 as the implementation
source, with terminal/PTY work proceeding in parallel.

Earlier SSH integration `678e487d` incorporates transport `a885ef5a` and adds a standalone
buffered-mode guard for #833: unfamiliar shared input/output modes refuse before connection
admission or process creation. The then-current constructor checks already excluded these shapes;
the guard let transport widen its types safely before SSH adopts them. Six simulated future-mode
cases prove that boundary, not live-mode support. A clean cherry-pick onto transport `a885ef5a`
passed its client and guard tests (53 passed, 4 skipped). The complete SSH combination passed 10,596
local tests with 12 skips, full Ruff/format and mypy. Three independent private reviews passed the
guard; removing its input and output checks broke the corresponding four and two cases. Transport
retained that guard when it published the widened types. SSH now supports all current choices, so
its duplicate allowlists and constructor-bypassing guard tests are retired. Shared constructor
validation remains; future shared types still require coordinated adoption and proof.

The [terminal experiment](terminal-lld.md#real-ssh-client-relay-experiment) demonstrates why an
owned local stdin PTY is preferable to pipes for POSIX OpenSSH: geometry, resize and nondefault
modes survive while transport's bootstrap supplies the payload/interactive handoffs. It does not
implement the shared terminal API, production relay or supported-platform acceptance. The later
[published-preparation proof](terminal-lld.md#published-preparation-joint-proof-2026-09-20) composes
the actual transport handoff in two real Linux SSH runs, including independent lead repetition.
Bootstrap EOF sufficed for the keyboard transition without SSH parsing readiness. Partial writes,
presentation stalls, early keys, resize, clean-exit restoration and fixture cleanup passed; the
remaining production/native gates still apply.

Integration revision `de18829d` rebases onto transport `806741ca3cde218bbe5ca5dfd7bbe3d319eb9e32`.
That increment replaces the destination file-lock prerequisite with transport-owned database
coordination; SSH's independent trust-maintenance lock is unchanged. The file-helper test retains
the current transport fixture names and SSH's explicit trust argument. The byte-adoption and
terminal results above retain their original pins; they do not certify the newer process core.

Transport's published launch owner now addresses the measured Linux process-construction
interruption by retaining the client outside the caller's byte pump. Transport still records an
asynchronous interruption at cleanup-loop entry that can leave a child and pipes live. See its
[launch-interruption evidence](../2026-09-12-transport-improv/prior-art-research.md#local-process-startup-and-interruption)
and [lifecycle design](../2026-09-12-transport-improv/execution-lifecycle-lld.md). Shared cleanup
correction and native proof remain production acceptance work. Forwarding now uses that shared
owner, with the focused adoption evidence recorded above.

Integration revision `de18829d` passes **11,939 non-integration tests with 14 skips**. Both SSH
live-byte cases pass, including installed OpenSSH. The pre-authentication pipe fixture now uses
empty finite input: it still creates an owned stdin pipe, without racing unused payload delivery
against the fixture's deliberate disconnect. Two new helper-sizing tests supply SSH's explicit trust
argument. No SSH runtime code changed in this rebase. Ruff/format, mypy (1,004 source files), file
lint, locked-SDD, rulesync, typer isolation and website gates passed, including 160 Python tests,
103 Node tests and both deterministic double-build comparisons. Independent project, complexity and
correctness reviews are clean at `de18829d`. Hosted CI records the subsequently published head
separately; these results do not close the remaining production acceptance gates.

Also outstanding are additive RunContext/platform composition, a genuine creation-flow provenance
and publication binding, complete SSH-backed workflow evidence, and supported Linux/macOS/Windows
workstation/provider observations. Native Windows trust permissions/publication and console
restoration are not established by POSIX fixtures or hosted unit tests. Existing PoC coverage
retains its recorded qualifications; it does not close new Phase 2 obligations.

The [plan](plan.md#phase-2-full-ssh-implementation-in-the-second-pr) therefore remains open, the PR
remains draft without `review-requested`, and there is no merge or full-phase acceptance claim. The
initial artifact checkpoint closed with 0 of 1 public fix rounds used. The separate allowance of up
to three implementation public feedback/fix rounds remains unused. Shared integration must be
completed before ready; final SSH deletion still waits for the later operator request and preceding
transport retirement stages.

## Historical dependency chronology

The following record was moved from the plan without changing its historical content.
Revision-specific statements, pending proposals and landing order describe their recorded
checkpoints; the current plan and latest disposition control the work.

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

Published SSH `9644741564047e351c7eb7c5557fec102329e3fb` has a complete
[hosted report](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6032185596) on
transport `2add24df7`. Run `37579050040` passes all four pytest jobs: Linux Python 3.12-3.14 each
report 16,754 passed, 203 skipped and 29 warnings; Windows Server 2025/Python 3.13 reports 1,622
passed, 68 skipped and 25 warnings in 827.38 seconds. All six test/static/website logs identify
checkout `b7986c72fbcf6fce9198488b5006e50dd0d666f7`; its exact transport/SSH parents and tree
`ee11c9b9199d21d85c810b76bdb7bdf4589458f2` match published source. Only the three enrollment custody
typing errors and aggregate gate fail. Other repository/site jobs pass, including website 160 Python
and 103 Node tests, four builds and both deterministic comparisons. Hosted Typer isolation skips
after mypy failure; the exact local check passes. Transport's exact base also has complete green
hosted evidence from run `37578187771`, including Linux 16,332/203/29 and Windows 1,407/58/25 in
842.87 seconds. Neither report establishes full native workflow acceptance.

Transport's
[reviewed RESOURCE disposal increment](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-6032434995)
`dd10ebbeee677570c95f9b0f30688f32f6891aeb` reuses ExecutionAccess and the existing actual-helper
tracker. Exact core-bound namespace, confirmed receipt and positive terminal evidence precede its
first effect. A dedicated run-ID-only action binding retains the receipt, terminal proof and prior
remote-action uncertainty without adopting the independent job or adding an executor. Helper
termination is distinct from action settlement. Bookkeeping and finish never dispatch; a separately
requested exact retry after proved helper closure reconciles its prior row without rearming a
resolved attempt or rereading removed launch artifacts. Unknown helper/local custody remains held.
Whole-unit review found initial and retry admission races with closing; the corrected code checks
closing under the admission guard and proves both refusals without another borrow or disposal. All
three private code lanes and both completion-record lanes clear. The publication's formatting
correction changes only completion prose. Carrier/custody wire, schema and SSH implementation are
unchanged. The
[coordination notice](https://github.com/WayfarerLabs/agentworks/pull/832#issuecomment-6032441878)
adds no carrier requirement or operator resource ruling. Transport's unchecked unified independent
launch design and private implementation remain its work, with separately required platform-owned
availability, production composition and full RunContext.

SSH rebases without conflicts at `4bf3832d1cecf55e793d3e577e135360569e9e52`, preserving all 180
prior patches exactly. SSH production and fixtures are unchanged, and shared process/custody/pump
files match transport. Full combined Linux Python 3.12 non-integration validation passes 16,983
tests, 49 skips and 29 warnings, exit 0 in 223.88 seconds. Full strict mypy checks 1,328 sources and
repeats only the three held enrollment errors, exit 1. Ruff/format (1,365 files), exact local CI
Typer isolation, frozen dependency sync and Rulesync through Node pass. The exact owned pytest root
has no same-user argv/cwd/fd references or permission gaps and is removed after terminal completion.
Website files are unchanged; local website and native tests were not rerun for this draft rebase.
Transport's new hosted run `37582372538` and fresh combined hosted results require their own
complete reports.

Both operator resource decisions, retained consumers, terminal and physical Windows input, genuine
creation/trust authority, writer coexistence/rollback, both usable RunContext paths and complete
R1-R5/native acceptance remain open. All 25 completed plan blocks are unchanged, no public
final-product fix round is consumed, and this SDD remains unlocked.

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
