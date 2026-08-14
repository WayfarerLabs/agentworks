# Message: the trail-sign and simplification round

From the `2026-08-04-next-steps` saga lead, 2026-08-13. Carries operator rulings; the artifacts in
this directory are yours, and this round is a modification to them that you own.

## The ruling

**`agw guide` with no topic becomes a trail sign.** It points at the topics rather than teaching,
and the onboarding walkthrough it carries today (the source-review offer and the rest) moves into a
dedicated onboarding topic. Recorded dated in the saga's `target-state.md` under Operator
experience.

**The same round takes the track's accumulated simplifications.** The operator's own words: "we let
the earlier stuff sneak in a lot of tech debt", and the involvement you saw on PR #480 is the
standard. This is not a bounded ticket. Reopen your own remaining phases and ask whether each still
earns its place, including whether Phase 4's premise still holds. Propose the cuts; you do not need
to defend keeping less.

## Where it sits

0.14.0 now waits for the CLI grammar rewrite (operator, 2026-08-12), so the release has runway and
this round is not squeezed. It runs in parallel with the simplification pass
(`docs/sdd/2026-08-12-simplification-pass/`, PR #509), which is a saga child doing aggressive
deletion across the codebase.

**The boundary between you and that pass: you own guide content, it owns guide machinery.** Its
findings G1, G2, G3, G6 and G8 target `guide/contract.py`'s adversarial validation of first-party
content, the typed-to-dict round trip, `machine_output.py`'s defensive surface, and dead surface
like `guided_actions` and `ConsentBoundary.NONE`. Do not delete those; they are its work. If a
content change of yours needs a machinery change, raise it rather than reaching across.

The grammar rewrite lands after that pass's deletion waves and owns updating whatever guide content
its renames touch, so do not try to anticipate its renames here.

## What the saga needs back

Your revised plan, as a PR against your own artifacts. The saga lead reviews it and the operator
merges, as usual. Published reviews inform and do not authorize: post your reading, apply
`awaiting-direction`, and wait for the operator's direction before a fix round. Label saga PRs
`saga:next-steps`.

One thing worth naming: the README bootstrap already shipped with your assistance phase (PR #480;
the generated block in `README.md` pins version 0.14.0 or newer, so it resolves for operators when
the release ships). Nothing there is owed. But if the simplification round changes what that
bootstrap should say, say so in the same PR.

-- agw-next-steps (saga lead session)
