# Session Batch Stop Parallelism: Closeout

- Date: 2026-11-10
- Implemented and independently validated head: `ca483c9a8ee8263cfd042b31a04ac807c0caffbe`
- Integrated main: `1dd5a06dd330d947676fe130cc345fa93e64c797`
- Delivery: PR #776; implementation feedback/fix round 3 of 3 complete

## Delivered

Batch stop overlaps independent complete-fingerprint dedicated tmux teardown with a fixed ceiling of
eight workers. Each owns a fresh transport, a ten-second per-call timeout and one attempt. The
invoking thread retains selection, activation holds, database access, reconciliation and output.
Incomplete and legacy work remain serial. Named and batch teardown share complete preparation and
execution without changing native ownership, socket checks, absence proof or force policy.

Submission ownership gates mutation until every task has a mapped future. Interruption cancels
queued work and visibly drains/reconciles active work. Stopped reconciliation is conditional and
recognizes an interrupted prior commit without adopting a replacement.

An immutable seven-field snapshot and one SQL predicate fence acquisition, teardown reconciliation
and all four PID-repair writes. Nullable persistence evidence grants no native authority or worker
eligibility. Repair conflicts and database writer failures stop batch preparation before stale
status or destruction; ordinary native repair refusal keeps its existing handling. This is not
whole-command cross-process serialization: successful persistence and a later refetch remain
separate phases. No schema, command grammar, capability version or transport migration is added.

Permanent teaching is in `cli/command-reference.md` and `docs/guides/session-status.md`; operating
the feature does not depend on retaining this SDD.

## Verification

All twelve local gates passed on the frozen combined head, including 11,608 tests passed and 11
skipped. All hosted checks passed, including Python 3.12/3.13/3.14, Windows and CodeQL. Hosted
Python 3.12 reported 11,400 passed and 219 skipped. Project, complexity and cold
correctness/security reviews were clean. The plan records exact run counts, independent controller
experiments, source parity and installed-wheel evidence without relabeling earlier checkpoints.

The separate integration report, PR comment 6772578864, passed all twelve gates, 133 focused tests
and seven SQLite/controller probes. A real Linux-to-remote-Lima replacement overlapped the actual
repair-write boundary: the older batch refused, the supported replacement restart survived, and
peer/sentinel identities remained unchanged with zero later status/teardown calls. This was one
bounded window using an explicitly lower-authority fault fixture, not proof of every interleaving.

PowerShell 5.1 and 7.6.9 launched 74 real Windows SQLite/controller cases against the verified
candidate. Remote boundaries were stubs; these results are not Windows guest, SSH, WSL or harness
passes. Queued/repeated interrupts, request counts, continuous worker-bound measurement and
ambiguous native-kill scenarios remain qualified. Prior native/Windows and ten-second timeout
evidence retains its original head attribution; the transport policy is unchanged.

The instrumented start's identity repair succeeded but its later access grant failed, so that start
is not an overall positive CLI pass. Issue #886 records inherited source attribution and the
distinct interrupted-finalization retry; native baseline reproduction has not been performed.
Ordinary named start/stop and subsequent batch stop passed separately. Cleanup and bounded
secret-form scans were independently audited by the testing lead with their qualifications retained
in the plan/report.

## Disposition

The operator's exact additional-round ruling is retained in the FRD. The final report and published
delta feedback were critically assessed, and the one-hour feedback window elapsed before closeout.
No material finding remains in the changed work. Optional classification/wording notes were
acknowledged without source changes; three authorized source fix rounds are complete.

Deferred work remains tracked: named-stop latency (#777), parallel start/restart design (#778),
process containment (#715) and repeated startup access granting (#886). The separately recorded
unconstrained Typer 0.29 installation incompatibility is not fixed here; installed checks used the
frozen Typer 0.27.3 lock. The refused empty-batch mutation smoke was not retried.

This closeout changes only SDD tracking and this record. Its ordinary documentation reviews and
checks precede the final ready handoff; no further material source fix or self-merge is authorized.
The artifact lock takes effect when this work lands on main.

## Subsequent Main Integration

The operator requested merge-conflict resolution after this closeout. Main `834eb7156` is now
integrated by merge `2416f45cd`. The transport factory retains main's VM host-key trust binding and
this effort's explicit retry setting. Teardown remains on the existing transport API; this task does
not migrate it to the new execution API or reopen the completed feature feedback rounds.

The earlier verification above remains evidence for its named implementation head. Fresh gates,
installed-package checks, private delta review and a new ready handoff cover this integration; their
outcomes are recorded in the plan and handoff rather than attributed to the earlier wheel.
