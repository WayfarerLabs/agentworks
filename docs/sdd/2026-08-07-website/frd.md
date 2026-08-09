# FRD: The agentworks.build Website

- Status: Interim implementation complete; release acceptance in progress
- Date: 2026-08-07
- Last revised: 2026-08-09
- Seeded by: the saga lead, at operator request. This is a standalone effort, deliberately NOT a
  child of the 2026-08-04-next-steps saga (see that saga's `target-state.md` out-of-scope section
  for the recorded relationship). It follows the ordinary SDD process: the effort lead owns the HLA
  and plan; the saga lead reviews PRs.

## Purpose

Agentworks gets a public front door at `agentworks.build` (domain purchased 2026-08-07): the place a
curious human or their agent lands first and learns what Agentworks is. In the completed site, they
leave with the one copy-paste block that starts onboarding. Until that contract is ready, the public
site gives them useful product/security depth, permanent project links, and an honest availability
notice.

The operator's sizing mandate is the controlling constraint: **super simple at first**. The first
slice is a small static site, its publishing pipeline, and one bounded interactive surprise on the
otherwise useful 404 page. It may ship in two complete stages: a useful public landing page while
the onboarding contract is still in development, followed by the canonical bootstrap as soon as that
contract lands. Every ambition beyond that is recorded as a growth path so nothing forecloses it,
and none of it is in scope now.

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
- R10. Before the onboarding effort's canonical bootstrap source lands, an **interim public
  release** may serve the complete site shell, repository-sourced problem and principle content,
  selected brand, permanent links, custom 404, deployment pipeline, and custom domain. It states
  plainly that guided onboarding is not yet published and provides no substitute installation
  command, bootstrap text, disabled copy affordance, or implication that onboarding is available.
  The later bootstrap integration replaces this bounded notice; it does not require a redesign or a
  second site.
- R11. The main page includes the restrained link text `We take security seriously.` as optional
  depth, not a warning gate or dominant call to action. It leads to a dedicated static security page
  that explains the actual threat model, isolation and control model, material limitations,
  credential/secret posture, and vulnerability-reporting path from repository-owned sources. The
  page is candid and specific without assuming every visitor wants a security lecture.
- R12. The completed onboarding surface makes the access tradeoff plain before setup: the onboarding
  agent runs on the machine the operator intends to use as their workstation and needs full file
  inspection and command execution access with the permissions of the workstation account running
  the harness. This does not grant root implicitly; privilege elevation remains a separate, explicit
  action. It recommends a strict harness security posture for approval and visibility, not a sandbox
  that prevents the access onboarding needs. This language remains owned by onboarding's canonical
  bootstrap/disclosure contract; the website must consume or link that source, never paraphrase it
  into a drifting second copy.
- R13. The visual language hints at terminal and TUI paradigms while remaining a modern website:
  monospaced accents, crisp bounded regions, compact status-like details, strong hierarchy, and
  efficient use of space should communicate `simple but powerful`. It must not become a fake
  terminal, green-on-black pastiche, command-line prerequisite, or excuse to weaken reflow,
  readability, pointer use, or accessibility.
- R14. The landing page is a deliberately compact single-page product handoff, not a rendered
  manifesto. It shows the selected rocket mark as a dominant hero element at two to three times its
  original header scale, gives only the concise product identity and onboarding availability, and
  offers one link each to the repository, package, deeper rationale, and security deep dive. A
  destination must not be repeated in the landing page's header, body, and footer. Visitors who want
  the longer problem statement or principles follow the single Manifesto link to a page generated
  from their permanent repository source. The dedicated Manifesto, security, and host-required 404
  pages remain separate optional/error surfaces rather than being folded into the landing page.
- R15. Navigation follows familiar page conventions without reintroducing duplicate destinations.
  Home, Manifesto, Security, and 404 use the same responsive header structure: a breadcrumb at the
  upper left and one GitHub and one PyPI call to action at the upper right. Each external call to
  action pairs its visible text with a local decorative service icon and remains understandable
  without the icon. The breadcrumb contains one `Agentworks` link to the home route, a visual
  separator, and a non-linked current-page label (`Home`, `Manifesto`, `Security`, or `404`) marked
  as current. The home header omits the small rocket because the large mark immediately follows as
  the hero; every other page places the small rocket immediately left of its breadcrumb. On the 404,
  the breadcrumb's `Agentworks` link is the sole visible route-home action. A shared traditional
  footer places the exact text `Product of Wayfarer Labs, LLC` at the left and one
  `Agentworks Manifesto` and one `We take security seriously` link at the right. These placements
  supersede the Phase 4A combined exploration panel while preserving one link per external,
  manifesto, and security destination.
- R16. The footer's `Agentworks Manifesto` link opens a semantic static page at `/manifesto/`, not
  the repository document. The page renders the long-form argument from the canonical
  `docs/why-agentworks.md` source at build time, including its problem-space and key-principles
  structure, without a separately maintained site copy. Relative source links are deliberately
  mapped to their permanent repository destinations. Missing, duplicate, unsupported, or drifted
  canonical content fails the build before output replacement. The permanent source may be renamed
  from `Why Agentworks` to `Agentworks Manifesto`; the website contract follows the reviewed source
  change rather than maintaining a conflicting title.

## Settled constraints (inherited; do not reopen)

- C1. **No lockstep twins.** Prose that already lives in the repository (the README's problem
  statement and principles, the bootstrap block) is sourced from or verified against the repository
  copy, not re-authored into a second hand-maintained version. The mechanism is the effort lead's
  call (build-time include, CI check, or generation), but two independently edited copies of the
  bootstrap block is a rejected outcome.
- C2. **Simplicity mandate** (operator, 2026-08-07). Choose the smallest tech that meets R1-R13; a
  static generator or plain HTML both qualify. Anything requiring a running service does not. The
  `development-principles` rule's bad-complexity test applies to the stack choice itself.
- C3. The site never becomes a second source of truth for product behavior. Reference and teaching
  content, when it eventually arrives (growth path), renders from the same authoritative sources as
  `agw guide` and the reference surfaces.
- C4. The logo and lander are local SVG and plain JavaScript, not a reason to add a framework,
  package ecosystem, canvas renderer, remote asset, or general game engine.
- C5. The interim release is a real, useful release, not a preview mode or a parallel product. It
  introduces no runtime flag, alternate URL, duplicated product prose, speculative onboarding
  contract, or permanent staging architecture. Each public stage must be honest and operable on its
  own terms.

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
- AC9. Before onboarding is available, `https://agentworks.build` serves the useful interim release
  described by R10 over TLS. The page contains no bootstrap code region, copy control, installation
  instruction, empty onboarding container, or unexpanded template token, and its availability notice
  is exposed in ordinary semantic markup.
- AC10. The interim release satisfies AC1, AC2, and AC5-AC8 independently. AC3 and AC4 remain
  explicitly unaccepted until the canonical bootstrap is integrated; replacing the interim notice
  with that bootstrap leaves the established information architecture, visual system, URLs, 404, and
  deployment path intact.
- AC11. The home-page security link is visible but visually secondary, works without JavaScript, and
  resolves to a semantic security page at a stable URL. That page distinguishes claims, boundaries,
  current limitations, operator practices, and private vulnerability reporting; every
  product/security claim is sourced from or verified against permanent repository documentation.
- AC12. Before the onboarding integration is accepted, tooling or a pinned contract test proves the
  canonical disclosure explicitly covers the intended-workstation requirement, full
  workstation-account file/command access, separately explicit elevation, and strict-posture
  recommendation. The interim release does not invent or imply that disclosure while the upstream
  contract is absent.
- AC13. The landing, Manifesto, security, and 404 surfaces share a restrained terminal/TUI-derived
  visual system at desktop and narrow widths. Text remains real semantic content, ordinary links and
  controls remain recognizable, and the design meets the existing contrast, focus, zoom, reflow,
  reduced-motion, keyboard, and touch requirements without depending on terminal familiarity.
- AC14. In a clean-context interim-release check, a visitor with no prior Agentworks knowledge can
  understand what the project is, recognize that guided onboarding is not yet published, and choose
  the repository, package, rationale, or security path without explanation. This is the interim
  usefulness bar; it does not claim AC4's completed onboarding handoff.
- AC15. The landing page contains exactly one navigable anchor for each of the GitHub repository,
  PyPI package, deeper rationale, and security destinations; it contains no rendered problem-space
  or principles section. The selected rocket is a prominent hero element without displacing the page
  identity, availability notice, or four destinations at 320 CSS pixels or 400 percent zoom.
- AC16. Generated Home, Manifesto, Security, and 404 documents expose the shared responsive header
  and footer landmarks with the exact per-page breadcrumb current item. Home has no small header
  mark; every other page has exactly one small header mark, and 404 has no separate body home link.
  GitHub and PyPI occur once per page in the header with visible text and hidden decorative icons.
  Manifesto and Security occur once per page in the footer, beside the exact Wayfarer Labs ownership
  text. Link purposes, keyboard focus, accessible names, source order, narrow-screen wrapping, and
  400-percent zoom stay useful with images or CSS unavailable.
- AC17. `/manifesto/` renders the canonical `docs/why-agentworks.md` long-form introduction, problem
  space, and key principles as semantic headings, paragraphs, and lists. Its generated content and
  mapped links are verified against the permanent source, contain no unexpanded source-relative URL,
  and remain useful without CSS or JavaScript. Changing a selected canonical passage without
  updating its reviewed build contract fails closed.

## Settled implementation rulings

- GitHub Pages hosts the site, with an operator-coordinated GoDaddy DNS cutover.
- A standard-library Python builder produces the static artifact, and CI runs the README identity
  check.
- The first slice is a landing page, one security deep-dive page, and the host-required custom 404
  error surface.
- The first public release intentionally omits onboarding rather than blocking the rest of the site;
  canonical onboarding follows as a separately reviewed additive release.
