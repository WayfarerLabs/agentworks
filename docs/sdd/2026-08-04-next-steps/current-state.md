# Current State

- Snapshot date: 2026-08-05 (update at wave boundaries)
- Baseline: Agentworks 0.13.0 plus the phase 1 TOML sunset (PR #316, merged 2026-08-05)

This document records where the system actually is, verified by code reconnaissance rather than
assumed from the perspectives. It is the ground truth the phasing rests on; when a wave lands,
update the affected section and the snapshot date, in place (git history is the append-only record).
The immutable origin snapshot is `starting-state.md`; the journey is the diff from there to here to
`target-state.md`.

## Declarative schema

Phase 1 (TOML sunset) is merged to `main` via PR #316: the TOML resource path hard-errors, the
legacy loaders are relocated into the migrator as its independent verification oracle, and the
fixture suite is converted. The `2026-07-31-declarative-schema` SDD stays unlocked with phase 2 held
at its phase gate until this roadmap settles the capability-kind descriptor and the 0.14 removal
ordering (see `phasing.md`). Phase 2 is fully specified but deliberately unimplemented; its settled
contracts are recorded in `target-state.md`.

## Deprecation removal targets

Every surface in the removal perspective exists as described, with corrections the wave 1 FRD
carries:

- `agw vm console` is not a thin alias. It is a separate legacy implementation
  (`cli/agentworks/sessions/console.py`, ~218 lines) unrelated to the canonical `agw console`
  family's `multi_console` code, and one canonical caller feeds it: session create's roll-forward
  best-effort hook that adds new sessions to a live legacy console.
- Two aliases are silently accepted today with no warning at all: `[paths].code_workspaces` and
  `agw vm shell --provisioner`. Their removal is a hard behavior change with zero prior notice.
- The `[user]` section warning bypasses the aggregated deprecation channel (raw stderr print), an
  inconsistency that dissolves once the alias is removed.
- `output.phase()` and `env_compat.py` are fully dead (zero production callers); `UserConfig` is a
  one-line alias. These are trivial deletions.

## Capability framework

- Exactly four capability kinds exist, and the switchboard is real: the kind set is independently
  enumerated in at least six places (adapter table, graph kind set and readiness dispatch, registry
  loaders, bootstrap publication, plugin snapshot/restore, manifest kind sections), plus a
  five-method adapter class, a publisher, an entry dataclass, and a `ResourceKind` strategy repeated
  per kind. A guard test makes a fifth kind fail loudly, but every site still needs hand-extension.
  This is the concrete duplication surface the descriptor collapses.
- The secret-source direction goes with the grain of the code: the backend module's own docstring
  already names the graduation signal ("when a backend needs config shared across many secrets...
  graduate the backend to a declarable instance kind, the secret-backend analog of vm-site").
- There is no Pydantic anywhere in `cli/` today, and the tagged-union `{name: ...}` config shape
  already works for three surfaces (vm-site, git-credential, the session harness selector),
  implemented three times independently. Phase 2 consolidates an existing pattern behind one
  contract rather than introducing a new one.
- One irregularity worth fixing during descriptor work: `_VMPlatformKind` lives in `vms/kinds.py`,
  outside `capabilities/`, unlike its three siblings.

## Session runtime (observability groundwork)

- Sessions have no run/incarnation identity. `sessions.name` is the sole key and is reusable after
  delete-and-recreate; `boot_id` exists only to detect VM reboots. Any transcript keyed by session
  name alone will splice unrelated histories. This is the single sharpest schema gap for the
  observability effort.
- There is no PTY observation, no input interception, no event or fanout infrastructure, and no
  supervisor or heartbeat. tmux owns the PTY (one tmux server per session on a private socket);
  Agentworks only ever pulls scrollback via `capture-pane`.
- The one existing push-style precedent is the Codex `notify` recorder
  (`plugins/codex/recorder.py`): the harness invokes an Agentworks-provisioned script with a
  structured JSON payload per turn, which today extracts a single thread id and discards the rest.
  This is the embryo of the "harness reports events" channel.
- The Claude integration only probes for its transcript file's existence to decide
  resume-versus-launch; nothing reads transcript content yet.

## Open SDD ledger (pre-roadmap efforts)

- Release-spanning, unblocked now that 0.13.0 shipped: `2026-08-03-harness-integration` (0.14
  removal phase plus closeout) and `2026-08-04-session-resume` (0.14 removal phase plus closeout).
  Both are discharged by wave 1.
- Operationally complete, needing verification and `locked.md` only: `2026-03-29-proxmox-provider`
  (20/20 checked) and `2026-05-03-session-enhancements` (69/69 checked).
- `2026-03-26-mise-integration` shows 0/30 checked but the implementation demonstrably landed
  (`vms/initializer/mise.py`, `sources.py`, config fields). Its checkboxes need evidence-based
  reconciliation before locking.
- Unmerged drafts on remote branches: `2026-07-29-herdr-integration` (explicitly not scheduled,
  gated on a spike, but unbundling two standalone wins) and
  `2026-07-19-named-console-template-selector` (small, mechanical, ready). The
  `2026-07-29-harness-transcripts` draft is harvested into `inputs/harness-transcripts-harvest.md`
  and its branch is deleted once the harvest lands on `main` (operator ruling, 2026-08-05).

## Environment notes

- Copilot's automated PR review is currently failing on monthly quota exhaustion (observed
  2026-08-05), so per the development process the fresh-eyes generic pass is substituted with a
  local reviewer until quota resets.
