# FRD: The agentworks.build Website

- Status: Phase 4S free-flight and terrain-variety correction in design; canonical assistance
  integrated
- Date: 2026-08-07
- Last revised: 2026-08-14
- Seeded by: the saga lead, at operator request. This is a standalone effort, deliberately NOT a
  child of the 2026-08-04-next-steps saga (see that saga's `target-state.md` out-of-scope section
  for the recorded relationship). It follows the ordinary SDD process: the effort lead owns the HLA
  and plan; the saga lead reviews PRs.
- Integration delegation: on 2026-08-10 the operator authorized the onboarding-and-discovery effort
  lead to reconcile this FRD, HLA, plan, and shell LLD while integrating the canonical assistance
  source in PR #480. The website effort retains production acceptance and closeout ownership.

## Purpose

Agentworks gets a public front door at `agentworks.build` (domain purchased 2026-08-07): the place a
curious human or their agent lands first and learns what Agentworks is. They leave with the one
copy-paste block that installs or updates the CLI, verifies it, and hands continuing assistance to
`agw guide --agent`, alongside useful product/security depth and permanent project links.

The operator's sizing mandate is the controlling constraint: **super simple at first**. The first
slice is a small static site, its publishing pipeline, and one optional interactive surprise shared
by a dedicated Lander page and the otherwise useful 404 page. The operator later selected the
continuous expedition in R21-R26 as the bounded form of that surprise; it adds no product surface,
backend, framework, persistence, telemetry, or activation dependency. The first release shipped an
honest availability notice while onboarding was still in development; PR #480 replaced it with the
canonical bootstrap. Every ambition beyond the accepted static site and continuous surprise is
recorded as a growth path so nothing forecloses it, and none of it is in scope now.

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
  in-memory run. After activation, native visible `Exit mission` and crash-state `Retry` buttons
  provide touch and assistive-technology equivalents to Escape and `r`; they remain hidden during
  hint-free preflight.
- R10. The completed site replaces the former bounded onboarding-availability notice with the one
  canonical bootstrap source. It retains the established shell, brand, permanent links, custom 404,
  deployment pipeline, and domain; no runtime mode, second site, or dormant interim notice remains.
- R11. The main page includes the restrained link text `We take security seriously.` as optional
  depth, not a warning gate or dominant call to action. It leads to a dedicated static security page
  that renders the complete root `SECURITY.md` as HTML. That document is the single authority for
  the page's threat model, isolation and control model, material limitations, credential/secret
  posture, and GitHub-only vulnerability-reporting path. The website adds no selected passages or
  separately maintained security prose. The page is candid and specific without assuming every
  visitor wants a security lecture.
- R12. The website consumes the canonical thin bootstrap source byte-for-byte and does not append a
  security recital, source-review offer, or operating guidance. That prompt only installs or updates
  an exact compatible CLI, verifies it, and invokes `agw guide --agent`; the installed guide owns
  the startup disclosure, authorization posture, optional source review, and continuing assistance.
- R13. The visual language hints at terminal and TUI paradigms while remaining a modern website:
  monospaced accents, crisp bounded regions, compact status-like details, strong hierarchy, and
  efficient use of space should communicate `simple but powerful`. It must not become a fake
  terminal, green-on-black pastiche, command-line prerequisite, or excuse to weaken reflow,
  readability, pointer use, or accessibility.
- R14. The landing page is a deliberately compact single-page product handoff, not a rendered
  manifesto. It shows the selected rocket mark as a dominant hero element at two to three times its
  original header scale, gives only the concise product identity and canonical bootstrap, and offers
  one link each to the repository, package, deeper rationale, and security deep dive. A destination
  must not be repeated in the landing page's header, body, and footer. Visitors who want the longer
  problem statement or principles follow the single Manifesto link to a page generated from their
  permanent repository source. The dedicated Manifesto, security, Lander, and host-required 404
  pages remain separate optional/play/error surfaces rather than being folded into the landing page.
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
  icon-only AGW rocket link to `/lander/` at the right. The rocket link is the final footer item,
  has the accessible name and hover text `Help deploy some agents!` independent of the image, and is
  the sole Lander destination on each page. These placements supersede the Phase 4A combined
  exploration panel while preserving one link per external, manifesto, security, and Lander
  destination.
- R16. The footer's `Agentworks Manifesto` link opens a semantic static page at `/manifesto/`, not
  the repository document. The page renders the complete `docs/manifesto.md` document at build time,
  including its source `h1`, introduction, problem space, and key principles, without a separately
  maintained site copy or selected-passage contract. Relative source links are deliberately mapped
  to their permanent repository destinations. Missing or unreadable input, invalid UTF-8,
  unsupported Markdown, invalid links, or an invalid whole-document structure fails the build before
  output replacement. The website has no fallback, autodetection, or simultaneous support for the
  retired source path.
- R17. The builder emits only the complete linked site artifact. The earlier `--only 404` partial
  demo mode is retired because the accepted 404 now shares navigation with Home, Manifesto, and
  Security; emitting only `404.html` would make its sole recovery action and footer links dead. Game
  development and demos use `/lander/` from the same complete local build that production uses,
  while `/404.html` remains the host fallback and renders the identical shared game subtree. No
  validator exception may permit an emitted local link to resolve outside the selected manifest.
- R18. Manifesto, Security, Lander, and 404 start with their `h1` after only the shared compact
  detail-page inset. They show no eyebrow, error-code, repository-provenance, or other pre-title
  label. The dedicated Lander page uses the exact visible `h1` `We need agents!`. The 404 retains
  `Page not found` and the exact explanatory copy `This route is broken! We need agents!` below the
  title; removing the redundant `404` label does not weaken document metadata, breadcrumb state,
  HTTP fallback behavior, or recovery.
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
- R21. The game is one continuous lunar deployment expedition rather than a terminal level. It
  presents visibly rising, falling, and sloped lunar-lander terrain between sites and a
  deterministic sequence of sites generated from one fresh in-memory run seed. Each site has one
  materially elevated helicopter-style landing platform exactly three lander widths long beside one
  compact NOC building. One continuous collider-backed exposed truss spans beneath the complete
  platform-to-NOC structure with a uniform alternating-triangle rhythm rather than separate pad,
  connector, and NOC fields of X braces. Exactly three visible, collider-backed supports descend
  from that structure to the lunar surface at its left, center, and right; decorative openings must
  not imply a traversable gap where the collision model is solid. Terrain changes at deliberately
  wide intervals with stronger, irregular elevation changes rather than a fine repeated sawtooth,
  and the native deterministic terrain continues beneath every site without being replaced by a flat
  shelf. The terrain projection is one ordered, continuous polyline with no duplicate-position
  vertical seams. The elevated platform and NOC read as one supported structure without exposing a
  long sky-colored slot beneath the deck. A safe landing collects that platform's single gas can
  into the lander's visible fuel reserve, deploys an agent from the G opening, fills a clean
  rectangular vertical, multicolor phone-battery-style power indicator without a terminal nub, then
  builds a vertically symmetric network signal through the final three power stages. Completing the
  sequence exposes the exact visible and announced banner `Agent Deployed!` while the lander waits
  safely on the pad. The deployed agent remains visibly installed at that NOC while the powered site
  is retained in the run's rolling world. The player must command the subsequent liftoff; there is
  no automatic launch and no terminal success after a deployment.
- R22. After each safe landing, the next site is deterministically placed beyond the right edge of
  the current view. A visible edge arrow points toward that target from either side while it remains
  offscreen and hides once the site enters view; under reduced motion it remains a static direction
  cue. Before issuing the departing site's gas can, the game calculates a deterministic sufficient
  fuel allowance for a certified reference flight to the generated next platform. The allowance is
  an independently derived conservative base plus the positive platform-height difference divided by
  three, rounded upward to the existing fuel quantum; descending platforms receive no negative
  credit. The can adds that allowance multiplied by the **refuel ratio** `1 + 0.5^(n-1)`, where `n`
  is the one-indexed number of the base just powered. The first base therefore uses `2`, followed by
  `1.5`, `1.25`, `1.125`, and a mathematically monotonic approach to one from above. The runtime's
  binary-number projection never falls below one and may round to exactly one once the remaining
  bonus is below representable precision. Unused fuel carries forward and is never discarded merely
  because another site was completed. The sufficient allowance is not represented as the smallest
  successful reserve, and the reference plan does not waste fuel to manufacture a
  one-quantum-smaller failure.
- R23. Unsafe terrain, platform, or building contact produces a brief vacuum-appropriate crash: a
  compact propellant flash and ballistic fragments, with no smoke cloud, atmospheric shock wave,
  sustained fireball, audio, or page movement. Reduced motion skips fragment travel and exposes the
  final failed state directly. Safe-contact limits remain demanding but modestly more forgiving than
  the initial continuous-expedition tuning, especially for the angular speed produced by an ordinary
  steering pulse. Retry through either `r` or the native control resumes from the last successfully
  powered platform with its post-refuel checkpoint; before the first success it restarts the initial
  approach. Exit or reload starts a fresh run.
- R24. Once activated, the game chrome uses a compact 1980s-arcade presentation without introducing
  a downloaded font, remote asset, second status authority, canvas, or framework. The left fuel
  reserve is visual-only for sighted players: its bottom-up fill changes from danger red through
  amber to ready green, while a named rounded representation of the exact model reserve remains
  available to assistive technology. At exactly zero fuel the entire gauge flashes red when motion
  is allowed, pauses with the game, and remains a strong static red warning under reduced motion. A
  successful touchdown shows the collected can traveling toward that gauge while its fill rises over
  the existing refuel interval; reduced motion exposes the final full gauge atomically. The sole
  status live region becomes the centered, bordered arcade banner for exact `Agent Deployed!` and
  exact `Crashed!` outcomes. Concise controls move inside the scene in a small bottom rail whose
  reserved band never overlaps terrain. Exit is a persistent bottom-right rail control, while
  failure adds only Retry beneath `Crashed!`; each control shows its keyboard shortcut on a smaller
  second line and keeps its accessible name, focus behavior, and minimum touch target. Launch-ready
  presents only `Agent Deployed!`: departure uses the same keyboard, vi, pointer, and touch thrust
  controls as flight and has no dedicated Launch action. Arcade decoration and motion never
  duplicate semantic text, obscure the world, expand the bounded world DOM, or survive under reduced
  motion.
- R25. The initial approach starts with exactly half of the visual fuel reference instead of a full
  gauge, while later post-award checkpoints continue to establish a full leg-relative gauge without
  capping or discarding carried fuel. The player may explore indefinitely in either horizontal
  direction while fuel and collision outcomes permit: leaving the current or target platform behind
  is never itself a crash, the camera follows in both directions, and the offscreen target cue
  points toward the retained target from either side. Deterministic native terrain remains
  collision-backed throughout that exploration. The decorative sky shares the world's travel rather
  than staying fixed to the viewport: bounded deterministic stars pan at a slower parallax rate and
  occasional local celestial landmarks add variety without collision, semantics, requests,
  persistence, or an unbounded DOM. Each of the three platform supports reads as an open lattice
  column integrated with the continuous Warren truss, reaches its independently sampled terrain
  foot, and has a conservative collider that contains every rendered member without implying
  traversable openings. Agent travel from the lander to the NOC completes in half the Phase 4L time;
  refueling and the subsequent battery and network power stages retain their existing durations.
- R26. **Superseded historical requirement; not active for shipping.** The lunar terrain uses
  substantially more of the scene's vertical range without becoming noisy. Measure normalized relief
  from the top of the in-game instruction rail: zero is that rail's top edge and one is the scene's
  top edge in the canonical, untransformed scene coordinate system; any later camera transform
  changes only final viewport placement and cannot redefine terrain height. Every point on every
  continuous native terrain surface lies between `0.1` and `0.6` on that canonical scale, and
  deterministic witness runs include both low basins and high peaks near the ends of the band.
  Elevation changes form broad slopes, ridges, and canyons with bounded grade and bounded changes in
  grade; the generator must not alternate direction at every sample or turn the surface into a
  repeated sawtooth. The visible surface, collision terrain, platform-support feet, site clearance,
  route proof, ceiling behavior, and static no-JavaScript scene all consume the same terrain
  authority. Platforms remain honestly supported above untouched native terrain, and the player and
  target remain visible and reachable across the expanded relief.
- R27. **Superseded historical requirement; not active for shipping.** The operator's hands-on
  review supersedes R26's exact global relief-band interpretation and rejects its shipping behavior.
  The game keeps one fixed-height scene with no vertical camera, page-height growth, or vertical
  scrolling. Terrain variation must be obvious within ordinary gameplay windows as broad navigable
  peaks, valleys, ridges, and canyons, not only across distant mathematical witnesses; bounded slope
  and grade change still prevent noisy sawtooth profiles. Each platform uses the lowest
  collision-safe deck derived from the native terrain envelope under that individual platform and
  NOC footprint, plus only the required structural clearance. A global deck datum and unnecessarily
  tall supports are forbidden. Rendering, collision, support feet, site clearance, routes, and the
  static scene still consume one terrain authority. The known-good pre-R26 terrain/camera behavior
  is restored before corrected relief is introduced, so a visually rejected redesign never remains
  the branch baseline merely because automated tests pass.
- R28. The operator's hands-on review supersedes Phase 4Q's curved summit terrain and
  terrain-coupled site cycle. Terrain is one deterministic lunar-lander polyline made exclusively of
  straight segments between canonical vertices: no spline, smoothed interpolation, Bézier, curve
  command, rounded interpolation, or presentation-only relief authority is permitted. With zero at
  the top of the instruction rail and one at the top of the fixed scene, every point on the polyline
  lies inclusively within normalized height `[0.1,0.6]`, and ordinary gameplay windows visibly
  exercise substantial height variation rather than clustering near one level. Slopes and changes
  between adjacent segment grades remain bounded so peaks and valleys are navigable rather than
  noisy. Horizontal site placement is selected from the mission route without consulting terrain
  height. After the full platform-to-NOC footprint is fixed, its deck is exactly `2.5 m` above the
  maximum native terrain height under that closed footprint; exposed supports extend independently
  from the structure to their native terrain contacts, however long that local geometry requires.
  Rendering, collision, support feet, static recovery, and route proof consume the same polyline.
  The scene retains its fixed `25/16` projection with no vertical camera, document growth, or
  vertical scroll.
- R29. The operator's follow-up review rejects Phase 4R's forced alternating hill/valley rhythm and
  the remaining non-contact flight failures. The straight terrain remains deterministic and bounded
  within normalized height `[0.1,0.6]`, but its seeded global vertices vary without a fixed
  high-block/low-block alternation or a short repeating silhouette. Ordinary windows show a less
  predictable mix of rises, descents, peaks, shelves, and intermediate angular facets while exact
  segment-grade and adjacent-grade-change limits keep the surface navigable rather than noisy.
  Terrain generation remains wholly independent of site placement; decks and supports retain the
  exact R28 `max+2.5 m` rule after each site position is fixed. Passing a target, traveling anywhere
  inside the generated world, crossing the former vertical ceiling, or moving farther than a bounded
  collision subdivision budget can cover must never itself enter the crash sequence. The collision
  implementation must continue checking the complete swept path without converting large motion into
  a synthetic failure. Fuel exhaustion suppresses thrust but is not itself a crash; only a
  subsequent real collision with terrain or a platform, support, NOC structure, or an explicitly
  rendered physical world boundary can destroy the lander. Infinite horizontal scrolling is not
  required, but any finite world edge must be visible before contact and cannot cause a failure
  while the lander remains inside known terrain. The fixed `25/16` scene and zero vertical page
  scrolling remain unchanged.

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
- C5. The interim release was a real, useful release, not a preview mode or a parallel product. It
  introduced no runtime flag, alternate URL, duplicated product prose, speculative onboarding
  contract, or permanent staging architecture. Each public stage was honest and operable on its own
  terms.

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
- AC9. `https://agentworks.build` contains exactly one semantic canonical bootstrap region and no
  retired availability notice, empty onboarding container, or unexpanded template token.
- AC10. Canonical bootstrap integration leaves the established information architecture, visual
  system, URLs, 404, and deployment path intact.
- AC11. The home-page security link is visible but visually secondary, works without JavaScript, and
  resolves to a semantic security page at a stable URL. That page distinguishes claims, boundaries,
  current limitations, operator practices, and private vulnerability reporting; every
  product/security claim is sourced from or verified against permanent repository documentation.
- AC12. Tooling proves the website and README decode to the canonical thin bootstrap bytes and that
  this surface contains no source-review, security-posture, or startup-disclosure substitute. A
  clean guide invocation proves the installed guide, rather than the website, owns that context.
- AC13. The landing, Manifesto, security, Lander, and 404 surfaces share a restrained
  terminal/TUI-derived visual system at desktop and narrow widths. Text remains real semantic
  content, ordinary links and controls remain recognizable, and the design meets the existing
  contrast, focus, zoom, reflow, reduced-motion, keyboard, and touch requirements without depending
  on terminal familiarity.
- AC14. In a clean-context check, a visitor with no prior Agentworks knowledge can understand what
  the project is, copy the bootstrap, and choose the repository, package, rationale, or security
  path without explanation.
- AC15. The landing page contains exactly one navigable anchor for each of the GitHub repository,
  PyPI package, deeper rationale, and security destinations; it contains no rendered problem-space
  or principles section. The selected rocket is a prominent hero element without displacing the page
  identity, canonical bootstrap, or four destinations at 320 CSS pixels or 400 percent zoom.
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
  minimum terrain-diversity constraints and visibly include coarse rising, falling, and sloped spans
  separated by the LLD's wider sample interval, including uninterrupted varied terrain below each
  site. No site replaces that terrain with a horizontal shelf, introduces duplicate vertices at one
  horizontal coordinate, or renders a vertical seam at a chunk or site boundary. The platform deck
  remains visibly and physically elevated on one continuous collider-backed exposed truss that
  attaches the pad to its NOC with a uniform alternating-triangle structural rhythm. Exactly three
  visible collision-backed supports reach from that structure to the independently interpolated
  terrain at its left, center, and right, without separate brace fields, an uninterrupted
  sky-colored rectangle beneath it, or a visual opening that contradicts collision. Each award
  equals the next route's deterministic sufficient allowance multiplied by the refuel ratio
  `1 + 0.5^(n-1)` for one-indexed powered-base number `n`, beginning at `2` and approaching one
  without crossing it. A test-controlled reference flight reaches and safely lands on every
  representative generated next platform using no more than the calculated allowance. Independent
  arithmetic tests pin upward fuel-quantum rounding and exact exhaustion behavior without asserting
  that one quantum less than a deliberately conservative allowance must make the route fail.
- AC23. While the next site is outside the viewport, a visible cue points toward it from the
  corresponding left or right edge and blinks only when motion is allowed and the document is
  active. It becomes static under reduced motion, pauses while hidden, reverses correctly when the
  player passes the target, and disappears when the target enters view. Direction is never
  communicated by animation alone.
- AC24. Every unsafe terrain, pad, or building impact reaches a finite crash sequence with a brief
  flash and deterministic ballistic debris but no smoke, shock wave, sustained fire, sound, page
  movement, storage, or request. Reduced motion reaches the same final failure atomically. Retry
  restores the exact last post-refuel, post-power launch-ready platform checkpoint, including its
  centered pose and carried post-award fuel, without duplicating its can or fuel; retry before the
  first deployment restores the initial approach, while Exit and reload create a fresh run. Boundary
  tests pin the modestly relaxed safe-contact envelope, revised upward just enough to accept
  near-miss arrivals at `2.2` model units per second horizontal speed, `3.6` model units per second
  descent, `18` degrees tilt, and `26` degrees per second angular speed. Browser handling proves
  that collective-plus-turn vectoring stays within the LLD's materially lower axial ceiling,
  turn-only input does not overcome gravity, neutral collective counters residual rotation through
  the deterministic assist, and engine-off vehicle motion plus crash debris remain undamped and
  ballistic.
- AC25. In both `/lander/` and `404.html`, activation reveals one blocky local-system-font arcade
  HUD: no numeric fuel text is visible, one named rounded representation of the exact model reserve
  remains available to assistive technology without live-region behavior, and the same bottom-origin
  gauge independently communicates level by height and a red-to-amber-to-green progression. Normal
  motion shows one deterministic can-to-gauge transfer and gauge-rise sequence during the pinned
  refuel interval; reduced motion shows neither animation and lands directly on the same full value.
  Exactly zero fuel selects a distinct empty projection whose red gauge blinks with normal motion,
  pauses with the inactive game, and stays statically red with reduced motion. The existing sole
  live region presents centered, bordered `Agent Deployed!` and `Crashed!` banners, with no
  pseudo-element or duplicate text authority. The controls legend omits shortcuts represented by
  self-documenting buttons and shares the scene's bottom rail with an always-available bottom-right
  Exit action whose smaller second line identifies Escape. Terrain remains wholly above that rail.
  Launch-ready contains no action control, while failure adds only Retry beneath the crash banner
  with `r` on its smaller second line. Those controls and overlays do not overlap at 320 CSS pixels
  or 400 percent zoom. Normal arcade motion pauses while hidden and is absent under reduced motion.
  The installed-agent mark persists at each powered retained NOC without increasing the established
  80-descendant world ceiling. Shared-game byte identity, focus order, 44-pixel targets, contrast,
  no-JavaScript recovery, and zero runtime font or cross-origin requests remain intact. Phase 4L
  regenerates the geometry and world digests atomically for the continuous truss and regenerates the
  physics and derived output digests for that geometry plus the revised safe-contact envelope.
- AC26. A fresh run begins with an exact half-height fuel gauge and enough model fuel for the pinned
  first reference landing, while its accessible reserve remains the exact rounded model value.
  Crossing either former horizontal mission boundary causes no crash and no implicit target
  completion; deterministic tests and browser input drive the lander past the target and back from
  both directions while the camera, bounded retained world, collision terrain, and bidirectional cue
  remain coherent. Fuel exhaustion still suppresses thrust, and only actual terrain, platform,
  support, or building contact can enter the crash sequence; the former vertical ceiling and
  excessive-speed fallback are not failures. Fixed-seed browser evidence shows stars moving with the
  world at the pinned parallax rate and deterministic occasional celestial landmarks entering and
  leaving a bounded sky projection without extra requests or accessibility nodes. Crescent moons
  remain closed astronomical silhouettes. Each planet has one or two modest elliptical rings; each
  ring's rear center is hidden by the planet while its foreground arc remains visible. The NOC's
  physical mast and antenna head remain black at every power stage while only the radiating signal
  arches gain their established colors. Every retained site renders exactly three independently
  reconstructed open lattice support columns whose member pixels lie inside their fixture-derived
  colliders, whose feet meet native terrain, and whose visual rhythm joins the platform truss.
  Normal motion completes the agent's pre-NOC travel in half its Phase 4L duration, reduced motion
  remains atomic, and refuel plus power-stage timings are byte-for-byte unchanged. The built footer
  resolves directly to `/lander/`, its live accessible name and hover text agree, and the reviewed
  Lander/404 copy uses the shortened operator wording without adding a fragment redirect or a second
  game route.
- AC27. **Superseded historical criterion; not active for closeout.** Before any camera transform,
  every point on the independently reconstructed continuous terrain surface has canonical normalized
  height `(640-sceneY)/640` inclusively within `[0.1,0.6]`; the interpolation kernel is
  non-overshooting, so this invariant is not inferred from vertices alone. A fixed, reviewed
  seed/window corpus reaches at least one value no greater than `0.11` and one no less than `0.59`,
  while independent grade, grade-change, and reversal-density checks enforce the LLD's
  realistic-relief limits. Forward and reverse traversal reproduce byte-identical terrain and
  collision heights without a seam, duplicate horizontal position, per-frame randomness, or retained
  history. All site feet meet that terrain, all structures clear it, every canonical success and
  one-quantum failure proof remains valid against regenerated worlds, and 100-site runs terminate
  without generation failure. Real browser evidence covers a low basin, high ridge, broad peak, and
  canyon at wide and narrow layouts while the lander, target, cue, HUD, and instruction rail remain
  visible and non-overlapping.
- AC28. **Superseded historical criterion; not active for closeout.** The recovery checkpoint
  reproduces the pre-R26 terrain, deck, camera, route, and static behavior while preserving later
  accepted Lander refinements and canonical onboarding. Corrected relief then demonstrates multiple
  locally visible peaks and valleys in real-browser ordinary gameplay windows, with independently
  bounded slope and grade change. Every site deck is reconstructed as the lowest permitted value
  above its own native terrain envelope; fixtures reject a global datum or avoidable support height.
  Wide, 320 CSS pixel, and 400-percent zoom evidence proves a fixed scene and zero page/game
  vertical overflow, with no vertical camera transform. Route, collision, first-landing, Retry,
  100-site retention, deterministic build, and static/runtime parity evidence remains green, and
  human review confirms the terrain reads as a navigable landscape rather than a flat line beneath
  towers.
- AC29. Independent reconstruction proves that the shipped terrain contains only strict-X
  straight-line segments and that every vertex and interpolated point has normalized height within
  `[0.1,0.6]`. A reviewed seed/window corpus reaches both the lower and upper portions of that band
  and shows navigable peaks, valleys, rises, and descents at ordinary gameplay scale while enforcing
  the LLD's exact segment-length, grade, and adjacent-grade-change limits. For every retained site,
  tests first derive its horizontal placement without terrain-height reads, then independently
  compute the maximum native height beneath the closed platform-to-NOC footprint and prove exact
  `deck=max+2.5 m` equality. Every support foot equals the native terrain at its own horizontal
  coordinate and every rendered member stays within its conservative collider. Forward/reverse
  generation, canonical sufficient-allowance route proofs, collision/render parity, static recovery,
  Retry, and 100-site retention remain deterministic and bounded. Real-browser evidence at wide, 320
  CSS pixel, 400-percent-equivalent short-height, and touch-landscape viewport sizes proves the
  fixed `25/16` scene, zero vertical transform, and equality of document client and scroll heights
  throughout preflight, flight, service, crash, Retry, reversal, and Exit. Operator hands-on
  acceptance of the actual terrain is required before closeout.
- AC30. Long-duration browser and model witnesses fly beyond the current target in both directions,
  cross the former `56 m` ceiling, and exceed the former 64-slice collision-sweep threshold without
  a failure, progress mutation, or collision omission. A zero-fuel witness continues ballistic
  flight until an independently reconstructed real surface or structure contact and crashes only at
  that contact. Independent terrain reconstruction rejects any mandatory high/low block alternation,
  short repeating profile cycle, curve command, value outside normalized `[0.1,0.6]`, site input, or
  mismatch among rendered, colliding, static, and support-foot terrain. Reviewed wide and narrow
  seed windows visibly differ and contain irregular angular sequences without exceeding the pinned
  grade or grade-change bounds. If the implementation uses finite horizontal bounds, browser
  evidence proves a visible physical terminus and contact-backed failure there while every pose
  inside the generated terrain remains free of boundary failure. Existing local deck equality,
  route-proof, retention, fixed-scene, and zero-scroll acceptance remains green.

## Settled implementation rulings

- GitHub Pages hosts the site, with an operator-coordinated GoDaddy DNS cutover.
- A standard-library Python builder produces the static artifact, and CI runs the README identity
  check.
- The first slice is a landing page, Manifesto, security deep-dive, dedicated Lander page, and the
  host-required custom 404 error surface.
- The first public release intentionally omitted onboarding rather than blocking the rest of the
  site; PR #480 delivered canonical onboarding as a separately reviewed additive release.
