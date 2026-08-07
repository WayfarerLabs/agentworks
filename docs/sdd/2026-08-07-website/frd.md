# FRD: The agentworks.build Website

- Status: Seed, ready for an effort lead
- Date: 2026-08-07
- Seeded by: the roadmap lead, at operator request. This is a standalone effort, deliberately NOT a
  child of the 2026-08-04-next-steps roadmap (see that roadmap's `target-state.md` out-of-scope
  section for the recorded relationship). It follows the ordinary SDD process: the effort lead owns
  the HLA and plan; the roadmap lead reviews PRs.

## Purpose

Agentworks gets a public front door at `agentworks.build` (domain purchased 2026-08-07): the place a
curious human or their agent lands first, learns what Agentworks is, and leaves with the one
copy-paste block that starts onboarding.

The operator's sizing mandate is the controlling constraint: **super simple at first**. The first
slice is a small static site and the pipeline that publishes it. Every ambition beyond that is
recorded as a growth path so nothing forecloses it, and none of it is in scope now.

## Requirements (first slice)

- R1. A static site served at `https://agentworks.build`: what Agentworks is (problem, principles),
  the agent-addressed bootstrap block, and links to the GitHub repository and the PyPI package.
  Content fits on one page or very few; design is clean and minimal.
- R2. The bootstrap block is the centerpiece, presented for its real consumer: an operator will
  paste it to their agent, so it must be trivially copyable and byte-identical to the block the
  repository README carries.
- R3. An integrated deployment pipeline: the site deploys automatically when its source merges to
  `main`, with no manual publish step. The site source and pipeline live in this repository unless
  the effort lead makes a recorded case otherwise.
- R4. Static only. No backend, no accounts, no data collection beyond whatever minimal analytics the
  operator explicitly approves (none is the default).
- R5. The site serves humans and agents alike, consistent with destination 1's spirit: content is
  legible markup, not image-baked text, and the bootstrap block is machine-copyable.

## Settled constraints (inherited; do not reopen)

- C1. **No lockstep twins.** Prose that already lives in the repository (the README's problem
  statement and principles, the bootstrap block) is sourced from or verified against the repository
  copy, not re-authored into a second hand-maintained version. The mechanism is the effort lead's
  call (build-time include, CI check, or generation), but two independently edited copies of the
  bootstrap block is a rejected outcome.
- C2. **Simplicity mandate** (operator, 2026-08-07). Choose the smallest tech that meets R1-R5; a
  static generator or plain HTML both qualify. Anything requiring a running service does not. The
  `development-principles` rule's bad-complexity test applies to the stack choice itself.
- C3. The site never becomes a second source of truth for product behavior. Reference and teaching
  content, when it eventually arrives (growth path), renders from the same authoritative sources as
  `agw guide` and the reference surfaces.

## Growth path (recorded, explicitly out of scope now)

- Rendering guide topics on the web from the same topic-content contract `agw guide` consumes,
  making the site the second consumer that proves the contract's universality.
- Schema-derived reference documentation from the emission surfaces.
- Release notes and changelog surfacing.

Each of these waits until its upstream surface (the guide topic contract, schema emission) has
merged and settled on `main`. The first slice must not build toward them speculatively (C2).

## Acceptance (first slice)

- AC1. `https://agentworks.build` serves the site over TLS.
- AC2. A change to the site source merged to `main` is live without manual steps.
- AC3. The bootstrap block on the site and in the README are verified identical by tooling, not by
  discipline.
- AC4. An operator who has never heard of Agentworks can land, understand what it is, and hand their
  agent the bootstrap block in under a minute.

## Open questions for the effort lead

- Hosting and DNS mechanics (the operator holds the domain; coordinate credentials/setup needs
  through the operator).
- Stack choice under C2, and where the README-identity check (AC3) runs.
- Whether the first slice is one page or a very small few.
