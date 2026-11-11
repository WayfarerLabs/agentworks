# Desired runtime state: completed

Date: 2026-11-10. Delivered in [PR #885](https://github.com/WayfarerLabs/agentworks/pull/885).

The implementation converged at `452eb0622520fcb816e6c9a0c6ff85f810ce5495`, containing main
`1dd5a06dd330d947676fe130cc345fa93e64c797`. VMs, sessions and named consoles retain durable desired
runtime state and support explicit scoped reconciliation and pure intent selection. Schema 39 fully
replaces the VM intent flag, migrates existing VM policy and requires explicit adoption of ambiguous
legacy workload intent. Batch workload secrets resolve before deferred runtime changes; healthy
runtimes retain identities and runtime exit does not clear intent. Permanent guidance, CLI help,
completion and JSON inspection are synchronized.

The FRD and design each converged within two authorized fix rounds. Implementation converged after
one of its two authorized rounds, with clean private and published reviews and completed separate
integration reports. The operator's instruction to wait for each integration result before another
iteration was followed. Current-main conflicts were resolved and the selected-key/forced-prompt
rekey composition was tested without weakening identity guards or the shared secret boundary.

[CI 42851513484](https://github.com/WayfarerLabs/agentworks/actions/runs/42851513484) passed all ten
jobs at the converged head. The full suite passed 11,678 tests with 11 skips. Real Linux and Windows
Server 2025 workstation campaigns against remote Lima each recovered two sessions and a named
console after a new boot with one VM command, then preserved identities on repetition. Earlier
native WSL admission/stop and migration results retain their original head attribution. Both live
campaigns independently audited cleanup and credential disposal.

[validation.md](validation.md) maps every requirement and acceptance criterion to code, behavioral
tests, permanent guidance and actual reports, including platform and fault-injection limits.
[plan.md](plan.md) records completed work and final closeout checks. Closeout changes only these SDD
records; the tested application and permanent documentation remain unchanged.

At the original closeout, no material finding or implementation work remained open. No PR merge is
authorized or performed by this effort. The SDD lock becomes binding when this PR lands on main.

## 2026-11-11: current-main integration follow-up

The operator requested resolution of new conflicts after main advanced to
`834eb715666d2d8539874a042484bf9da60fdb00`. The prior implementation and closeout results above
remain attributed to their original heads. The unmerged PR is draft while its lifecycle, trust,
artifact and compatibility integrations are resolved and independently reviewed. The
[follow-up plan](plan.md#2026-11-11-operator-directed-current-main-integration) tracks new checks,
handoff, separate integration evidence and cleanup. The integrated head requires fresh validation
before its completion can be claimed. No new feature policy or PR merge is authorized.

The main-integration handoff at `dbbbd31cc` received green CI and completed integration
report 6786419851. Its fresh Linux/Windows-to-Lima recovery and artifact results retain that head's
attribution. The report confirmed an R6 preview blocker omission. The final original implementation
feedback round, two of two, corrects that omission and protects Console preacceptance. The operator
also directed GCP suspend/resume recovery in the same PR. All three private reviews were clean at
`98b957b40`, and all twelve required local gates passed with 12,366 tests and 34 skips in the
non-overlapping root confirmation. Exact final-head CI, ready handoff and the associated separate
integration result remain required. Earlier completion claims do not mark this follow-up complete or
establish live GCP validation.

The round-two ready handoff at `863c499aa` had green CI and clean final private delta reviews.
Completed integration preflight 6787498628 then found main at `07feb4a45` and stopped on four
conflicts before gates or native tests. The operator explicitly authorized one supplemental round
and continued monitoring. Its main teardown integration and deterministic test clock are in
development under the follow-up plan. New validation, handoff, actual integration evidence and
feedback disposition remain required; the goal remains active. This historical lock does not mark
the supplemental work complete.

Implementation merge `d7260da42` is integrated with passing impacted/static checks from its owner.
The complete combined candidate still requires independent source review, root gates, final CI,
ready publication and its actual integration result. Monitoring and feedback disposition remain part
of the active effort.

Private adversarial review then found stop-error suppression, an inherited caller-interruption gap
and session evidence loss across suspension. Corrections at `afe92857f` retain authoritative errors,
completed-future reconciliation and last-known fingerprints while the parent is inactive. The owner
passed 1,365 impacted tests and full static checks. Final independent review, root gates, CI, ready
publication and actual integration evidence remain required; the goal remains active.
