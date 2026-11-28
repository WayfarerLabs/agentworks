# Native lock hierarchy: plan

Status: locked; PR 2's last box did not run. See [locked.md](./locked.md).

Three PRs: the design PR (#949) carrying the FRD, HLA, LLD and this plan, then two stacked
implementation PRs, each green on its own. The first makes the shared-state fixes, guards and prompt
moves that are correct under today's single lock. The second replaces the single guard and its token
in one step, with no old and new lock paths side by side on `main`.

## Design

- [x] Draft the FRD and have the operator accept it (2026-11-23).
- [x] Write the HLA and the LLD, including the per-command lock table at HEAD.
- [x] Run the private review loop on the design (project review and complexity pass), address its
      findings, then mark #949 ready and address published feedback.
- [x] Merge the design PR.

Done when the HLA, LLD and plan agree with each other and with the code at HEAD, no review finding
is left undisposed, and #949 is merged.

## Implementation, PR 1: under today's single lock

- [x] Shared state: `groupadd -f` in the workspace-group helper, membership removal through
      `usermod -rG` with its status checked, the membership removed before the last grant row is
      deleted, workspace delete removing its normalized path and keeping its row when the removal
      fails, agent deletion stopping processes before `userdel -r` and keeping the agent's row when
      the removal fails, the per-user rebuild mutex for the SSH config, the bounded package-manager
      wait in workspace repair, and unique operation log names created exclusively.
- [x] The mutators still unguarded at HEAD (revoke, workspace copy's source, `vm import-host-keys`,
      `vm confirm-release`) take the single lock; grant, repair and copy's destination already do.
- [x] Prompts moved before acquisition per the LLD's rules and Appendix A.1, with re-checks after
      acquiring, except the rows A.1 moves to PR 2.
- [x] Tests for each, collateral where behavior changes, private review loop, integration test, and
      published feedback addressed.

Done when PR 1 is merged with green CI on all platforms and the tester has confirmed each prompt now
precedes its lock and each newly guarded mutator refuses while another operation holds the VM.

## Implementation, PR 2: the hierarchy

- [x] The primitive: the `native_locks` package (entities, modes, a probe, ordered fail-fast
      acquisition, nested reuse), POSIX `flock` and Windows `LockFileEx`, holder records and the
      busy error naming its holder, replacing `harness_setup/locking.py`.
- [x] Activation: the eight-step sequence (read, probe, confirm early, gate with a boot under a
      brief vm exclusive only when the provider reports the VM stopped, prepare, acquire with the vm
      shared, re-check, run), with admission-only shared locks for access commands and runtime
      families recording intent under a brief acquisition of their own set before the gate.
- [x] The prompt moves Appendix A.1 defers to PR 2 (vm start and stop, agent reinit's early save,
      the runtime families, console start, restart and stop), with `session start --spec`'s overlay
      hand-off to the batch and the removal of `run_session_batch(held=)`.
- [x] Every call site in the LLD's lock-set table moved to its set, each re-check comparing the
      fields the LLD names, and the three sanctioned acquisitions marked as prompting with any other
      prompt under a held set raising.
- [x] Tests: the conflict matrix at the primitive on POSIX and Windows, a crashed holder, the
      older-version interlock, holder naming, the probe, nested reuse, the prompt-notice check, and
      one concurrent pair per FRD story through real services; existing lock tests updated.
- [x] Collateral: every doc the HLA's Delivery section lists, and the upgrade guide entry.
- [x] Private review loop, three integration rounds from a Linux workstation against a live VM, and
      published feedback addressed.
- [ ] Integration test from macOS and Windows workstations, concurrent account edits on a trixie
      guest, and each FRD story driven concurrently on a live VM (#981).

Done when PR 2 is merged with green CI on all platforms, the tester has driven each FRD story
concurrently on a live VM from Linux, macOS and Windows workstations (two unrelated operations
succeed together, a conflicting one fails at once naming its holder), and no doc describes the
single guard.

## Closeout

- [x] Write `locked.md`.
