# SSH Phase 2 Implementation Progress

- Updated: 2026-09-21
- PR: [#832](https://github.com/WayfarerLabs/agentworks/pull/832), draft
- Initial evaluated code: `e63a1ec4a8e9d99e89d09d6b162e908d3a4b96d6`
- Main/contract base: `cea5e8523aac05edfc3a99a940d7cfb4d71fe32f` (#830)
- State: Byte I/O adoption validated locally; full terminal and production integration remain open

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

## Managed-process fix integration

Transport's
[round-1 handoff](https://github.com/WayfarerLabs/agentworks/pull/833#issuecomment-5768952091)
publishes `ae1ce293` after clean private reviews and hosted validation. It removes the candidate's
post-wait helper-failure veto from terminal/lifecycle completion and adds fixed transient-unit
collection. It changes neither the shared process pump nor SSH interfaces. The requested second
native round retains both prior material findings as retest obligations, including systemd 252 and
the unavailable Proxmox 9 cell; green local checks alone do not close them.

Local SSH rebase `3c7ede4d64c4034d87dca88aa1e42bbc00b55284` has no conflicts or SSH-source edits.
Its full tree `5857ccbc0f66e8591b56cba8bf78282275757ced` exactly equals the requested composition of
transport `ae1ce293` and published SSH `b57b45df1`; the CLI tree is
`77a52a47fb94ae74eb7106f41b156eed8b2e7bea`. This proves source equivalence, not native acceptance.
The published SSH pin remains unchanged while the complete native report is pending.

The combined non-integration suite passes **12,707 tests with 14 skips**. Full Ruff/format, mypy
(1,067 sources), file lint, locked-SDD, rulesync and typer-isolation checks pass. Website Python and
Node suites and deterministic double builds for both site bases pass. Earlier SSH runtime reviews
and live evidence retain their recorded pins; this dependency rebase introduces no SSH behavior.
Terminal delivery, RunContext composition, production creation binding and full supported-platform
acceptance remain open.

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
