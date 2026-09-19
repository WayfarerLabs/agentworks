# SSH Phase 2 Implementation Progress

- Updated: 2026-09-19
- PR: [#832](https://github.com/WayfarerLabs/agentworks/pull/832), draft
- Evaluated code: `e63a1ec4a8e9d99e89d09d6b162e908d3a4b96d6`
- Main/contract base: `cea5e8523aac05edfc3a99a940d7cfb4d71fe32f` (#830)
- State: Independent policy and forwarding increment validated locally; full integration remains
  open

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

## Remaining integration and acceptance

Transport [#833](https://github.com/WayfarerLabs/agentworks/pull/833), observed at
`6f20bdb5930ccf09c278c419acf784a65bc79dad`, still provides only buffered carrier types. Its live I/O
LLD is a candidate. This SSH branch has not substituted local shared types or claimed integration
with that unfinished implementation. Full live/terminal delivery requires transport's concrete
carrier/report/terminal definitions and joint proof.

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
