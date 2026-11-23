# Native lock hierarchy: plan

Status: design in progress. The [FRD](./frd.md) is accepted; the [HLA](./hla.md) and the
[LLD](./lock-lld.md) are drafts in the design PR.

Three PRs: the design PR (#949) carrying the FRD, HLA, LLD and this plan, then two stacked
implementation PRs, each green on its own. The first makes the shared-state fixes, guards and prompt
moves that are correct under today's single lock. The second replaces the single guard and its token
in one step, with no old and new lock paths side by side on `main`.

## Design

- [x] Draft the FRD and have the operator accept it (2026-11-23).
- [x] Write the HLA and the LLD, including the per-command lock table at HEAD.
- [ ] Run the private review loop on the design (project review and complexity pass), address its
      findings, then mark #949 ready and address published feedback.
- [ ] Merge the design PR.

Done when the HLA, LLD and plan agree with each other and with the code at HEAD, no review finding
is left undisposed, and #949 is merged.

## Implementation, PR 1: under today's single lock

- [ ] Shared state: `groupadd -f` in the workspace-group helper, `userdel` split from the home
      removal, the per-user rebuild mutex for the SSH config, the bounded package-manager wait in
      workspace repair, and unique operation log names created exclusively.
- [ ] The unguarded mutators (grant and revoke, workspace repair, workspace copy,
      `vm import-host-keys`, `vm confirm-release`) take the single lock.
- [ ] Prompts moved before acquisition per the LLD's rules and appendix, with re-checks after
      acquiring.
- [ ] Tests for each, collateral where behavior changes, private review loop, integration test, and
      published feedback addressed.

Done when PR 1 is merged with green CI on all platforms and the tester has confirmed each prompt now
precedes its lock and each newly guarded mutator refuses while another operation holds the VM.

## Implementation, PR 2: the hierarchy

- [ ] The primitive: the `native_locks` package (entities, modes, a probe, ordered fail-fast
      acquisition, nested reuse), POSIX `flock` and Windows `LockFileEx`, holder records and the
      busy error naming its holder, replacing `harness_setup/locking.py`.
- [ ] Activation: the eight-step sequence (read, probe, confirm early, gate with a boot under a
      brief vm exclusive only when the provider reports the VM stopped, prepare, acquire with the vm
      shared, re-check, run), with admission-only shared locks for access commands and runtime
      families recording intent under a brief acquisition of their own set before the gate.
- [ ] Every call site in the LLD's lock-set table moved to its set, each re-check comparing the
      fields the LLD names, and the three sanctioned acquisitions marked as prompting with any other
      prompt under a held set raising.
- [ ] Tests: the conflict matrix at the primitive on POSIX and Windows, a crashed holder, the
      older-version interlock, holder naming, the probe, nested reuse, the prompt-notice check, and
      one concurrent pair per FRD story through real services; existing lock tests updated.
- [ ] Collateral: every doc the HLA's Delivery section lists, and the upgrade guide entry.
- [ ] Private review loop, integration test on Linux, macOS and Windows workstations against at
      least one VM including concurrent account edits on a trixie guest, and published feedback
      addressed.

Done when PR 2 is merged with green CI on all platforms, the tester has driven each FRD story
concurrently on a live VM from Linux, macOS and Windows workstations (two unrelated operations
succeed together, a conflicting one fails at once naming its holder), and no doc describes the
single guard.

## Closeout

- [ ] Write `locked.md`.
