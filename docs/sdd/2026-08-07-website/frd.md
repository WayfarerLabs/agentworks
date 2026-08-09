# FRD: The agentworks.build Website

- Status: Requirements accepted; owned by the website effort lead
- Date: 2026-08-07
- Last revised: 2026-08-08
- Seeded by: the roadmap lead, at operator request. This is a standalone effort, deliberately NOT a
  child of the 2026-08-04-next-steps roadmap (see that roadmap's `target-state.md` out-of-scope
  section for the recorded relationship). It follows the ordinary SDD process: the effort lead owns
  the HLA and plan; the roadmap lead reviews PRs.

## Purpose

Agentworks gets a public front door at `agentworks.build` (domain purchased 2026-08-07): the place a
curious human or their agent lands first, learns what Agentworks is, and leaves with the one
copy-paste block that starts onboarding.

The operator's sizing mandate is the controlling constraint: **super simple at first**. The first
slice is a small static site, its publishing pipeline, and one bounded interactive surprise on the
otherwise useful 404 page. Every ambition beyond that is recorded as a growth path so nothing
forecloses it, and none of it is in scope now.

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
- R6. The site uses the selected custom AGW rocket mark: symmetric custom A, G, and W geometry in a
  neutral graphite, with the original twin layered flame treatment. The mark remains legible as AGW
  and as a rocket without relying on an installed font.
- R7. A custom 404 page remains a useful error page with a clear path home and progressively
  enhances into a hidden lunar deployment game. Before activation it shows the hovering AGW lander
  without visual control hints; only a brief, subtle twin-plume cue suggests that it is interactive.
  Space starts the game. A deliberate tap or activation on the lander is the touch and assistive
  technology equivalent.
- R8. During flight, Space or Up fires both engines; Left or `h` increases right-engine thrust to
  turn left; Right or `l` increases left-engine thrust to turn right. The visible twin plumes track
  collective and differential thrust. On touch, a tap produces a short collective-thrust pulse,
  press-and-hold sustains it, and a left or right drag biases the opposite engine while pressed. A
  safe landing left of a small dark NOC cluster deploys a small agent from the G opening. The agent
  enters the NOC, which powers up and remains visibly active while the lander departs. The sequence
  concludes with the exact status `Agent deployed. Mission continues.`
- R9. The 404 content and route home work without JavaScript. The game has no audio, telemetry,
  network request, storage, or critical content; it pauses when inactive, can be exited, and honors
  reduced motion for all nonessential animation. Powered-NOC state lasts only for the current run.

## Settled constraints (inherited; do not reopen)

- C1. **No lockstep twins.** Prose that already lives in the repository (the README's problem
  statement and principles, the bootstrap block) is sourced from or verified against the repository
  copy, not re-authored into a second hand-maintained version. The mechanism is the effort lead's
  call (build-time include, CI check, or generation), but two independently edited copies of the
  bootstrap block is a rejected outcome.
- C2. **Simplicity mandate** (operator, 2026-08-07). Choose the smallest tech that meets R1-R9; a
  static generator or plain HTML both qualify. Anything requiring a running service does not. The
  `development-principles` rule's bad-complexity test applies to the stack choice itself.
- C3. The site never becomes a second source of truth for product behavior. Reference and teaching
  content, when it eventually arrives (growth path), renders from the same authoritative sources as
  `agw guide` and the reference surfaces.
- C4. The logo and lander are local SVG and plain JavaScript, not a reason to add a framework,
  package ecosystem, canvas renderer, remote asset, or general game engine.

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
- AC5. A missing URL serves a semantic 404 with a visible home link before scripts run; no control
  instructions are visually disclosed until deliberate activation starts the game.
- AC6. Space starts the game from the initial 404 state when focus is not inside another control.
  Starting moves focus to the game scene. Arrow and vi controls produce the specified collective and
  differential thrust; tap, hold, and horizontal drag provide equivalent touch control; and visible
  plume length reflects the commanded engine thrust.
- AC7. A safe, upright touchdown left of the NOC completes the agent exit, NOC power-up, and lander
  departure sequence; the powered NOC remains visibly changed for the rest of the run; and the exact
  success status is exposed. An unsafe touchdown has a distinct non-destructive failure state and
  can restart.
- AC8. Automated and browser acceptance cover state transitions, input mapping, consistent
  fixed-step physics across representative frame schedules, route-home fallback, hidden-until-start
  instructions, reduced motion, keyboard focus, narrow screens, and paused background behavior.

## Settled implementation rulings

- GitHub Pages hosts the site, with an operator-coordinated GoDaddy DNS cutover.
- A standard-library Python builder produces the static artifact, and CI runs the README identity
  check.
- The first slice is one content page plus the host-required custom 404 error surface.
