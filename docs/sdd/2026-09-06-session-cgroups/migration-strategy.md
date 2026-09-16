# Session Cgroups: Migration Strategy

- Status: Draft strategy supporting [FRD R6](frd.md#r6-supported-environments-and-migration).
- Snapshot: source baseline `b22cc49c984aa91e8205e035c5766b5a2aede90a`, 2026-09-06.

## Starting and target state

At the original snapshot, the session row has five runtime observations: socket path, PID, boot ID,
tmux process start ticks, and last-started time (`cli/agentworks/db/models.py:149`). It has no
`session_uuid` or `run_id` at that snapshot. Dedicated sockets coexist with legacy rows lacking a
dedicated socket. Agent session launch uses direct agent SSH, with an admin transport separately
available for setup. No operator inventory was read, so the number of live or legacy sessions on
actual VMs is unknown. The operator subsequently confirmed existing Bookworm VMs and accepts a
required upgrade to Trixie if concrete compatibility friction warrants it; this is evidence of
demand, not an inventory count.

The shared identity prerequisite has since landed through
[PR #794](https://github.com/WayfarerLabs/agentworks/pull/794), verified at merge commit
`908258a932c7715ff92dc75a7a64163a99920872` on 2026-09-15. Migration 38 supplies `session_uuid` and
nullable `run_id`; core allocates identities for managed launches. This closes the schema ownership
dependency, not the containment or protected-registration work.

The target keeps the human session name and consumes those logical session/run identities, backed by
a protected service registration on the VM. Agent runs use system-owned containment; admin runs
retain their existing behavior. A session called `research` can be deleted and recreated without
reusing its identity or inheriting permissions from the previous run.

Before choosing a transition, price Bookworm support against upgrading existing VMs to Trixie. Keep
Bookworm in the target until the cost is known, and preserve R7 in either outcome. If an upgrade
becomes a prerequisite, document its interruption and recovery costs; operator acceptance of that
possible prerequisite does not authorize this draft to upgrade or recreate live VMs.

## Transition mechanics

1. Consume the shared identity fields, migration 38, and managed-launch allocation delivered by PR
   #794. Preserve existing logical UUIDs and recorded run identities; legacy rows receive no
   invented historical run. Integrate launch allocation with protected VM registration and recovery
   in the identity/lifecycle LLD. A stored identity does not assert that running processes have
   acquired protection; do not infer ownership of old descendants from their current parent PID.
2. Install the trusted runtime and finalized restrictions through VM/agent init and reinit. Preserve
   authorized operator access. A prerequisite probe refuses protected launch until setup is proven;
   it cannot weaken the policy or silently rewrite broader-scope configuration.
3. Inventory old agent runtimes and reachable outside execution services. Retain legacy lifecycle
   handling only as a bounded migration path, clearly distinguishable from protected execution. New
   launches after conversion use the single protected path.
4. Require old work to be gone before certifying conversion. Stopping tmux alone cannot establish
   this for detached children that were never tracked. A verified VM reboot is a sufficient process
   reset, but interrupts other workloads and therefore requires an explicit operator action. A less
   disruptive alternative is acceptable only with independent proof covering all old agent work and
   prevention of automatic relaunch. Do not use an indiscriminate UID kill from a single-session
   command.
5. Launch the first protected run only after the chosen transition proof and prerequisite checks.
   Retire legacy handling when the supported migration path no longer needs it; the implementation
   plan must name its bounded disposition rather than leave two permanent launch modes.

## Worked example

An existing `research` session has a stored tmux fingerprint but a detached tool may still exist.
The shared schema migration has assigned a logical UUID to the row, without declaring the live
execution protected. A normal legacy stop can report only its existing terminal-level result.
Conversion cannot interpret that result as proof of the new process-lifetime guarantee. After the
operator's approved transition establishes that old work is gone and relaunch paths are closed, a
new start mints a run ID and registers its service. Restart later proves that group's emptiness
before minting another run; reusing the name after deletion creates a different logical UUID as
well.

## Safeguards and decisions

- Interrupted installation leaves diagnosis and retry available, not a partially enabled guarantee.
- Reboot invalidates old live bindings; it does not automatically prove that startup services cannot
  recreate old work. Outside execution channels must be closed before protected launch.
- Failed workstation persistence is reconciled from the protected VM registration. Existing
  fingerprint observations remain useful legacy evidence but do not authenticate protected runs.
- Persisted unknown or stopping runs block replacement until reconciled; do not discard their
  identities to make an inventory look healthy.
- The operator must decide the acceptable interruption policy and approve any changes to SSH,
  containers, or shared-home behavior exposed by the proof gate. This draft schedules no reboot,
  migration, or live workload interruption.
