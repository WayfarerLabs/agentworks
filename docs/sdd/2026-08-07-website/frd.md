# FRD: The agentworks.build Website

- Status: Interim implementation complete; continuous Lander Phase 4J operator acceptance in
  progress
- Date: 2026-08-07
- Last revised: 2026-08-11
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
slice is a small static site, its publishing pipeline, and one optional interactive surprise shared
by a dedicated Lander page and the otherwise useful 404 page. The operator later selected the
continuous expedition in R21-R23 as the bounded form of that surprise; it adds no product surface,
backend, framework, persistence, telemetry, or activation dependency to PR #439. The site may ship
in two complete stages: a useful public landing page while the onboarding contract is still in
development, followed by the canonical bootstrap as soon as that contract lands. Every ambition
beyond the accepted static site and continuous surprise is recorded as a growth path so nothing
forecloses it, and none of it is in scope now.

## Requirements (first slice)

- R1. A static site served at `https://agentworks.build`: what Agentworks is (problem, principles),
  the agent-addressed bootstrap block, and links to the GitHub repository and the PyPI package.
  Content fits on one page or very few; design is clean and minimal.
- R2. The bootstrap block is the centerpiece, presented for its real consumer: an operator will
  paste it to their agent, so it must be trivially copyable and byte-identical to the block the
  repository README carries.
- R3. An integrated deployment pipeline: routine site-source merges to `main` deploy automatically,
  with no manual publish step. The one-time default-host to custom-domain base-path transition uses
  an explicitly invoked deployment of the same `main` commit after the custom domain is attached and
  before DNS changes, because a Pages settings change does not trigger a build. That restricted
  activation is setup, not a second publishing path. The site source and pipeline live in this
  repository unless the effort lead makes a recorded case otherwise.
- R4. Static only. No backend, no accounts, no data collection beyond whatever minimal analytics the
  operator explicitly approves (none is the default).
- R5. The site serves humans and agents alike, consistent with destination 1's spirit: content is
  legible markup, not image-baked text, and the bootstrap block is machine-copyable.
- R6. The site uses the selected custom AGW rocket mark: symmetric custom A, G, and W geometry in a
  neutral graphite, with the original twin layered flame treatment. The mark remains legible as AGW
  and as a rocket without relying on an installed font.
- R7. A custom 404 page remains a useful error page with a clear path home and progressively
  enhances into a hidden lunar deployment game. The same game is also available deliberately at
  `/lander/`; both routes render one shared game subtree and use the same CSS, controller, model,
  state, controls, and accessibility behavior rather than maintaining parallel implementations.
  Before activation each shows the hovering AGW lander without visual control hints; only a brief,
  subtle twin-plume cue suggests that it is interactive. Space starts the game. A deliberate tap or
  activation on the lander is the touch and assistive technology equivalent.
- R8. During flight, Space or Up fires both engines; Left or `h` increases right-engine thrust to
  turn left; Right or `l` increases left-engine thrust to turn right. The visible twin plumes track
  collective and differential thrust. On touch, a tap produces a short collective-thrust pulse,
  press-and-hold sustains it, and a left or right drag biases the opposite engine while pressed. The
  initial tuning target is approximately twice the original per-engine translational authority
  (nominally `8.4` rather than `4.2` in model units), so a player can brake materially later without
  changing the fixed-step clock or simply doubling gravity. Differential input also vectors the
  combined thrust toward the commanded turn while materially reducing its axial component. Full
  steering with collective produces no more than approximately half of straight collective's axial
  thrust, and turn-only input does not overcome gravity. A light, deterministic flight-control
  assist counters residual rotation through visibly differential, fuel-consuming main-engine thrust
  when collective remains engaged; engine-off coasting and ballistic crash fragments remain undamped
  in vacuum. The LLD may pin nearby browser-tested authority, vector, and assist values when
  handling evidence justifies them.
- R9. The 404 content and route home and the dedicated Lander page work without JavaScript. The game
  has no audio, telemetry, network request, durable storage, or critical content; it pauses physics
  and motion when inactive, can be exited, and honors reduced motion for all nonessential animation.
  World generation, powered sites, checkpoints, fuel, and progress last only for the current
  in-memory run. After activation, native visible `Exit mission` and crash-state `Restart mission`
  buttons provide touch and assistive-technology equivalents to Escape and `r`; they remain hidden
  during hint-free preflight.
- R10. Before the onboarding effort's canonical bootstrap source lands, an **interim public
  release** may serve the complete site shell, repository-sourced problem and principle content,
  selected brand, permanent links, custom 404, deployment pipeline, and custom domain. It states
  plainly that guided onboarding is not yet published and provides no substitute installation
  command, bootstrap text, disabled copy affordance, or implication that onboarding is available.
  The later bootstrap integration replaces this bounded notice; it does not require a redesign or a
  second site.
- R11. The main page includes the restrained link text `We take security seriously.` as optional
  depth, not a warning gate or dominant call to action. It leads to a dedicated static security page
  that renders the complete root `SECURITY.md` as HTML. That document is the single authority for
  the page's threat model, isolation and control model, material limitations, credential/secret
  posture, and GitHub-only vulnerability-reporting path. The website adds no selected passages or
  separately maintained security prose. The page is candid and specific without assuming every
  visitor wants a security lecture.
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
  from their permanent repository source. The dedicated Manifesto, security, Lander, and
  host-required 404 pages remain separate optional/play/error surfaces rather than being folded into
  the landing page.
- R15. Navigation follows familiar page conventions without reintroducing duplicate destinations.
  Home, Manifesto, Security, Lander, and 404 use the same responsive header structure: a breadcrumb
  at the upper left and one GitHub and one PyPI call to action at the upper right. Each external
  call to action pairs its visible text with a local decorative service icon and remains
  understandable without the icon. The breadcrumb contains one `Agentworks` link to the home route,
  a visual separator, and a non-linked current-page label (`Home`, `Manifesto`, `Security`,
  `Lander`, or `404`) marked as current. The home header omits the small rocket because the large
  mark immediately follows as the hero; every other page places the small rocket immediately left of
  its breadcrumb. On the 404, the breadcrumb's `Agentworks` link is the sole visible route-home
  action. A shared traditional footer places the exact text `Product of Wayfarer Labs, LLC` at the
  left and one `Agentworks Manifesto` link, one `We take security seriously` link, and one small
  icon-only AGW rocket link to `/lander/#lander-game` at the right. The rocket link is the final
  footer item, has an accessible name independent of the image, and is the sole Lander destination
  on each page. These placements supersede the Phase 4A combined exploration panel while preserving
  one link per external, manifesto, security, and Lander destination.
- R16. The footer's `Agentworks Manifesto` link opens a semantic static page at `/manifesto/`, not
  the repository document. The page renders the complete `docs/manifesto.md` document at build time,
  including its source `h1`, introduction, problem space, and key principles, without a separately
  maintained site copy or selected-passage contract. Relative source links are deliberately mapped
  to their permanent repository destinations. Missing or unreadable input, invalid UTF-8,
  unsupported Markdown, invalid links, or an invalid whole-document structure fails the build before
  output replacement. The reviewed 2026-08-10 repository rename changed the single configured source
  path from `docs/why-agentworks.md` to `docs/manifesto.md` and removed the former path; the website
  has no fallback, autodetection, or simultaneous support for both paths.
- R17. The builder emits only the complete linked site artifact. The earlier `--only 404` partial
  demo mode is retired because the accepted 404 now shares navigation with Home, Manifesto, and
  Security; emitting only `404.html` would make its sole recovery action and footer links dead. Game
  development and demos use `/lander/` from the same complete local build that production uses,
  while `/404.html` remains the host fallback and renders the identical shared game subtree. No
  validator exception may permit an emitted local link to resolve outside the selected manifest.
- R18. Manifesto, Security, Lander, and 404 start with their `h1` after only the shared compact
  detail-page inset. They show no eyebrow, error-code, repository-provenance, or other pre-title
  label. The dedicated Lander page uses the exact visible `h1` `We need to deploy some agents!`. The
  404 retains `Page not found` and the exact explanatory copy
  `This route is broken! We need to deploy some agents!` below the title; removing the redundant
  `404` label does not weaken document metadata, breadcrumb state, HTTP fallback behavior, or
  recovery.
- R19. Manifesto and Security automatically expose an `On this page` navigation generated from every
  source `h2` and `h3`, preserving heading order and nesting without a separately maintained
  inventory. On narrow or zoomed layouts it appears inline immediately after the source `h1`. When
  enough horizontal room exists, the same navigation becomes a left rail beside the document. It
  uses ordinary same-page anchors, remains useful without CSS or JavaScript, and introduces no
  duplicate body prose or alternate document model.
- R20. Every generated page advertises one local SVG favicon showing the selected neutral graphite
  A/G/W rocket mark without exhaust. The favicon preserves the exact selected mark geometry, has no
  flame paths or colors, resolves beneath both supported site bases, and adds no remote request,
  runtime script, font, or hand-maintained raster fallback.
- R21. The game is one continuous, forward-moving lunar expedition rather than a terminal level. It
  presents visibly rising, falling, and sloped lunar-lander terrain between sites and a
  deterministic sequence of sites generated from one fresh in-memory run seed. Each site has one
  materially elevated helicopter-style landing platform exactly three lander widths long beside one
  compact NOC building. Exposed collider-backed trusses and scaffolding visibly connect the platform
  and NOC into one supported structure; decorative openings must not imply a traversable gap where
  the collision model is solid. Terrain changes at deliberately wide intervals with stronger,
  irregular elevation changes between sites rather than a fine repeated sawtooth; every platform and
  its NOC occupy one flat site shelf. The elevated platform reads as a supported structure without
  exposing a long sky-colored slot beneath its deck. A safe landing collects that platform's single
  gas can into the lander's visible fuel reserve, deploys an agent from the G opening, fills a clean
  rectangular vertical, multicolor phone-battery-style power indicator without a terminal nub, then
  builds a vertically symmetric network signal through the final three power stages. Completing the
  sequence exposes the exact visible and announced banner `Agent Deployed!` while the lander waits
  safely on the pad. The deployed agent remains visibly installed at that NOC while the powered site
  is retained in the run's rolling world. The player must command the subsequent liftoff; there is
  no automatic launch and no terminal success after a deployment.
- R22. After each safe landing, the next site is deterministically placed beyond the right edge of
  the current view. A visible right-edge arrow blinks while that target remains offscreen and hides
  once the site enters view; under reduced motion it remains a static direction cue. Before issuing
  the departing site's gas can, the game calculates a conservative minimum fuel requirement for a
  demonstrated reference flight to the generated next platform. The can adds that requirement
  multiplied by a difficulty ratio that starts near three and monotonically approaches one as sites
  advance. Unused fuel carries forward and is never discarded merely because another site was
  completed.
- R23. Unsafe terrain, platform, or building contact produces a brief vacuum-appropriate crash: a
  compact propellant flash and ballistic fragments, with no smoke cloud, atmospheric shock wave,
  sustained fireball, audio, or page movement. Reduced motion skips fragment travel and exposes the
  final failed state directly. Safe-contact limits remain demanding but modestly more forgiving than
  the initial continuous-expedition tuning, especially for the angular speed produced by an ordinary
  steering pulse. Restart through either `r` or the native control resumes from the last
  successfully powered platform with its post-refuel checkpoint; before the first success it
  restarts the initial approach. Exit or reload starts a fresh run.
- R24. Once activated, the game chrome uses a compact 1980s-arcade presentation without introducing
  a downloaded font, remote asset, second status authority, canvas, or framework. The left fuel
  reserve is visual-only for sighted players: its bottom-up fill changes from danger red through
  amber to ready green, while a named rounded representation of the exact model reserve remains
  available to assistive technology. A successful touchdown shows the collected can traveling toward
  that gauge while its fill rises over the existing refuel interval; reduced motion exposes the
  final full gauge atomically. The sole status live region becomes the centered, bordered arcade
  banner for exact `Agent Deployed!` and exact `Crashed!` outcomes. Concise controls move inside the
  scene in a small bottom rail whose reserved band never overlaps terrain. On failure, native
  Restart and Exit controls appear inside the scene beneath `Crashed!`; all native controls keep
  their accessible names, focus behavior, and minimum touch targets. Arcade decoration and motion
  never duplicate semantic text, obscure the world, expand the bounded world DOM, or survive under
  reduced motion.

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
- AC5. A missing URL serves a semantic 404 with a visible home link before scripts run. Its content
  begins with `Page not found` after the compact shared inset, with no redundant error-code or other
  pre-title label; no control instructions are visually disclosed until deliberate activation starts
  the game.
- AC6. Space starts the game from the initial state on either 404 or `/lander/` when focus is not
  inside another control. Starting moves focus to the game scene. Arrow and vi controls produce the
  specified collective and differential thrust; tap, hold, and horizontal drag provide equivalent
  touch control; the same collective controls initiate player-commanded liftoff from a powered pad;
  visible plume length reflects the commanded engine thrust; and the native Exit control returns
  either input mode to settled preflight.
- AC7. A safe upright touchdown on the elevated three-lander-width platform consumes its gas can
  exactly once, increases the programmatically named numeric reserve without discarding carried
  excess, and refills a left-side vertical gauge whose scale is explicitly relative to that leg's
  departure reserve rather than a false fixed tank capacity. It completes agent entry, fills the
  single-building NOC battery indicator from bottom to top through distinct visible colors, builds
  the vertically symmetric network signal through its final three stages, and presents one
  `Agent Deployed!` banner through the existing status authority. The centered lander remains safely
  at rest with fuel unchanged until the player commands thrust, then returns to ordinary flight once
  both feet clear the deck. Completing three successive sites proves that deployment is not
  terminal, that each agent remains visibly installed at its powered NOC, and that powered sites
  remain visibly changed while they remain in the rolling view.
- AC8. Automated and browser acceptance cover state transitions, input mapping, consistent
  fixed-step physics across representative frame schedules, seeded terrain and site generation,
  route-home fallback, hidden-until-start instructions, fuel and checkpoint transitions, reduced
  motion, keyboard focus, narrow screens, bounded runtime work, and paused background behavior.
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
- AC13. The landing, Manifesto, security, Lander, and 404 surfaces share a restrained
  terminal/TUI-derived visual system at desktop and narrow widths. Text remains real semantic
  content, ordinary links and controls remain recognizable, and the design meets the existing
  contrast, focus, zoom, reflow, reduced-motion, keyboard, and touch requirements without depending
  on terminal familiarity.
- AC14. In a clean-context interim-release check, a visitor with no prior Agentworks knowledge can
  understand what the project is, recognize that guided onboarding is not yet published, and choose
  the repository, package, rationale, or security path without explanation. This is the interim
  usefulness bar; it does not claim AC4's completed onboarding handoff.
- AC15. The landing page contains exactly one navigable anchor for each of the GitHub repository,
  PyPI package, deeper rationale, and security destinations; it contains no rendered problem-space
  or principles section. The selected rocket is a prominent hero element without displacing the page
  identity, availability notice, or four destinations at 320 CSS pixels or 400 percent zoom.
- AC16. Generated Home, Manifesto, Security, Lander, and 404 documents expose the shared responsive
  header and footer landmarks with the exact per-page breadcrumb current item. Home has no small
  header mark; every other page has exactly one small header mark, and 404 has no separate body home
  link. GitHub and PyPI occur once per page in the header with visible text and hidden decorative
  icons. Manifesto, Security, and the icon-only Lander destination occur once per page in the
  footer, beside the exact Wayfarer Labs ownership text. The footer rocket remains operable and
  named when its image is unavailable. Link purposes, keyboard focus, accessible names, source
  order, narrow-screen wrapping, and 400-percent zoom stay useful with images or CSS unavailable.
- AC17. `/manifesto/` renders every supported block in `docs/manifesto.md`, and `/security/` renders
  every supported block in root `SECURITY.md`, including each document's sole source `h1`, as
  semantic headings, paragraphs, and lists. Generated content and mapped links are verified against
  the complete permanent source, contain no unexpanded source-relative URL, and remain useful
  without CSS or JavaScript. Ordinary supported prose or heading edits flow through without a
  website-code update; malformed document structure, unsupported Markdown, unsafe or unexpected
  links, invalid encoding, and missing input fail closed.
- AC18. The builder CLI has no focused or partial-output option, its complete artifact contains
  every local destination exposed by Home, Manifesto, Security, Lander, or 404, and validation
  rejects every absent local reference. Automated game checks build the complete artifact and
  exercise `/lander/` plus the host fallback `/404.html`; documented local preview commands use the
  dedicated route.
- AC19. Generated `/lander/` and `/404.html` contain byte-equivalent `#lander-game` subtrees after
  site-base rendering. One reviewed template fragment owns that subtree, and mutation tests reject
  duplicate, missing, moved, or independently edited game markup. Both routes pass the same no-JS,
  focus, input, motion, lifecycle, and zero-runtime-request acceptance.
- AC20. Manifesto and Security each contain one labeled table-of-contents navigation whose links and
  visible labels exactly match the source-derived `h2` and `h3` sequence. `h3` entries are nested
  beneath their preceding `h2`; every fragment resolves to exactly one generated heading ID. The
  default flow places the navigation after the `h1`, and a tested wide-screen media query moves it
  into a left column without changing source order or requiring JavaScript.
- AC21. Home, Manifesto, Security, Lander, and 404 each contain exactly one
  `rel="icon" type="image/svg+xml"` link to the emitted flame-free favicon beneath the selected site
  base. Automated asset checks prove its A/G/W path and presentation attributes equal the selected
  mark in `agw-rocket.svg`, while plume identifiers, flame colors, scripts, animation, images, and
  external references are absent.
- AC22. For fixed seeds, generated terrain, elevated platforms, single NOCs, gas cans, next-site
  positions, and fuel awards are byte-for-byte repeatable. Representative seeds meet the LLD's
  minimum terrain-diversity constraints and visibly include coarse rising, falling, and sloped
  non-platform spans separated by the LLD's wider sample interval. Every retained platform and NOC
  stands on one flat shelf, while the platform deck remains visibly and physically elevated on its
  collider-backed exposed trusses and scaffolding that attach the pad to its NOC, without an
  uninterrupted sky-colored rectangle beneath it or a visual opening that contradicts collision.
  Each award equals the next route's deterministic demonstrated minimum multiplied by a monotonic
  ratio that is approximately three for the first award and approaches one without crossing it. A
  test-controlled reference flight reaches and safely lands on every representative generated next
  platform using no more than the calculated minimum; a one-step-smaller tested allowance cannot
  complete that same reference plan.
- AC23. While the next site is right of the viewport, a visible right-pointing cue is present and
  blinks only when motion is allowed and the document is active. It becomes static under reduced
  motion, pauses while hidden, and disappears when the target enters view. Direction is never
  communicated by animation alone.
- AC24. Every unsafe terrain, pad, or building impact reaches a finite crash sequence with a brief
  flash and deterministic ballistic debris but no smoke, shock wave, sustained fire, sound, page
  movement, storage, or request. Reduced motion reaches the same final failure atomically. Restart
  restores the exact last post-refuel, post-power launch-ready platform checkpoint without
  duplicating its can or fuel; restart before the first deployment restores the initial approach,
  while Exit and reload create a fresh run. Boundary tests pin the modestly relaxed safe-contact
  envelope. Browser handling proves that collective-plus-turn vectoring stays within the LLD's
  materially lower axial ceiling, turn-only input does not overcome gravity, neutral collective
  counters residual rotation through the deterministic assist, and engine-off vehicle motion plus
  crash debris remain undamped and ballistic.
- AC25. In both `/lander/` and `404.html`, activation reveals one blocky local-system-font arcade
  HUD: no numeric fuel text is visible, one named rounded representation of the exact model reserve
  remains available to assistive technology without live-region behavior, and the same bottom-origin
  gauge independently communicates level by height and a red-to-amber-to-green progression. Normal
  motion shows one deterministic can-to-gauge transfer and gauge-rise sequence during the pinned
  refuel interval; reduced motion shows neither animation and lands directly on the same full value.
  The existing sole live region presents centered, bordered `Agent Deployed!` and `Crashed!`
  banners, with no pseudo-element or duplicate text authority. The controls legend and
  state-appropriate native buttons render inside the scene; terrain remains wholly above the legend
  rail, and failed-state Restart and Exit controls sit below the crash banner without overlap at 320
  CSS pixels or 400 percent zoom. Normal arcade motion pauses while hidden and is absent under
  reduced motion. The installed-agent mark persists at each powered retained NOC without increasing
  the established 80-descendant world ceiling. Shared-game byte identity, focus order, 44-pixel
  targets, contrast, no-JavaScript recovery, zero runtime font or cross-origin requests, and all
  Phase 4I physics/route/world digests remain unchanged.

## Settled implementation rulings

- GitHub Pages hosts the site, with an operator-coordinated GoDaddy DNS cutover.
- A standard-library Python builder produces the static artifact, and CI runs the README identity
  check.
- The first slice is a landing page, Manifesto, security deep-dive, dedicated Lander page, and the
  host-required custom 404 error surface.
- The first public release intentionally omits onboarding rather than blocking the rest of the site;
  canonical onboarding follows as a separately reviewed additive release.
