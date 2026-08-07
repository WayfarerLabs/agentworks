# Child SDDs

- Status: Active ledger
- Last updated: 2026-08-05

This is the roadmap's tracking document, the analog of an ordinary SDD's `plan.md`. Completed
checkboxes are immutable records, per the standard rule. The roadmap SDD locks when every entry here
is locked.

## Ledger

### Wave 1: 2026-08-05-deprecation-removal

- [x] Seeded (FRD, PR #401, reviewed with findings applied)
- [x] FRD merged to `main` (2026-08-05)
- [x] Picked up by an effort lead (HLA, plan, residual inventory; PR #406)
- [x] Implementation complete, pre-roadmap SDD closeouts done (PR #406, 2026-08-05: all five
      pre-roadmap SDDs locked, plan 46/46, docs promoted including ADR 0020)
- [ ] Ships in 0.14.0 with phase 1 (cut held for the guide first slice per operator ruling,
      2026-08-06; see `phasing.md` release mapping)
- [x] Locked (`locked.md` on `main` via PR #406)

### Wave 2 (adopted child): 2026-07-31-declarative-schema

- [x] Phase 1 (TOML sunset) merged to `main` (PR #316, 2026-08-05)
- [x] Seed notes published for the effort lead (delivered as
      `docs/sdd/2026-07-31-declarative-schema/roadmap-seed-notes.md` via new-file message passing;
      the adopted-child analog of a seed FRD)
- [x] Effort lead launched (2026-08-05, seed notes merged via PR #411)
- [ ] Phase 2 released from its hold (prerequisites landed via PRs #405 and #406; the effort lead's
      first commit records the release in that SDD's plan)
- [ ] Phase 2 implementation complete (absorbs generic-discriminator removal and the migrator's
      fate)
- [x] Five-pass roadmap review of PR #414 delivered (2026-08-07): request changes, three blockers,
      structural core verified sound
- [ ] Migrator deleted per the remediation-posture ruling (operator, 2026-08-07; see
      `target-state.md` compatibility posture): both halves of `migrate/` removed on the branch,
      error hints repointed at guide-led remediation, `locked.md` updated with a dated entry
- [ ] Surviving review findings fixed on the branch (inheritance merge guard and restored coverage,
      the schema silent-edge cluster, robustness items, SDD artifact corruption)
- [ ] Locked

### Design track: capability-kind descriptor contract

- [x] Contract artifact drafted (`capability-descriptor-contract.md`, 2026-08-05)
- [x] Reviewed and merged to `main` (PR #405, 2026-08-05)
- [x] Consumed by wave 2 seeding (seed notes delivered via PR #411; effort lead launched 2026-08-05)

### Design track: scope participation (formerly facet-model boundary)

- [x] Contract artifact drafted (`facet-boundary-contract.md`, 2026-08-05; gates the wave 4 and wave
      5 seeds)
- [x] Recast as `scope-participation-contract.md` per operator simplification (2026-08-05): setup
      pipeline, per-scope init methods, trust-based scope discipline, grants dropped, facet
      vocabulary retired
- [x] Facet returns as a plain noun (2026-08-06, operator ruling): the level a capability is driven
      at (vm, user, workspace, session), pairing methods and config; core owns the scope-to-facet
      mapping; the retired machinery meaning stays dead
- [ ] Reviewed and merged to `main`

### Onboarding and discovery (destination 1): 2026-08-05-onboarding-and-discovery

- [x] Seeded (FRD, PR #413, 2026-08-05; runs parallel to wave 2 per `phasing.md`)
- [x] FRD merged to `main` (2026-08-06)
- [ ] Picked up by an effort lead (HLA, plan)
- [ ] First-slice implementation (plugin, skills, output contract); schema-derived depth adopts wave
      2 surfaces as they land
- [ ] Locked

### Not yet spawned

Planned children, seeded when their prerequisites land (see `phasing.md`):

- Wave 3: secret-source instances
- Wave 4: harness scope framework
- Wave 5: session observability phase 1
- Wave 6: agentic artifacts and distillation
- Wave 7: structured control
- Wave 8: external plugin API
- Closeout wave: comprehensive security, test-consolidation, and cleanup review (gates the lock)

Not children of this roadmap (explicitly out of scope, see `target-state.md`): the
named-console-template selector SDD, the herdr effort, and the companion-shell and resilient-attach
wins. They proceed standalone.

## Issue intake (2026-08-05)

A sweep of the open issue tracker pulled the following issues into the roadmap. Each rolled-in issue
received a comment pointing back here so it is not worked out-of-band (posted 2026-08-05); issues
not listed stay standalone.

| Issue      | Lands in                                                                            |
| ---------- | ----------------------------------------------------------------------------------- |
| #76        | Wave 1 SDD closeouts (stale lockfile half only; the derive audit stays standalone)  |
| #165       | Not rolled in: its own pre-roadmap SDD proceeds standalone (out of roadmap scope)   |
| #170       | Wave 2 rider (expires metadata belongs in the envelope/metadata modeling)           |
| #205, #212 | Design track: instance-state store and per-instance specs; future living-graph SDD  |
| #214       | Wave 2 (live samples, uniform validation; unknown keys go hard-error there)         |
| #242       | Waves 4 and 5 (harness-owned adoption plus resume re-pointing)                      |
| #257       | Onboarding-and-discovery child (machine-readable output contract)                   |
| #311       | Wave 2 (structural reference extraction from annotated models)                      |
| #370       | Wave 3 (resolution-API evolution owns the batching question)                        |
| #373       | Wave 4 (environment-appropriate defaults belong to per-scope integration config)    |
| #374       | Design track descriptor plus wave 3 (capability mandate; see `target-state.md`)     |
| #387       | Waves 4 and 6 (workspace-scope integration hooks and features own post-clone setup) |
| #390, #391 | Onboarding-and-discovery child (samples and plan A onboarding)                      |

Notes: #242 also picks up the 0.14 rename (it lands as `session resume --update-template`); the
capability-contract sibling cluster #368 through #374 splits, with #370/#373/#374 rolled in and the
platform-specific siblings staying standalone; #362 (codex enhancements) stays standalone as an
open-ended research placeholder.

## Immediate next actions

1. Done (2026-08-05): PR #316 merged; wave 0 complete.
2. Done (2026-08-05): roadmap PR #400 and wave 1 seed #401 merged; harvested
   `feat/harness-transcripts-sdd` branch deleted.
3. Done (2026-08-05): issue-intake comments posted.
4. Done (2026-08-05): descriptor contract merged (PR #405).
5. Done (2026-08-05): wave 1 implementation, closeouts, and lock merged (PR #406). The 0.14.0
   release ships when cut.
6. Done (2026-08-05): wave 2 seed notes merged (PR #411) and effort lead launched.
7. Done (2026-08-05): onboarding-and-discovery seeded (FRD, PR #413).
8. Done (2026-08-06): onboarding seed merged (PR #413). Launch its effort lead when ready.
9. Roadmap lead reviews wave 2 and onboarding PRs as they arrive.
10. Design track: scope participation contract in review (PR #407); then the instance-state store
    schema and the event vocabulary's first slice.
