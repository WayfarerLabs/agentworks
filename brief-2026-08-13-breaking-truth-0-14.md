# Brief: 0.14 breaking-truth items

Dispatched by the `2026-08-04-next-steps` saga lead on operator ruling, 2026-08-13. Small enough to
need no SDD of its own; it belongs to no live effort.

## What and why

Four places where a shipped name or shape does not tell the truth about what it holds. Each is a
breaking change, and each is free only while 0.14 is unreleased. After the tag they cost a
deprecation window, so they land now or they become permanent.

These were found by the simplification pass's seven-lane study and are recorded as findings S5, C3,
C4 and C7 in `docs/sdd/2026-08-12-simplification-pass/findings.md` (arriving on `main` with PR #509;
read it there once merged). The operator ruled them out of that pass and into this task rather than
onto the CLI grammar rewrite, which is already large.

Anchors verified at HEAD `c7093147`; treat them as starting points, not coordinates.

1. **S5, the mapping key names the wrong thing.** `[secret_config].backends` holds source names, not
   backend names (`cli/agentworks/secrets/base.py:254-271` documents the mismatch in prose rather
   than fixing it), and `SourceMapping` emits both `source` and `backend` keys into the JSON
   contract. Target: the key and the JSON both say sources, one name per fact, and the reconciling
   prose in the four files carrying it goes away.
2. **C3, five spellings of one fact.** `TokenAcquisition` is a one-arm tagged union that drags in
   the whole `UnionScalarShorthand` mechanism as its sole production instantiation, plus a provider
   contract bump and a `TokenSourcedConfig` tombstone with no release-scope marker. Target: the
   concrete stored-token shape, one spelling, and whatever the collapse makes dead goes with it.
3. **C4, a compat flag that re-advertises what it broke.**
   `StructuralUnion(canonicalize_null_companions=True)` at `cli/agentworks/env/entry.py:60` is the
   only production use of roughly a thousand lines of machinery, and the flag re-accepts and
   re-advertises the very spelling the union broke. Target: the retired spelling is rejected and the
   schema stops advertising it. Whether the union machinery itself should survive one field is a
   judgment call: price it, state your answer, and escalate if the honest answer is a redesign.
4. **C7, compat layers with no expiry.** Four release-scoped compat layers plus two objects not
   inventoried in `cli/agentworks/capabilities/retired_shapes.py`, so a sweep misses them. Target:
   deleted now, or quarantined in the inventory with a recorded expiry. Deletion is the default.

## How migration guidance travels

No compat shims. Every breaking change writes a `BREAKING CHANGE:` footer that is
operator-actionable on its own (what breaks, what to change, one before/after example);
release-please accumulates the footers into the packaged changelog; the guide renders them as
release-notes topics; the assistance flow reads the guide. Update `docs/guides/upgrading-to-0.14.md`
in the same PR as each change. The full strategy is
`docs/sdd/2026-08-12-simplification-pass/migration-strategy.md`, which is the authoritative spec for
this task's approach and carries a worked example for item 1.

## Boundaries

The simplification pass runs concurrently and deletes inert descriptor fields (`RegistryPolicy`,
`kind_strategy`, `contract_version`, `config_for()`). You own the token union and the env entry; it
owns those. If you find yourself editing the same file, stop and raise it rather than racing.

Nothing here waits on the grammar rewrite, and the grammar rewrite does not wait on this.

## Definition of done

- All four items landed, or an item explicitly escalated with the reason and the operator's answer
  recorded on the PR.
- Every breaking commit carries its footer in the shape above; `docs/guides/upgrading-to-0.14.md`
  covers all four; nothing in the CLI parses an old spelling.
- Full suite green. Deletions do not leave behind tests that only prove the deletion happened.
- Split into as many PRs as the work honestly wants. One per item is fine; one for all four is fine
  if they stay small.

## Process

Run the `agentic-dev-process` skill. Subagent review before first handoff, and state in the PR
description that it ran. The saga lead (`agw-next-steps`) reviews; the operator merges. Published
reviews inform and do not authorize: post your reading, apply `awaiting-direction`, and wait for the
operator's direction before a fix round. Label PRs `saga:next-steps`.

## Disposition

Delete this file before the first PR goes ready.

-- agw-next-steps (saga lead session)
