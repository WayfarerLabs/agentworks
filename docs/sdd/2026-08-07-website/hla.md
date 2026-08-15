# HLA: The agentworks.build Website

- Status: Phase 4T implemented and independently reviewed; operator acceptance pending; canonical
  assistance integrated
- Date: 2026-08-07
- Last revised: 2026-08-15
- FRD: `frd.md`
- Research: `prior-art-research.md`
- Brand direction: `brand-direction.md`

## Architectural summary

Build five semantic static pages from repository-owned inputs and deploy their artifact to GitHub
Pages on every push to `main`. Site sources live in `website/`; generated output does not. A small
standard-library Python builder performs explicit substitutions, escapes shared text for HTML, emits
the finished artifact, and fails when a content contract required by the current release is
unavailable or ambiguous.

The completed architecture publishes the Home, Manifesto, Security, Lander, and 404 shells,
repository-derived product/security passages, stable links, selected brand, one shared Lander/404
game, pipeline, domain, and canonical assistance prompt. The former interim notice was a staged
delivery step and has been deleted rather than retained as a runtime mode or parallel site.

The content pages use HTML and CSS for their full experience. Small local scripts progressively
enhance the canonical prompt with copy-button behavior and the shared static scene on Lander and 404
with the same nonessential game. The prompt, static Lander scene, and all error content remain
useful when scripts are absent. The visual system includes local SVG logo assets. There are no
remote fonts, scripts, images, runtime APIs, analytics, cookies, accounts, forms, or backend.

GitHub Actions builds and checks the same artifact in pull requests and on `main`. A dedicated Pages
workflow uploads that artifact and deploys it through the protected `github-pages` environment.
GitHub Pages serves `agentworks.build` over HTTPS; `www.agentworks.build` redirects to the apex.

## Decisions

### D1. One compact landing page plus optional security depth

The completed home page has this information order:

1. the selected AGW rocket as a prominent hero with a compact product identity;
2. the agent-addressed bootstrap as the dominant action;
3. one direct link each to GitHub, PyPI, deeper rationale, and security.

The retired interim notice is absent. Home renders the real canonical bootstrap, not an empty
region, disabled control, speculative command, wait-list form, countdown, or parallel marketing
panel.

The landing page does not render the longer problem statement or principles. Those passages render
on the generated Manifesto page from their permanent repository source. GitHub and PyPI appear once
in the shared header; Manifesto and Security appear once in the shared footer. The body does not
repeat those destinations under different labels.

The home page gives the security posture one calm, visually secondary link labeled
`We take security seriously.` That link opens a dedicated static security page rendered from the
complete root `SECURITY.md`. That Markdown document owns the page's order and all security claims;
the HTML template owns only the shared shell and metadata placement.

The Manifesto and security pages are optional depth, not modals, warning gates, prerequisites, or
long pitches on the home page. A dedicated `/lander/` page exposes the same continuous game used by
the host-required 404 without turning the error route into a navigation destination. These are the
only separate pages in the first slice; the primary product experience stays on the compact landing
page. Pages have in-page navigation only if their final length makes it useful. A custom `404.html`
is an error surface, not a content page or client-side route. There is no blog, documentation
hierarchy, release feed, search, or client-side routing. Growth-path content gets its own design
when its authoritative contracts have landed.

Home, Manifesto, Security, Lander, and 404 share one landmark shape. A breadcrumb sits at the upper
left: one linked `Agentworks` home crumb, a visual separator hidden from the accessibility tree, and
a non-linked current item marked with `aria-current="page"`. The current item is `Home`,
`Manifesto`, `Security`, `Lander`, or `404` as appropriate. Every page except Home places the small
selected rocket immediately before the breadcrumb. The 404's linked `Agentworks` crumb replaces the
body-level return-home action. One GitHub and one PyPI call to action sit at the upper right with
visible labels and local decorative icons hidden from the accessibility tree. Home alone omits the
small header mark because its large hero follows immediately in `main`.

The shared footer contains `Product of Wayfarer Labs, LLC` at the left. Its right side contains the
only manifesto and security text links, labeled `Agentworks Manifesto` and
`We take security seriously`, followed by a small AGW rocket link to `/lander/`. That final
icon-only link uses `Help deploy some agents!` as both its accessible name and hover text; its image
has empty alternative text so the accessible name is not duplicated. The header and footer wrap in
source order rather than collapsing behind a menu. This is consistent navigation across a tiny
static site, not a new navigation system.

### D1A. Long-form pages are complete generated documents

`/manifesto/` renders the complete `docs/manifesto.md` document, and `/security/` renders the
complete root `SECURITY.md`. The same closed Markdown transform emits every supported source block,
including the one required source `h1`; the templates do not add, replace, select, reorder, or
duplicate body headings or prose. Source-relative links are mapped by an explicit allowlist to
permanent repository URLs. The site owns only metadata, breadcrumb labels, and the connective shell.

The Manifesto source path is exactly `docs/manifesto.md`. The builder does not probe for or accept
the retired path.

### D2. Plain web technologies with a narrow build step

The checked-in source consists of home, Manifesto, security, Lander, and 404 HTML shells, one shared
lander-game template fragment, local CSS, focused progressive-enhancement JavaScript, SVG assets,
and a standard-library Python builder under `website/`. The shared fragment references stable
same-origin groups in the selected SVG rather than duplicating its paths. The builder renders that
fragment with the validated site base and inserts the exact result into both Lander and 404 shells.
It substitutes the same base into home and local asset URLs, allowing the source to run at the
local/custom-domain root and at the pre-DNS GitHub Pages project path. There is no separate
build-metadata abstraction. The builder's explicit input list is the build manifest. It uses the
supported Python runtime already present in this repository and only the standard library. It does
not introduce Node package metadata, a JavaScript framework, Jekyll, or a general template language.

The builder accepts an explicit repository root and output directory, writes only beneath the output
directory, and produces deterministic bytes for the same inputs. It starts from an empty
caller-provided output directory in CI. Generated artifacts are ignored by Git and never committed.

The builder has one output shape: the complete linked site. The former focused `--only 404` seam is
removed now that 404 uses the shared navigation shell; a partial artifact would contain dead local
links and would no longer be an honest page. Local game work serves `/lander/` from the complete
artifact, and fallback acceptance also exercises `/404.html`. Manifest validation has no
missing-local-reference exception.

The template vocabulary is closed to named placeholders owned by the builder. Source text is HTML
escaped and interpreted only by the closed Markdown subset before insertion. It is never evaluated
as a template, Python, or JavaScript.

`website/build.py` remains the sole CLI entry point, while content projection/Markdown rendering and
template/CSS/reference validation live in focused sibling modules. Production and test modules stay
below the repository's 1,000-line ceiling. Tests mirror those seams rather than concentrating all
contracts in one file. The split introduces no package dependency, plugin surface, or alternate
builder API.

The Lander's exact continuous-contact arithmetic is a focused shipped production module,
`static/lander-collision.js`. `static/lander-world.js` owns world candidates and contact
classification and imports only that pure arithmetic boundary. Both modules independently remain
below the same 1,000-line ceiling; the builder copies them as separate modules and never
concatenates them into an oversized output.

### D3. Repository content is a checked contract

The site has three content classes:

- **Canonical shared content.** The build reads `packaging/agentworks/assistance.md` directly. The
  built code element's decoded text, the canonical source, and the README's generated fenced block
  must be byte-identical. The build fails closed on missing, duplicate, malformed, or drifted
  sources. The website does not own or rewrite this text.
- **Repository-derived product and security content.** The Manifesto and Security pages each render
  one complete permanent Markdown document through a closed, escaping transform: `docs/manifesto.md`
  and root `SECURITY.md`, respectively. Each source has exactly one `h1` and owns all of its page's
  body headings, order, and prose; templates cannot supply alternate claims. Supported content edits
  flow directly to the generated page without synchronized hashes, heading inventories, expected
  passages, or heading-path selections in website code. Missing or unreadable inputs, invalid UTF-8,
  an absent or duplicate `h1`, unsupported Markdown, unsafe links or links outside the reviewed set,
  and reporting-channel violations fail the build. Links point to permanent repository docs, never
  to this SDD.
- **Site-owned connective content.** The website owns only presentation-neutral labels and
  instructions such as navigation, link introductions, the operator-approved security-link label,
  "Copy", and copy-status feedback. It does not make claims about Agentworks behavior, guarantees,
  principles, installation, security properties, or requirements.

The shell LLD pins the whole-document source, canonical assistance path, extraction contract, README
fence semantics, and rendering contract. Website validation proves byte equality and rejects broad
startup-disclosure, source-review, and security-posture prose in the thin prompt. The installed
guide owns those continuing-assistance concerns.

### D4. HTML is the agent surface too

The same documents serve humans, agents, assistive technology, text browsers, and indexing tools.
The built pages provide:

- one descriptive `title`, meta description, canonical URL, and ordinary indexable markup;
- semantic landmarks and one correctly nested heading outline;
- real anchor and button elements with accessible names;
- the complete bootstrap in a `pre`/`code` region, without image-baked or CSS-generated text, plus a
  progressively enhanced copy control;
- useful link text that identifies GitHub, PyPI, rationale, and security destinations;
- on complete long-form pages, one source-derived `h2`/`h3` contents navigation using ordinary
  fragment links, inline after the source `h1` by default and presented as a left rail only when the
  viewport has enough horizontal room;
- no essential state hidden behind interaction, animation, canvas, or client rendering.

No `llms.txt` or second agent-only representation is introduced. It would be another content copy
without a settled standard or first-slice need.

### D5. Visual system is local, restrained, and terminal-aware

The first slice uses system fonts, a small local color/token layer, and the selected custom AGW
rocket mark. The mark stacks custom symmetric A, G, and W geometry into a rocket silhouette; the G
has four equal corner radii, with only its opening and inward stroke breaking O-like symmetry. The
ordinary mark is neutral graphite. Its original twin-flame treatment uses compact pale-yellow hot
cores within orange and deeper orange-red plumes. On the landing page the mark is a dominant hero
element at two to three times the original small-header presentation, while the Manifesto, security,
Lander, and 404 header contexts retain the shared compact size. The final checked-in SVG is
self-contained, font-independent, semantic where displayed as content, and reusable without this
SDD.

Every document head references one local SVG favicon that projects the same exact neutral A/G/W mark
without the twin plumes. The dedicated projection keeps browser icon behavior independent of SVG
fragment support; automated geometry equality checks keep it synchronized with the selected mark. It
remains transparent and self-contained, with no raster fallback or additional runtime request.

The presentation should feel like a capable workbench rather than a generic SaaS landing page:
simple but powerful, with strong typography, restrained color, visible structure, and efficient
density. Terminal and TUI paradigms appear through monospaced accents, crisp panel boundaries,
compact status-like labels, and deliberate alignment. They do not appear as a fake window frame,
wall of command prompts, green-on-black theme, CRT effect, decorative ASCII text, or keyboard-only
interaction. The canonical bootstrap is the visual center; the deleted interim notice required no
parallel layout or retained compatibility branch. The site introduces no remote font, icon library,
or existing architecture diagram.

The shell LLD pins final tokens and layouts across the home, Manifesto, security, Lander, and 404
surfaces with these invariants:

- useful at 320 CSS pixels and at 400 percent zoom without page-level horizontal scrolling;
- WCAG 2.2 AA text, component, focus, and interaction contrast;
- visible keyboard focus and a logical tab order;
- no required motion, and any decorative motion disabled under `prefers-reduced-motion`;
- light and dark presentation only if both can be tested to the same bar. A single excellent theme
  is preferred to two partially verified ones.

### D6. Copy is progressive enhancement

The bootstrap text is selectable and readable before JavaScript runs. The copy button reads the code
element's `textContent`, invokes the Clipboard API only from the user's activation, and reports
success or failure in an `aria-live` status region without moving focus. If the API is unavailable,
the button is absent or explains that manual selection remains available. No clipboard content is
read.

### D7. One continuous deployment expedition renders on Lander and 404

The built artifact includes a semantic `404.html` that identifies the missing page and exposes a
normal link home without CSS or JavaScript, plus a deliberate `/lander/` play surface. One reviewed
template fragment owns the complete `#lander-game` subtree and is rendered into both shells; the
controller, flight model, and world model remain page-agnostic. The selected twin-plume mark hovers
over a static first slice of varied lunar terrain with one elevated platform and one compact NOC. No
visual instructions, fuel readout, direction cue, or other game chrome appear initially. On arrival,
the plumes run a subtle cue for less than five seconds and settle; `prefers-reduced-motion: reduce`
suppresses that cue entirely. This bounded cue preserves the surprise without requiring a pre-game
pause control.

An unmodified, non-repeated Space key starts the game from the initial state on either shell when
its event target is the document body or lander scene, never when focus is on the home link or
another interactive/editable element. The lander is also an operable, accessibly named start control
without visible instruction text. Activating it provides the pointer and assistive-technology path.
The accepted preflight Space event is consumed so it cannot also scroll the page. Starting moves
focus to the game scene and reveals concise in-scene controls, a programmatically named but visually
hidden rounded representation of the exact model fuel reserve, one left-side vertical visual gauge,
status, and a native `Exit mission` button at the bottom-right of the controls rail. A native
`Retry` button is revealed beneath the crash banner and is the only failure-specific action. Both
buttons show their keyboard equivalents on a smaller second line, invoke the same EXIT and RESTART
model events as Escape and `r`, preserve the established focus destinations, and make the complete
lifecycle available to touch and assistive technology. Exit remains available in every active state;
both controls remain hidden during preflight.

While active, Space or Up commands equal thrust; Left or `h` increases the right engine to turn
left; Right or `l` increases the left engine to turn right. Differential input vectors the combined
force toward the commanded turn and materially reduces its axial component: full steering with
collective is tuned near half of straight collective's axial force, and turn-only input does not
overcome gravity. While neutral collective remains engaged, a small deterministic flight-control
assist counters residual rotation by redistributing that same fuel-consuming thrust between the two
visible engines. It is not atmospheric drag; engine-off vehicle coasting and ballistic crash
fragments remain undamped. Apart from the accepted preflight Space event, only active game controls
prevent their ordinary browser behavior. A first pointer activation starts without also applying
thrust. Thereafter, pointer down captures that pointer and starts collective thrust; movement
applies a bounded horizontal differential after a dead zone. A short pointer release with little
travel records its completed pointer token before releasing capture and retains the remaining pinned
minimum impulse; the browser's automatic lost-capture event for that completed token cannot cancel
the pulse. Other cancel, lost-capture, exit, or page-hide paths cut both engines and release
capture. Holding sustains thrust. Dragging left biases the right engine, and dragging right biases
the left. `touch-action` and scroll suppression apply only inside the active game scene. Escape
exits to the settled initial state, and `r` restarts a crashed mission from its last in-memory
checkpoint.

The 404 body begins directly with `Page not found` after the compact detail-page inset. It has no
error-code, eyebrow, provenance, or other pre-title label; its explanatory copy remains below the
title and is exactly `This route is broken! We need agents!`. The dedicated page similarly begins
with `We need agents!`. Both titles and their shared game remain semantic and useful before
JavaScript runs.

The game is a small DOM/SVG state machine, not canvas and not a general engine. A timestamp-driven
animation loop integrates a fixed-step two-dimensional flight model with bounded catch-up: gravity,
position, velocity, attitude, angular velocity, collective thrust, differential torque, fuel, and
terrain contact. Per-engine translational authority initially doubles from `4.2` to a nominal `8.4`
model units while gravity and the fixed-step clock retain their roles, giving later braking without
changing the scheduler. The LLD may pin a nearby value only when recorded browser handling evidence
justifies the tolerance. The loop pauses when the document is hidden and resumes without
accumulating hidden time. Within the pinned catch-up bound, the same timestamped input timeline
produces equivalent simulation across representative 30, 60, and 120 Hz frame schedules. A larger
stall discards accumulated wall time and resumes from the last state rather than pretending to
simulate unseen play. Each plume group scales independently from the commanded engine thrust.

A new pure world module owns deterministic functions for the run seed, terrain chunks, site
positions, landing-platform and building geometry, camera position, offscreen targeting, and bounded
rolling-world projection. It owns no mutable singleton and imports no flight or controller module.
The flight model imports pure world functions, owns the immutable physics profile and
reference-route calculation, and is the sole authority for the mutable run aggregate: vehicle
physics, fuel, mission sequence, world cursor, retained site records and their can/power state, and
the last successful checkpoint. The controller imports the flight model plus read-only
camera/path/offscreen projection helpers directly from the pure world module and remains the sole
browser clock, input, focus, and DOM adapter. This acyclic controller-to-model-and-world,
model-to-world graph prevents a physics/world cycle and split-brain site state while avoiding
duplicate projection math. The controller translates one stable nearby-world SVG group as the camera
follows the lander in either horizontal direction and only regenerates terrain/site nodes when the
rolling window changes. There is no horizontal mission boundary: passing a target or returning
behind a prior site is not a contact and cannot itself crash the vehicle. The runtime retains a
fixed number of nearby chunks and sites, so arbitrarily long or reversing exploration does not imply
unbounded DOM or terrain history. A separate decorative sky projection derives bounded deterministic
stars and occasional celestial landmarks from the same run seed and nearby sky chunks. It translates
at a slower camera rate for parallax, owns no collision or semantic state, and retains a fixed node
count. Decorative landmarks remain recognizable astronomical silhouettes: a crescent moon or one
circular planet with one or two modest elliptical rings. Planet geometry occludes each ring's rear
center while retaining its foreground arc and the two exposed rear-side segments.

Each deterministic site retains native terrain beneath its complete footprint; it does not replace a
site span with a flat shelf. One ordered strict-X terrain chain remains authoritative through site
and chunk boundaries, so rendering and collision cannot disagree through duplicate horizontal
positions or vertical closing segments. Phase 4R uses only linear interpolation between canonical
vertices: the visible lunar and collision surface contains no curve or smoothing authority. Its
vertices span the requested normalized `[0.1,0.6]` vertical band at ordinary gameplay scale while
independently tested segment length, grade, and adjacent-grade-change bounds prevent noisy
sample-to-sample chatter. Site horizontal position is derived from mission progression without
terrain-height input. Once its complete structural footprint is fixed, the deck is derived exactly
`2.5 m` above the maximum native terrain beneath that closed footprint. Rendering, collision, site
feet, clearance, routes, and the static scene consume the same terrain and derived deck result. The
scene retains one fixed vertical projection and clips its world internally: there is no vertical
camera authority, presentation-only terrain offset, page growth, or game/page vertical scrolling.

The global terrain sampler does not force alternating hill and valley blocks or cycle through a
short profile silhouette. Seeded global vertex authority supplies a less periodic angular sequence
while retaining exact normalized-height, segment-grade, and adjacent-grade-change bounds; every
consumer still linearly interpolates the same strict-X vertices. Site generation remains a separate
mission-index calculation and samples terrain only after its horizontal footprint is immutable.
Flight inside generated terrain has no invisible positional or synthetic vehicle-safety failure
boundary. The swept-collision routine adapts its traversal to the complete relevant path instead of
converting a large subdivision count into `overspeed`, and the model does not classify crossing a
vertical ceiling as a crash. Empty fuel only removes thrust. The crash state is reachable solely
through a concrete swept intersection with the canonical terrain, a platform, support, NOC collider,
or an explicitly rendered physical terminus if the implementation chooses a finite world. Such a
terminus is optional, visible before contact, and separate from the bounded rolling render window;
crossing a retained-window edge is never a crash. These changes do not introduce a vertical camera,
page growth, scrolling, or unbounded retained world state.

Phase 4T increases relief without adding another terrain authority. Each frozen-profile successor
has exactly twice its Phase 4S predecessor's true slope-sign reversals; measuring reversal intervals
cyclically across the same `512 m` span therefore halves their mean horizontal spacing. The reviewed
corpus also doubles typical reversal strength, permits adjacent segment-grade change through `0.80`,
and caps absolute grade at `0.40`, while the same strict linear chain remains within normalized
`[0.1,0.6]`. Mission progression proposes a horizontal candidate without reading terrain. Only after
the full structural footprint is immutable does site generation sample the native envelope and
compute the unchanged `max+2.5 m` deck. A normalized deck above `0.5` makes that candidate
ineligible; generation advances through one seeded, bounded, directly reproducible candidate order
until an eligible site is found. It never clamps the deck, lowers terrain, inserts a shelf, or
conditions the terrain generator on site state. The route fixture exhaustively certifies every
accepted distance/deck class and termination bound. Rendering, collision, static recovery, supports,
fuel, and no-scroll projection continue to consume the same accepted result.

The materially elevated platform remains exactly three lander widths long beside one solid NOC
building. One collider-backed open truss uses continuous top and bottom chords and a uniform
sequence of alternating triangular braces across the complete platform-to-NOC span. Exactly three
visible, collider-backed open lattice columns at the structure's left, center, and right descend to
the terrain height interpolated from that same authoritative chain. Each column uses the same member
weight, joins, and triangular rhythm as the Warren truss; its conservative fixture-derived envelope
contains every rail and brace pixel while remaining collision-honest. The structure has no
region-specific pad, connector, or NOC brace pattern, so the site reads as one elevated engineered
structure without a long exposed sky strip, repeated X fields, a false flat foundation, or a
decorative opening the lander could appear able to traverse. A safe landing under the pinned,
modestly relaxed speed and attitude limits consumes that site's one gas can exactly once. The fuel
award is computed only after the next site exists: a deterministic reference plan starts at the
post-refuel, post-power platform checkpoint, includes the pinned player-reachable launch prefix,
uses the same immutable physics profile as play, and demonstrates a safe next landing within a
deterministic sufficient allowance. That allowance is an independently certified conservative base
plus one third of positive platform-height gain, rounded upward to the existing fuel quantum;
descents receive no negative credit. The award is that allowance multiplied by the refuel ratio
`1 + 0.5^(n-1)`, where `n` is the one-indexed number of the base just powered. The sequence begins
`2, 1.5, 1.25, 1.125` and mathematically approaches one from above in constant time. Its binary
runtime projection never falls below one and may round to exactly one when the remaining bonus is
smaller than representable precision. The award is added to the carried reserve without erasing
unused fuel. Exhaustive finite geometry classes and independent runtime replay pin the route proof,
allowance upper bound, and quantum rounding. The sufficient allowance is not described or tested as
the smallest successful reserve, and the reference plan does not waste fuel to force a
one-quantum-smaller failure.

At the sole final site in a finite physical world, the service transaction preserves the same can,
refuel, deployment, power, checkpoint, and player-commanded launch behavior without attempting to
construct a target beyond the terminus. Its terminal award applies the same independently certified
base allowance with zero deck delta. Target, cue, and route-proof authority then become absent; no
new outcome, copy, automatic Exit, or hidden boundary state is introduced.

After refueling, the G opening acts as a deployment bay: a small terminal-inspired agent reaches the
surface and enters the single NOC in half the Phase 4L travel time. The later battery and signal
sequence keeps its existing pace. A clean rectangular vertical phone-battery-style indicator, with
no terminal nub, fills from bottom to top in distinct warm-to-cool stages. A vertically symmetric
network signal then builds through the final three stages above it. State and timing, not color
alone, communicate progress. The agent remains visibly installed in the NOC entry as the first
durable power-up mark, and the powered appearance remains while the site stays in the rolling
window. The model then records one immutable checkpoint snapshot containing the vehicle/platform
pose, post-award fuel, world generator cursor, active and target site identities, retained-site
can/power states, and mission progress. It enters a launch-ready state that holds the centered
vehicle safely at rest, exposes the exact visible and announced `Agent Deployed!` banner through the
existing status live region, and consumes no fuel until the player commands thrust. No Launch button
or other launch-ready action is rendered: the same keyboard, vi, pointer, and touch collective
controls used in flight initiate liftoff; once both feet clear the deck, ordinary flight resumes
with the next site already generated offscreen. Retry returns to this same launch-ready checkpoint,
including its centered pose and carried post-award fuel, rather than replaying an award or launch. A
directional edge cue appears only while that target is outside the viewport and points left or right
toward it; reduced motion keeps the useful arrow static. Deployment never enters a terminal success
state.

The fuel projection does not cap an uncapped reserve. The visually hidden non-live text remains the
sole accessible rounded presentation of exact model engine-seconds. A decorative left-side vertical
bar shows the current reserve against an immutable visual reference for the current leg. A fresh
initial approach begins at exactly one half of its reference; an award/checkpoint establishes the
later leg at full and it drains toward empty as fuel is spent. Its fill height and
red-to-amber-to-green presentation are independent level signals. On touchdown, model sequence time
projects the collected can toward the gauge and interpolates the displayed fill from the pre-award
fraction to full over the existing landed interval; the physics reserve and checkpoint award remain
one atomic authority. Reduced motion skips both projections and shows the final full gauge. The next
award can establish a larger reference without clipping or discarding carried excess. At exact
exhaustion, the gauge becomes a whole-track red warning that blinks only when motion is allowed and
the game is active; reduced motion preserves the red warning without animation.

Activated game chrome uses one local system-monospace arcade stack, heavy block lettering, crisp
shadows, and bounded stepped animation; it adds no webfont, asset request, canvas, or semantic-text
copy. The controls paragraph moves into an opaque bottom rail within the scene shell and omits
keyboard shortcuts already shown by the rail's persistent Exit control or the conditional Retry
control. The world projection keeps every possible terrain surface above that reserved rail rather
than relying on the rail to hide an overlap. The existing status live region is the only banner
authority: it presents centered bordered `Agent Deployed!` and `Crashed!` panels. Launch-ready shows
only its banner; failure adds only Retry beneath its banner; Exit stays at the rail's bottom-right.
At narrow width and 400 percent zoom, the fuel gauge, centered panel, controls rail, and native
controls remain within the scene and do not overlap. Reduced motion removes arcade/refuel animation;
document hiding pauses it with the existing lifecycle. Decoration uses existing elements or
pseudo-elements only, and persistent installed-agent geometry reuses the per-site NOC entry path so
the exact 80-descendant world ceiling does not grow.

Unsafe terrain, platform, or building contact enters a finite crash sequence. Normal motion shows a
compact propellant flash and deterministic fragments following ballistic paths; it has no smoke,
atmospheric shock wave, sustained fireball, sound, or page movement. Reduced motion atomically shows
the final failed state. Retry restores the last post-refuel, post-power launch-ready checkpoint on
its powered pad without duplicating the consumed can or fuel; before any successful site it restores
the initial approach. Exit and reload discard all world, fuel, checkpoint, and powered-site state.
The home link remains available in every state.

Game constants, seed progression, terrain generation, route search resolution, DOM states, collision
rules, deployment geometry, crash timing, and test vectors belong in `brand-and-lander-lld.md`. The
game makes no product claim and carries no analytics, durable storage, network request, uncontrolled
per-frame randomness, or critical content. Seed injection keeps automated acceptance exact while
each ordinary run receives a fresh in-memory world.

### D8. GitHub Pages is the replaceable delivery adapter

The existing CI workflow builds and tests the site on pull requests and pushes to `main`, without
Pages permissions. Before the implementation PR merges, the operator enables GitHub Actions as this
repository's Pages source and restricts the `github-pages` environment to the default branch. The
publishing workflow then runs only after a push to `main` and has two jobs:

1. **Build:** check out the repository, run the website contract tests, configure Pages metadata,
   run the builder with the normalized Pages base, verify the artifact, and upload exactly the
   generated directory.
2. **Deploy:** on `main` only, deploy the uploaded artifact to the `github-pages` environment with
   the minimum `pages: write` and `id-token: write` permissions.

The publishing build repeats the deterministic checks before it uploads, so deployment never trusts
an artifact from a different event. Deployment concurrency allows one active publish and cancels
superseded queued work without cancelling an in-progress production deployment.

The publishing workflow is not path-filtered. The site's authoritative inputs include files outside
`website/`, so every push to `main` must rebuild and verify the artifact rather than risk serving a
stale bootstrap or product passage.

A Pages custom-domain settings change does not itself trigger this workflow. After attaching the
domain and before DNS mutation, the operator uses GitHub's `Re-run all jobs` action on the already
verified implementation merge-push run. The rerun retains the same push event, `main` ref, source
SHA, permissions, and protected environment while `configure-pages` reads the current root base. The
root-base build and deployment must succeed and be verified before cutover. No broader manual
dispatch trigger or second workflow is introduced; if the original run is no longer available for
rerun, cutover stops until a separately reviewed activation path exists.

The default project URL may be degraded after the custom-domain setting is attached and before the
root-base rerun succeeds. If that rerun fails or cannot be verified, the operator removes the
repository custom-domain setting, uses **Re-run all jobs** on that same latest verified `main` push
workflow so `configure-pages` rebuilds and deploys with `/agentworks/`, verifies the same SHA at the
default project URL, leaves DNS unchanged, and stops. Activation restarts later through the complete
reviewed sequence; it never proceeds from a partially changed Pages setting.

The workflow pins current stable major versions of official GitHub actions at implementation time,
consistent with repository conventions. No third-party deploy action or long-lived cloud credential
is introduced.

The artifact boundary is portable: another static host can accept the generated directory without
changing content or build contracts. Hosting-specific behavior stays in the workflow and operator
runbook.

The workflow first went live with the complete pre-assistance release. PR #480 then carried the
canonical assistance source and website integration through the same merge-to-`main` path; it adds
neither a second Pages project nor a domain change. This preserves the proven delivery system and
custom 404 while keeping every deployed artifact reproducible from its source commit.

### D9. DNS and domain setup are explicit one-time operations

The permanent `website/README.md` runbook records external setup, current DNS inventory, approval,
rollback, and recovery. Setup and go-live are ordered so deployment exists before DNS cutover:

1. enable GitHub Actions as this repository's Pages source and protect the `github-pages`
   environment so only `main` can deploy;
2. merge the implementation and verify its automatic deployment at the default Pages URL;
3. verify `agentworks.build` in the WayfarerLabs GitHub organization and retain GitHub's TXT record;
4. set `agentworks.build` as this repository's custom domain;
5. rerun all jobs on the verified `main` merge-push workflow, prove it built with site base `/`, and
   verify the expected commit's successful Pages deployment before any DNS mutation;
6. if step 5 fails, detach the custom domain, rerun that same workflow to restore the same SHA at
   `/agentworks/`, verify the default project URL, leave DNS unchanged, and stop;
7. after explicit operator approval, replace GoDaddy's apex parking record with GitHub's documented
   `A` records and point `www` by `CNAME` to `wayfarerlabs.github.io`;
8. verify DNS answers, apex content, `www` redirect, certificate, and HTTPS enforcement;
9. remove or repoint the DNS records promptly if Pages is ever disabled.

DNS values are copied from current GitHub documentation during go-live and recorded in acceptance
evidence. They are not hidden in application code. No wildcard record is created.

The DNS cutover belonged to the first complete release after that artifact passed acceptance at the
default Pages URL. The canonical-assistance integration is an ordinary site-source deployment.
Production closeout waits until the canonical bootstrap is live and AC3/AC4 are accepted; the
existing healthy domain does not lock the effort early.

### D10. The interim-to-complete transition is explicit and disposable

The first checked-in main-page template contained one bounded availability notice at the future
bootstrap position, with no preview mode or hidden complete template. PR #480 deleted that temporary
notice, made the canonical assistance source a required builder input, emitted the verified
`pre`/`code` region, and adds the focused copy enhancement. Current tests forbid the retired notice
and require exact bootstrap identity. The transition leaves no permanent configuration branch or
second authored prompt.

## Component topology

```text
README.md identity selectors ------------------+--> home page
packaging/agentworks/assistance.md canonical ---+

docs/manifesto.md complete document --> Manifesto page
SECURITY.md complete document ------------> security page

shared game fragment + logo/game assets --+-----> Lander page
                                           +-----> 404 page

home + Manifesto + security + Lander + 404 --> deterministic builder --> PR/CI
                                                                       |
                                                                       +--> GitHub Pages --> agentworks.build

canonical bootstrap + README fenced block
                  --> byte-identity and thin-scope checks
                  --> generated bootstrap region
```

## Source layout

The detailed filenames belong in the shell and onboarding-integration LLDs, but responsibilities are
fixed here:

- `website/`: home, Manifesto, security, Lander, and 404 source; final SVG assets; focused
  CSS/JavaScript; builder; tests; and permanent operator/developer runbook.
- `.github/workflows/`: Pages build/deploy workflow and the existing CI integration.
- `.gitignore`: generated site artifact exclusion.
- repository README and `packaging/agentworks/assistance.md`: required inputs, not website-owned
  copies.
- `docs/manifesto.md`: complete Manifesto source, with no fallback to the retired path.
- `SECURITY.md`: complete Security page and private vulnerability-reporting authority.
- this feature directory: temporary design, plan, research, and acceptance evidence only.

## Verification strategy

### Build and content contracts

- deterministic clean build in a temporary directory;
- no unexpanded placeholders or writes outside the requested output;
- complete-document rendering, exactly one source `h1`, closed Markdown support, and reviewed links;
- complete Security source, actual-boundary/limitation content, stable security URL, private
  vulnerability reporting, and no address-shaped reporting path;
- valid internal paths, canonical URL, metadata, and no external runtime assets;
- generated output absent from Git status.

Tests reject the retired interim notice and require exact bootstrap equality across canonical
source, README fenced block, and decoded built HTML. Human review owns the authored prompt wording;
behavioral guide tests prove that the installed guide owns continuing assistance.

### Document behavior

- semantic landmarks, heading order, language, named controls, and the release-appropriate
  onboarding region asserted;
- the home security link remains visually secondary but programmatically clear, and the security
  page remains useful without script or terminal familiarity;
- copy behavior exercised for success, unavailable API, and rejected write without changing the
  source text;
- custom 404 fallback, initial hidden controls, bounded idle cue, keyboard/vi/pointer mappings,
  deterministic flight and world vectors, plume-to-thrust mapping, repeated deployment, generated
  route fuel proofs, checkpoint restart, finite crash/exit states, bounded rolling geometry,
  background pause, and reduced-motion equivalents asserted;
- arcade fuel/refuel projection, installed-agent persistence, centered success/crash banners,
  in-scene native actions, and the terrain-separated bottom control rail asserted without changing
  physics, route, world, privacy, or shared-fragment contracts;
- keyboard-only traversal, visible focus, narrow-width reflow, zoom, reduced motion, and screen
  reader landmarks checked in acceptance;
- color tokens verified with computed contrast evidence for normal text, large text, components, and
  focus indicators.
- terminal/TUI cues remain present across all surfaces without fake-terminal chrome, inaccessible
  density, or loss of recognizable links and controls.

### Deployment and production

- pull request proves build without deploy permissions;
- a merge-to-`main` source change produces a successful Pages deployment with the expected commit;
- production returns the byte-identical bootstrap and no retired interim notice over HTTPS at the
  apex;
- `www` redirects to the apex without a certificate warning;
- DNS A, AAAA, CNAME, MX, TXT, and CAA answers match the recorded before-state plus approved cutover
  delta;
- GitHub and PyPI links resolve.

## Security and privacy

- The site adds no first-party telemetry, user storage, analytics, cookies, forms, third-party
  script, remote font, or runtime network request. GitHub retains its ordinary hosting and
  operational processing.
- Shared text is escaped before HTML insertion. The builder never evaluates it.
- Game state is ephemeral in memory and accepts no text, URL, storage, or network input. Pointer and
  keyboard handlers are scoped to the active scene and released on exit or page lifecycle changes.
- A restrictive document policy is expressed where GitHub Pages allows it without breaking the page;
  lack of configurable response headers is documented as a Pages constraint, not simulated as a
  guarantee.
- The deployment workflow grants read-only contents plus job-local Pages/OIDC permissions and uses
  no repository or cloud secret.
- Organization domain verification and no-wildcard DNS reduce custom-domain takeover risk.
- External links do not receive opener authority if a new browsing context is used.

## Failure and recovery

- **Canonical assistance unavailable or drifted:** build fails before artifact upload and names the
  missing or mismatched contract. There is no interim fallback or branch inspection. The owner
  updates the website integration against the repository-owned source; it never substitutes local
  copy.
- **JavaScript unavailable or game failure:** the dedicated Lander page retains its static named
  scene, while the semantic 404 message and ordinary home link remain available. The game is
  nonessential and never owns navigation or recovery.
- **Bad site merge:** the Pages environment exposes deployment history. Fix forward or redeploy the
  last known-good artifact according to the runbook.
- **DNS or certificate delay:** the Pages deployment history, status, and captured pre-attachment
  default-URL acceptance remain the diagnostics. Do not weaken HTTPS or add alternate forwarding
  machinery while propagation is incomplete.
- **Root-base activation fails before DNS cutover:** detach the repository custom domain, rerun the
  same latest verified `main` push workflow, verify the same SHA rebuilt with `/agentworks/` at the
  default project URL, leave DNS unchanged, and stop before retrying the reviewed activation path.
- **Pages disabled or repository moved:** remove the public DNS records immediately if the domain is
  no longer attached, then follow the runbook to restore verified ownership before repointing.
- **Hosting migration:** deploy the same generated artifact elsewhere, validate it at a temporary
  hostname, then move DNS. Site sources and content contracts stay unchanged.

## Deliberately deferred growth paths

- Web rendering of `agw guide` topics.
- Schema-derived reference pages.
- Release notes, changelog presentation, search, and multiple-page navigation.
- Analytics, feedback capture, accounts, forms, APIs, and dynamic backends.
- A general component system, design system package, or static-site framework.

Each requires a new requirement or a stable upstream contract. None is scaffolded in this slice.
