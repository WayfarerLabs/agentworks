# Child SDDs

- Status: Active ledger
- Last updated: 2026-08-05

This is the roadmap's tracking document, the analog of an ordinary SDD's `plan.md`. Completed
checkboxes are immutable records, per the standard rule. The roadmap SDD locks when every entry here
is locked.

## Ledger

### Wave 1: 2026-08-05-deprecation-removal

- [x] Seeded (FRD, PR #401, reviewed with findings applied)
- [ ] FRD merged to `main`
- [ ] Picked up by an effort lead (HLA, plan)
- [ ] Implementation complete, pre-roadmap SDD closeouts done (see `current-state.md` ledger)
- [ ] Ships in 0.14.0 with phase 1
- [ ] Locked

### Wave 2 (adopted child): 2026-07-31-declarative-schema

- [x] Phase 1 (TOML sunset) merged to `main` (PR #316, 2026-08-05)
- [ ] Phase 2 released from its hold (requires descriptor contract from the design track and wave 1
      removals)
- [ ] Phase 2 implementation complete (absorbs generic-discriminator removal and the migrator's
      fate)
- [ ] Locked

### Design track: capability-kind descriptor contract

- [ ] Contract artifact drafted (roadmap lead, in progress)
- [ ] Reviewed and merged to `main`
- [ ] Consumed by wave 2 seeding

### Not yet spawned

Planned children, seeded when their prerequisites land (see `phasing.md`):

- Wave 3: secret-source instances
- Wave 4: harness facet framework
- Wave 5: session observability phase 1
- Wave 6: agentic artifacts and distillation
- Wave 7: structured control (and the herdr revisit, if its spike passes)
- Wave 8: external plugin API
- Operator-experience track efforts (onboarding plugin, discovery surfaces)
- Continuous lane: 2026-07-19-named-console-template-selector (drafted pre-roadmap, ready to
  schedule)

## Issue intake (2026-08-05)

A sweep of the open issue tracker pulled the following issues into the roadmap. Each gets a comment
pointing back here so it is not worked out-of-band (comments pending a PAT with issue-write access);
issues not listed stay standalone.

| Issue      | Lands in                                                                           |
| ---------- | ---------------------------------------------------------------------------------- |
| #76        | Wave 1 SDD closeouts (stale lockfile half only; the derive audit stays standalone) |
| #165       | Continuous lane: the named-console-template selector SDD                           |
| #170       | Wave 2 rider (expires metadata belongs in the envelope/metadata modeling)          |
| #205, #212 | Design track: instance-state store and per-instance specs; future living-graph SDD |
| #214       | Wave 2 (live samples, uniform validation; unknown keys go hard-error there)        |
| #242       | Waves 4 and 5 (harness-owned adoption plus resume re-pointing)                     |
| #257       | Operator-experience track (machine-readable output contract)                       |
| #311       | Wave 2 (structural reference extraction from annotated models)                     |
| #370       | Wave 3 (resolution-API evolution owns the batching question)                       |
| #373       | Wave 4 (environment-appropriate defaults belong to facet config)                   |
| #374       | Design track descriptor plus wave 3 (capability mandate; see `target-state.md`)    |
| #387       | Waves 4 and 6 (workspace facets and features own post-clone setup)                 |
| #390, #391 | Operator-experience track (schema-derived samples and plan A onboarding)           |

Notes: #242 also picks up the 0.14 rename (it lands as `session resume --update-template`); the
capability-contract sibling cluster #368 through #374 splits, with #370/#373/#374 rolled in and the
platform-specific siblings staying standalone; #362 (codex enhancements) stays standalone as an
open-ended research placeholder.

## Immediate next actions

1. Done (2026-08-05): PR #316 merged; wave 0 complete.
2. Merge the roadmap PR (#400) and the wave 1 seed FRD (#401); then delete the harvested
   `feat/harness-transcripts-sdd` branch.
3. Post the held issue-intake comments once the PAT gains issue-write access.
4. Finish the design track's first artifact: the capability-kind descriptor contract.
5. Launch an effort lead to pick up the wave 1 SDD from `main`.
