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
- [ ] Ships in 0.14.0 with phase 1 (release pending)
- [x] Locked (`locked.md` on `main` via PR #406)

### Wave 2 (adopted child): 2026-07-31-declarative-schema

- [x] Phase 1 (TOML sunset) merged to `main` (PR #316, 2026-08-05)
- [x] Seed notes published for the effort lead (delivered as
      `docs/sdd/2026-07-31-declarative-schema/roadmap-seed-notes.md` via new-file message passing;
      the adopted-child analog of a seed FRD)
- [ ] Phase 2 released from its hold (prerequisites landed via PRs #405 and #406; the effort lead's
      first commit records the release in that SDD's plan)
- [ ] Phase 2 implementation complete (absorbs generic-discriminator removal and the migrator's
      fate)
- [ ] Locked

### Design track: capability-kind descriptor contract

- [x] Contract artifact drafted (`capability-descriptor-contract.md`, 2026-08-05)
- [x] Reviewed and merged to `main` (PR #405, 2026-08-05)
- [ ] Consumed by wave 2 seeding (wave 2 releases from its hold once wave 1's removals land)

### Design track: facet-model boundary

- [x] Contract artifact drafted (`facet-boundary-contract.md`, 2026-08-05; gates the wave 4 and wave
      5 seeds)
- [ ] Reviewed and merged to `main`

### Not yet spawned

Planned children, seeded when their prerequisites land (see `phasing.md`):

- Onboarding and discovery (destination 1): seeds at wave 1 completion and runs parallel to wave 2.
  First slice needs no schema emission (onboarding harness plugin and skills, consent-first probing,
  the machine-readable output contract); the schema-derived depth follows wave 2's emission as it
  lands. Slotted this early deliberately: it teaches the post-cleanup 0.14 surface, so nothing it
  ships gets reworked by wave 1, and waiting longer just delays destination 1.
- Wave 3: secret-source instances
- Wave 4: harness facet framework
- Wave 5: session observability phase 1
- Wave 6: agentic artifacts and distillation
- Wave 7: structured control
- Wave 8: external plugin API

Not children of this roadmap (explicitly out of scope, see `target-state.md`): the
named-console-template selector SDD, the herdr effort, and the companion-shell and resilient-attach
wins. They proceed standalone.

## Issue intake (2026-08-05)

A sweep of the open issue tracker pulled the following issues into the roadmap. Each rolled-in issue
received a comment pointing back here so it is not worked out-of-band (posted 2026-08-05); issues
not listed stay standalone.

| Issue      | Lands in                                                                           |
| ---------- | ---------------------------------------------------------------------------------- |
| #76        | Wave 1 SDD closeouts (stale lockfile half only; the derive audit stays standalone) |
| #165       | Not rolled in: its own pre-roadmap SDD proceeds standalone (out of roadmap scope)  |
| #170       | Wave 2 rider (expires metadata belongs in the envelope/metadata modeling)          |
| #205, #212 | Design track: instance-state store and per-instance specs; future living-graph SDD |
| #214       | Wave 2 (live samples, uniform validation; unknown keys go hard-error there)        |
| #242       | Waves 4 and 5 (harness-owned adoption plus resume re-pointing)                     |
| #257       | Onboarding-and-discovery child (machine-readable output contract)                  |
| #311       | Wave 2 (structural reference extraction from annotated models)                     |
| #370       | Wave 3 (resolution-API evolution owns the batching question)                       |
| #373       | Wave 4 (environment-appropriate defaults belong to facet config)                   |
| #374       | Design track descriptor plus wave 3 (capability mandate; see `target-state.md`)    |
| #387       | Waves 4 and 6 (workspace facets and features own post-clone setup)                 |
| #390, #391 | Onboarding-and-discovery child (samples and plan A onboarding)                     |

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
6. Wave 2 is unblocked (descriptor merged plus removals landed): launch an effort lead to pick up
   `2026-07-31-declarative-schema` phase 2 from `main`; its first act records the hold release in
   that SDD's plan, citing `capability-descriptor-contract.md`.
7. Onboarding-and-discovery child is now ready to seed (wave 1 complete): roadmap lead drafts its
   seed FRD next.
8. Design track: facet boundary contract in review; then the instance-state store schema and the
   event vocabulary's first slice.
