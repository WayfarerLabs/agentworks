# Locked

2026-11-28: The effort is complete. All three PRs are merged for 0.20.0:

- the design PR, [#949](https://github.com/WayfarerLabs/agentworks/pull/949);
- PR 1, [#972](https://github.com/WayfarerLabs/agentworks/pull/972): shared-state fixes, newly
  guarded mutators and prompt moves under the single lock;
- PR 2, [#973](https://github.com/WayfarerLabs/agentworks/pull/973): the hierarchy.

## What shipped

- **The hierarchy:** the `native_locks` package replaces the single per-VM native mutation guard.
  - Every native command takes the lock set the LLD's table gives it.
  - Locks are shared or exclusive on the VM, workspaces, accounts, grant-all, paths, grants,
    sessions and consoles.
  - Acquisition is ordered and fail-fast, and a refusal names its holder.
  - Locking uses POSIX `flock` and Windows `LockFileEx`.
- **Prompts:** no lock is held across a prompt, except the three sanctioned acquisitions, which
  print a notice first.
- **Re-checks:** every command re-checks under its final set what it read before it.
  - The lock request is rebuilt from a fresh read and compared with the one it holds.
  - Bodies run on the VM row their gate admitted.
  - Setup and launches compare the template layers they were prepared from.
  - Cleanup re-checks its candidate's eligibility.
  - Intent writes are bound to the entity the command first read.
- **Restore hints:** a refusal after recorded intent says how to restore the old intent, only while
  the entity that recorded it and that intent still stand.
- **Tool homes:** a harness tool home must resolve beneath the account's physical HOME.

The permanent docs and the 0.20 upgrade guide describe the behavior. No permanent doc refers to this
directory.

## Validation

- **#972:** three integration rounds; the third recommended merging as-is.
- **#973:** three integration rounds from a Linux workstation against GCP. The final report on
  `3182811f1` recommended merging as-is, with no Blocking or Important finding. It covered:
  - all canonical gates;
  - the VM, agent, workspace, session and console lifecycles live;
  - Claude and Codex harness health calls;
  - a focused run of the lock tests: real separate processes for conflicts, killed-holder release
    and the older locking algorithm, and real-database regressions for every race fixed during the
    rounds.
- **CI:** green on each PR's merged head, including the Windows test job.

The plan's cross-platform live box stays unchecked: macOS and Windows workstations, concurrent
account edits on a trixie guest, and each FRD story driven concurrently on a live VM. The tester
left the live concurrency experiment shelved at the operator's direction. The coverage above stands
in for it, and the operator merged #973 on the tester's merge-as-is recommendation.

## Follow-ups filed

- [#974](https://github.com/WayfarerLabs/agentworks/issues/974): a stored overlay from a newer
  release breaks the registry load.
- [#975](https://github.com/WayfarerLabs/agentworks/issues/975): duplicate explicit grant rows.
- [#977](https://github.com/WayfarerLabs/agentworks/issues/977): instance IDs for workspaces, agents
  and consoles, which would retire the LLD's named-slot rule. The operator chose this for after
  0.20.0.
- [#978](https://github.com/WayfarerLabs/agentworks/issues/978): three console runtime checks
  compare whole rows, which spuriously refuses a console another command just started. The operator
  chose this for after 0.20.0.
- [#979](https://github.com/WayfarerLabs/agentworks/issues/979): workspace create doesn't re-check
  its VM's init status under the lock.
