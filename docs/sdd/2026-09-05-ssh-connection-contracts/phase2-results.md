# SSH Phase 2 Implementation Progress

- Updated: 2026-09-20
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

## Remaining integration and acceptance

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

Current integration rebases onto transport `806741ca3cde218bbe5ca5dfd7bbe3d319eb9e32`. That
increment replaces the destination file-lock prerequisite with transport-owned database
coordination; SSH's independent trust-maintenance lock is unchanged. The file-helper test retains
the current transport fixture names and SSH's explicit trust argument. The byte-adoption and
terminal results above retain their original pins; they do not certify the newer process core.

Transport's published launch owner now addresses the measured Linux process-construction
interruption by retaining the client outside the caller's byte pump. Transport still records an
asynchronous interruption at cleanup-loop entry that can leave a child and pipes live. See its
[launch-interruption evidence](../2026-09-12-transport-improv/prior-art-research.md#local-process-startup-and-interruption)
and [lifecycle design](../2026-09-12-transport-improv/execution-lifecycle-lld.md). Shared cleanup
correction, native proof and SSH forwarding's separate startup path remain production acceptance
work.

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
