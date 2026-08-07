# Current State

- Snapshot date: 2026-08-07, post-wave-2 (update at wave boundaries)
- Baseline: Agentworks 0.13.0 plus the phase 1 TOML sunset (PR #316), the 0.14 expired-compat
  removals (PR #406), and declarative-schema phase 2 through the descriptor (PR #414); the 0.14.0
  release itself is pending on the guide first slice and the installer-plugins child

This document records where the system actually is, verified by code reconnaissance rather than
assumed from the perspectives. It is the ground truth the phasing rests on; when a wave lands,
update the affected section and the snapshot date, in place (git history is the append-only record).
The immutable origin snapshot is `starting-state.md`; the journey is the diff from there to here to
`target-state.md`.

## Declarative schema

Both phases are on `main` and the `2026-07-31-declarative-schema` SDD is locked (phase 1 via PR
# 316; phase 2 via PR #414, 2026-08-07). Every schema fact is authored once in a registration-time
Pydantic model: validation, reference extraction (`agentworks/schema/`, a total two-walker split
with shared iterative traversal in `agentworks/traversal.py`), JSON Schema emission with `x-agw-ref`
markers, live samples, and `describe-kind` all derive from the models. The error bridge is the
single framing choke point. `agw resource migrate` was deleted before release per the
remediation-posture ruling; the operator path is precise hard errors plus
`docs/guides/upgrading-to-0.14.md`. Settings that name resources (`defaults.site`,
`[secret_config].backends`) are shape-checked at load and resolved once at the composition boundary
as hard errors. The config deprecation channel is kept deliberately as the warn-window carrier.
`capabilities/facets.py` was removed pending its wave 4 consumer; the `config_for(facet)` contract
stays settled in the docs and this roadmap's contracts.

## Deprecation removal targets

Cleared by wave 1 (PR #406, 2026-08-05): every in-scope expired surface is removed, including the
session restart vocabulary, the legacy harness selectors, the older configuration aliases, the
legacy VM console module, and the dead Python surfaces. The generic deprecation framework survives
with only live registrations; the remaining deprecated shape is the generic capability discriminator
compatibility, which wave 2 removes with its tagged-union hardening.

## Capability framework

- The switchboard is gone (wave 2, PR #414): one frozen, core-owned `CapabilityKindDescriptor` per
  kind in a single table is the only capability-kind enumeration, with the seven former
  hand-enumerated sites (adapter, graph kind set and readiness dispatch, registry loaders, bootstrap
  publication, snapshot/restore, decode sections) derived from it and a guard test asserting
  derivation. Registration-time conformance (contract, metadata, constructibility, operations,
  config-model contract, `contract_version`) replaced the type-and-cast seam, with atomic seating
  preserved.
- Each capability implementation registers exactly one config model; validation is one blob at a
  time against the tagged union assembled at the registration boundary, cached on its arms. The
  secret-backend constructed-singleton policy is a descriptor-carried interim exception for wave 3
  to remove. `_VMPlatformKind` moved into `capabilities/` with its siblings.
- The secret-source direction still goes with the grain: the backend/source split is specified in
  `target-state.md` and the descriptor records the open readiness-shape choice for the
  `secret-source` kind (wave 3's call).
- One recorded descriptor-contract escalation stands open for the roadmap: map-keyed
  `backend_mappings` emission needs the descriptor to record where a map-keyed capability is hosted
  (the `onepassword` trigger has fired; see the locked declarative-schema SDD).

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

Cleared by wave 1 (PR #406): all five pre-roadmap SDDs are locked (`2026-08-03-harness-integration`,
`2026-08-04-session-resume`, `2026-03-29-proxmox-provider`, `2026-05-03-session-enhancements`, and
`2026-03-26-mise-integration` with its plan reconciled against evidence). The
`2026-07-29-harness-transcripts` draft is harvested into `inputs/harness-transcripts-harvest.md` and
its branch is deleted. Remaining unmerged drafts on remote branches, both out of roadmap scope:
`2026-07-29-herdr-integration` (spike-gated) and `2026-07-19-named-console-template-selector`
(ready, standalone).

## Environment notes

- Copilot's automated PR review is currently failing on monthly quota exhaustion (observed
  2026-08-05), so per the development process the fresh-eyes generic pass is substituted with a
  local reviewer until quota resets.
