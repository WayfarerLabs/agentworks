# Native lock hierarchy: locked

2026-11-28: The effort is complete. All three PRs are merged for 0.20.0:

- the design PR, [#949](https://github.com/WayfarerLabs/agentworks/pull/949);
- PR 1, [#972](https://github.com/WayfarerLabs/agentworks/pull/972): shared-state fixes, newly
  guarded mutators and prompt moves under the single lock;
- PR 2, [#973](https://github.com/WayfarerLabs/agentworks/pull/973): the hierarchy.

## What shipped

The `native_locks` package replaced the single per-VM native mutation guard, as the HLA and LLD
describe. The LLD was kept in step with the code through #973's final head, `3182811f1`, except for
the whole-row console compares in #978. Two pieces of PR 2 have no plan box of their own:

- **Restore hints:** a refusal after recorded intent says how to restore the old intent.
- **Tool homes:** a harness tool home must resolve beneath the account's physical HOME.

The permanent homes are:

- the `native_locks` and `recheck` module docstrings: the hierarchy, the operation sequence, the
  re-check rule and the named-slot binding;
- the request builders in `agents/locks.py` and `workspaces/locks.py`: the lock sets;
- the command reference's busy paragraph;
- the orchestration README;
- the 0.20 upgrade guide.

No permanent doc refers to this directory.

## Validation

- **#972:** three integration rounds. The
  [final report](https://github.com/WayfarerLabs/agentworks/pull/972#issuecomment-7220442171)
  recommended merging as-is, with coverage gaps.
- **#973:** three integration rounds from a Linux workstation, against Proxmox 9.2 (round 1) and GCP
  (rounds 2 and 3). The
  [final report](https://github.com/WayfarerLabs/agentworks/pull/973#issuecomment-7234484921)
  recommended merging as-is, with no Blocking or Important finding. Its locking evidence:
  - real separate processes for conflicts, killed-holder release and a compatibility helper for the
    older locking algorithm;
  - real-database regressions, with a substituted transport, for each race the rounds found.
- **CI:** green on each PR's merged head. The Windows job runs the primitive's conflict tests
  through `LockFileEx`. There is no macOS job.

**Not validated.** PR 2's last plan box and the live checks in LLD section 6 did not run. The
operator merged #973 without them, on the tester's merge-as-is recommendation. Two gaps back a
design assumption: that shadow-utils serializes concurrent account edits and group-member removals,
which no run has observed on trixie. The fixed races were never re-run end to end on a live VM.
[#981](https://github.com/WayfarerLabs/agentworks/issues/981) tracks all of these.

## Follow-ups filed

- [#974](https://github.com/WayfarerLabs/agentworks/issues/974): a stored overlay from a newer
  release breaks the registry load.
- [#975](https://github.com/WayfarerLabs/agentworks/issues/975): duplicate explicit grant rows.
- [#977](https://github.com/WayfarerLabs/agentworks/issues/977): instance IDs for workspaces, agents
  and consoles, which would retire the named-slot binding in `recheck.py`. The operator chose this
  for after 0.20.0.
- [#978](https://github.com/WayfarerLabs/agentworks/issues/978): three console runtime checks
  compare whole rows, which spuriously refuses a console another command just started. The operator
  chose this for after 0.20.0.
- [#979](https://github.com/WayfarerLabs/agentworks/issues/979): workspace create doesn't re-check
  its VM's init status under the lock.
- [#981](https://github.com/WayfarerLabs/agentworks/issues/981): the outstanding live validation.
