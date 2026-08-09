# Task brief: the manifesto gets one home (2026-08-09)

From the saga lead, via the branch-seeded brief mechanism. You own this branch from pickup; the saga
lead reviews; the operator merges. Delete this file before the PR goes ready.

## Charter

Operator ruling (2026-08-09): the project leans into its manifesto. Conviction-voiced content gets
exactly one home, a repository page titled Manifesto, and every other permanent doc stays
just-the-facts. Consumers choose whether to read the why.

1. Survey first: enumerate every manifesto-voiced passage in the permanent docs (start from
   `docs/why-agentworks.md`, but sweep `docs/`, the capability and schema READMEs, and
   `cli/README.md`), and report the inventory with your proposed disposition per passage before
   moving anything.
2. Rename-and-absorb: `docs/why-agentworks.md` becomes `docs/manifesto.md` (git rename so history
   follows), absorbing the manifesto-voiced passages from elsewhere; anything just-the-facts in the
   old file moves to the appropriate reference doc rather than staying.
3. Fix every inbound link and anchor (`git grep why-agentworks` across the tree, including the
   pinned deep link consumers in `cli/`).
4. Add the `concept-manifesto` guide topic through the guide-contributions mechanism, teaching
   agents the project's values through the same surface that teaches commands.
5. Add one clause to the development-principles rule's destination-not-journey paragraph naming the
   manifesto as the sanctioned destination for the voice that rule removes from reference docs, and
   regenerate rulesync outputs per the standing edit order.

## Constraints

- **Merge order**: this PR merges only after PR #453 (wave 3) lands; its branch edits
  `docs/why-agentworks.md` and must not be conflicted underneath. Build and review freely meanwhile.
- **Operator review gate**: the operator reviews the assembled manifesto text before this merges. It
  is their voice; request that review explicitly in the PR.
- **Website coordination** (updated 2026-08-09, after the website effort's Phase 4B design landed):
  the website now renders a generated `/manifesto/` page from `docs/why-agentworks.md` at build
  time, selecting content by heading path and failing closed on missing or drifted source. CI runs
  that build on every PR, so your rename PR goes red unless it updates the website's source
  selection (source path, heading paths, and the source-link allowlist under `website/`) in the same
  PR. If PR #439 has merged by the time you're ready (likely), `website/` is ordinary main-tree code
  and you make that update directly, in lockstep with the rename. If #439 is still open, deliver a
  message file to `docs/sdd/2026-08-07-website/` (per the sdd skill's message-passing convention)
  with the new source path and heading map, and hold the rename until you and the website lead agree
  on landing order.
- **No contact information** of any kind (email, social handles) is added anywhere; the operator has
  not released any for publication.
- Saga vocabulary throughout; message-signatures and Agentworks-Session trailer rules apply; the
  always-consider rules (docs, sample config, completions, SDD artifacts) apply as ever.

## Definition of done

One manifesto page holds every conviction-voiced passage; no permanent reference doc carries
manifesto voice; the guide teaches it; the principle names it; all links resolve; lint and rulesync
gates green; operator has blessed the text.

-- agw-next-steps (saga lead session)
